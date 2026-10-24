from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

import pytest
import requests

import src.podcast_collector as collector
from src.podcast_collector import (
    CollectionError, CollectorConfig, DEFAULT_SEARCH_PLAN, FeedMetadata, SearchStratum,
    _safe_error_category, collect_podcasts,
    fetch_rss_metadata, parse_rss_metadata, write_collection,
)

RSS = b'''<?xml version="1.0"?><rss version="2.0"><channel>
<title>Example</title><link>https://publisher.example/show</link>
<description>Publisher description</description><language>en-us</language>
<category>Technology</category></channel></rss>'''

class Handler(BaseHTTPRequestHandler):
    body = RSS
    status = 200
    location = None
    def do_GET(self):
        self.server.requests_seen += 1
        if self.path == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1/private")
            self.end_headers()
            return
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("Content-Type", "application/rss+xml")
        self.end_headers()
        self.wfile.write(self.body)
    def log_message(self, *_args):
        pass

@pytest.fixture
def local_server():
    Handler.body, Handler.status = RSS, 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests_seen = 0
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    original = collector._validated_destination
    def allow_only_local_fixture(url, **kwargs):
        parts = urlsplit(url)
        if parts.hostname == "127.0.0.1" and parts.port == server.server_port and not url.endswith("/private"):
            return url, None, None, "127.0.0.1", "127.0.0.1", server.server_port
        return original(url, **kwargs)
    collector._validated_destination = allow_only_local_fixture
    try:
        yield server, base
    finally:
        collector._validated_destination = original
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_parse_rss_metadata_extracts_only_channel_fields():
    result = parse_rss_metadata(RSS)
    assert result == FeedMetadata(
        "https://publisher.example/show", "Publisher description", "Technology", "en-us"
    )


def test_parse_rss_metadata_finds_channel_in_default_namespace():
    body = b'''<rss xmlns="urn:example:rss" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
    <channel><title>Namespaced show</title><link>https://publisher.example/show</link>
    <description>Channel details</description><itunes:category text="Science"/>
    </channel></rss>'''
    result = parse_rss_metadata(body)
    assert result.website_url == "https://publisher.example/show"
    assert result.description == "Channel details"
    assert result.categories == "Science"


def test_fetch_rss_metadata_from_controlled_local_fixture(local_server):
    server, base = local_server
    result = fetch_rss_metadata(base + "/feed", CollectorConfig())
    assert result.website_url == "https://publisher.example/show"
    assert server.requests_seen == 1


def test_redirect_to_private_destination_is_rejected_before_second_request(local_server):
    server, base = local_server
    with pytest.raises(CollectionError, match="non-public"):
        fetch_rss_metadata(base + "/redirect-private")
    assert server.requests_seen == 1


def test_malformed_xml_is_reported():
    with pytest.raises(CollectionError, match="malformed"):
        parse_rss_metadata(b"<rss><channel>")


def test_oversized_rss_response_is_rejected(local_server):
    server, base = local_server
    Handler.body = b"x" * 100
    with pytest.raises(CollectionError, match="size limit"):
        fetch_rss_metadata(base + "/feed", CollectorConfig(max_response_bytes=20))
    assert server.requests_seen == 1


def test_public_url_guard_rejects_unsafe_destinations(monkeypatch):
    monkeypatch.setattr(collector, "_resolve_host", lambda *_: ["127.0.0.1"])
    with pytest.raises(CollectionError, match="non-public"):
        collector._validate_public_url("http://example.test/feed")
    with pytest.raises(CollectionError, match="Only absolute"):
        collector._validate_public_url("ftp://example.test/feed")


