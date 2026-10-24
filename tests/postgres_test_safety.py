"""Safety gates for PostgreSQL integration-test targets."""
from __future__ import annotations

import ipaddress
import os
import uuid
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit
from src.postgres_database import PostgresConnectionError, verify_postgres_connection_identity

REMOTE_OPT_IN = "PODCAST_TEST_ALLOW_REMOTE"
EXPECTED_HOST = "PODCAST_TEST_EXPECTED_HOST"
EXPECTED_DATABASE = "PODCAST_TEST_EXPECTED_DATABASE"
EXPECTED_USER = "PODCAST_TEST_EXPECTED_USER"
DATABASE_URL = "PODCAST_TEST_DATABASE_URL"
SCHEMA_PREFIX = "podcast_pipeline_test_"
CONNECT_TIMEOUT_SECONDS = 10


class DatabaseTestSafetyError(ValueError):
    """Safe test-target configuration or isolation failure (contains no URL)."""


@dataclass(frozen=True)
class TestDatabaseTarget:
    url: str = field(repr=False)
    hostname: str = field(repr=False)
    database: str = field(repr=False)
    username: str | None = field(repr=False)
    remote: bool


def _loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def resolve_test_target(environ: Mapping[str, str] | None = None) -> TestDatabaseTarget | None:
    """Resolve the test-only URL; local loopback mode remains the default.

    Remote testing requires PODCAST_TEST_ALLOW_REMOTE=1, independently
    configured host/database/user expectations, and sslmode=verify-full.
    """
    settings = os.environ if environ is None else environ
    value = settings.get(DATABASE_URL, "").strip()
    opt_in = settings.get(REMOTE_OPT_IN, "").strip()
    if opt_in not in {"", "0", "1"}:
        raise DatabaseTestSafetyError(f"{REMOTE_OPT_IN} must be unset or set to '1'.")
    remote_enabled = opt_in == "1"
    if not value:
        if remote_enabled:
            raise DatabaseTestSafetyError("Remote PostgreSQL testing is enabled but its test URL is missing.")
        return None

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port  # Validate malformed/out-of-range port syntax.
        database = unquote(parsed.path.lstrip("/"))
        username = unquote(parsed.username) if parsed.username is not None else None
        query = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        raise DatabaseTestSafetyError("The PostgreSQL test URL is malformed; connection details were omitted.") from None

    if parsed.scheme.lower() not in {"postgres", "postgresql"} or not hostname or not database:
        raise DatabaseTestSafetyError("The PostgreSQL test URL must include a supported scheme, hostname, and database.")
    if any(char.isspace() for char in hostname) or "/" in database:
        raise DatabaseTestSafetyError("The PostgreSQL test URL is malformed; connection details were omitted.")

    if _loopback_host(hostname):
        if remote_enabled:
            raise DatabaseTestSafetyError("Remote-test opt-in cannot be used with a loopback URL.")
        if "test" not in database.casefold():
            raise DatabaseTestSafetyError("Local integration tests require a dedicated database name containing 'test'.")
        return TestDatabaseTarget(value, hostname, database, username, False)

    if not remote_enabled:
        raise DatabaseTestSafetyError(
            f"Hosted PostgreSQL URLs are rejected unless {REMOTE_OPT_IN}=1 is explicitly set."
        )
    if not username:
        raise DatabaseTestSafetyError("Remote test URLs must specify a database user.")
    if query.get("sslmode", []) != ["verify-full"]:
        raise DatabaseTestSafetyError("Remote integration tests require exactly one sslmode=verify-full setting.")

    expected_host = settings.get(EXPECTED_HOST, "").strip().lower()
    expected_database = settings.get(EXPECTED_DATABASE, "").strip()
    expected_user = settings.get(EXPECTED_USER, "").strip()
    if not expected_host or not expected_database or not expected_user:
        raise DatabaseTestSafetyError(
            f"Remote tests require independently configured {EXPECTED_HOST}, {EXPECTED_DATABASE}, and {EXPECTED_USER}."
        )
    if hostname.lower() != expected_host or database != expected_database or username != expected_user:
        raise DatabaseTestSafetyError("The remote test URL does not match the independently configured expected target.")
    if "test" not in database.casefold():
        raise DatabaseTestSafetyError("The expected remote database name must identify a dedicated test database.")

    return TestDatabaseTarget(value, hostname, database, username, True)


def close_quietly(connection) -> None:
    try:
        connection.close()
    except Exception:
        pass


