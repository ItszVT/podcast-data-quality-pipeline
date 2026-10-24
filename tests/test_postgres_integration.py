"""PostgreSQL integration tests; run only against an explicit local test database."""
from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from src import postgres_database
from postgres_test_safety import (
    DATABASE_URL, SCHEMA_PREFIX, DatabaseTestSafetyError,
    close_quietly, drop_fixture_schema, open_verified_connection, resolve_test_target,
)




@pytest.fixture
def isolated_postgres_schema(monkeypatch):
    try:
        target = resolve_test_target()
    except DatabaseTestSafetyError as exc:
        pytest.fail(str(exc), pytrace=False)
    if target is None:
        pytest.skip(f"Set {DATABASE_URL} to a dedicated local PostgreSQL test database.")
    try:
        import psycopg
        from psycopg import sql
    except ImportError:
        pytest.skip("Install requirements-postgres.txt to run PostgreSQL integration tests.")

    schema = SCHEMA_PREFIX + uuid.uuid4().hex
    created_schema: str | None = None
    try:
        connection = open_verified_connection(psycopg, target)
        try:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            created_schema = schema
            connection.commit()
        except DatabaseTestSafetyError:
            raise
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            raise DatabaseTestSafetyError("Could not create the isolated test schema; check test-role permissions.") from None
        finally:
            close_quietly(connection)

        parsed = urlsplit(target.url)
        query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if key != "options"]
        query.append(("options", f"-c search_path={schema}"))
        isolated_url = urlunsplit(parsed._replace(query=urlencode(query, quote_via=quote)))
        monkeypatch.setenv("PODCAST_DB_BACKEND", "postgres")
        monkeypatch.setenv("PODCAST_DATABASE_URL", isolated_url)
        isolated_target = replace(target, url=isolated_url)
        yield isolated_url, schema, isolated_target
    finally:
        if created_schema is not None:
            cleanup_connection = None
            try:
                cleanup_connection = open_verified_connection(psycopg, target)
                drop_fixture_schema(cleanup_connection, sql, schema, created_schema)
            except DatabaseTestSafetyError:
                raise
            except Exception:
                raise DatabaseTestSafetyError("Could not safely clean up the isolated test schema; details omitted.") from None
            finally:
                if cleanup_connection is not None:
                    close_quietly(cleanup_connection)



def test_migration_version_tracking_schema_constraints_indexes_and_foreign_keys(isolated_postgres_schema):
    import psycopg

    url, _schema, test_target = isolated_postgres_schema
    assert postgres_database.migrate_postgres() == (1,)
    assert postgres_database.migrate_postgres() == ()

    with open_verified_connection(psycopg, test_target) as connection:
        versions = connection.execute("SELECT version, name FROM schema_migrations").fetchall()
        assert versions == [(1, "initial_schema")]
        tables = {row[0] for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall()}
        assert {"audit_runs", "podcast_records", "data_quality_issues", "url_checks", "schema_migrations"} <= tables
        indexes = {row[0] for row in connection.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
        ).fetchall()}
        assert {
            "idx_audit_runs_started_at", "idx_audit_runs_status",
            "idx_podcast_records_feed_id", "idx_podcast_records_run",
            "idx_url_checks_classification", "idx_url_checks_run",
            "idx_data_quality_issues_category", "idx_data_quality_issues_run",
        } <= indexes

        connection.execute(
            "INSERT INTO audit_runs (audit_run_id, started_at, input_filename, run_status, records_processed) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("run-1", "2026-10-24T00:00:00+00:00", "test.csv", "succeeded", 1),
        )
        record_id = connection.execute(
            "INSERT INTO podcast_records (audit_run_id, source_row, record_json) VALUES (%s, %s, %s) RETURNING record_id",
            ("run-1", 2, "{\"original\":{}}"),
        ).fetchone()[0]
        # Schema-level findings are allowed to have a NULL record_id.
        connection.execute(
            "INSERT INTO data_quality_issues (audit_run_id, record_id, category, severity, message) "
            "VALUES (%s, NULL, %s, %s, %s)",
            ("run-1", "missing_required_column", "error", "Missing feed_id"),
        )
        connection.execute(
            "INSERT INTO url_checks (audit_run_id, record_id, classification, result_json) "
            "VALUES (%s, %s, %s, %s)",
            ("run-1", record_id, "inconclusive", "{}"),
        )

    with open_verified_connection(psycopg, test_target) as connection:
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                "INSERT INTO podcast_records (audit_run_id, source_row, record_json) "
                "VALUES (%s, %s, %s)",
                ("run-1", 2, "{}"),
            )

    with open_verified_connection(psycopg, test_target) as connection:
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                "INSERT INTO url_checks (audit_run_id, record_id, classification, result_json) "
                "VALUES (%s, %s, %s, %s)",
                ("run-1", record_id, "http_error", "{}"),
            )

    with open_verified_connection(psycopg, test_target) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO audit_runs (audit_run_id, started_at, input_filename, run_status, records_processed) "
                "VALUES (%s, %s, %s, %s, %s)",
                ("bad-run", "2026-10-24T00:00:00+00:00", "test.csv", "unknown", 0),
            )

    with open_verified_connection(psycopg, test_target) as connection:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                "INSERT INTO url_checks (audit_run_id, record_id, classification, result_json) "
                "VALUES (%s, %s, %s, %s)",
                ("run-1", record_id + 1000, "inconclusive", "{}"),
            )

    with open_verified_connection(psycopg, test_target) as connection:
        connection.execute("DELETE FROM audit_runs WHERE audit_run_id = %s", ("run-1",))
        assert connection.execute("SELECT COUNT(*) FROM podcast_records").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM data_quality_issues").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM url_checks").fetchone()[0] == 0


def test_failed_migration_rolls_back_schema_and_version_row(isolated_postgres_schema, monkeypatch):
    import psycopg

    url, _schema, test_target = isolated_postgres_schema
    failing_migration = (1, "controlled_failure", (
        "CREATE TABLE migration_rollback_probe (id INTEGER PRIMARY KEY)",
        "CREATE TABLE migration_syntax_error (",
    ))
    monkeypatch.setattr(postgres_database, "MIGRATIONS", (failing_migration,))

    with pytest.raises(postgres_database.PostgresTransactionError):
        postgres_database.migrate_postgres()

    with open_verified_connection(psycopg, test_target) as connection:
        assert connection.execute("SELECT to_regclass('schema_migrations')").fetchone()[0] is None
        assert connection.execute("SELECT to_regclass('migration_rollback_probe')").fetchone()[0] is None


def test_migration_sql_preserves_identity_and_nullable_composite_issue_relationship():
    statements = "\n".join(postgres_database.MIGRATIONS[0][2])
    assert "GENERATED BY DEFAULT AS IDENTITY" in statements
    assert "UNIQUE (audit_run_id, record_id)" in statements
    assert "record_id BIGINT," in statements
    assert "FOREIGN KEY (audit_run_id, record_id)" in statements
    assert "ON DELETE CASCADE" in statements
