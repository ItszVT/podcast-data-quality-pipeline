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
from src.url_checker import INCONCLUSIVE, LIKELY_WORKING, UrlCheckResult


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


def test_opt_in_website_checks_are_deduplicated_and_persisted(tmp_path):
    database = tmp_path / "website-checks.sqlite"
    calls = []

    def checker(url, _config, *, deadline_monotonic):
        calls.append((url, deadline_monotonic))
        return UrlCheckResult(url, url, 200, 0, (), 1.0, LIKELY_WORKING, "HTTP 200")

    result = run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: collection([
            record("one"), record("two"), record("three", website="https://other.example")
        ]),
        audit_run_id="website-check-run",
        check_websites=True,
        website_checker=checker,
    )

    assert result.pipeline_status == "succeeded"
    assert result.website_check_status == "succeeded"
    assert result.website_check_unique_urls == 2
    assert result.website_check_records == 3
    assert len(calls) == 2
    with read_database(database) as db:
        rows = db.execute("SELECT original_url, classification FROM url_checks ORDER BY original_url, url_check_id").fetchall()
        assert [(row[0], row[1]) for row in rows] == [
            ("https://other.example", LIKELY_WORKING),
            ("https://show.example", LIKELY_WORKING),
            ("https://show.example", LIKELY_WORKING),
        ]
        metadata = json.loads(db.execute("SELECT metadata_json FROM audit_runs").fetchone()[0])
        assert metadata["website_checking"]["unique_urls_checked"] == 2


def test_website_check_limit_and_checker_error_are_recorded_safely(tmp_path):
    database = tmp_path / "bounded-checks.sqlite"
    records = [record(f"show-{index}", website=f"https://show-{index}.example") for index in range(22)]
    calls = []

    def checker(url, _config, *, deadline_monotonic):
        calls.append(url)
        if url.endswith("0.example"):
            raise RuntimeError("sensitive URL and token should not be saved")
        return UrlCheckResult(url, None, None, 0, (), 1.0, INCONCLUSIVE, "Request failed safely", "connection_error")

    result = run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: collection(records),
        audit_run_id="bounded-check-run",
        check_websites=True,
        website_checker=checker,
    )

    assert result.pipeline_status == "succeeded"
    assert result.website_check_status == "partial"
    assert result.website_check_unique_urls == 20
    assert result.website_check_records == 20
    assert len(calls) == 20
    with read_database(database) as db:
        metadata = json.loads(db.execute("SELECT metadata_json FROM audit_runs").fetchone()[0])
        assert metadata["website_checking"]["stop_reason"] == "unique_url_limit_reached"
        saved = " ".join(str(value) for row in db.execute("SELECT result_json FROM url_checks") for value in row)
        assert "sensitive URL" not in saved
        assert "token" not in saved


def test_website_checks_interleave_collection_strata(tmp_path):
    records = [record(f"show-{index}", website=f"https://show-{index}.example") for index in range(24)]
    strata = ("alpha", "beta", "gamma")
    provenance = {
        row["feed_id"]: {"source": "Apple iTunes Search API", "query_stratum": strata[index % 3], "storefront": "US", "observed_at": "2026-01-01T00:00:00+00:00"}
        for index, row in enumerate(records)
    }
    calls = []
    def checker(url, _config, *, deadline_monotonic):
        calls.append(url)
        return UrlCheckResult(url, url, 200, 0, (), 1.0, LIKELY_WORKING, "HTTP 200")
    run_daily_pipeline(
        tmp_path / "stratified.sqlite",
        collector=lambda *_args, **_kwargs: collection(
            records, record_provenance_by_feed=provenance, search_terms=list(strata)
        ),
        audit_run_id="stratified-checks", check_websites=True, website_checker=checker,
    )
    assert len(calls) == 20
    assert calls[:6] == [f"https://show-{index}.example" for index in range(6)]


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