def _connection_error_message(exc: BaseException) -> str:
    """Return a fixed, non-sensitive diagnostic selected from exception metadata."""
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    sqlstates: list[str] = []
    hints: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if isinstance(value, str):
                sqlstates.append(value)
        hints.append(type(current).__name__.casefold())
        # Driver exception text is inspected only for known diagnostic markers;
        # it is never returned, interpolated, or attached as a cause.
        hints.append(str(current).casefold())
        for attribute in ("__cause__", "__context__"):
            nested = getattr(current, attribute, None)
            if isinstance(nested, BaseException):
                pending.append(nested)

    if any(state.startswith("08") for state in sqlstates):
        return "PostgreSQL connection failure."
    text = " ".join(hints)
    if any(token in text for token in ("password", "authentication", "invalid_authorization", "28p01", "28000")):
        return "PostgreSQL authentication failed."
    if any(token in text for token in ("hostname mismatch", "host name mismatch", "does not match the host name", "does not match host name", "no alternative certificate subject name")):
        return "PostgreSQL TLS hostname verification failed."
    if any(token in text for token in (
        "root certificate file", "root.crt", "unable to get local issuer certificate",
        "unable to verify the first certificate", "unknown ca", "self-signed certificate",
    )):
        return "PostgreSQL TLS certificate trust verification failed."
    if any(token in text for token in (
        "certificate has expired", "certificate is not yet valid", "certificate revoked",
        "certificate has been revoked",
    )):
        return "PostgreSQL TLS certificate validity check failed."
    if "certificate verify failed" in text:
        return "PostgreSQL TLS certificate verification failed."
    if any(token in text for token in ("ssl", "tls", "certificate", "cert verification", "sslerror")):
        return "PostgreSQL TLS verification failed."
    if any(token in text for token in ("getaddrinfo", "name or service not known", "name resolution", "nodename nor servname", "dns", "could not translate host")):
        return "PostgreSQL host name could not be resolved."
    if any(token in text for token in ("timeout", "timed out", "connection refused", "network is unreachable", "no route to host", "connection reset", "operationalerror")):
        return "PostgreSQL network connection failed."
    if any(state.startswith(("3d", "42")) for state in sqlstates) or any(
        token in text for token in ("database", "does not exist", "permission denied", "not authorized", "insufficient_privilege", "3d000", "42501")
    ):
        return "PostgreSQL database access failed."
    return "Could not connect to the configured PostgreSQL test database; details omitted."


def open_verified_connection(psycopg, target: TestDatabaseTarget):
    """Verify server identity and transport encryption before schema changes."""
    options = {"connect_timeout": CONNECT_TIMEOUT_SECONDS}
    if target.remote:
        try:
            libpq_version = psycopg.pq.version()
        except Exception:
            raise DatabaseTestSafetyError(
                "Could not confirm PostgreSQL client support for system CA verification; no connection was attempted."
            ) from None
        if not isinstance(libpq_version, int) or libpq_version < 160000:
            raise DatabaseTestSafetyError(
                "Remote PostgreSQL tests require libpq 16 or newer for system CA verification; no connection was attempted."
            )
        options["sslmode"] = "verify-full"
        options["sslrootcert"] = "system"
    try:
        connection = psycopg.connect(target.url, **options)
    except Exception as exc:
        raise DatabaseTestSafetyError(_connection_error_message(exc)) from None

    try:
        verify_postgres_connection_identity(
            connection,
            expected_database=target.database,
            expected_user=target.username,
            require_tls=target.remote,
        )
    except PostgresConnectionError as exc:
        close_quietly(connection)
        raise DatabaseTestSafetyError(str(exc)) from None
    except Exception:
        close_quietly(connection)
        raise DatabaseTestSafetyError("Could not verify PostgreSQL test database identity and encryption; details omitted.") from None
    return connection


def drop_fixture_schema(connection, sql, schema: str, owned_schema: str | None) -> None:
    """Drop only the exact generated schema this fixture recorded as created."""
    if owned_schema is None or schema != owned_schema or not schema.startswith(SCHEMA_PREFIX):
        raise DatabaseTestSafetyError("Refusing to drop a schema not owned by this test fixture.")
    row = connection.execute(
        "SELECT (n.nspowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)) "
        "FROM pg_namespace AS n WHERE n.nspname = %s",
        (owned_schema,),
    ).fetchone()
    if row is None:
        return
    if row[0] is not True:
        raise DatabaseTestSafetyError("Refusing to drop the fixture schema because the current user does not own it.")
    connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(owned_schema)))
    connection.commit()
