"""payment domain redesign: EventPaymentConfiguration, PaymentProof, Payment snapshots

Phase 2C. Splits payment into three concepts:
  - EventPaymentConfiguration: the organizer's current, mutable payment
    settings for an event (payee UPI id/name, QR source).
  - Payment: one per Registration, a frozen snapshot of the configuration
    that applied when the registration was created, plus current review
    state. Never re-reads EventPaymentConfiguration after creation.
  - PaymentProof: append-only, one row per submitted screenshot. Replaces
    the old single-mutable-screenshot-per-Payment shape, which destroyed
    evidence on resubmission.

Data backfill, in order:
  1. Every Match without an EventPaymentConfiguration gets one, built from
     its (about-to-be-dropped) upi_id column.
  2. Every Payment gets its new snapshot columns filled from the config just
     created (joined through registration -> match). This is NOT a true
     historical snapshot — no such snapshot was ever recorded before this
     migration — it reflects whatever the configuration is *at migration
     time*. This is a documented, unavoidable limitation of migrating data
     that predates the concept of a snapshot; every Payment created after
     this migration gets a real point-in-time snapshot going forward.
  3. Every Registration without a Payment row (never submitted proof under
     the old system, where Payment only existed once a screenshot was
     uploaded) gets one created fresh in AWAITING_PROOF, satisfying the new
     invariant that a Registration always has a Payment from creation
     onward.
  4. Every pre-existing Payment with a non-null screenshot_path gets exactly
     one PaymentProof row, IF that file still exists on disk. If the file is
     missing, no proof row is fabricated for it — the Payment record is left
     exactly as-is (its status/verified_at/rejection_reason already reflect
     that a proof was submitted and reviewed; there is just no recoverable
     evidence for it in the new append-only history). This is deliberately
     NOT a hard migration failure: refusing to start the entire application
     over one historically lost file is worse than a Payment with no
     backing PaymentProof, which is visible and auditable (a warning is
     printed for every such row, with a summary count at the end).

Finally, the columns this migration supersedes are dropped: matches.upi_id,
matches.qr_code_path, payments.amount, payments.screenshot_path,
payments.screenshot_token.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-05

"""
from __future__ import annotations

