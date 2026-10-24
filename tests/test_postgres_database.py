from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.postgres_database as postgres_database


class FakeConnection:
    def __init__(self, identity=("podcasts", "user", True)):
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.executed = []
        self.identity = identity

    def execute(self, query, params=None):
        self.executed.append((query, params))
        return self

    def fetchone(self):
        return self.identity

    def fetchall(self):
        return []

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakeDriverError(Exception):
    pass


def configure_postgres(monkeypatch, url="postgresql://user:secret@example.test/podcasts?sslmode=verify-full"):
    monkeypatch.setenv("PODCAST_DB_BACKEND", "postgres")
    monkeypatch.setenv("PODCAST_DATABASE_URL", url)



class MigrationTrackingConnection(FakeConnection):
    def __init__(self, applied_versions):
        super().__init__()
        self.applied_versions = applied_versions
        self.rows = []

    def execute(self, query, params=None):
        super().execute(query, params)
        if query == "SELECT version FROM schema_migrations ORDER BY version":
            self.rows = [(version,) for version in self.applied_versions]
        elif query.startswith("INSERT INTO schema_migrations"):
            self.applied_versions.append(params[0])
        return self

    def fetchone(self):
        if self.executed and "current_database()" in self.executed[-1][0]:
            return ("podcasts", "user", True)
        return None

    def fetchall(self):
        return self.rows


def test_migration_version_tracking_skips_already_applied_versions(monkeypatch):
    configure_postgres(monkeypatch)
    versions = []
    connections = []

    def make_connection(*_args, **_kwargs):
        connection = MigrationTrackingConnection(versions)
        connections.append(connection)
        return connection

    driver = SimpleNamespace(connect=make_connection, Error=FakeDriverError)
    monkeypatch.setattr(postgres_database, "_import_psycopg", lambda: driver)
    monkeypatch.setattr(
        postgres_database,
        "MIGRATIONS",
        ((1, "controlled", ("CREATE TABLE controlled_migration (id INTEGER)",)),),
    )

    assert postgres_database.migrate_postgres() == (1,)
    assert postgres_database.migrate_postgres() == ()
    assert versions == [1]
    assert sum(
        query == "CREATE TABLE controlled_migration (id INTEGER)"
        for connection in connections
        for query, _params in connection.executed
    ) == 1
    assert all(connection.committed and connection.closed for connection in connections)


def test_connection_requires_explicit_postgres_backend(monkeypatch):
    monkeypatch.delenv("PODCAST_DB_BACKEND", raising=False)
    monkeypatch.delenv("PODCAST_DATABASE_URL", raising=False)
    monkeypatch.setattr(postgres_database, "_import_psycopg", lambda: pytest.fail("Driver must not load."))

    with pytest.raises(postgres_database.PostgresBackendRequiredError, match="was not selected"):
        with postgres_database.connect_postgres():
            pass


def test_connection_passes_validated_url_timeout_and_verified_tls(monkeypatch):
    url = "postgres://user:secret@db.example.test/podcasts?sslmode=verify-full"
    configure_postgres(monkeypatch, url)
    connection = FakeConnection()
    calls = []
    driver = SimpleNamespace(connect=lambda *args, **kwargs: (calls.append((args, kwargs)) or connection), Error=FakeDriverError)
    monkeypatch.setattr(postgres_database, "_import_psycopg", lambda: driver)

    with postgres_database.connect_postgres() as actual:
        assert actual is connection

    assert calls == [((url,), {"connect_timeout": 10, "sslmode": "verify-full"})]
    assert connection.committed and connection.closed and not connection.rolled_back


def test_connection_preserves_stronger_remote_tls_verification(monkeypatch):
    url = "postgresql://user:secret@db.example.test/podcasts?sslmode=verify-full"
    configure_postgres(monkeypatch, url)
    connection = FakeConnection()
    calls = []
    driver = SimpleNamespace(connect=lambda *args, **kwargs: (calls.append(kwargs) or connection), Error=FakeDriverError)
    monkeypatch.setattr(postgres_database, "_import_psycopg", lambda: driver)

    with postgres_database.connect_postgres():
        pass

    assert calls[0]["sslmode"] == "verify-full"


def test_local_connection_can_use_local_tls_settings(monkeypatch):
    url = "postgresql://user:secret@127.0.0.1/podcasts?sslmode=disable"
    configure_postgres(monkeypatch, url)
    connection = FakeConnection(identity=("podcasts", "user", False))
    calls = []
    driver = SimpleNamespace(connect=lambda *args, **kwargs: (calls.append(kwargs) or connection), Error=FakeDriverError)
    monkeypatch.setattr(postgres_database, "_import_psycopg", lambda: driver)

    with postgres_database.connect_postgres():
        pass

    assert calls[0] == {"connect_timeout": 10}


