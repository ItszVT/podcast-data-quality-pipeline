"""Paired RSS response-cap experiment; reads audits and reports aggregates only."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .podcast_collector import (
    CollectionError,
    CollectorConfig,
    FeedMetadata,
    _apple_collection_id,
    _safe_error_category,
    fetch_rss_metadata,
    lookup_itunes,
)

CAP_2_MB = 2_000_000
CAP_4_MB = 4_000_000
EXPERIMENT_CAPS = (CAP_2_MB, CAP_4_MB)
MAX_RECORDS = 50
REQUEST_TIMEOUT_SECONDS = 12.0
CONNECT_TIMEOUT_SECONDS = 4.0
REDIRECT_LIMIT = 3
RSS_PACING_SECONDS = 0.4
RSS_STAGE_BUDGET_SECONDS = 240.0
RSS_TOTAL_BUDGET_SECONDS = 480.0
LOOKUP_PACING_SECONDS = 3.2
LOOKUP_BUDGET_SECONDS = 780.0
LOOKUP_REQUESTS_MAX = 50
PROCESS_RSS_BUDGET_BYTES = 512 * 1024 * 1024
PROCESS_VIRTUAL_MEMORY_LIMIT_MIB = 1024
WORKFLOW_TIMEOUT_MINUTES = 30

OUTCOMES = (
    "successful_parse",
    "response_size_limit",
    "timeout",
    "other_failure",
    "lookup_unavailable",
    "missing_feed_url",
    "not_attempted_deadline",
    "memory_budget_exceeded",
    "memory_measurement_unavailable",
)
METADATA_FIELDS = {
    "website": "website_url",
    "description": "description",
    "categories": "categories",
    "language": "language",
    "episode_dates": "episode_publication_dates",
}


class ExperimentError(RuntimeError):
    """Safe, user-facing experiment validation error."""


def validate_audit_ids(feed_ids: Sequence[object]) -> tuple[str, ...]:
    """Require exactly 50 distinct, syntactically valid Apple collection IDs."""
    if len(feed_ids) != MAX_RECORDS:
        raise ExperimentError("Selected audit must contain exactly 50 podcast records.")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in feed_ids:
        text = str(value or "")
        match = re.fullmatch(r"itunes:collection:([1-9][0-9]*)", text)
        if match is None or _apple_collection_id(match.group(1)) is None:
            raise ExperimentError("Selected audit contains an invalid Apple podcast ID.")
        if text in seen:
            raise ExperimentError("Selected audit contains duplicate Apple podcast IDs.")
        seen.add(text)
        normalized.append(text)
    return tuple(normalized)


def balanced_cap_order(index: int) -> tuple[int, int]:
    """Alternate the first cap by stable record order, balancing 25/25 for 50."""
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("Record index must be a nonnegative integer.")
    return (EXPERIMENT_CAPS if index % 2 == 0 else tuple(reversed(EXPERIMENT_CAPS)))


def _read_audit(database_path: str | Path, audit_run_id: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not isinstance(audit_run_id, str) or not audit_run_id.strip() or len(audit_run_id) > 200:
        raise ExperimentError("An explicit valid audit_run_id is required.")
    database = Path(database_path)
    if not database.is_file():
        raise ExperimentError("Temporary audit database is unavailable.")
    uri = database.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            rows = connection.execute(
                "SELECT feed_id FROM podcast_records WHERE audit_run_id = ? ORDER BY source_row",
                (audit_run_id,),
            ).fetchall()
            meta_row = connection.execute(
                "SELECT run_status, records_processed, metadata_json FROM audit_runs WHERE audit_run_id = ?",
                (audit_run_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise ExperimentError("Selected audit could not be read from the temporary snapshot.") from exc
    if meta_row is None:
        raise ExperimentError("Requested audit_run_id was not found in the published snapshot.")
    if meta_row[0] not in {"succeeded", "partial"} or meta_row[1] != MAX_RECORDS:
        raise ExperimentError("Selected audit is not a completed 50-record run.")
    ids = validate_audit_ids([row[0] for row in rows])
    try:
        metadata = json.loads(meta_row[2])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExperimentError("Selected audit metadata is malformed.") from exc
    if not isinstance(metadata, dict):
        raise ExperimentError("Selected audit metadata is malformed.")
    report = metadata.get("collection_report")
    report = report if isinstance(report, dict) else {}
    provenance = report.get("record_provenance_by_feed")
    provenance = provenance if isinstance(provenance, dict) else {}
    records = []
    for feed_id in ids:
        details = provenance.get(feed_id)
        storefront = details.get("storefront") if isinstance(details, dict) else "us"
        if not isinstance(storefront, str) or re.fullmatch(r"[A-Za-z]{2}", storefront) is None:
            storefront = "us"
        records.append({"feed_id": feed_id, "storefront": storefront.lower()})
    return records, report


def _resolve_feed_urls(
    records: Sequence[Mapping[str, str]],
    *,
    lookup: Callable[[str, CollectorConfig | None], list[dict[str, Any]]],
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> tuple[list[dict[str, Any]], dict[str, int], float]:
    started = clock()
    resolved: list[dict[str, Any]] = []
    counts = {"found": 0, "lookup_unavailable": 0, "mismatched_result": 0, "missing_feed_url": 0, "not_attempted_deadline": 0}
    for index, record in enumerate(records):
        if index:
            remaining = LOOKUP_BUDGET_SECONDS - (clock() - started)
            if remaining <= LOOKUP_PACING_SECONDS:
                counts["not_attempted_deadline"] += len(records) - index
                resolved.extend({"feed_id": item["feed_id"], "url": None, "lookup_status": "not_attempted_deadline"} for item in records[index:])
                break
            sleeper(LOOKUP_PACING_SECONDS)
        elapsed = clock() - started
        if elapsed + REQUEST_TIMEOUT_SECONDS > LOOKUP_BUDGET_SECONDS:
            counts["not_attempted_deadline"] += len(records) - index
            resolved.extend({"feed_id": item["feed_id"], "url": None, "lookup_status": "not_attempted_deadline"} for item in records[index:])
            break
        feed_id = record["feed_id"]
        numeric_id = feed_id.rsplit(":", 1)[1]
        config = CollectorConfig(
            storefront=record["storefront"], max_response_bytes=CAP_2_MB,
            rss_request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            connect_timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            max_redirects=REDIRECT_LIMIT,
        )
        try:
            results = lookup(numeric_id, config)
        except Exception as exc:
            _safe_error_category(exc)  # classify privately; never include raw exception text
            counts["lookup_unavailable"] += 1
            resolved.append({"feed_id": feed_id, "url": None, "lookup_status": "lookup_unavailable"})
            continue
        if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
            counts["lookup_unavailable"] += 1
            resolved.append({"feed_id": feed_id, "url": None, "lookup_status": "lookup_unavailable"})
            continue
        matches = [item for item in results if _apple_collection_id(item.get("collectionId")) == numeric_id]
        if len(matches) != 1:
            key = "lookup_unavailable" if not matches else "mismatched_result"
            counts[key] += 1
            resolved.append({"feed_id": feed_id, "url": None, "lookup_status": key})
            continue
        url = matches[0].get("feedUrl")
        if not isinstance(url, str) or not url.strip():
            counts["missing_feed_url"] += 1
            resolved.append({"feed_id": feed_id, "url": None, "lookup_status": "missing_feed_url"})
            continue
        # This URL remains only in this in-memory mapping and is never serialized.
        counts["found"] += 1
        resolved.append({"feed_id": feed_id, "url": url, "lookup_status": "found"})
    duration = max(0.0, clock() - started)
    return resolved, counts, duration


def _outcome_for_error(error: BaseException) -> str:
    if isinstance(error, MemoryError):
        return "memory_budget_exceeded"
    category = _safe_error_category(error)
    if category == "response_size_limit":
        return "response_size_limit"
    if category == "timeout":
        return "timeout"
    return "other_failure"


def _metadata_is_nonempty(value: object) -> bool:
    if isinstance(value, (tuple, list, dict, set)):
        return len(value) > 0
    return bool(str(value or "").strip())


def run_paired_experiment(
    resolved: Sequence[Mapping[str, Any]],
    *,
    fetcher: Callable[..., FeedMetadata] = fetch_rss_metadata,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    memory_reader: Callable[[], int | None] | None = None,
    memory_budget_bytes: int = PROCESS_RSS_BUDGET_BYTES,
) -> dict[str, Any]:
    """Fetch each in-memory URL under both fixed caps in balanced paired order."""
    if len(resolved) != MAX_RECORDS:
        raise ExperimentError("RSS comparison requires the same 50 resolved podcast IDs.")
    ids = validate_audit_ids([record.get("feed_id") for record in resolved])
    if isinstance(memory_budget_bytes, bool) or memory_budget_bytes != PROCESS_RSS_BUDGET_BYTES:
        raise ExperimentError("Only the fixed process-memory budget is supported.")
    memory_reader = memory_reader or _read_process_rss_bytes
    arm = {
        cap: {
            "outcome_counts": {name: 0 for name in OUTCOMES},
            "metadata_coverage": {name: 0 for name in METADATA_FIELDS},
            "request_elapsed_seconds": 0.0,
            "attempt_count": 0,
            "first_request_at": None,
            "last_request_at": None,
            "peak_process_rss_bytes": None,
        }
        for cap in EXPERIMENT_CAPS
    }
    outcomes: dict[str, dict[int, str]] = {feed_id: {} for feed_id in ids}
    arm_deadline: dict[int, float | None] = {cap: None for cap in EXPERIMENT_CAPS}
    stopped: set[int] = set()
    rss_started = clock()
    total_deadline = rss_started + RSS_TOTAL_BUDGET_SECONDS
    memory_exceeded = False
    memory_measurement_available = memory_reader() is not None
    memory_unavailable = not memory_measurement_available

    for index, item in enumerate(resolved):
        feed_id = str(item["feed_id"])
        url = item.get("url")
        initial_status = item.get("lookup_status")
        if not isinstance(url, str) or not url.strip():
            outcome = initial_status if initial_status in {
                "lookup_unavailable", "missing_feed_url", "not_attempted_deadline",
            } else "missing_feed_url"
            for cap in EXPERIMENT_CAPS:
                outcomes[feed_id][cap] = outcome
                arm[cap]["outcome_counts"][outcome] += 1
            continue
        for cap in balanced_cap_order(index):
            details = arm[cap]
            if memory_exceeded:
                outcome = "memory_budget_exceeded"
            elif memory_unavailable:
                outcome = "memory_measurement_unavailable"
            elif cap in stopped or clock() >= total_deadline:
                outcome = "not_attempted_deadline"
                stopped.add(cap)
            else:
                now = clock()
                if arm_deadline[cap] is None:
                    arm_deadline[cap] = now + RSS_STAGE_BUDGET_SECONDS
                remaining = min(arm_deadline[cap] - now, total_deadline - now)
                if remaining <= 0:
                    outcome = "not_attempted_deadline"
                    stopped.add(cap)
                else:
                    pause = min(RSS_PACING_SECONDS, remaining)
                    sleeper(pause)
                    call_started = clock()
                    remaining = min(arm_deadline[cap] - call_started, total_deadline - call_started)
                    if remaining <= 0:
                        outcome = "not_attempted_deadline"
                        stopped.add(cap)
                    else:
                        config = CollectorConfig(
                            max_response_bytes=cap,
                            connect_timeout_seconds=CONNECT_TIMEOUT_SECONDS,
                            rss_request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
                            rss_collection_budget_seconds=RSS_STAGE_BUDGET_SECONDS,
                            max_redirects=REDIRECT_LIMIT,
                            rss_pacing_seconds=RSS_PACING_SECONDS,
                        )
                        deadline = call_started + min(REQUEST_TIMEOUT_SECONDS, remaining)
                        details["attempt_count"] += 1
                        details["first_request_at"] = details["first_request_at"] or call_started
                        metadata, observed_memory, fetch_error = _fetch_with_sampled_peak(
                            fetcher, url, config, deadline, cap, memory_reader,
                        )
                        if fetch_error is not None:
                            outcome = _outcome_for_error(fetch_error)
                        else:
                            outcome = "successful_parse"
                            for field_name, attr in METADATA_FIELDS.items():
                                if _metadata_is_nonempty(getattr(metadata, attr, None)):
                                    details["metadata_coverage"][field_name] += 1
                        finished = clock()
                        details["request_elapsed_seconds"] += max(0.0, finished - call_started)
                        details["last_request_at"] = finished
                        if finished > deadline and outcome == "successful_parse":
                            outcome = "timeout"
                            stopped.add(cap)
                        if observed_memory is not None:
                            prior_peak = details["peak_process_rss_bytes"] or 0
                            details["peak_process_rss_bytes"] = max(prior_peak, observed_memory)
                            if observed_memory > memory_budget_bytes:
                                memory_exceeded = True
                                outcome = "memory_budget_exceeded"
                        else:
                            memory_unavailable = True
                            memory_measurement_available = False
                            outcome = "memory_measurement_unavailable"
            outcomes[feed_id][cap] = outcome
            details["outcome_counts"][outcome] += 1

    for cap in EXPERIMENT_CAPS:
        details = arm[cap]
        first, last = details.pop("first_request_at"), details.pop("last_request_at")
        details["elapsed_seconds"] = round(max(0.0, (last or 0) - (first or last or 0)), 3)
        details["request_elapsed_seconds"] = round(details["request_elapsed_seconds"], 3)
        peak_rss = details.pop("peak_process_rss_bytes")
        details["peak_sampled_process_rss_mib"] = round(peak_rss / (1024 * 1024), 2) if peak_rss is not None else None
    transitions: dict[str, int] = {}
    for feed_id in ids:
        low = outcomes[feed_id][CAP_2_MB]
        high = outcomes[feed_id][CAP_4_MB]
        key = f"{low}_to_{high}"
        transitions[key] = transitions.get(key, 0) + 1
    pair_counts = {
        "2mb_size_limit_to_4mb_success": sum(
            outcomes[feed_id][CAP_2_MB] == "response_size_limit" and outcomes[feed_id][CAP_4_MB] == "successful_parse"
            for feed_id in ids
        ),
        "size_limit_under_both": sum(
            outcomes[feed_id][CAP_2_MB] == outcomes[feed_id][CAP_4_MB] == "response_size_limit"
            for feed_id in ids
        ),
        "successful_parse_under_both": sum(
            outcomes[feed_id][CAP_2_MB] == outcomes[feed_id][CAP_4_MB] == "successful_parse"
            for feed_id in ids
        ),
        "2mb_success_to_4mb_failure": sum(
            outcomes[feed_id][CAP_2_MB] == "successful_parse" and outcomes[feed_id][CAP_4_MB] != "successful_parse"
            for feed_id in ids
        ),
        "other_outcome_transitions": sum(
            not (
                outcomes[feed_id][CAP_2_MB] == "response_size_limit" and outcomes[feed_id][CAP_4_MB] == "successful_parse"
                or outcomes[feed_id][CAP_2_MB] == outcomes[feed_id][CAP_4_MB] == "response_size_limit"
                or outcomes[feed_id][CAP_2_MB] == outcomes[feed_id][CAP_4_MB] == "successful_parse"
                or outcomes[feed_id][CAP_2_MB] == "successful_parse" and outcomes[feed_id][CAP_4_MB] != "successful_parse"
            )
            for feed_id in ids
        ),
        "reconciled_pairs": len(transitions) and sum(transitions.values()),
    }
    first_order_counts = {
        "2mb_first": sum(balanced_cap_order(i)[0] == CAP_2_MB for i in range(len(ids))),
        "4mb_first": sum(balanced_cap_order(i)[0] == CAP_4_MB for i in range(len(ids))),
    }
    return {
        "selected_record_count": len(ids),
        "unique_id_count": len(set(ids)),
        "lookup_outcomes": {key: sum(item.get("lookup_status") == key for item in resolved) for key in ("found", "lookup_unavailable", "mismatched_result", "missing_feed_url", "not_attempted_deadline")},
        "first_condition_order_counts": first_order_counts,
        "conditions": {str(cap): arm[cap] for cap in EXPERIMENT_CAPS},
        "paired_outcomes": pair_counts,
        "outcome_transitions": dict(sorted(transitions.items())),
        "paired_outcomes_reconcile": pair_counts["reconciled_pairs"] == len(ids),
        "total_elapsed_seconds": round(max(0.0, clock() - rss_started), 3),
        "rss_stage_budget_per_condition_seconds": RSS_STAGE_BUDGET_SECONDS,
        "rss_total_stage_budget_seconds": RSS_TOTAL_BUDGET_SECONDS,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "process_rss_budget_mib": PROCESS_RSS_BUDGET_BYTES // (1024 * 1024),
        "process_virtual_memory_limit_mib": PROCESS_VIRTUAL_MEMORY_LIMIT_MIB,
        "workflow_timeout_minutes": WORKFLOW_TIMEOUT_MINUTES,
        "process_memory_measurement_available": memory_measurement_available,
        "memory_budget_exceeded": memory_exceeded,
    }


def _read_process_rss_bytes() -> int | None:
    """Return current resident process bytes on Linux, or None if unavailable."""
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * __import__("os").sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def _fetch_with_sampled_peak(
    fetcher: Callable[..., FeedMetadata], url: str, config: CollectorConfig,
    deadline: float, cap: int, memory_reader: Callable[[], int | None],
) -> tuple[FeedMetadata | None, int | None, Exception | None]:
    """Sample resident process memory while the bounded transport and parser run."""
    stop = threading.Event()
    peak = [memory_reader() or 0]

    def sample() -> None:
        while not stop.wait(0.025):
            current = memory_reader()
            if current is not None:
                peak[0] = max(peak[0], current)

    thread = threading.Thread(target=sample, name=f"rss-memory-{cap}", daemon=True)
    thread.start()
    result: FeedMetadata | None = None
    error: Exception | None = None
    try:
        result = fetcher(url, config, deadline_monotonic=deadline)
    except Exception as exc:
        error = exc
    finally:
        stop.set()
        thread.join(timeout=0.2)
        current = memory_reader()
        if current is not None:
            peak[0] = max(peak[0], current)
    return result, peak[0] or None, error


def run_experiment(
    database_path: str | Path,
    audit_run_id: str,
    *,
    lookup: Callable[[str, CollectorConfig | None], list[dict[str, Any]]] = lookup_itunes,
    fetcher: Callable[..., FeedMetadata] = fetch_rss_metadata,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    memory_reader: Callable[[], int | None] | None = None,
) -> dict[str, Any]:
    records, _report = _read_audit(database_path, audit_run_id)
    resolved, lookup_counts, lookup_elapsed = _resolve_feed_urls(
        records, lookup=lookup, clock=clock, sleeper=sleeper,
    )
    result = run_paired_experiment(
        resolved, fetcher=fetcher, clock=clock, sleeper=sleeper,
        memory_reader=memory_reader,
    )
    result["lookup_outcomes"] = lookup_counts
    result["lookup_requests_attempted"] = sum(
        lookup_counts[name] for name in ("found", "lookup_unavailable", "mismatched_result", "missing_feed_url")
    )
    result["lookup_elapsed_seconds"] = round(lookup_elapsed, 3)
    result["lookup_request_limit"] = LOOKUP_REQUESTS_MAX
    result["lookup_budget_seconds"] = LOOKUP_BUDGET_SECONDS
    result["experiment_caps_bytes"] = list(EXPERIMENT_CAPS)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare the two fixed RSS response caps without writing to the audit database.")
    parser.add_argument("--database", required=True)
    parser.add_argument("--audit-run-id", required=True)
    args = parser.parse_args(argv)
    try:
        summary = run_experiment(args.database, args.audit_run_id)
    except Exception:
        print("RSS cap experiment stopped; category=preflight_or_runtime_failure.")
        return 1
    # The summary contains only fixed labels and aggregate counts; no ID or URL values.
    print(json.dumps(summary, sort_keys=True))
    return 1 if summary["memory_budget_exceeded"] or not summary["paired_outcomes_reconcile"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
