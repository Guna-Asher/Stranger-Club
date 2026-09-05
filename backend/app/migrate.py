"""Explicit deployment migration step.

Run as a one-shot release-phase command BEFORE new application instances
start — never as an application-startup side effect once the deployment is
multi-instance. Safe to invoke concurrently (e.g. two deploy pipelines
racing): PostgreSQL callers serialize on a session-scoped advisory lock (see
database.py), so only one actually applies migrations while the other waits
and then finds nothing left to do.

Usage:
    python -m backend.app.migrate

Reads DATABASE_URL from the environment (via backend.app.config), same as
the application itself.
"""
from __future__ import annotations

import logging
import sys

from .config import ConfigError, load_config
from .database import run_migrations_to_head

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("stranger_club.migrate")


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("configuration_error error=%s", exc)
        return 1

    logger.info("migration_run_starting env=%s database_dialect=%s", config.env, "postgresql" if not config.is_sqlite else "sqlite")
    try:
        run_migrations_to_head(config.database_url)
    except Exception:
        logger.exception("migration_run_failed")
        return 1
    logger.info("migration_run_complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
