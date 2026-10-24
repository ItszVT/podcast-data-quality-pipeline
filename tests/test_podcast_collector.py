from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

import pytest
import requests

import src.podcast_collector as collector
from src.podcast_collector import (
    CollectionError, CollectorConfig, FeedMetadata, collect_podcasts,
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
    original = collector._validate_public_url
    def allow_only_local_fixture(url):
        if urlsplit(url).hostname == "127.0.0.1" and urlsplit(url).port == server.server_port and not url.endswith("/private"):
            return url, "127.0.0.1", server.server_port
        return original(url)
    collector._validate_public_url = allow_only_local_fixture
    try:
        yield server, base
    finally:
        collector._validate_public_url = original
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_parse_rss_metadata_extracts_only_channel_fields():
    result = parse_rss_metadata(RSS)
    assert result == FeedMetadata(
        "https://publisher.example/show", "Publisher description", "Technology", "en-us"
    )


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
    assert result.report["rss_errors"] == ["itunes:collection:12: CollectionError"]
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
    assert result.report["apple_storefront_by_feed"][feed_id] == "USA"
    assert result.records[0]["country"] == ""
