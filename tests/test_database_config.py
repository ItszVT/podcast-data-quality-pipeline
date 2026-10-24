from pathlib import Path

import pytest

from src.database_config import (
    DatabaseConfigurationError,
    load_database_config,
    require_sqlite_backend,
)


def test_sqlite_is_selected_by_default_and_keeps_supplied_path(tmp_path, monkeypatch):
    monkeypatch.delenv("PODCAST_DB_BACKEND", raising=False)
    monkeypatch.delenv("PODCAST_DATABASE_URL", raising=False)
    path = tmp_path / "local.sqlite"

    config = load_database_config(path)

    assert config.backend == "sqlite"
    assert config.sqlite_path == path
    assert config.database_url is None


def test_explicit_sqlite_selection_does_not_use_a_configured_postgres_url(tmp_path):
    url = "postgresql://alice:secret@example.test/podcasts"

    config = load_database_config(
        tmp_path / "local.sqlite",
        environ={"PODCAST_DB_BACKEND": "sqlite", "PODCAST_DATABASE_URL": url},
    )

    assert config.backend == "sqlite"
    assert config.sqlite_path == tmp_path / "local.sqlite"
    assert config.database_url is None
    assert "secret" not in repr(config)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://alice:secret@example.test/podcasts?sslmode=require",
        "postgres://alice:secret@example.test/podcasts?sslmode=require",
    ],
)
def test_postgres_selection_accepts_supported_url_schemes_without_connecting(url):

    config = load_database_config(
        environ={"PODCAST_DB_BACKEND": "postgres", "PODCAST_DATABASE_URL": url}
    )

    assert config.backend == "postgres"
    assert config.database_url == url
    assert config.sqlite_path is None
    assert "secret" not in repr(config)


@pytest.mark.parametrize("database_url", [None, "", "   "])
def test_postgres_selection_without_url_fails_clearly(database_url):
    environ = {"PODCAST_DB_BACKEND": "postgres"}
    if database_url is not None:
        environ["PODCAST_DATABASE_URL"] = database_url
    with pytest.raises(DatabaseConfigurationError, match="PODCAST_DATABASE_URL is required"):
        load_database_config(environ=environ)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://",
        "postgres://?sslmode=require",
        "postgresql://[::1",
        "postgresql://alice:secret@example.test:invalid/podcasts",
        "postgresql://host name/podcasts",
    ],
)
def test_malformed_postgres_urls_are_rejected_without_leaking_credentials(database_url):
    with pytest.raises(DatabaseConfigurationError) as error:
        load_database_config(
            environ={"PODCAST_DB_BACKEND": "postgres", "PODCAST_DATABASE_URL": database_url}
        )

    assert "secret" not in str(error.value)
    assert database_url not in str(error.value)


def test_unsupported_backend_is_rejected_without_echoing_value():
    sensitive_value = "postgresql://alice:secret@example.test/db"

    with pytest.raises(DatabaseConfigurationError) as error:
        load_database_config(environ={"PODCAST_DB_BACKEND": sensitive_value})

    assert "secret" not in str(error.value)
    assert sensitive_value not in str(error.value)


def test_invalid_postgres_url_is_rejected_without_echoing_credentials():
    sensitive_url = "mysql://alice:secret@example.test/db"

    with pytest.raises(DatabaseConfigurationError) as error:
        load_database_config(
            environ={"PODCAST_DB_BACKEND": "postgres", "PODCAST_DATABASE_URL": sensitive_url}
        )

    assert "secret" not in str(error.value)
    assert sensitive_url not in str(error.value)


def test_sqlite_only_entry_point_fails_closed_for_postgres(tmp_path, monkeypatch):
    monkeypatch.setenv("PODCAST_DB_BACKEND", "postgres")
    monkeypatch.setenv("PODCAST_DATABASE_URL", "postgresql://alice:secret@example.test/db")

    with pytest.raises(DatabaseConfigurationError, match="No SQLite fallback was used") as error:
        require_sqlite_backend(tmp_path / "local.sqlite")

    assert "secret" not in str(error.value)


def test_sqlite_only_entry_point_preserves_default_behavior(tmp_path, monkeypatch):
    monkeypatch.delenv("PODCAST_DB_BACKEND", raising=False)
    monkeypatch.delenv("PODCAST_DATABASE_URL", raising=False)

    config = require_sqlite_backend(tmp_path / "local.sqlite")

    assert config.backend == "sqlite"
    assert config.sqlite_path == Path(tmp_path / "local.sqlite")