def test_missing_psycopg_has_actionable_error(monkeypatch):
    configure_postgres(monkeypatch)

    def missing_driver(_name):
        raise ImportError("psycopg is not installed")

    monkeypatch.setattr(postgres_database.importlib, "import_module", missing_driver)
    with pytest.raises(postgres_database.PostgresDriverUnavailableError, match="requirements-postgres.txt"):
        with postgres_database.connect_postgres():
            pass


def test_connection_failure_does_not_expose_credentials(monkeypatch):
    url = "postgresql://user:very-secret-password@bad.example/podcasts?sslmode=verify-full"
    configure_postgres(monkeypatch, url)

    def fail_connect(*_args, **_kwargs):
        raise RuntimeError(f"could not connect using {url}")

    driver = SimpleNamespace(connect=fail_connect, Error=FakeDriverError)
    monkeypatch.setattr(postgres_database, "_import_psycopg", lambda: driver)
    with pytest.raises(postgres_database.PostgresConnectionError) as error:
        with postgres_database.connect_postgres():
            pass

    assert "very-secret-password" not in str(error.value)
    assert url not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_transaction_exception_rolls_back_and_closes(monkeypatch):
    configure_postgres(monkeypatch)
    connection = FakeConnection()
    driver = SimpleNamespace(connect=lambda *_args, **_kwargs: connection, Error=FakeDriverError)
    monkeypatch.setattr(postgres_database, "_import_psycopg", lambda: driver)

    with pytest.raises(RuntimeError, match="controlled failure"):
        with postgres_database.connect_postgres():
            raise RuntimeError("controlled failure")

    assert connection.rolled_back and connection.closed and not connection.committed


def test_driver_transaction_error_is_redacted_and_rolled_back(monkeypatch):
    configure_postgres(monkeypatch)
    connection = FakeConnection()
    driver = SimpleNamespace(connect=lambda *_args, **_kwargs: connection, Error=FakeDriverError)
    monkeypatch.setattr(postgres_database, "_import_psycopg", lambda: driver)

    with pytest.raises(postgres_database.PostgresTransactionError) as error:
        with postgres_database.connect_postgres():
            raise FakeDriverError("secret=very-secret-password")

    assert "very-secret-password" not in str(error.value)
    assert error.value.__cause__ is None
    assert connection.rolled_back and connection.closed


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?sslmode=",
        "?sslmode=require",
        "?sslmode=prefer",
        "?sslmode=disable",
        "?sslmode=allow",
        "?sslmode=verify-ca",
        "?sslmode=verify-full&sslmode=verify-full",
        "?sslmode=require&sslmode=verify-full",
    ],
)
def test_hosted_tls_rejects_missing_weak_or_repeated_sslmode_before_connect(monkeypatch, query):
    url = f"postgresql://user:secret@db.example.test/podcasts{query}"
    configure_postgres(monkeypatch, url)
    calls = []
    driver = SimpleNamespace(connect=lambda *args, **kwargs: calls.append(kwargs), Error=FakeDriverError)
    monkeypatch.setattr(postgres_database, "_import_psycopg", lambda: driver)

    with pytest.raises(
        postgres_database.PostgresConnectionError,
        match="exactly one sslmode=verify-full",
    ):
        with postgres_database.connect_postgres():
            pass

    assert calls == []


@pytest.mark.parametrize(
    "identity",
    [("other_database", "user", True), ("podcasts", "other_user", True)],
)
def test_remote_migration_identity_mismatch_prevents_migration_writes(monkeypatch, identity):
    url = "postgresql://user:secret@db.example.test/podcasts?sslmode=verify-full"
    configure_postgres(monkeypatch, url)
    connection = FakeConnection(identity=identity)
    driver = SimpleNamespace(connect=lambda *_args, **_kwargs: connection, Error=FakeDriverError)
    monkeypatch.setattr(postgres_database, "_import_psycopg", lambda: driver)

    with pytest.raises(postgres_database.PostgresConnectionError, match="did not match"):
        postgres_database.migrate_postgres()

    assert len(connection.executed) == 1
    assert "current_database()" in connection.executed[0][0]
    assert connection.rolled_back and connection.closed
    assert not connection.committed


def test_remote_migration_tls_failure_prevents_migration_writes(monkeypatch):
    url = "postgresql://user:secret@db.example.test/podcasts?sslmode=verify-full"
    configure_postgres(monkeypatch, url)
    connection = FakeConnection(identity=("podcasts", "user", False))
    calls = []
    driver = SimpleNamespace(
        connect=lambda *args, **kwargs: (calls.append((args, kwargs)) or connection),
        Error=FakeDriverError,
    )
    monkeypatch.setattr(postgres_database, "_import_psycopg", lambda: driver)

    with pytest.raises(postgres_database.PostgresConnectionError, match="not encrypted"):
        postgres_database.migrate_postgres()

    assert calls[0][1]["sslmode"] == "verify-full"
    assert len(connection.executed) == 1
    assert "current_database()" in connection.executed[0][0]
    assert connection.rolled_back and connection.closed
    assert not connection.committed