def test_collection_maps_api_metadata_deduplicates_and_enriches_from_rss():
    results_by_term = {
        "technology": [
            {"collectionId": 101, "collectionName": "Tech Show", "feedUrl": "https://feed.invalid/rss", "genres": ["Technology"]},
            {"collectionId": 101, "collectionName": "duplicate", "feedUrl": "https://feed.invalid/rss"},
        ],
        "science": [{"collectionId": 202, "collectionName": "Science Show", "genres": ["Science"]}],
    }
    fetched = []
    def api_search(term, _config):
        return results_by_term[term]
    def rss_fetch(url, _config):
        fetched.append(url)
        return FeedMetadata("https://publisher.example/tech", "Real RSS description", "Tech", "en")
    result = collect_podcasts(
        CollectorConfig(max_records=50, api_pacing_seconds=0, rss_pacing_seconds=0),
        api_search=api_search, rss_fetcher=rss_fetch, sleeper=lambda _: None,
        search_terms=("technology", "science"),
    )
    assert len(result.records) == 2
    assert result.records[0] == {
        "feed_id": "itunes:collection:101", "title": "Tech Show",
        "website_url": "https://publisher.example/tech", "description": "Real RSS description",
        "categories": "Tech", "language": "en", "country": "",
    }
    assert result.records[1]["website_url"] == ""
    assert result.report["unique_records_retained"] == 2
    assert result.report["rss_retrieval_successes"] == 1
    assert result.report["missing_rss_feed_urls"] == 1
    assert result.report["missing_field_counts"]["country"] == 2
    assert fetched == ["https://feed.invalid/rss"]


def test_lookup_itunes_uses_validated_id_and_requested_storefront(monkeypatch):
    seen = {}
    def fake_get(url, _config):
        seen["url"] = url
        return b'{"results":[{"collectionId":123,"collectionName":"Show"}]}', url
    monkeypatch.setattr(collector, "_safe_get_bytes", fake_get)
    result = collector.lookup_itunes("123", CollectorConfig(storefront="gb"))
    assert result[0]["collectionName"] == "Show"
    assert "lookup?" in seen["url"]
    assert "id=123" in seen["url"]
    assert "country=gb" in seen["url"]
    with pytest.raises(CollectionError, match="numeric collection ID"):
        collector.lookup_itunes("https://host.invalid", CollectorConfig())


def test_failed_rss_keeps_api_record_and_reports_failure():
    sensitive_url = "https://feed.invalid/rss?token=do-not-persist"
    result = collect_podcasts(
        CollectorConfig(api_pacing_seconds=0, rss_pacing_seconds=0),
        api_search=lambda *_: [{"collectionId": 12, "collectionName": "Retained", "feedUrl": sensitive_url}],
        rss_fetcher=lambda *_: (_ for _ in ()).throw(CollectionError(f"request failed for {sensitive_url}")),
        sleeper=lambda _: None, search_terms=("one",),
    )
    assert len(result.records) == 1
    assert result.records[0]["title"] == "Retained"
    assert result.records[0]["website_url"] == ""
    assert result.report["rss_retrieval_failures"] == 1
    assert result.report["rss_errors"] == ["itunes:collection:12: request_failure"]
    assert result.report["rss_evidence_status_by_feed"]["itunes:collection:12"] == "unavailable"
    assert sensitive_url not in repr(result.report)


def test_write_collection_creates_expected_csv_and_refuses_overwrite(tmp_path):
    result = collect_podcasts(
        CollectorConfig(api_pacing_seconds=0, rss_pacing_seconds=0),
        api_search=lambda *_: [{"collectionId": 7, "collectionName": "Show"}],
        sleeper=lambda _: None, search_terms=("one",),
    )
    output, report = tmp_path / "real.csv", tmp_path / "report.md"
    write_collection(result, output, report)
    assert output.read_text(encoding="utf-8-sig").splitlines()[0] == "feed_id,title,website_url,description,categories,language,country"
    assert "Unique records retained: 1" in report.read_text(encoding="utf-8")
    with pytest.raises(CollectionError, match="Refusing to overwrite"):
        write_collection(result, output, report)


def test_rss_parser_extracts_namespaced_creator_and_subject_locations_safely():
    body = b'''<rss xmlns:podcast="https://podcastindex.org/namespace/1.0"><channel>
    <podcast:location rel="creator" country="CA">Toronto</podcast:location>
    <podcast:location rel="subject" country="US">New York</podcast:location>
    <podcast:location rel="creator">Montreal</podcast:location>
    <podcast:location rel="unknown">Ignored</podcast:location>
    <podcast:location rel="subject"></podcast:location>
    </channel></rss>'''
    metadata = parse_rss_metadata(body)
    assert metadata.locations == (
        {"rel": "creator", "text": "Toronto", "country": "CA"},
        {"rel": "subject", "text": "New York", "country": "US"},
        {"rel": "creator", "text": "Montreal", "country": ""},
    )
    assert metadata.location_parse_errors == 2


