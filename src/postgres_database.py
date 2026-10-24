"""Explicit PostgreSQL connection and schema migration foundation.

SQLite remains the application default. Nothing in this module runs unless a
caller explicitly selects the PostgreSQL backend and invokes a function here.
"""
from __future__ import annotations

import argparse
import importlib
import ipaddress
import sys
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlsplit

from .database_config import DatabaseConfigurationError, load_database_config
from .db.migrations import MIGRATIONS

MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
# One stable transaction-scoped advisory lock serializes this project's migrations.
MIGRATION_LOCK_KEY = 0x504F4443415354
CONNECT_TIMEOUT_SECONDS = 10


class PostgresDatabaseError(RuntimeError):
    """A safe, credential-free PostgreSQL operation error."""


class PostgresBackendRequiredError(PostgresDatabaseError):
    """Raised when a PostgreSQL-only operation is requested under SQLite."""


class PostgresDriverUnavailableError(PostgresDatabaseError):
    """Raised when optional psycopg support has not been installed."""


class PostgresConnectionError(PostgresDatabaseError):
    """Raised when a PostgreSQL connection cannot be established."""


class PostgresTransactionError(PostgresDatabaseError):
    """Raised when a PostgreSQL transaction fails."""


class PostgresMigrationError(PostgresDatabaseError):
    """Raised when the migration history is inconsistent or unsupported."""


def _import_psycopg() -> Any:
    try:
        return importlib.import_module("psycopg")
    except ImportError:
        raise PostgresDriverUnavailableError(
            "PostgreSQL support requires the optional dependencies in requirements-postgres.txt."
        ) from None


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _connection_options(database_url: str) -> dict[str, Any]:
    """Require verified TLS for remote hosts; preserve local test settings."""
    parsed = urlsplit(database_url)
    hostname = parsed.hostname or ""
    if _is_loopback_host(hostname):
        return {"connect_timeout": CONNECT_TIMEOUT_SECONDS}

    configured_modes = parse_qs(parsed.query, keep_blank_values=True).get("sslmode", [])
    if configured_modes != ["verify-full"]:
        raise PostgresConnectionError(
            "Hosted PostgreSQL connections require exactly one sslmode=verify-full setting."
        )
    return {"connect_timeout": CONNECT_TIMEOUT_SECONDS, "sslmode": "verify-full"}


def verify_postgres_connection_identity(
    connection: Any,
    *,
    expected_database: str,
    expected_user: str | None,
    require_tls: bool,
) -> None:
    """Check the identity and transport of this exact connection before use."""
    try:
        row = connection.execute(
            "SELECT current_database(), current_user, "
            "(SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid())"
        ).fetchone()
    except Exception:
        raise PostgresConnectionError(
            "Could not verify the PostgreSQL connection identity and TLS state; details omitted."
        ) from None

    if not row or row[0] != expected_database:
        raise PostgresConnectionError(
            "Connected PostgreSQL database did not match the configured database."
        )
    if expected_user is not None and row[1] != expected_user:
        raise PostgresConnectionError(
            "Connected PostgreSQL role did not match the configured role."
        )
    if require_tls and row[2] is not True:
        raise PostgresConnectionError(
            "The remote PostgreSQL connection is not encrypted; no writes were performed."
        )

@contextmanager
def connect_postgres() -> Iterator[Any]:
    """Yield an explicitly configured psycopg connection in one transaction.

    Normal exit commits; exceptions roll back. The connection is always closed.
    Connection and driver errors are deliberately redacted rather than echoing
    a URL that may contain credentials.
    """
    try:
        config = load_database_config()
    except DatabaseConfigurationError as exc:
        raise PostgresBackendRequiredError(str(exc)) from None
    if config.backend != "postgres" or not config.database_url:
        raise PostgresBackendRequiredError(
            "PostgreSQL was not selected. Set PODCAST_DB_BACKEND=postgres and configure PODCAST_DATABASE_URL."
        )

    # Validate transport requirements before importing or invoking the driver.
    options = _connection_options(config.database_url)
    driver = _import_psycopg()
    connection = None
    connect_failed = False
    try:
        connection = driver.connect(config.database_url, **options)
    except Exception:
        connect_failed = True
    if connect_failed or connection is None:
        raise PostgresConnectionError(
            "Could not connect to the configured PostgreSQL database. Check its availability and TLS settings; connection details were omitted."
        ) from None

    parsed_url = urlsplit(config.database_url)
    expected_database = unquote(parsed_url.path.lstrip("/"))
    expected_user = unquote(parsed_url.username) if parsed_url.username is not None else None
    remote = not _is_loopback_host(parsed_url.hostname or "")
    if remote and (not expected_database or not expected_user):
        try:
            connection.close()
        except Exception:
            pass
        raise PostgresConnectionError(
            "Hosted PostgreSQL URLs must specify a database and role for identity verification."
        ) from None
    try:
        verify_postgres_connection_identity(
            connection,
            expected_database=expected_database,
            expected_user=expected_user,
            require_tls=remote,
        )
    except PostgresConnectionError:
        try:
            connection.rollback()
        except Exception:
            pass
        try:
            connection.close()
        except Exception:
            pass
        raise

    redact_transaction_error = False
    try:
        try:
            yield connection
            connection.commit()
        except BaseException as exc:
            try:
                connection.rollback()
            except Exception:
                pass
            driver_error = getattr(driver, "Error", ())
            if driver_error and isinstance(exc, driver_error):
                redact_transaction_error = True
            else:
                raise
        if redact_transaction_error:
            raise PostgresTransactionError(
                "The PostgreSQL transaction failed; database details were omitted."
            ) from None
    finally:
        try:
            connection.close()
        except Exception:
            # Do not replace a more useful transaction error with a close error.
            pass


def migrate_postgres() -> tuple[int, ...]:
    """Apply pending versioned schema migrations explicitly and atomically.

    Returns the version numbers applied by this call. A repeated call returns
    an empty tuple. No migration is run during dashboard startup.
    """
    migrations = tuple(sorted(MIGRATIONS, key=lambda item: item[0]))
    known_versions = {migration[0] for migration in migrations}
    if len(known_versions) != len(migrations) or any(version < 1 for version in known_versions):
        raise PostgresMigrationError("Migration versions must be unique positive integers.")

    applied_now: list[int] = []
    with connect_postgres() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
        connection.execute(MIGRATION_TABLE_SQL)
        rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied = {int(row[0]) for row in rows}
        unknown_versions = applied - known_versions
        if unknown_versions:
            raise PostgresMigrationError(
                "The database has a migration version newer than this application supports."
            )

        for version, name, statements in migrations:
            if version in applied:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (version, name),
            )
            applied_now.append(version)
    return tuple(applied_now)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply explicit PostgreSQL schema migrations.")
    parser.add_argument("command", choices=("migrate",), help="Apply pending PostgreSQL migrations.")
    parser.parse_args(argv)
    try:
        applied = migrate_postgres()
    except (PostgresDatabaseError, DatabaseConfigurationError) as exc:
        parser.error(str(exc))
    if applied:
        print("Applied PostgreSQL migration version(s): " + ", ".join(map(str, applied)))
    else:
        print("PostgreSQL schema is already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
