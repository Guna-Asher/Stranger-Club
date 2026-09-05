from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised when required production configuration is missing or invalid.
    Always fatal at startup — never caught to silently fall back to an
    insecure default."""


def _env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def _require(name: str, *, context: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"{name} is required {context} but was not set.")
    return value


@dataclass(frozen=True)
class StorageConfig:
    backend: str  # "local" | "s3"
    bucket: str | None = None
    endpoint_url: str | None = None
    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None


@dataclass(frozen=True)
class AppConfig:
    env: str  # "development" | "staging" | "production"
    database_url: str
    auto_migrate: bool
    db_pool_size: int
    db_max_overflow: int
    storage: StorageConfig
    trusted_proxy_ips: tuple[str, ...]
    admin_username: str
    admin_password: str | None
    sentry_dsn: str | None
    secure_cookies: bool

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


def load_config() -> AppConfig:
    env = _env("SC_ENV", "development")
    if env not in {"development", "staging", "production"}:
        raise ConfigError(f"SC_ENV must be one of development|staging|production, got {env!r}")

    database_url = _env("DATABASE_URL") or f"sqlite:///{_env('SC_DATA_DIR', 'data')}/stranger_club.db"
    if env in {"staging", "production"} and not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ConfigError(
            f"SC_ENV={env} requires a PostgreSQL DATABASE_URL (postgresql:// or postgresql+psycopg://). "
            "SQLite is a development-only database and must never back a staging or production deployment."
        )

    storage_backend = _env("SC_STORAGE_BACKEND", "local")
    if storage_backend not in {"local", "s3"}:
        raise ConfigError(f"SC_STORAGE_BACKEND must be local|s3, got {storage_backend!r}")
    if env in {"staging", "production"} and storage_backend != "s3":
        raise ConfigError(
            f"SC_ENV={env} requires SC_STORAGE_BACKEND=s3. Local filesystem storage is development/testing-only "
            "and does not survive a restart of a stateless production instance."
        )

    if storage_backend == "s3":
        storage = StorageConfig(
            backend="s3",
            bucket=_require("SC_STORAGE_BUCKET", context="when SC_STORAGE_BACKEND=s3"),
            endpoint_url=_require("SC_STORAGE_ENDPOINT_URL", context="when SC_STORAGE_BACKEND=s3"),
            region=_env("SC_STORAGE_REGION", "auto"),
            access_key_id=_require("SC_STORAGE_ACCESS_KEY_ID", context="when SC_STORAGE_BACKEND=s3"),
            secret_access_key=_require("SC_STORAGE_SECRET_ACCESS_KEY", context="when SC_STORAGE_BACKEND=s3"),
        )
    else:
        storage = StorageConfig(backend="local")

    trusted_proxy_raw = _env("SC_TRUSTED_PROXY_IPS", "")
    trusted_proxy_ips = tuple(ip.strip() for ip in trusted_proxy_raw.split(",") if ip.strip())
    if env == "production" and not trusted_proxy_ips:
        raise ConfigError(
            "SC_ENV=production requires SC_TRUSTED_PROXY_IPS to be set to the reverse proxy's IP(s)/CIDR(s) — "
            "without it, X-Forwarded-For could be spoofed by any client to bypass IP-based rate limiting."
        )

    def _int_env(name: str, default: int) -> int:
        raw = _env(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{name} must be an integer, got {raw!r}")

    cookie_secure_raw = _env("SC_COOKIE_SECURE")
    secure_cookies = (cookie_secure_raw.lower() == "true") if cookie_secure_raw is not None else (env != "development")

    return AppConfig(
        env=env,
        database_url=database_url,
        auto_migrate=(_env("SC_AUTO_MIGRATE", "true") or "true").lower() != "false",
        db_pool_size=_int_env("SC_DB_POOL_SIZE", 5),
        db_max_overflow=_int_env("SC_DB_MAX_OVERFLOW", 5),
        storage=storage,
        trusted_proxy_ips=trusted_proxy_ips,
        admin_username=_env("SC_ADMIN_USERNAME", "organizer"),
        admin_password=_env("SC_ADMIN_PASSWORD"),
        sentry_dsn=_env("SENTRY_DSN"),
        secure_cookies=secure_cookies,
    )