def test_wrong_namespace_location_is_not_misclassified():
    metadata = parse_rss_metadata(b'''<rss xmlns:other="urn:not-podcast"><channel>
    <other:location rel="creator" country="CA">Toronto</other:location>
    </channel></rss>''')
    assert metadata.locations == ()
    assert metadata.location_parse_errors == 0


def test_collector_records_rss_coverage_and_apple_storefront_without_origin():
    metadata = FeedMetadata(language="en", locations=({"rel": "creator", "text": "Toronto", "country": "CA"},))
    result = collect_podcasts(
        CollectorConfig(api_pacing_seconds=0, rss_pacing_seconds=0),
        api_search=lambda *_: [{"collectionId": 9, "collectionName": "Show", "country": "USA", "feedUrl": "https://feed.invalid/rss"}],
        rss_fetcher=lambda *_: metadata, sleeper=lambda _: None, search_terms=("one",),
    )
    feed_id = "itunes:collection:9"
    assert result.report["rss_evidence_status_by_feed"][feed_id] == "checked"
    assert result.report["structured_locations_by_feed"][feed_id][0]["rel"] == "creator"
    assert result.report["apple_storefront_by_feed"][feed_id] == "US"
    assert result.records[0]["country"] == ""


def test_repeat_and_explore_sampling_is_deterministic_interleaved_and_deduplicated():
    by_term = {
        "alpha": [{"collectionId": i, "collectionName": f"A{i}"} for i in range(1, 5)],
        "beta": [{"collectionId": i, "collectionName": f"B{i}"} for i in range(5, 9)],
    }
    kwargs = dict(
        config=CollectorConfig(max_records=8, repeat_target=4, exploration_target=4,
                               api_pacing_seconds=0, rss_pacing_seconds=0),
        api_search=lambda term, _config: by_term[term],
        api_lookup=lambda collection_id, _config: [
            {"collectionId": int(collection_id), "collectionName": f"Prior{collection_id}"}
        ],
        sleeper=lambda _: None, search_terms=("alpha", "beta"),
        previously_audited_feed_ids={"itunes:collection:1", "itunes:collection:2",
                                     "itunes:collection:5", "itunes:collection:6"},
    )
    first = collect_podcasts(**kwargs)
    second = collect_podcasts(**kwargs)
    assert [row["feed_id"] for row in first.records] == [row["feed_id"] for row in second.records]
    assert len({row["feed_id"] for row in first.records}) == 8
    assert first.report["sampling"]["actual_repeat_count"] == 4
    assert first.report["sampling"]["actual_new_count"] == 4
    assert [row["feed_id"] for row in first.records[:4]] == [
        "itunes:collection:1", "itunes:collection:2", "itunes:collection:5", "itunes:collection:6",
    ]
    assert all(first.report["record_provenance_by_feed"][row["feed_id"]]["source"] == "Apple iTunes Lookup API"
               for row in first.records[:4])


def test_default_plan_looks_up_saved_ids_and_searches_only_for_exploration():
    calls = []
    def api_search(query, config):
        calls.append((query, config.storefront))
        index = len(calls)
        return [{"collectionId": 100 + index, "collectionName": f"Explore{index}"}]
    lookups = []
    def api_lookup(collection_id, config):
        lookups.append((collection_id, config.storefront))
        return [{"collectionId": int(collection_id), "collectionName": f"Prior{collection_id}"}]
    result = collect_podcasts(
        CollectorConfig(max_records=9, repeat_target=5, exploration_target=4,
                        api_pacing_seconds=0, rss_pacing_seconds=0),
        api_search=api_search, api_lookup=api_lookup, sleeper=lambda _: None,
        previously_audited_feed_ids={f"itunes:collection:{i}" for i in range(1, 11)},
    )
    assert len(calls) == 15
    assert {country for _, country in calls} >= {"gb", "ca", "au", "in", "mx", "br", "fr", "jp"}
    assert lookups == [(str(i), "us") for i in range(1, 6)]
    assert result.report["sampling"]["actual_repeat_count"] == 5
    assert result.report["sampling"]["actual_new_count"] == 4
    assert all(result.report["record_provenance_by_feed"][row["feed_id"]]["sampling_purpose"]
               in {"repeat_lookup", "exploration"} for row in result.records)
    assert all(not isinstance(value, SearchStratum) or value.storefront
               for value in DEFAULT_SEARCH_PLAN)


