from __future__ import annotations

import os
import sys
import types

import pytest

import test_postgres_integration as postgres_integration

from postgres_test_safety import (
    DATABASE_URL,
    EXPECTED_DATABASE,
    EXPECTED_HOST,
    EXPECTED_USER,
    REMOTE_OPT_IN,
    SCHEMA_PREFIX,
    DatabaseTestSafetyError,
    drop_fixture_schema,
    open_verified_connection,
    resolve_test_target,
)
from postgres_test_safety import TestDatabaseTarget as _TestDatabaseTarget


def remote_env(url="postgresql://pipeline_test:secret@unit.neon.tech/podcast_test?sslmode=verify-full"):
    return {
        DATABASE_URL: url,
        REMOTE_OPT_IN: "1",
        EXPECTED_HOST: "unit.neon.tech",
        EXPECTED_DATABASE: "podcast_test",
        EXPECTED_USER: "pipeline_test",
    }


def test_local_only_mode_is_default_and_loopback_still_accepted():
    assert resolve_test_target({}) is None
    target = resolve_test_target({DATABASE_URL: "postgresql://tester:pw@localhost/podcasts_test"})
    assert target is not None
    assert target.remote is False
    assert target.database == "podcasts_test"


def test_hosted_url_is_rejected_without_explicit_opt_in():
    with pytest.raises(DatabaseTestSafetyError, match="explicitly set"):
        resolve_test_target({DATABASE_URL: "postgresql://tester:secret@unit.neon.tech/podcast_test?sslmode=verify-full"})


def test_remote_opt_in_must_be_explicit_and_well_formed():
    env = remote_env()
    env[REMOTE_OPT_IN] = "true"
    with pytest.raises(DatabaseTestSafetyError, match="must be unset or set"):
        resolve_test_target(env)


@pytest.mark.parametrize("url", [
    "postgresql://pipeline_test:secret@unit.neon.tech/podcast_test",
    "postgresql://pipeline_test:secret@unit.neon.tech/podcast_test?sslmode=require",
    "postgresql://pipeline_test:secret@unit.neon.tech/podcast_test?sslmode=disable",
    "postgresql://pipeline_test:secret@unit.neon.tech/podcast_test?sslmode=verify-full&sslmode=require",
])
def test_remote_target_requires_certificate_and_hostname_verification(url):
    with pytest.raises(DatabaseTestSafetyError, match="verify-full"):
        resolve_test_target(remote_env(url))


@pytest.mark.parametrize("key", [EXPECTED_DATABASE, EXPECTED_USER, EXPECTED_HOST])
def test_remote_target_requires_independent_expected_values(key):
    env = remote_env()
    env[key] = ""
    with pytest.raises(DatabaseTestSafetyError, match="independently configured"):
        resolve_test_target(env)


@pytest.mark.parametrize("key,value", [
    (EXPECTED_DATABASE, "production"),
    (EXPECTED_USER, "admin"),
    (EXPECTED_HOST, "prod.neon.tech"),
])
def test_remote_url_must_match_independent_expected_values(key, value):
    env = remote_env()
    env[key] = value
    with pytest.raises(DatabaseTestSafetyError, match="does not match"):
        resolve_test_target(env)


def test_remote_database_and_user_mismatch_fail_before_any_schema_operation():
    target = resolve_test_target(remote_env())

    class FakeConnection:
        def __init__(self, row):
            self.row = row
            self.statements = []
            self.closed = False

        def execute(self, statement, params=None):
            self.statements.append(statement)
            return self

        def fetchone(self):
            return self.row

        def close(self):
            self.closed = True

    for actual in [
        ("unexpected_db", "pipeline_test", True),
        ("podcast_test", "unexpected_user", True),
    ]:
        connection = FakeConnection(actual)

        class FakePsycopg:
            pq = types.SimpleNamespace(version=lambda: 160000)

            @staticmethod
            def connect(*_args, **_kwargs):
                return connection

        with pytest.raises(DatabaseTestSafetyError, match="did not match"):
            open_verified_connection(FakePsycopg, target)
        assert connection.closed
        assert not any("CREATE SCHEMA" in str(q) for q in connection.statements)


def test_unencrypted_remote_connection_fails_before_schema_creation():
    target = resolve_test_target(remote_env())

    class FakeConnection:
        def __init__(self):
            self.statements = []
            self.closed = False

        def execute(self, statement, params=None):
            self.statements.append(statement)
            return self

        def fetchone(self):
            return ("podcast_test", "pipeline_test", False)

        def close(self):
            self.closed = True

    connection = FakeConnection()

    class FakePsycopg:
        pq = types.SimpleNamespace(version=lambda: 160000)

        @staticmethod
        def connect(*_args, **kwargs):
            assert kwargs["sslmode"] == "verify-full"
            assert kwargs["sslrootcert"] == "system"
            return connection

    with pytest.raises(DatabaseTestSafetyError, match="not encrypted"):
        open_verified_connection(FakePsycopg, target)
    assert connection.closed
    assert not any("CREATE SCHEMA" in str(q) for q in connection.statements)


