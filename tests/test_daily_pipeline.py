from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import src.database as database_module
import src.daily_pipeline as daily_pipeline_module
from src.audit_runner import AuditRunnerError

from src.daily_pipeline import (
    PipelineRunAlreadyExists,
    run_daily_pipeline,
)
from src.podcast_collector import CollectionResult, CollectorConfig


def collection(records, **report_overrides):
    report = {
        "api_search_errors": [],
        "rss_retrieval_failures": 0,
        "unique_records_retained": len(records),
    }
    report.update(report_overrides)
    return CollectionResult(tuple(records), report)


def record(identifier="itunes:collection:1", *, website="https://show.example",
           description="A show", categories="Education", language="en", country=""):
    return {
        "feed_id": identifier,
        "title": "Example Show",
        "website_url": website,
        "description": description,
        "categories": categories,
        "language": language,
        "country": country,
    }


def read_database(path: Path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def test_successful_pipeline_persists_validation_findings_without_failing(tmp_path):
    database = tmp_path / "audits.sqlite"
    result = run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: collection([
            record(country="US"),
            record("itunes:collection:2", website="", description="", categories="", language="", country=""),
        ]),
        audit_run_id="success-run",
    )

    assert result.pipeline_status == "succeeded"
    assert result.collection_status == "succeeded"
    assert result.validation_status == "succeeded"
    assert result.persistence_status == "succeeded"
    assert result.records_collected == result.records_validated == result.records_stored == 2
    assert result.records_with_findings == 1
    assert result.finding_occurrences == 5
    assert result.issue_counts == {"blank_website_url": 1, "missing_optional_field": 4}
    assert result.started_at and result.completed_at
    with read_database(database) as db:
        run = db.execute("SELECT * FROM audit_runs").fetchone()
        assert run["run_status"] == "succeeded"
        assert run["records_processed"] == 2
        assert db.execute("SELECT COUNT(*) FROM podcast_records").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM url_checks").fetchone()[0] == 0
        metadata = json.loads(run["metadata_json"])
        assert metadata["stages"]["collection"] == "succeeded"
        assert metadata["stages"]["validation"] == "succeeded"


def test_empty_collection_is_a_successful_zero_record_snapshot(tmp_path):
    database = tmp_path / "empty.sqlite"
    result = run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: collection([]),
        audit_run_id="empty-run",
    )

    assert result.pipeline_status == "succeeded"
    assert result.records_collected == result.records_validated == result.records_stored == 0
    assert result.records_with_findings == result.finding_occurrences == 0
    with read_database(database) as db:
        assert db.execute("SELECT run_status FROM audit_runs").fetchone()[0] == "succeeded"
        assert db.execute("SELECT COUNT(*) FROM podcast_records").fetchone()[0] == 0


def test_collection_failure_is_recorded_and_pipeline_is_not_successful(tmp_path):
    database = tmp_path / "failed.sqlite"

    def fail_collection(*_args, **_kwargs):
        raise RuntimeError("controlled collection failure")

    result = run_daily_pipeline(database, collector=fail_collection, audit_run_id="failed-run")

    assert result.pipeline_status == "failed"
    assert result.collection_status == "failed"
    assert result.validation_status == "skipped"
    assert result.persistence_status == "succeeded"
    assert result.records_collected == result.records_validated == result.records_stored == 0
    assert result.stage_failures[0].startswith("collection: RuntimeError:")
    with read_database(database) as db:
        run = db.execute("SELECT run_status, error_summary FROM audit_runs").fetchone()
        assert run["run_status"] == "failed"
        assert "controlled collection failure" in run["error_summary"]


def test_partial_collection_keeps_returned_records_and_marks_snapshot_partial(tmp_path):
    database = tmp_path / "partial.sqlite"
    result = run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: collection(
            [record()], api_search_errors=["science: temporary failure"]
        ),
        audit_run_id="partial-run",
    )

    assert result.pipeline_status == "partial"
    assert result.collection_status == "partial"
    assert result.records_collected == result.records_validated == result.records_stored == 1
    assert result.stage_failures and result.stage_failures[0].startswith("collection partial (API search):")
    with read_database(database) as db:
        run = db.execute("SELECT run_status, metadata_json FROM audit_runs").fetchone()
        assert run["run_status"] == "partial"
        assert json.loads(run["metadata_json"])["collection_report"]["api_search_errors"]