def test_sparse_sampling_reports_repeat_and_exploration_shortfalls():
    results = [{"collectionId": 1, "collectionName": "Prior"},
               {"collectionId": 2, "collectionName": "New"}]
    result = collect_podcasts(
        CollectorConfig(max_records=6, repeat_target=4, exploration_target=2,
                        api_pacing_seconds=0, rss_pacing_seconds=0),
        api_search=lambda *_: results, sleeper=lambda _: None,
        api_lookup=lambda *_: [{"collectionId": 1, "collectionName": "Prior"}],
        search_terms=("one", "two"), previously_audited_feed_ids={"itunes:collection:1"},
    )
    sampling = result.report["sampling"]
    assert len(result.records) == 2
    assert (sampling["actual_repeat_count"], sampling["actual_new_count"]) == (1, 1)
    assert (sampling["repeat_shortfall"], sampling["exploration_shortfall"]) == (3, 1)
    assert sampling["record_cap_shortfall"] == 4


def test_unavailable_repeat_lookup_is_not_replaced_by_a_search_result():
    result = collect_podcasts(
        CollectorConfig(max_records=2, repeat_target=1, exploration_target=1,
                        api_pacing_seconds=0, rss_pacing_seconds=0),
        api_lookup=lambda *_: [],
        api_search=lambda *_: [
            {"collectionId": 1, "collectionName": "Prior from search"},
            {"collectionId": 2, "collectionName": "New"},
        ],
        sleeper=lambda _: None, search_terms=("explore",),
        previously_audited_feed_ids={"itunes:collection:1"},
    )
    assert [record["feed_id"] for record in result.records] == ["itunes:collection:2"]
    assert result.report["sampling"]["actual_repeat_count"] == 0
    assert result.report["sampling"]["repeat_shortfall"] == 1
    assert result.report["sampling"]["repeat_lookup_outcomes"]["deleted_or_unavailable"] == 1


def test_mismatched_lookup_results_are_rejected_and_reported():
    result = collect_podcasts(
        CollectorConfig(max_records=1, repeat_target=1, exploration_target=0,
                        api_pacing_seconds=0, rss_pacing_seconds=0),
        api_lookup=lambda *_: [{"collectionId": 999, "collectionName": "Wrong show"}],
        sleeper=lambda _: None, previously_audited_feed_ids={"itunes:collection:123"},
    )
    assert result.records == ()
    assert result.report["sampling"]["repeat_lookup_outcomes"]["mismatched_result"] == 1
    assert result.report["sampling"]["repeat_shortfall"] == 1


def test_lookup_missing_required_metadata_and_unsupported_saved_ids_are_explicit():
    requested = []
    result = collect_podcasts(
        CollectorConfig(max_records=2, repeat_target=2, exploration_target=0,
                        api_pacing_seconds=0, rss_pacing_seconds=0),
        api_lookup=lambda collection_id, _config: (
            requested.append(collection_id) or [{"collectionId": int(collection_id)}]
        ),
        sleeper=lambda _: None,
        previously_audited_feed_ids={"itunes:collection:123", "legacy:unsafe-id"},
    )
    assert requested == ["123"]
    assert result.records == ()
    sampling = result.report["sampling"]
    assert sampling["repeat_lookup_outcomes"]["missing_required_metadata"] == 1
    assert sampling["previously_audited_ids_unsupported"] == 1
    assert sampling["repeat_shortfall"] == 2


def test_lookup_failure_diagnostics_do_not_retain_request_details():
    result = collect_podcasts(
        CollectorConfig(max_records=1, repeat_target=1, exploration_target=0,
                        api_pacing_seconds=0, rss_pacing_seconds=0),
        api_lookup=lambda *_: (_ for _ in ()).throw(CollectionError(
            "Connection failed for https://user:secret@api.example/lookup?token=hidden"
        )),
        sleeper=lambda _: None,
        previously_audited_feed_ids={"itunes:collection:321"},
    )
    assert result.report["api_lookup_errors"] == ["repeat_lookup_1: connection_failure"]
    assert "api.example" not in repr(result.report)
    assert "secret" not in repr(result.report)
    assert "token=hidden" not in repr(result.report)


