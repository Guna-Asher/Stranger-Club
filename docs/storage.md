# Object Storage

## Provider

**Recommended default: Cloudflare R2.** The application talks to it (or any
S3-compatible provider — AWS S3, MinIO for local/CI) through the generic S3
API via `boto3`, so switching providers is a configuration change, not a
code change. R2 was chosen for this project specifically because it has no
egress fees — organizers repeatedly re-view the same proof screenshots
during review, which is read-heavy, egress-sensitive traffic. AWS S3 works
identically if you already run AWS infrastructure for other reasons.

Actual provider credentials/bucket are deployment configuration, never
source-controlled.

## Configuration

| Variable | Required when `SC_STORAGE_BACKEND=s3` |
|---|---|
| `SC_STORAGE_BACKEND` | `local` (dev/test) or `s3` (required in staging/production) |
| `SC_STORAGE_BUCKET` | yes |
| `SC_STORAGE_ENDPOINT_URL` | yes (R2/MinIO/custom S3 endpoint) |
| `SC_STORAGE_REGION` | no, defaults `auto` |
| `SC_STORAGE_ACCESS_KEY_ID` | yes |
| `SC_STORAGE_SECRET_ACCESS_KEY` | yes |

## Namespaces

| Prefix | Contents | Access policy |
|---|---|---|
| `proofs/` | Payment-proof screenshots | Private. Every read goes through `require_proof_access` + a 60-second presigned URL. |
| `qr/` | Organizer-uploaded custom QR images | Private. Served only through the per-registration, ownership-checked `payment-qr` endpoint. |
| `profiles/` | Reserved for a future profile-photo feature | Not built yet — prefix reserved so it never collides with payment evidence |
| `event-media/` | Reserved for future public event media | Not built yet |
| `backups/postgres/` | Encrypted `pg_dump` archives | Separate bucket/credential from application storage — see `backups.md` |

Payment proofs never share an access policy with anything intended to be
public. Object keys are always opaque (`uuid4().hex` + extension) — never
derived from phone number, email, UTR, session token, or a predictable
registration ID (`storage._guard_key` also defends against path traversal
even though every real key is generated, never user input).

## Protocol

```python
class Storage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> tuple[bytes, str] | None: ...
    def exists(self, key: str) -> bool: ...
    def presigned_url(self, key: str, *, expires_in: int) -> str | None: ...
```

Deliberately has **no delete method**. Payment-proof evidence is append-only
— see "Payment-proof immutability" below. `LocalFilesystemStorage` (dev),
`S3Storage` (production, `backend/app/storage_s3.py`), and `FakeStorage`
(unit tests, in-memory) all implement this exact interface; application
services never know which one they were given.

## Payment-proof access model

`PLAYER` → own proofs only. `ORGANIZER` → proofs belonging to events they
own. `PLATFORM_ADMIN` → any. Enforced by `require_proof_access` /
`assert_registration_owner` in `backend/app/deps.py` and `services.py` —
unchanged from Phase 2C, storage-implementation-agnostic by design.

Retrieval never exposes a permanent public URL:

```
GET /api/payment-proofs/{proof_id}
  -> require_proof_access (authorization decided here, on every request)
  -> S3 backend: 302 redirect to a presigned URL, expires_in=60
  -> local backend: bytes streamed directly (dev only)
```

The presigned URL is minted fresh on every authorized request and expires
in 60 seconds. A leaked URL exposes exactly one screenshot for at most a
minute — the signature grants time-boxed object access, it never decides
*who* may see it; that decision is made by the application on every single
request, before a URL is ever generated.

## Payment-proof immutability

The application's runtime storage credential **must not** have
`s3:DeleteObject` permission on the `proofs/` prefix. This is enforced two
ways:

1. **IAM policy** (provisioned by the operator against the real
   provider — see the provider's console/API for scoping a token to a
   bucket+prefix with only `GetObject`/`PutObject`).
2. **Code**: the `Storage` protocol has no delete method at all — normal
   request-handling code (`services.submit_payment_proof`) cannot
   accidentally invoke one even if it wanted to. When a concurrent request
   loses a race or an idempotent retry detects a duplicate, the
   just-written object is left in place as a harmless, unreferenced orphan
   — never deleted by application code.

Exceptional cleanup — reviewing and removing confirmed orphans — is a
separate, manually-run tool with its own, more-privileged credential:
`scripts/reconcile_storage.py`, which reads `SC_STORAGE_ADMIN_ACCESS_KEY_ID`
/ `SC_STORAGE_ADMIN_SECRET_ACCESS_KEY` — deliberately different environment
variables from the application's own `SC_STORAGE_*`, so the delete
capability is never reachable via the credential the running application
holds.

## Versioning / overwrite protection

Payment-proof evidence has three independent layers of protection. They
are genuinely independent — a gap in one does not remove the others — and
each is at a different level of certainty, which is deliberate:

1. **Code-enforced (verified, always true, everywhere this code runs).**
   The `Storage` protocol has no delete method at all (see above) — no
   application code path, including a bug, can call one. This is true in
   every environment (local, CI, production) because it's a property of
   the code, not of configuration.
2. **Infrastructure configuration (must be provisioned by the operator
   against the real account — not automatic, not yet done for a real R2/S3
   account because none exists in this environment).**
   - The runtime `SC_STORAGE_*` credential must be IAM-scoped with no
     `s3:DeleteObject`/`s3:DeleteObjectVersion` on `proofs/` or `qr/`.
   - Bucket versioning should be enabled on the production bucket.
     `scripts/configure_bucket_protection.py` does this and — critically —
     **never claims success without reading the status back from the
     provider**: `enable_versioning()` calls `PutBucketVersioning` then
     immediately calls `GetBucketVersioning` and reports the actual
     returned status, distinguishing "provider rejected the call"
     (`PROVIDER_REJECTED`, e.g. an endpoint that doesn't implement
     versioning) from "enabled and confirmed" (`VERIFIED_ENABLED`) from "put
     succeeded but the read-back didn't match" (`VERIFY_MISMATCH`). Run it
     against the real bucket during provisioning:
     ```bash
     python scripts/configure_bucket_protection.py --bucket <bucket> --enable
     ```
   - Cloudflare R2 and AWS S3 both document support for the S3 bucket
     versioning API as of when this was written — verify against the
     provider's *current* documentation at setup time regardless, since
     provider capabilities change, and treat this script's own read-back as
     the source of truth over any documentation (including this one).
   - Object Lock / retention policies are a further option if the
     product's risk tolerance later demands protection against a
     compromised admin credential deleting a specific version — not
     enabled by default, since it adds real operational friction (locked
     versions must then be explicitly retained/expired) for a benefit not
     yet justified by real incident history.
3. **Deployment-time verification (required before trusting this in a real
   deployment).** Run `configure_bucket_protection.py --enable` against the
   real production bucket after it's created, and confirm the printed
   outcome is `VERIFIED_ENABLED` — not "code exists that could enable it."

### What was actually verified locally (MinIO), and what wasn't

**Verified against a real MinIO instance in this environment** — not
mocked, not assumed:
- `configure_bucket_protection.py --enable` genuinely enables versioning on
  a MinIO bucket and its read-back confirms `Enabled`
  (`test_bucket_versioning_can_be_enabled_and_is_verified_by_readback`).
- With versioning enabled, an object that is overwritten and then deleted
  is still fully recoverable: `list_object_versions` returns every prior
  version's real bytes, fetchable individually by `VersionId`, even though
  a normal `GET` on the current key correctly reports not-found
  (`test_versioned_bucket_recovers_overwritten_and_deleted_objects`).

**Not verified, and not claimed** — requires a real provider account,
which does not exist in this environment:
- That Cloudflare R2 (specifically, as opposed to the generic S3 API MinIO
  also implements) accepts and honours these same calls identically. R2's
  S3-compatibility is generally close, but this must be re-run against a
  real R2 bucket during provisioning before relying on it — see the
  deployment-time step above.
- Any cost/retention implications of enabling versioning on a real
  provider account (stored bytes for old versions count toward storage
  usage/cost) — review the provider's pricing for versioned storage before
  enabling in production.

`scripts/reconcile_storage.py`'s report remains the practical, always-on
detection mechanism regardless of whether versioning is enabled or how a
given provider implements it: it compares every `PaymentProof.storage_key`
/ QR `storage_key` against what actually exists in the bucket and alerts on
anything referenced-but-missing, independent of the versioning layer.

## Failure handling

| Failure | Behaviour |
|---|---|
| S3 unavailable / timeout | Bounded retry (boto3, max 2 attempts, 5s connect / 10s read timeout) then a clean `503 STORAGE_UNAVAILABLE` — never an infinite retry loop |
| Upload succeeds, DB transaction fails | Object is orphaned (never referenced) — harmless, caught by reconciliation, never auto-deleted |
| DB succeeds, client response lost | Handled by the existing hash-based idempotent-retry path in `submit_payment_proof` — storage-agnostic |
| Object missing when referenced | `get()`/`presigned_url()` return `None` → `404`; this should never happen given the delete-restricted credential, so it is itself an operator signal — see `disaster-recovery.md` scenario F |

Every storage failure is logged with the request's correlation ID and a
sanitized error (`storage_s3._safe_error` strips anything resembling a
query string, so a presigned URL never ends up in a log line) — never
credentials, bucket names beyond what's needed to act on the alert, or raw
provider exception internals reaching the client.

## Local development / CI

- `LocalFilesystemStorage`: writes under `SC_DATA_DIR/uploads/{proofs,qr}`.
- `FakeStorage`: pure in-memory, used by most unit tests — deterministic,
  no I/O, and supports `fail_on_put` for storage-failure-injection tests.
- MinIO: a real S3-compatible backend for CI's `S3Storage` integration test
  and for local rehearsal of the production storage path — see
  `.github/workflows/ci.yml`.