def test_configuration_errors_never_echo_credentials_or_full_url():
    secret_url = "postgresql://pipeline_test:very-secret@unit.neon.tech/podcast_test?sslmode=require"
    with pytest.raises(DatabaseTestSafetyError) as error:
        resolve_test_target(remote_env(secret_url))
    assert "very-secret" not in str(error.value)
    assert secret_url not in str(error.value)


@pytest.mark.parametrize(
    "error, expected",
    [
        (type("DriverError", (Exception,), {"sqlstate": "08001"})
         ("postgresql://user:secret@private.example/db connection failed"), "PostgreSQL connection failure."),
        (type("OperationalError", (Exception,), {})
         ("password authentication failed for user secret at private.example"), "PostgreSQL authentication failed."),
        (type("OperationalError", (Exception,), {})
         ("certificate verify failed for private.example"), "PostgreSQL TLS certificate trust verification failed."),
        (type("OperationalError", (Exception,), {})
         ("could not translate host name private.example"), "PostgreSQL host name could not be resolved."),
        (type("OperationalError", (Exception,), {})
         ("connection timed out to private.example"), "PostgreSQL network connection failed."),
        (type("DatabaseError", (Exception,), {"sqlstate": "3D000"})
         ("database db does not exist at private.example"), "PostgreSQL database access failed."),
    ],
)
def test_connection_failures_use_safe_classifications_without_connection_details(error, expected):
    target = resolve_test_target(remote_env())

    class FakePsycopg:
        pq = types.SimpleNamespace(version=lambda: 160000)

        @staticmethod
        def connect(*_args, **_kwargs):
            raise error

    with pytest.raises(DatabaseTestSafetyError) as caught:
        open_verified_connection(FakePsycopg, target)

    message = str(caught.value)
    assert message == expected
    assert "secret" not in message
    assert "private.example" not in message
    assert "postgresql://" not in message


@pytest.mark.parametrize(
    "detail, expected",
    [
        ("certificate verify failed: unable to get local issuer certificate at hidden.example", "PostgreSQL TLS certificate trust verification failed."),
        ("hostname mismatch for hidden.example", "PostgreSQL TLS hostname verification failed."),
    ],
)
def test_tls_diagnostics_distinguish_trust_from_hostname_without_sensitive_details(detail, expected):
    from postgres_test_safety import _connection_error_message

    message = _connection_error_message(RuntimeError(detail))

    assert message == expected
    assert "hidden.example" not in message


def test_remote_connection_requires_libpq_16_for_system_ca_support():
    target = resolve_test_target(remote_env())
    called = []

    class FakePsycopg:
        pq = types.SimpleNamespace(version=lambda: 150000)

        @staticmethod
        def connect(*_args, **_kwargs):
            called.append(True)

    with pytest.raises(DatabaseTestSafetyError, match="libpq 16 or newer"):
        open_verified_connection(FakePsycopg, target)
    assert called == []


def test_cleanup_refuses_to_drop_schema_other_than_fixture_owned_name():
    class FakeSQL:
        @staticmethod
        def SQL(statement):
            return statement

    class FakeConnection:
        statements = []

        def execute(self, statement, params=None):
            self.statements.append((statement, params))
            return self

    connection = FakeConnection()
    with pytest.raises(DatabaseTestSafetyError, match="Refusing to drop"):
        drop_fixture_schema(connection, FakeSQL, SCHEMA_PREFIX + "foreign", SCHEMA_PREFIX + "owned")
    assert connection.statements == []


def test_cleanup_refuses_schema_not_owned_by_current_user():
    class FakeSQL:
        @staticmethod
        def SQL(statement):
            return statement

    class FakeConnection:
        def __init__(self):
            self.statements = []

        def execute(self, statement, params=None):
            self.statements.append((statement, params))
            return self

        def fetchone(self):
            return (False,)

    schema = SCHEMA_PREFIX + "owned"
    connection = FakeConnection()
    with pytest.raises(DatabaseTestSafetyError, match="does not own"):
        drop_fixture_schema(connection, FakeSQL, schema, schema)
    assert len(connection.statements) == 1
    assert not any("DROP SCHEMA" in str(q) for q, _ in connection.statements)