@pytest.mark.parametrize("failed_lookup_count", [1, 2, 4])
def test_lookup_request_failures_reconcile_and_remain_partial_shortfalls(failed_lookup_count):
    failed_ids = {str(value) for value in range(1, failed_lookup_count + 1)}

    def lookup(collection_id, _config):
        if collection_id in failed_ids:
            raise CollectionError("connection failed for https://private.example/lookup")
        return [{"collectionId": int(collection_id), "collectionName": f"Prior {collection_id}"}]

    result = collect_podcasts(
        CollectorConfig(max_records=6, repeat_target=4, exploration_target=2,
                        api_pacing_seconds=0, rss_pacing_seconds=0),
        api_lookup=lookup,
        api_search=lambda *_: [
            {"collectionId": 10, "collectionName": "New 10"},
            {"collectionId": 11, "collectionName": "New 11"},
        ],
        sleeper=lambda _: None, search_terms=("explore",),
        previously_audited_feed_ids={f"itunes:collection:{value}" for value in range(1, 5)},
    )

    sampling = result.report["sampling"]
    outcomes = sampling["repeat_lookup_outcomes"]
    assert len(result.report["api_lookup_errors"]) == failed_lookup_count
    assert outcomes["request_failure"] == failed_lookup_count
    assert outcomes["found"] == 4 - failed_lookup_count
    assert sampling["repeat_lookup_outcome_total"] == 4
    assert sampling["repeat_lookup_outcomes_reconcile"] is True
    assert (sampling["actual_repeat_count"], sampling["actual_new_count"]) == (4 - failed_lookup_count, 2)
    assert sampling["repeat_shortfall"] == failed_lookup_count
    assert len({row["feed_id"] for row in result.records}) == len(result.records)
    assert "private.example" not in repr(result.report)


def test_no_repeat_lookup_matches_are_explicitly_a_suspicious_shortfall():
    result = collect_podcasts(
        CollectorConfig(max_records=2, repeat_target=1, exploration_target=1,
                        api_pacing_seconds=0, rss_pacing_seconds=0),
        api_lookup=lambda *_: [],
        api_search=lambda *_: [{"collectionId": 20, "collectionName": "New"}],
        sleeper=lambda _: None, search_terms=("explore",),
        previously_audited_feed_ids={"itunes:collection:1"},
    )
    assert result.report["sampling"]["repeat_lookup_no_matches"] is True
    assert result.report["sampling"]["repeat_lookup_outcomes"]["deleted_or_unavailable"] == 1


def test_provenance_records_storefront_as_context_without_rank_or_origin():
    result = collect_podcasts(
        CollectorConfig(max_records=1, repeat_target=0, exploration_target=1,
                        api_pacing_seconds=0, rss_pacing_seconds=0),
        api_search=lambda *_: [{"collectionId": 11, "collectionName": "Sample", "country": "USA"}],
        sleeper=lambda _: None, search_terms=("science",),
    )
    provenance = result.report["record_provenance_by_feed"]["itunes:collection:11"]
    assert provenance["source"] == "Apple iTunes Search API"
    assert provenance["query_stratum"] == "science"
    assert provenance["storefront"] == "US"
    assert provenance["observed_at"].endswith("+00:00")
    assert "source_rank" not in provenance
    assert result.records[0]["country"] == ""


def test_error_category_does_not_retain_hostnames_urls_or_credentials():
    error = CollectionError("RSS TLS failed for https://secret-user:secret-token@host.invalid/feed")
    assert _safe_error_category(error) == "tls_connection"
    assert "host.invalid" not in _safe_error_category(error)
    assert "secret-token" not in _safe_error_category(error)
    assert _safe_error_category(CollectionError("RSS destination DNS resolution failed.")) == "dns_failure"


def test_rss_parser_extracts_bounded_valid_episode_dates():
    body = b'''<rss><channel>
      <item><pubDate>Mon, 02 Jan 2023 12:00:00 GMT</pubDate></item>
      <item><published>not a date</published><updated>2024-02-03T00:00:00Z</updated></item>
      <item><pubDate>invalid</pubDate></item>
      <item><pubDate>2025-06-07</pubDate></item>
    </channel></rss>'''
    metadata = parse_rss_metadata(body, max_episode_dates=2)
    assert metadata.episode_publication_dates == ("2023-01-02", "2024-02-03")