import hashlib
import os
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUS_TO_PROOF_STATUS = {"SUBMITTED": "PENDING", "VERIFIED": "ACCEPTED", "REJECTED": "REJECTED"}
CONTENT_TYPE_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def _has_table(bind, name: str) -> bool:
    return name in inspect(bind).get_table_names()


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "event_payment_configurations"):
        op.create_table(
            "event_payment_configurations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("match_id", sa.Integer(), sa.ForeignKey("matches.id"), unique=True, nullable=False),
            sa.Column("payee_upi_id", sa.String(120), nullable=False),
            sa.Column("payee_name", sa.String(80), nullable=False, server_default="Stranger Club"),
            sa.Column("qr_source", sa.String(20), nullable=False, server_default="GENERATED"),
            sa.Column("qr_storage_key", sa.String(255), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("updated_by_organizer_id", sa.Integer(), sa.ForeignKey("organizers.id"), nullable=True),
            sa.CheckConstraint("qr_source IN ('GENERATED','UPLOADED')", name="ck_payment_config_qr_source_valid"),
            sa.CheckConstraint(
                "(qr_source = 'UPLOADED' AND qr_storage_key IS NOT NULL) OR (qr_source = 'GENERATED' AND qr_storage_key IS NULL)",
                name="ck_payment_config_qr_storage_consistent",
            ),
        )

    if not _has_table(bind, "payment_proofs"):
        op.create_table(
            "payment_proofs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id"), nullable=False),
            sa.Column("storage_key", sa.String(255), unique=True, nullable=False),
            sa.Column("screenshot_hash", sa.String(64), nullable=False),
            sa.Column("file_size", sa.Integer(), nullable=False),
            sa.Column("content_type", sa.String(40), nullable=False),
            sa.Column("utr_reference", sa.String(30), nullable=True),
            sa.Column("uploaded_at", sa.DateTime(), nullable=False),
            sa.Column("submitted_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
            sa.Column("reviewed_by_organizer_id", sa.Integer(), sa.ForeignKey("organizers.id"), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
            sa.Column("rejection_reason", sa.Text(), nullable=True),
            sa.Column("superseded_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint("status IN ('PENDING','ACCEPTED','REJECTED','SUPERSEDED')", name="ck_payment_proof_status_valid"),
        )
        op.create_index("ix_payment_proofs_payment_id", "payment_proofs", ["payment_id"])
        op.create_index("ix_payment_proofs_screenshot_hash", "payment_proofs", ["screenshot_hash"])
        op.create_index(
            "uq_payment_proof_one_pending", "payment_proofs", ["payment_id"], unique=True,
            sqlite_where=sa.text("status = 'PENDING'"), postgresql_where=sa.text("status = 'PENDING'"),
        )

    new_payment_columns = {
        "amount_due": sa.Integer(), "payee_upi_id_snapshot": sa.String(120), "payee_name_snapshot": sa.String(80),
        "qr_source_snapshot": sa.String(20), "qr_storage_key_snapshot": sa.String(255),
    }
    missing_payment_columns = {name: type_ for name, type_ in new_payment_columns.items() if not _has_column(bind, "payments", name)}
    if missing_payment_columns:
        with op.batch_alter_table("payments") as batch_op:
            for name, type_ in missing_payment_columns.items():
                batch_op.add_column(sa.Column(name, type_, nullable=True))

    # --- Data backfill ---
    now = bind.execute(sa.text("SELECT CURRENT_TIMESTAMP")).scalar_one()
    an_organizer_id = bind.execute(sa.text("SELECT id FROM organizers ORDER BY id LIMIT 1")).scalar_one_or_none()

    matches_without_config = bind.execute(sa.text("""
        SELECT m.id, m.upi_id FROM matches m
        LEFT JOIN event_payment_configurations c ON c.match_id = m.id
        WHERE c.id IS NULL
    """)).mappings().all()
    configs_created = 0
    for row in matches_without_config:
        bind.execute(sa.text("""
            INSERT INTO event_payment_configurations
                (match_id, payee_upi_id, payee_name, qr_source, qr_storage_key, updated_at, updated_by_organizer_id)
            VALUES (:match_id, :payee_upi_id, 'Stranger Club', 'GENERATED', NULL, :now, :organizer_id)
        """), {"match_id": row["id"], "payee_upi_id": row["upi_id"] or "unknown@upi", "now": now, "organizer_id": an_organizer_id})
        configs_created += 1

    # Every Payment gets its snapshot columns filled from the (now guaranteed
    # to exist) configuration of its event. Reconstructed, not a true
    # historical, snapshot — see module docstring.
    payments_missing_snapshot = bind.execute(sa.text("""
        SELECT p.id, m.fee, c.payee_upi_id, c.payee_name, c.qr_source, c.qr_storage_key
        FROM payments p
        JOIN registrations r ON r.id = p.registration_id
        JOIN matches m ON m.id = r.match_id
        JOIN event_payment_configurations c ON c.match_id = m.id
        WHERE p.amount_due IS NULL
    """)).mappings().all()
    for row in payments_missing_snapshot:
        bind.execute(sa.text("""
            UPDATE payments SET amount_due = :amount_due, payee_upi_id_snapshot = :payee_upi_id,
                payee_name_snapshot = :payee_name, qr_source_snapshot = :qr_source, qr_storage_key_snapshot = :qr_storage_key
            WHERE id = :id
        """), {
            "amount_due": row["fee"], "payee_upi_id": row["payee_upi_id"], "payee_name": row["payee_name"],
            "qr_source": row["qr_source"], "qr_storage_key": row["qr_storage_key"], "id": row["id"],
        })

    # Every Registration without a Payment row gets one, fresh, in
    # AWAITING_PROOF — the new invariant is that a Registration always has a
    # Payment from creation onward.
    registrations_without_payment = bind.execute(sa.text("""
        SELECT r.id, m.fee, c.payee_upi_id, c.payee_name, c.qr_source, c.qr_storage_key
        FROM registrations r
        JOIN matches m ON m.id = r.match_id
        JOIN event_payment_configurations c ON c.match_id = m.id
        LEFT JOIN payments p ON p.registration_id = r.id
        WHERE p.id IS NULL
    """)).mappings().all()
    payments_created = 0
    for row in registrations_without_payment:
        bind.execute(sa.text("""
            INSERT INTO payments
                (registration_id, status, amount_due, payee_upi_id_snapshot, payee_name_snapshot, qr_source_snapshot, qr_storage_key_snapshot)
            VALUES (:registration_id, 'AWAITING_PROOF', :amount_due, :payee_upi_id, :payee_name, :qr_source, :qr_storage_key)
        """), {
            "registration_id": row["id"], "amount_due": row["fee"], "payee_upi_id": row["payee_upi_id"],
            "payee_name": row["payee_name"], "qr_source": row["qr_source"], "qr_storage_key": row["qr_storage_key"],
        })
        payments_created += 1

    # Every pre-existing Payment with a screenshot becomes exactly one
    # PaymentProof, IF the file still exists. A missing file is logged and
    # skipped, never fabricated, and never treated as a reason to refuse to
    # start the application over one historically lost file.
    data_dir = os.getenv("SC_DATA_DIR", "data")
    legacy_uploads_dir = os.path.join(data_dir, "uploads")
    proofs_uploads_dir = os.path.join(data_dir, "uploads", "proofs")
    os.makedirs(proofs_uploads_dir, exist_ok=True)

    payments_with_screenshots = bind.execute(sa.text("""
        SELECT p.id AS payment_id, p.screenshot_path, p.status, p.submitted_at, p.verified_at, r.user_id
        FROM payments p JOIN registrations r ON r.id = p.registration_id
        WHERE p.screenshot_path IS NOT NULL
    """)).mappings().all()
    proofs_created = 0
    proofs_skipped_missing_file = 0
    proofs_skipped_no_user = 0
    for row in payments_with_screenshots:
        source_path = os.path.join(legacy_uploads_dir, row["screenshot_path"])
        if not os.path.isfile(source_path):
            proofs_skipped_missing_file += 1
            print(f"[0005_payment_domain_redesign] WARNING payment_id={row['payment_id']} screenshot missing at {source_path}; no PaymentProof created")
            continue
        if row["user_id"] is None:
            # No authenticated submitter on record for this legacy row — cannot fabricate one.
            proofs_skipped_no_user += 1
            print(f"[0005_payment_domain_redesign] WARNING payment_id={row['payment_id']} has no registration.user_id; no PaymentProof created")
            continue
        with open(source_path, "rb") as handle:
            content = handle.read()
        digest = hashlib.sha256(content).hexdigest()
        ext = os.path.splitext(row["screenshot_path"])[1] or ".png"
        key = f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(proofs_uploads_dir, key), "wb") as handle:
            handle.write(content)
        proof_status = STATUS_TO_PROOF_STATUS.get(row["status"], "PENDING")
        bind.execute(sa.text("""
            INSERT INTO payment_proofs
                (payment_id, storage_key, screenshot_hash, file_size, content_type, uploaded_at,
                 submitted_by_user_id, status, reviewed_at)
            VALUES (:payment_id, :storage_key, :hash, :size, :content_type, :uploaded_at, :user_id, :status, :reviewed_at)
        """), {
            "payment_id": row["payment_id"], "storage_key": key, "hash": digest, "size": len(content),
            "content_type": CONTENT_TYPE_BY_EXT.get(ext.lower(), "application/octet-stream"),
            "uploaded_at": row["submitted_at"] or now, "user_id": row["user_id"],
            "status": proof_status, "reviewed_at": row["verified_at"],
        })
        proofs_created += 1

    print(
        f"[0005_payment_domain_redesign] configs_created={configs_created} payments_created={payments_created} "
        f"proofs_created={proofs_created} proofs_skipped_missing_file={proofs_skipped_missing_file} "
        f"proofs_skipped_no_user={proofs_skipped_no_user}"
    )

    has_screenshot_token_index = any(ix["name"] == "ix_payments_screenshot_token" for ix in inspect(bind).get_indexes("payments"))
    with op.batch_alter_table("payments") as batch_op:
        batch_op.alter_column("amount_due", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("payee_upi_id_snapshot", existing_type=sa.String(120), nullable=False)
        batch_op.alter_column("payee_name_snapshot", existing_type=sa.String(80), nullable=False)
        batch_op.alter_column("qr_source_snapshot", existing_type=sa.String(20), nullable=False)
        batch_op.create_check_constraint("ck_payment_status_valid", "status IN ('AWAITING_PROOF','SUBMITTED','VERIFIED','REJECTED')")
        batch_op.create_check_constraint(
            "ck_payment_qr_snapshot_consistent",
            "(qr_source_snapshot = 'UPLOADED' AND qr_storage_key_snapshot IS NOT NULL) OR (qr_source_snapshot = 'GENERATED' AND qr_storage_key_snapshot IS NULL)",
        )
        # The unique index backing the old screenshot_token column must be
        # dropped explicitly before the column itself — SQLite batch mode
        # does not infer that an index becomes invalid when its column goes.
        if has_screenshot_token_index:
            batch_op.drop_index("ix_payments_screenshot_token")
        if _has_column(bind, "payments", "amount"):
            batch_op.drop_column("amount")
        if _has_column(bind, "payments", "screenshot_path"):
            batch_op.drop_column("screenshot_path")
        if _has_column(bind, "payments", "screenshot_token"):
            batch_op.drop_column("screenshot_token")

    if _has_column(bind, "matches", "upi_id") or _has_column(bind, "matches", "qr_code_path"):
        with op.batch_alter_table("matches") as batch_op:
            if _has_column(bind, "matches", "upi_id"):
                batch_op.drop_column("upi_id")
            if _has_column(bind, "matches", "qr_code_path"):
                batch_op.drop_column("qr_code_path")


def downgrade() -> None:
    raise NotImplementedError("0005 is a one-way data migration (payment domain redesign); no downgrade path")