class _FakeIdentifier:
    def __init__(self, value):
        self.value = value


class _FakeSQL:
    @staticmethod
    def SQL(statement):
        class Statement:
            def __str__(self):
                return statement

            def format(self, identifier):
                return statement.replace("{}", f'"{identifier.value}"')
        return Statement()

    @staticmethod
    def Identifier(value):
        return _FakeIdentifier(value)


class _FixtureConnection:
    def __init__(self, identity=("podcasts_test", "tester", False), *, create_error=None, commit_error=None, owner=True):
        self.identity = identity
        self.create_error = create_error
        self.commit_error = commit_error
        self.owner = owner
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, statement, params=None):
        rendered = str(statement)
        self.statements.append((rendered, params))
        if "CREATE SCHEMA" in rendered and self.create_error:
            raise self.create_error
        return self

    def fetchone(self):
        statement = self.statements[-1][0]
        if "current_database()" in statement:
            return self.identity
        if "pg_namespace" in statement:
            return (self.owner,)
        return None

    def commit(self):
        self.commits += 1
        if self.commit_error:
            raise self.commit_error

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _local_target():
    return _TestDatabaseTarget(
        url="postgresql://tester:secret@localhost/podcasts_test",
        hostname="localhost",
        database="podcasts_test",
        username="tester",
        remote=False,
    )


def _install_fake_driver(monkeypatch, *connections):
    calls = []
    available_connections = list(connections)
    driver = types.ModuleType("psycopg")
    driver.sql = _FakeSQL
    driver.pq = types.SimpleNamespace(version=lambda: 160000)

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        if not available_connections:
            raise AssertionError("Unexpected PostgreSQL connection attempt")
        return available_connections.pop(0)

    driver.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg", driver)
    return calls


def _fixture_generator(monkeypatch):
    # Invoke the actual pytest fixture generator to cover setup/yield/teardown
    # orchestration without opening a real database connection.
    return postgres_integration.isolated_postgres_schema.__wrapped__(monkeypatch)


def test_fixture_configuration_failure_prevents_connection_and_schema_ddl(monkeypatch):
    calls = _install_fake_driver(monkeypatch)

    def reject_configuration():
        raise DatabaseTestSafetyError("invalid target")

    monkeypatch.setattr(postgres_integration, "resolve_test_target", reject_configuration)
    with pytest.raises(pytest.fail.Exception, match="invalid target"):
        next(_fixture_generator(monkeypatch))
    assert calls == []


@pytest.mark.parametrize(
    "identity, message",
    [
        (("wrong_database", "tester", False), "did not match"),
        (("podcasts_test", "wrong_user", False), "did not match"),
    ],
)
def test_fixture_identity_failure_prevents_create_and_cleanup(monkeypatch, identity, message):
    connection = _FixtureConnection(identity=identity)
    calls = _install_fake_driver(monkeypatch, connection)
    monkeypatch.setattr(postgres_integration, "resolve_test_target", _local_target)

    with pytest.raises(DatabaseTestSafetyError, match=message):
        next(_fixture_generator(monkeypatch))

    assert len(calls) == 1
    assert connection.closed
    assert not any("CREATE SCHEMA" in sql for sql, _ in connection.statements)
    assert not any("DROP SCHEMA" in sql for sql, _ in connection.statements)


def test_fixture_tls_failure_prevents_create_and_cleanup(monkeypatch):
    target = _TestDatabaseTarget(
        url="postgresql://tester:secret@host.example/podcast_test?sslmode=verify-full",
        hostname="host.example",
        database="podcast_test",
        username="tester",
        remote=True,
    )
    connection = _FixtureConnection(identity=("podcast_test", "tester", False))
    calls = _install_fake_driver(monkeypatch, connection)
    monkeypatch.setattr(postgres_integration, "resolve_test_target", lambda: target)

    with pytest.raises(DatabaseTestSafetyError, match="not encrypted"):
        next(_fixture_generator(monkeypatch))

    assert calls[0][1]["sslmode"] == "verify-full"
    assert connection.closed
    assert not any("CREATE SCHEMA" in sql for sql, _ in connection.statements)
    assert not any("DROP SCHEMA" in sql for sql, _ in connection.statements)


def test_fixture_schema_creation_failure_rolls_back_without_broad_cleanup(monkeypatch):
    connection = _FixtureConnection(create_error=RuntimeError("secret-bearing driver detail"))
    calls = _install_fake_driver(monkeypatch, connection)
    monkeypatch.setattr(postgres_integration, "resolve_test_target", _local_target)

    with pytest.raises(DatabaseTestSafetyError, match="Could not create") as error:
        next(_fixture_generator(monkeypatch))

    assert "secret-bearing" not in str(error.value)
    assert connection.rollbacks == 1
    assert connection.closed
    assert any("CREATE SCHEMA" in sql for sql, _ in connection.statements)
    assert not any("DROP SCHEMA" in sql for sql, _ in connection.statements)
    assert len(calls) == 1


