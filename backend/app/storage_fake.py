from __future__ import annotations

from .storage import StorageUnavailableError, _guard_key


class FakeStorage:
    """Pure in-memory Storage implementation for unit tests: deterministic,
    no filesystem or network I/O. Implements the same protocol as
    LocalFilesystemStorage/S3Storage — services never know which one they
    were given."""

    def __init__(self):
        self._objects: dict[str, tuple[bytes, str]] = {}
        # Test-only introspection, not part of the Storage protocol.
        self.put_calls: list[str] = []
        self.fail_on_put: bool = False

    def put(self, key: str, data: bytes, content_type: str) -> None:
        _guard_key(key)
        if self.fail_on_put:
            raise StorageUnavailableError("simulated storage failure")
        self._objects[key] = (data, content_type)
        self.put_calls.append(key)

    def get(self, key: str) -> tuple[bytes, str] | None:
        return self._objects.get(_guard_key(key))

    def exists(self, key: str) -> bool:
        return _guard_key(key) in self._objects

    def presigned_url(self, key: str, *, expires_in: int) -> str | None:
        if _guard_key(key) not in self._objects:
            return None
        return f"fake://storage/{key}?expires_in={expires_in}"