def test_validation_failure_stores_unmodified_collected_rows_and_fails_run(tmp_path):
    database = tmp_path / "validation-failed.sqlite"

    def fail_validation(*_args, **_kwargs):
        raise ValueError("controlled validation failure")

    result = run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: collection([record()]),
        validator=fail_validation,
        audit_run_id="validation-failed",
    )

    assert result.pipeline_status == "failed"
    assert result.collection_status == "succeeded"
    assert result.validation_status == "failed"
    assert result.persistence_status == "succeeded"
    assert result.records_collected == 1
    assert result.records_validated == 0
    assert result.records_stored == 1
    assert result.stage_failures[0].startswith("validation: ValueError:")
    with read_database(database) as db:
        row = db.execute("SELECT feed_id, title, website_url FROM podcast_records").fetchone()
        assert tuple(row) == ("itunes:collection:1", "Example Show", "https://show.example")
        assert db.execute("SELECT run_status FROM audit_runs").fetchone()[0] == "failed"


def test_persistence_failure_is_reported_and_not_claimed_as_success(tmp_path):
    database = tmp_path / "not-written.sqlite"

    def fail_persistence(*_args, **_kwargs):
        raise sqlite3.OperationalError("controlled persistence failure")

    result = run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: collection([record()]),
        persister=fail_persistence,
        audit_run_id="persistence-failed",
    )

    assert result.pipeline_status == "failed"
    assert result.collection_status == result.validation_status == "succeeded"
    assert result.persistence_status == "failed"
    assert result.records_collected == result.records_validated == 1
    assert result.records_stored == 0
    assert result.stage_failures[0].startswith("persistence: OperationalError:")
    assert not database.exists()


def test_reusing_run_id_refuses_before_collecting_or_adding_snapshot(tmp_path):
    database = tmp_path / "retry.sqlite"
    calls = 0

    def collect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return collection([record()])

    first = run_daily_pipeline(database, collector=collect, audit_run_id="same-run")
    assert first.pipeline_status == "succeeded"
    with pytest.raises(PipelineRunAlreadyExists, match="already exists"):
        run_daily_pipeline(database, collector=collect, audit_run_id="same-run")

    assert calls == 1
    with read_database(database) as db:
        assert db.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM podcast_records").fetchone()[0] == 1


def test_collection_parameters_and_search_terms_are_forwarded(tmp_path):
    captured = {}
    config = CollectorConfig(max_records=7, results_per_term=3, api_pacing_seconds=0)

    def collect(settings, *, search_terms):
        captured["config"] = settings
        captured["terms"] = search_terms
        return collection([])

    result = run_daily_pipeline(
        tmp_path / "options.sqlite",
        collector=collect,
        config=config,
        search_terms=("science", "history"),
    )

    assert result.pipeline_status == "succeeded"
    assert captured == {"config": config, "terms": ("science", "history")}







def test_write_failure_rolls_back_new_run_and_preserves_existing_history(tmp_path, monkeypatch):
    database = tmp_path / "atomic.sqlite"
    first = run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: collection([record(country="US")]),
        audit_run_id="existing-run",
    )
    assert first.pipeline_status == "succeeded"

    def fail_issue_insert(_issue):
        raise sqlite3.OperationalError("controlled mid-transaction failure")

    monkeypatch.setattr(database_module, "_issue_values", fail_issue_insert)
    result = run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: collection([record(country="")]),
        audit_run_id="interrupted-write",
    )

    assert result.pipeline_status == "failed"
    assert result.persistence_status == "failed"
    assert result.records_collected == result.records_validated == 1
    assert result.records_stored == 0
    assert any("controlled mid-transaction failure" in item for item in result.stage_failures)
    with read_database(database) as db:
        runs = db.execute("SELECT audit_run_id, run_status FROM audit_runs ORDER BY audit_run_id").fetchall()
        assert [tuple(row) for row in runs] == [("existing-run", "succeeded")]
        records = db.execute("SELECT audit_run_id, feed_id FROM podcast_records").fetchall()
        assert [tuple(row) for row in records] == [("existing-run", "itunes:collection:1")]


