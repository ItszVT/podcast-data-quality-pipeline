from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.database import initialize_database
from src.podcast_collector import CollectionError, CollectorConfig, FeedMetadata, parse_rss_metadata
from src.rss_size_experiment import (
    CAP_2_MB,
    CAP_4_MB,
    EXPERIMENT_CAPS,
    ExperimentError,
    MAX_RECORDS,
    PROCESS_RSS_BUDGET_BYTES,
    PROCESS_VIRTUAL_MEMORY_LIMIT_MIB,
    WORKFLOW_TIMEOUT_MINUTES,
    balanced_cap_order,
    run_experiment,
    run_paired_experiment,
    validate_audit_ids,
)


def audit_ids() -> list[str]:
    return [f"itunes:collection:{index}" for index in range(1, MAX_RECORDS + 1)]


def create_audit(path, feed_ids=None):
    ids = audit_ids() if feed_ids is None else feed_ids
    initialize_database(path)
    metadata = {
        "collection_report": {
            "record_provenance_by_feed": {
                feed_id: {"storefront": "CA"} for feed_id in ids
            }
        }
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO audit_runs (audit_run_id, started_at, completed_at, input_filename, run_status, records_processed, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("fixed-audit", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z", "fixture", "succeeded", len(ids), json.dumps(metadata)),
        )
        connection.executemany(
            "INSERT INTO podcast_records (audit_run_id, source_row, feed_id, record_json) VALUES (?, ?, ?, ?)",
            [("fixed-audit", i + 1, feed_id, "{}") for i, feed_id in enumerate(ids)],
        )
    return ids


def feed_rows(ids=None):
    ids = audit_ids() if ids is None else ids
    return [
        {"feed_id": feed_id, "url": f"https://rss.invalid/{i}", "lookup_status": "found"}
        for i, feed_id in enumerate(ids)
    ]


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def test_production_default_and_experiment_caps_are_fixed():
    assert CollectorConfig().max_response_bytes == 4_000_000
    assert CAP_2_MB == 2_000_000
    assert EXPERIMENT_CAPS == (2_000_000, 4_000_000)
    assert max(EXPERIMENT_CAPS) == CAP_4_MB


def test_workflow_is_manual_read_only_and_never_publishes_or_uploads_artifacts():
    workflow = Path(__file__).parents[1] / ".github/workflows/experiment-rss-response-size.yml"
    source = workflow.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in source
    assert "required: true" in source and "audit_run_id:" in source
    assert "permissions:\n  contents: read" in source
    assert "git show" in source and "SNAPSHOT_BRANCH: audit-snapshots" in source
    assert "timeout-minutes: 30" in source
    assert f"ulimit -v {PROCESS_VIRTUAL_MEMORY_LIMIT_MIB * 1024}" in source
    assert "git push" not in source and "git commit" not in source and "git add" not in source
    assert "upload-artifact" not in source
    assert WORKFLOW_TIMEOUT_MINUTES == 30


def test_selected_audit_requires_exactly_50_unique_valid_apple_ids():
    assert validate_audit_ids(audit_ids()) == tuple(audit_ids())
    with pytest.raises(ExperimentError, match="exactly 50"):
        validate_audit_ids(audit_ids()[:-1])
    duplicate = audit_ids()
    duplicate[-1] = duplicate[0]
    with pytest.raises(ExperimentError, match="duplicate"):
        validate_audit_ids(duplicate)
    invalid = audit_ids()
    invalid[2] = "itunes:collection:https://secret.invalid"
    with pytest.raises(ExperimentError, match="invalid Apple"):
        validate_audit_ids(invalid)


def test_run_experiment_uses_exact_audit_ids_and_only_one_lookup_per_id(tmp_path):
    database = tmp_path / "audit.sqlite"
    ids = create_audit(database)
    before = database.read_bytes()
    looked_up = []

    def lookup(identifier, config):
        looked_up.append((identifier, config.storefront, config.max_response_bytes))
        return [{"collectionId": int(identifier), "feedUrl": f"https://rss.invalid/{identifier}"}]

    def fetcher(_url, _config, *, deadline_monotonic):
        assert deadline_monotonic > 0
        return FeedMetadata(description="RSS description", language="en")

    result = run_experiment(database, "fixed-audit", lookup=lookup, fetcher=fetcher, sleeper=lambda _: None,
                            memory_reader=lambda: 64 * 1024 * 1024)
    assert [int(value[0]) for value in looked_up] == [int(feed_id.rsplit(":", 1)[1]) for feed_id in ids]
    assert len(looked_up) == MAX_RECORDS
    assert result["lookup_requests_attempted"] == MAX_RECORDS
    assert {value[2] for value in looked_up} == {CAP_2_MB}
    assert result["selected_record_count"] == result["unique_id_count"] == 50
    assert result["lookup_outcomes"]["found"] == 50
    assert result["conditions"][str(CAP_2_MB)]["attempt_count"] == 50
    assert result["conditions"][str(CAP_4_MB)]["attempt_count"] == 50
    assert database.read_bytes() == before


def test_lookup_failures_are_sanitized_and_reconciled_without_rss_requests(tmp_path):
    database = tmp_path / "audit.sqlite"
    create_audit(database)
    fetches = []

    def lookup(identifier, _config):
        if identifier == "1":
            raise RuntimeError("https://private.invalid/feed?token=secret")
        return []

    result = run_experiment(database, "fixed-audit", lookup=lookup,
                            fetcher=lambda *args, **kwargs: fetches.append(args), sleeper=lambda _: None)
    assert result["lookup_outcomes"]["lookup_unavailable"] == 50
    assert result["lookup_requests_attempted"] == 50
    assert result["paired_outcomes"]["reconciled_pairs"] == 50
    assert result["conditions"][str(CAP_2_MB)]["attempt_count"] == 0
    assert result["conditions"][str(CAP_4_MB)]["attempt_count"] == 0
    assert fetches == []
    serialized = json.dumps(result)
    assert "private.invalid" not in serialized and "secret" not in serialized and "https://" not in serialized


def test_paired_outcomes_and_metadata_coverage_reconcile_in_balanced_order():
    assert sum(balanced_cap_order(index)[0] == CAP_2_MB for index in range(50)) == 25
    assert sum(balanced_cap_order(index)[0] == CAP_4_MB for index in range(50)) == 25
    calls = []

    def fetcher(url, config, *, deadline_monotonic):
        calls.append((url, config.max_response_bytes, deadline_monotonic))
        index = int(url.rsplit("/", 1)[1])
        cap = config.max_response_bytes
        if index == 0 and cap == CAP_2_MB:
            raise CollectionError("RSS response exceeded the configured size limit.")
        if index == 1:
            raise CollectionError("RSS response exceeded the configured size limit.")
        if index == 3 and cap == CAP_4_MB:
            raise CollectionError("RSS request or collection deadline exceeded.")
        if index == 4:
            raise CollectionError("RSS XML is malformed.")
        return FeedMetadata(website_url="https://publisher.invalid", description="desc", language="en")

    result = run_paired_experiment(feed_rows(), fetcher=fetcher, sleeper=lambda _: None,
                                   memory_reader=lambda: 64 * 1024 * 1024)
    assert len(calls) == 100
    assert calls[0][1] == CAP_2_MB and calls[1][1] == CAP_4_MB
    assert calls[2][1] == CAP_4_MB and calls[3][1] == CAP_2_MB
    assert all(calls[index][0] == calls[index + 1][0] for index in range(0, len(calls), 2))
    pairs = result["paired_outcomes"]
    assert pairs["2mb_size_limit_to_4mb_success"] == 1
    assert pairs["size_limit_under_both"] == 1
    assert pairs["successful_parse_under_both"] == 46
    assert pairs["2mb_success_to_4mb_failure"] == 1
    assert pairs["other_outcome_transitions"] == 1
    assert pairs["reconciled_pairs"] == 50 and result["paired_outcomes_reconcile"] is True
    assert result["conditions"][str(CAP_2_MB)]["metadata_coverage"]["description"] == 47
    assert result["conditions"][str(CAP_4_MB)]["metadata_coverage"]["website"] == 47


def test_experiment_accepts_only_full_xml_parser_successes():
    valid_xml = b"<rss version='2.0'><channel><title>Complete</title><description>ok</description></channel></rss>"
    partial_xml = b"<rss version='2.0'><channel><title>Incomplete</title>"

    def fetcher(url, _config, *, deadline_monotonic):
        if url.endswith("/0"):
            return parse_rss_metadata(valid_xml)
        return parse_rss_metadata(partial_xml)

    result = run_paired_experiment(feed_rows(), fetcher=fetcher, sleeper=lambda _: None,
                                   memory_reader=lambda: 64 * 1024 * 1024)
    assert result["conditions"][str(CAP_2_MB)]["outcome_counts"]["successful_parse"] == 1
    assert result["conditions"][str(CAP_2_MB)]["outcome_counts"]["other_failure"] == 49


def test_stage_deadline_and_per_request_timeout_are_passed_and_enforced():
    clock = FakeClock()
    calls = []

    def fetcher(url, config, *, deadline_monotonic):
        calls.append((url, config.max_response_bytes, deadline_monotonic - clock()))
        if url.endswith("/0") and config.max_response_bytes == CAP_2_MB:
            clock.value += 250
            return FeedMetadata(description="late")
        raise CollectionError("RSS request or collection deadline exceeded.")

    result = run_paired_experiment(feed_rows(), fetcher=fetcher, clock=clock, sleeper=clock.sleep,
                                   memory_reader=lambda: 64 * 1024 * 1024)
    assert calls[0][2] <= 12.0
    assert result["conditions"][str(CAP_2_MB)]["outcome_counts"]["timeout"] == 1
    assert result["conditions"][str(CAP_2_MB)]["outcome_counts"]["not_attempted_deadline"] == 49
    assert result["rss_stage_budget_per_condition_seconds"] == 240
    assert result["rss_total_stage_budget_seconds"] == 480


def test_memory_budget_stops_further_fetches_and_reports_fixed_budget():
    calls = []

    def fetcher(*_args, **_kwargs):
        calls.append(1)
        return FeedMetadata(description="ok")

    result = run_paired_experiment(feed_rows(), fetcher=fetcher, sleeper=lambda _: None,
                                   memory_reader=lambda: PROCESS_RSS_BUDGET_BYTES + 1)
    assert len(calls) == 1
    assert result["memory_budget_exceeded"] is True
    assert result["process_rss_budget_mib"] == 512
    assert result["conditions"][str(CAP_2_MB)]["outcome_counts"]["memory_budget_exceeded"] > 0
    assert result["paired_outcomes"]["reconciled_pairs"] == 50


def test_missing_process_memory_measurement_fails_closed_before_fetches():
    calls = []
    result = run_paired_experiment(
        feed_rows(), fetcher=lambda *args, **kwargs: calls.append(args),
        sleeper=lambda _: None, memory_reader=lambda: None,
    )
    assert calls == []
    assert result["process_memory_measurement_available"] is False
    assert result["conditions"][str(CAP_2_MB)]["peak_sampled_process_rss_mib"] is None
    assert result["conditions"][str(CAP_2_MB)]["outcome_counts"]["memory_measurement_unavailable"] == 50
    assert result["paired_outcomes"]["reconciled_pairs"] == 50


def test_incomplete_lookup_deadline_still_reconciles_all_50_ids(tmp_path):
    database = tmp_path / "audit.sqlite"
    create_audit(database)
    clock = FakeClock()
    calls = []

    def lookup(identifier, _config):
        calls.append(identifier)
        clock.value += 20
        return [{"collectionId": int(identifier), "feedUrl": f"https://rss.invalid/{identifier}"}]

    result = run_experiment(database, "fixed-audit", lookup=lookup,
                            fetcher=lambda *_args, **_kwargs: FeedMetadata(), clock=clock,
                            sleeper=clock.sleep, memory_reader=lambda: 64 * 1024 * 1024)
    assert len(calls) < 50
    assert result["lookup_outcomes"]["not_attempted_deadline"] > 0
    assert result["conditions"][str(CAP_2_MB)]["outcome_counts"]["not_attempted_deadline"] > 0
    assert result["paired_outcomes"]["reconciled_pairs"] == 50