def test_prior_audited_ids_are_read_without_modifying_history(tmp_path):
    database = tmp_path / "repeat-history.sqlite"
    run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: collection([record("itunes:collection:prior")]),
        audit_run_id="prior-run",
    )
    assert daily_pipeline_module._previously_audited_feed_ids(database) == {"itunes:collection:prior"}


def test_partial_collection_keeps_returned_records_and_marks_snapshot_partial(tmp_path):
    database = tmp_path / "partial.sqlite"
    size_diagnostics = {
        "limit_bytes": 2_000_000,
        "attempt_count": 1,
        "declared_content_length_status_counts": {"valid": 1},
        "declared_content_length_size_bands": {"under_256_kib": 1},
        "size_limit_failure_count": 0,
        "size_limit_observed_body_lower_bound_bands": {},
    }
    result = run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: collection(
            [record()], api_search_errors=["science: temporary failure"],
            rss_response_size_diagnostics=size_diagnostics,
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
        metadata = json.loads(run["metadata_json"])
        assert metadata["collection_report"]["api_search_errors"]
        assert metadata["collection_report"]["rss_response_size_diagnostics"] == size_diagnostics


def test_rss_collection_deadline_marks_saved_audit_partial(tmp_path):
    report = {"rss_collection_timed_out": True, "rss_timeout_unattempted_feeds": 3}
    result = run_daily_pipeline(
        tmp_path / "rss-timeout.sqlite",
        collector=lambda *_args, **_kwargs: collection([record()], **report),
        audit_run_id="rss-timeout-run",
    )
    assert result.pipeline_status == "partial"
    assert result.collection_status == "partial"
    assert any("deadline reached" in failure for failure in result.stage_failures)


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
        ({"api_lookup_errors": ["repeat_lookup_1: dns_failure"]}, None, 2, "partial"),
        ({"api_lookup_errors": ["repeat_lookup_1: dns_failure", "repeat_lookup_2: dns_failure"]}, None, 2, "partial"),
        ({"api_lookup_errors": [f"repeat_lookup_{index}: dns_failure" for index in range(1, 36)]}, None, 2, "partial"),
        ({"sampling": {"repeat_lookup_no_matches": True}}, None, 2, "partial"),
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
    if report_overrides.get("api_lookup_errors"):
        assert any("Apple ID lookup" in item for item in summary["stage_failures"])


def test_cli_json_schema_keeps_collection_report_in_run_scoped_sqlite_metadata(
    tmp_path, monkeypatch, capsys
):
    database = tmp_path / "report-schema.sqlite"
    collection_report = {
        "sampling": {"repeat_target": 35, "actual_repeat_count": 1},
        "record_provenance_by_feed": {"itunes:collection:1": {"source": "Apple iTunes Lookup API"}},
    }
    monkeypatch.setattr(
        daily_pipeline_module,
        "collect_podcasts",
        lambda *_args, **_kwargs: collection([record()], **collection_report),
    )

    assert daily_pipeline_module.main([
        "--database", str(database), "--run-id", "report-schema-run"
    ]) == 0
    summary = json.loads(capsys.readouterr().out)

    assert set(summary) == {
        "audit_run_id", "preflight_status", "started_at", "completed_at",
        "pipeline_status", "collection_status", "validation_status", "persistence_status",
        "database_path", "records_collected", "records_validated", "records_stored",
        "records_with_findings", "finding_occurrences", "issue_counts", "stage_failures",
        "website_check_status", "website_check_unique_urls", "website_check_records",
    }
    assert summary["audit_run_id"] == "report-schema-run"
    assert "collection_report" not in summary
    with read_database(database) as db:
        row = db.execute(
            "SELECT metadata_json FROM audit_runs WHERE audit_run_id = ?",
            (summary["audit_run_id"],),
        ).fetchone()
    metadata = json.loads(row[0])
    assert metadata["collection_report"]["sampling"] == collection_report["sampling"]
    assert metadata["collection_report"]["record_provenance_by_feed"] == collection_report["record_provenance_by_feed"]


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