def test_initialize_database_refuses_postgres_before_open_or_create(tmp_path, monkeypatch):
    import src.database as database

    monkeypatch.setenv("PODCAST_DB_BACKEND", "postgres")
    monkeypatch.setenv("PODCAST_DATABASE_URL", "postgresql://alice:secret@example.test/db")
    database_path = tmp_path / "new" / "unused.sqlite"

    def unexpected_open(*_args, **_kwargs):
        pytest.fail("SQLite must not be opened when PostgreSQL is selected.")

    monkeypatch.setattr(database.sqlite3, "connect", unexpected_open)
    with pytest.raises(DatabaseConfigurationError, match="No SQLite fallback was used"):
        database.initialize_database(database_path)

    assert not database_path.parent.exists()
    assert not database_path.exists()


def test_persist_audit_run_refuses_postgres_before_open_or_iterating_records(tmp_path, monkeypatch):
    import src.database as database

    monkeypatch.setenv("PODCAST_DB_BACKEND", "postgres")
    monkeypatch.setenv("PODCAST_DATABASE_URL", "postgresql://alice:secret@example.test/db")
    database_path = tmp_path / "new" / "unused.sqlite"
    iterated = False

    def records():
        nonlocal iterated
        iterated = True
        yield {"feed_id": "1", "title": "Should not be read"}

    def unexpected_open(*_args, **_kwargs):
        pytest.fail("SQLite must not be opened when PostgreSQL is selected.")

    monkeypatch.setattr(database.sqlite3, "connect", unexpected_open)
    with pytest.raises(DatabaseConfigurationError, match="No SQLite fallback was used"):
        database.persist_audit_run(database_path, "input.csv", records())

    assert not iterated
    assert not database_path.parent.exists()
    assert not database_path.exists()


def test_run_audit_refuses_postgres_before_open_or_ingestion(tmp_path, monkeypatch):
    import src.audit_runner as audit_runner
    from src.audit_runner import AuditRunnerError, run_audit

    monkeypatch.setenv("PODCAST_DB_BACKEND", "postgres")
    monkeypatch.setenv("PODCAST_DATABASE_URL", "postgresql://alice:secret@example.test/db")
    input_path = tmp_path / "input.csv"
    database_path = tmp_path / "new" / "unused.sqlite"
    ingested = False

    def unexpected_ingestion(*_args, **_kwargs):
        nonlocal ingested
        ingested = True
        pytest.fail("Ingestion must not start when PostgreSQL is selected.")

    def unexpected_open(*_args, **_kwargs):
        pytest.fail("SQLite must not be opened when PostgreSQL is selected.")

    monkeypatch.setattr(audit_runner, "load_podcast_file", unexpected_ingestion)
    monkeypatch.setattr(audit_runner.sqlite3, "connect", unexpected_open)
    with pytest.raises(AuditRunnerError, match="No SQLite fallback was used"):
        run_audit(input_path, database_path)

    assert not ingested
    assert not database_path.parent.exists()
    assert not database_path.exists()


def test_daily_pipeline_refuses_postgres_before_collection_or_sqlite_open(tmp_path, monkeypatch):
    import src.daily_pipeline as daily_pipeline
    from src.daily_pipeline import PipelinePreflightError, run_daily_pipeline

    monkeypatch.setenv("PODCAST_DB_BACKEND", "postgres")
    monkeypatch.setenv("PODCAST_DATABASE_URL", "postgresql://alice:secret@example.test/db")
    database_path = tmp_path / "new" / "unused.sqlite"
    collection_called = False

    def unexpected_open(*_args, **_kwargs):
        pytest.fail("SQLite must not be opened when PostgreSQL is selected.")

    monkeypatch.setattr(daily_pipeline.sqlite3, "connect", unexpected_open)

    def collector(*_args, **_kwargs):
        nonlocal collection_called
        collection_called = True
        raise AssertionError("Collection must not start for an unsupported backend.")

    with pytest.raises(PipelinePreflightError, match="No SQLite fallback was used"):
        run_daily_pipeline(database_path, collector=collector)

    assert not collection_called
    assert not database_path.parent.exists()
    assert not database_path.exists()


def test_dashboard_refuses_postgres_before_opening_sqlite(tmp_path, monkeypatch):
    import src.dashboard_data as dashboard_data
    from src.dashboard_data import DashboardDataError, get_runs

    monkeypatch.setenv("PODCAST_DB_BACKEND", "postgres")
    monkeypatch.setenv("PODCAST_DATABASE_URL", "postgresql://alice:secret@example.test/db")
    database_path = tmp_path / "missing.sqlite"

    def unexpected_open(*_args, **_kwargs):
        pytest.fail("Dashboard must not open SQLite when PostgreSQL is selected.")

    monkeypatch.setattr(dashboard_data.sqlite3, "connect", unexpected_open)
    with pytest.raises(DashboardDataError, match="No SQLite fallback was used") as error:
        get_runs(database_path)

    assert "secret" not in str(error.value)
    assert not database_path.exists()