def test_collection_failure_and_failure_recording_failure_are_both_reported(tmp_path):
    database = tmp_path / "failed-recording.sqlite"
    first = run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: collection([record(country="US")]),
        audit_run_id="history-run",
    )
    assert first.pipeline_status == "succeeded"

    def fail_collection(*_args, **_kwargs):
        raise RuntimeError("collection unavailable")

    def fail_persistence(*_args, **_kwargs):
        raise sqlite3.OperationalError("failure record could not be saved")

    result = run_daily_pipeline(
        database,
        collector=fail_collection,
        persister=fail_persistence,
        audit_run_id="collection-failed",
    )

    assert result.pipeline_status == "failed"
    assert result.collection_status == "failed"
    assert result.validation_status == "skipped"
    assert result.persistence_status == "failed"
    assert result.records_collected == result.records_validated == result.records_stored == 0
    assert len(result.stage_failures) == 2
    assert result.stage_failures[0].startswith("collection: RuntimeError:")
    assert result.stage_failures[1].startswith("persistence: OperationalError:")
    with read_database(database) as db:
        assert [row[0] for row in db.execute("SELECT audit_run_id FROM audit_runs")] == ["history-run"]


def test_keyboard_interrupt_during_persistence_rolls_back_new_snapshot(tmp_path, monkeypatch):
    database = tmp_path / "interrupted.sqlite"
    run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: collection([record(country="US")]),
        audit_run_id="keep-this-run",
    )

    def interrupt_issue_insert(_issue):
        raise KeyboardInterrupt

    monkeypatch.setattr(database_module, "_issue_values", interrupt_issue_insert)
    with pytest.raises(KeyboardInterrupt):
        run_daily_pipeline(
            database,
            collector=lambda *_args, **_kwargs: collection([record(country="")]),
            audit_run_id="must-not-partially-exist",
        )

    with read_database(database) as db:
        assert [row[0] for row in db.execute("SELECT audit_run_id FROM audit_runs")] == ["keep-this-run"]
        assert [row[0] for row in db.execute("SELECT audit_run_id FROM podcast_records")] == ["keep-this-run"]


@pytest.mark.parametrize(
    ("report_overrides", "collector_error", "expected_exit", "expected_status"),
    [
        ({}, None, 0, "succeeded"),
        ({"api_search_errors": ["one term failed"]}, None, 2, "partial"),
        ({}, RuntimeError("total collection failure"), 1, "failed"),
    ],
)
def test_cli_exit_code_and_json_summary_match_run_status(
    tmp_path, monkeypatch, capsys, report_overrides, collector_error, expected_exit, expected_status
):
    database = tmp_path / f"cli-{expected_status}.sqlite"

    def collect(*_args, **_kwargs):
        if collector_error:
            raise collector_error
        return collection([record(country="US")], **report_overrides)

    monkeypatch.setattr(daily_pipeline_module, "collect_podcasts", collect)
    exit_code = daily_pipeline_module.main(["--database", str(database), "--run-id", f"cli-{expected_status}"])

    assert exit_code == expected_exit
    summary = json.loads(capsys.readouterr().out)
    assert summary["pipeline_status"] == expected_status
    assert summary["persistence_status"] == "succeeded"


def test_preflight_failure_reports_run_id_without_starting_collection(tmp_path, monkeypatch, capsys):
    called = False

    def collect(*_args, **_kwargs):
        nonlocal called
        called = True
        return collection([])

    def incompatible_database(_path):
        raise AuditRunnerError("incompatible database")

    monkeypatch.setattr(daily_pipeline_module, "collect_podcasts", collect)
    monkeypatch.setattr(daily_pipeline_module, "validate_existing_database", incompatible_database)
    exit_code = daily_pipeline_module.main([
        "--database", str(tmp_path / "existing.sqlite"), "--run-id", "preflight-run"
    ])

    assert exit_code == 1
    assert not called
    summary = json.loads(capsys.readouterr().err)
    assert summary["audit_run_id"] == "preflight-run"
    assert summary["pipeline_status"] == "failed"
    assert summary["preflight_status"] == "failed"
    assert summary["collection_status"] == "not_started"
    assert summary["validation_status"] == "not_started"
    assert summary["persistence_status"] == "not_started"
    assert summary["new_audit_record_written"] is False
    assert "incompatible database" in summary["stage_failures"][0]