def test_fixture_execution_exception_cleans_only_created_schema(monkeypatch):
    setup = _FixtureConnection()
    cleanup = _FixtureConnection()
    calls = _install_fake_driver(monkeypatch, setup, cleanup)
    monkeypatch.setattr(postgres_integration, "resolve_test_target", _local_target)
    generator = _fixture_generator(monkeypatch)

    _isolated_url, schema, _target = next(generator)
    with pytest.raises(RuntimeError, match="controlled test failure"):
        generator.throw(RuntimeError("controlled test failure"))

    assert schema.startswith(SCHEMA_PREFIX)
    assert any("CREATE SCHEMA" in sql for sql, _ in setup.statements)
    drops = [(sql, params) for sql, params in cleanup.statements if "DROP SCHEMA" in sql]
    assert drops == [(f'DROP SCHEMA "{schema}" CASCADE', None)]
    assert cleanup.commits == 1
    assert len(calls) == 2


def test_fixture_cleanup_failure_is_redacted_and_not_retried_broadly(monkeypatch):
    setup = _FixtureConnection()
    cleanup = _FixtureConnection()
    calls = _install_fake_driver(monkeypatch, setup, cleanup)
    monkeypatch.setattr(postgres_integration, "resolve_test_target", _local_target)
    drop_calls = []

    def fail_cleanup(_connection, _sql, schema, owned_schema):
        drop_calls.append((schema, owned_schema))
        raise RuntimeError("postgresql://tester:secret@host.example/podcast_test")

    monkeypatch.setattr(postgres_integration, "drop_fixture_schema", fail_cleanup)
    generator = _fixture_generator(monkeypatch)
    _isolated_url, schema, _target = next(generator)

    with pytest.raises(DatabaseTestSafetyError) as error:
        next(generator)

    assert "secret" not in str(error.value)
    assert drop_calls == [(schema, schema)]
    assert len(calls) == 2
    assert not any(
        "DROP DATABASE" in sql or "DROP SCHEMA public" in sql
        for conn in (setup, cleanup)
        for sql, _ in conn.statements
    )


def test_fixture_rewrites_application_url_to_isolated_test_url(monkeypatch):
    setup = _FixtureConnection()
    cleanup = _FixtureConnection()
    calls = _install_fake_driver(monkeypatch, setup, cleanup)
    monkeypatch.setattr(postgres_integration, "resolve_test_target", _local_target)
    monkeypatch.setenv("PODCAST_DATABASE_URL", "postgresql://app:secret@production.example/production")
    generator = _fixture_generator(monkeypatch)

    isolated_url, schema, _target = next(generator)
    configured_url = os.environ["PODCAST_DATABASE_URL"]
    assert configured_url == isolated_url
    assert "production.example" not in configured_url
    assert f"search_path%3D{schema}" in configured_url
    generator.close()
    assert len(calls) == 2


def test_fixture_drop_helper_rejects_an_altered_schema_before_ddl():
    class Connection:
        def __init__(self):
            self.statements = []

        def execute(self, statement, params=None):
            self.statements.append((statement, params))
            return self

    connection = Connection()
    owned = SCHEMA_PREFIX + "owned"
    altered = SCHEMA_PREFIX + "different"
    with pytest.raises(DatabaseTestSafetyError, match="not owned"):
        drop_fixture_schema(connection, _FakeSQL, altered, owned)
    assert connection.statements == []


def test_fixture_commit_failure_cleans_only_schema_created_by_current_fixture(monkeypatch):
    setup = _FixtureConnection(commit_error=RuntimeError("commit failed"))
    cleanup = _FixtureConnection()
    calls = _install_fake_driver(monkeypatch, setup, cleanup)
    monkeypatch.setattr(postgres_integration, "resolve_test_target", _local_target)

    with pytest.raises(DatabaseTestSafetyError, match="Could not create"):
        next(_fixture_generator(monkeypatch))

    create_statements = [sql for sql, _ in setup.statements if "CREATE SCHEMA" in sql]
    cleanup_drops = [sql for sql, _ in cleanup.statements if "DROP SCHEMA" in sql]
    assert len(create_statements) == 1
    schema = create_statements[0].split('"')[1]
    assert schema.startswith(SCHEMA_PREFIX)
    assert cleanup_drops == [f'DROP SCHEMA "{schema}" CASCADE']
    assert len(calls) == 2