def test_rss_collection_deadline_preserves_completed_observations_and_marks_timeout():
    ticks = [0.0]
    records = [{"collectionId": i, "collectionName": f"Show{i}", "feedUrl": f"https://feed{i}.invalid/rss"} for i in range(1, 4)]
    def rss_fetch(_url, _config):
        ticks[0] += 2.0
        return FeedMetadata(episode_publication_dates=("2024-01-01",))
    result = collect_podcasts(
        CollectorConfig(max_records=3, repeat_target=0, exploration_target=3,
                        rss_collection_budget_seconds=2, rss_pacing_seconds=0,
                        api_pacing_seconds=0),
        api_search=lambda *_: records, rss_fetcher=rss_fetch,
        sleeper=lambda _: None, search_terms=("one",), clock=lambda: ticks[0],
    )
    assert result.report["rss_retrieval_successes"] == 1
    assert result.report["rss_collection_timed_out"] is True
    assert result.report["rss_timeout_unattempted_feeds"] == 2
    assert len(result.report["episode_publication_dates_by_feed"]) == 1
    assert result.report["rss_evidence_status_by_feed"]["itunes:collection:3"] == "unavailable"
    assert result.report["rss_attempted_count"] == 1
    assert result.report["rss_attempt_outcome_total"] == 1
    assert result.report["rss_attempt_counts_reconcile"] is True
    assert result.report["rss_failure_feeds_unattempted_due_to_deadline"] == 2
    assert result.report["rss_retrieval_successes"] + result.report["rss_retrieval_failures"] == 3


def test_rss_failure_categories_are_exclusive_and_reconcile_attempts():
    errors = (
        CollectionError("RSS destination DNS resolution failed."),
        CollectionError("RSS connection failed safely."),
        CollectionError("RSS TLS certificate verification failed."),
        CollectionError("RSS server returned an unsuccessful status."),
        CollectionError("RSS request or collection deadline exceeded."),
        CollectionError("RSS response exceeded the configured size limit."),
        CollectionError("RSS XML is malformed."),
        CollectionError("RSS destination failed URL safety validation."),
    )
    records = [
        {"collectionId": index + 1, "collectionName": f"Show{index + 1}",
         "feedUrl": f"https://feed{index + 1}.invalid/rss"}
        for index in range(len(errors))
    ]
    records.append({"collectionId": 9, "collectionName": "No feed URL"})
    def rss_fetch(url, _config):
        index = int(url.split("feed")[1].split(".")[0]) - 1
        error = errors[index]
        raise CollectionError(f"{error} https://user:secret@private.invalid/feed?token=hidden")
    result = collect_podcasts(
        CollectorConfig(max_records=9, repeat_target=0, exploration_target=9,
                        api_pacing_seconds=0, rss_pacing_seconds=0),
        api_search=lambda *_: records, rss_fetcher=rss_fetch,
        sleeper=lambda _: None, search_terms=("one",),
    )
    report = result.report
    assert report["rss_attempted_count"] == 8
    assert report["rss_attempt_outcome_total"] == 8
    assert report["rss_attempt_counts_reconcile"] is True
    assert report["rss_outcome_counts"] == {
        "dns_failure": 1,
        "connection_tls_failure": 2,
        "http_error": 1,
        "timeout": 1,
        "response_size_limit": 1,
        "xml_parsing_failure": 1,
        "successful_parsing": 0,
        "other_or_unknown_failure": 1,
        "missing_feed_url": 1,
    }
    assert report["rss_retrieval_failures"] == 8
    assert len(report["rss_errors"]) == report["rss_retrieval_failures"]
    assert report["missing_rss_feed_urls"] == 1
    assert "secret" not in repr(report)
    assert "private.invalid" not in repr(report)
    assert "token=hidden" not in repr(report)


def test_rss_success_is_counted_as_successful_parse():
    records = [
        {"collectionId": 1, "collectionName": "Parsed", "feedUrl": "https://feed.invalid/rss"}
    ]
    result = collect_podcasts(
        CollectorConfig(max_records=1, repeat_target=0, exploration_target=1,
                        api_pacing_seconds=0, rss_pacing_seconds=0),
        api_search=lambda *_: records, rss_fetcher=lambda *_: FeedMetadata(),
        sleeper=lambda _: None, search_terms=("one",),
    )
    assert result.report["rss_outcome_counts"]["successful_parsing"] == 1
    assert result.report["rss_attempt_outcome_total"] == 1
    assert result.report["rss_retrieval_successes"] == 1
