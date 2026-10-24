from __future__ import annotations

import json
import sqlite3

from src import geo_intelligence as geo
from src import dashboard_data
from src.daily_pipeline import run_daily_pipeline
from src.podcast_collector import CollectionResult, parse_rss_metadata


class _Country:
    def __init__(self, alpha_2, alpha_3, name, official_name=None):
        self.alpha_2 = alpha_2
        self.alpha_3 = alpha_3
        self.name = name
        self.official_name = official_name


class _CountryList:
    def __init__(self):
        self.values = [
            _Country("US", "USA", "United States", "United States of America"),
            _Country("CA", "CAN", "Canada"),
            _Country("AU", "AUS", "Australia"),
        ]

    def __iter__(self):
        return iter(self.values)

    def get(self, **values):
        return next((item for item in self.values if all(getattr(item, key, None) == value for key, value in values.items())), None)


class _Reference:
    countries = _CountryList()


def _record(**values):
    return {
        "feed_id": "show:1", "title": "Example Show", "website_url": "",
        "description": "", "categories": "", "language": "", "country": "",
        **values,
    }


def test_explicit_geographic_claims_are_separate_from_storefront_and_origin(monkeypatch):
    monkeypatch.setattr(geo, "_country_reference", lambda: _Reference())
    item = geo._observations_for_record(_record(
        country="CA", language="en", description="A show based in Canada for listeners in Australia."
    ), {}, {"show:1": {"description": "publisher RSS channel metadata"}})

    assert item["identity"]["podcast_country_of_origin"]["status"] == "unknown"
    assert item["identity"]["api_storefront"]["value"] == "US"
    assert item["identity"]["declared_publisher_country"]["value"] == "CA"
    assert [x["country_code"] for x in item["identity"]["publisher_location_claims"]] == ["CA"]
    assert [x["country_code"] for x in item["identity"]["intended_audience_claims"]] == ["AU"]
    assert item["identity"]["publisher_location_claims"][0]["source"] == "publisher RSS channel metadata"
    assert item["geographic_identity_conflict"]["potential_conflict"] is False


def test_storefront_and_language_alone_do_not_create_geographic_claims(monkeypatch):
    monkeypatch.setattr(geo, "_country_reference", lambda: _Reference())
    item = geo._observations_for_record(_record(language="en"), {}, {})
    assert item["identity"]["podcast_country_of_origin"]["value"] is None
    assert item["identity"]["declared_publisher_country"]["value"] is None
    assert item["identity"]["publisher_location_claims"] == []
    assert item["geographic_identity_conflict"]["potential_conflict"] is False


def test_only_repeated_high_scoring_episode_evidence_identifies_or_marks_multilingual():
    samples = ["x" * 160] * 4
    decisions = iter([("en", .97), ("en", .91), ("fr", .97), ("fr", .9)])
    observed = geo.classify_episode_languages(samples, detector=lambda _text: next(decisions))
    assert observed["status"] == "multilingual"
    assert observed["languages"] == ["en", "fr"]
    assert observed["raw_episode_text_saved"] is False

    weak = geo.classify_episode_languages(["short"], detector=lambda _text: ("en", .99))
    assert weak["status"] == "undetermined"

    unavailable = geo.classify_episode_languages(
        ["x" * 160], detector=lambda _text: (_ for _ in ()).throw(geo.LanguageDetectorUnavailable())
    )
    assert unavailable["status"] == "undetermined"
    assert unavailable["detector_available"] is False


def test_explicit_conflict_and_language_difference_are_review_signals(monkeypatch):
    monkeypatch.setattr(geo, "_country_reference", lambda: _Reference())
    item = geo._observations_for_record(
        _record(country="US", language="en", description="This show is based in Canada."),
        {"show:1": {"status": "identified", "languages": ["fr"], "evidence_source": "RSS episode titles/descriptions"}},
        {"show:1": {"description": "publisher RSS channel metadata"}},
    )
    assert item["geographic_identity_conflict"]["potential_conflict"] is True
    assert item["language_inconsistency"]["potential_inconsistency"] is True


def test_pipeline_persists_drift_without_treating_missing_data_as_a_change(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_country_reference", lambda: _Reference())
    database = tmp_path / "geo.sqlite"

    def save(identifier, language, country, episode_language):
        row = _record(feed_id="itunes:77", language=language, country=country)
        return run_daily_pipeline(
            database,
            collector=lambda *_args, **_kwargs: CollectionResult(
                (row,), {"api_search_errors": [], "rss_retrieval_failures": 0,
                         "episode_language_evidence": {"itunes:77": {
                             "status": "identified", "languages": [episode_language],
                             "evidence_source": "RSS episode titles/descriptions",
                         }}}
            ),
            audit_run_id=identifier,
        )

    save("run-one", "en", "US", "en")
    save("run-two", "fr", "", "fr")
    with sqlite3.connect(database) as connection:
        metadata = json.loads(connection.execute(
            "SELECT metadata_json FROM audit_runs WHERE audit_run_id='run-two'"
        ).fetchone()[0])
    geo_run = metadata["geo_intelligence"]
    by_field = {item["field"]: item for item in geo_run["drift"]}
    assert by_field["declared_language"]["change_classification"] == "changed"
    assert by_field["declared_language"]["previous_value"] == "en"
    assert by_field["declared_publisher_country"]["change_classification"] == "missing_or_unavailable"
    assert by_field["declared_publisher_country"]["previous_value"] == "US"
    assert geo_run["regional_website_reliability"]["status"] == "not_tested"
    assert geo_run["regional_website_reliability"]["observations"] == []
    assert dashboard_data.get_geo_intelligence(database, "run-two")["version"] == 2
    assert dashboard_data.get_geo_intelligence(database.read_bytes(), "run-two")["version"] == 2


def test_pipeline_does_not_persist_episode_text_samples(tmp_path):
    database = tmp_path / "episode-text.sqlite"
    private_episode_text = "PRIVATE EPISODE TEXT MUST NOT BE STORED " + ("word " * 40)
    run_daily_pipeline(
        database,
        collector=lambda *_args, **_kwargs: CollectionResult(
            (_record(feed_id="show:private", language="en"),),
            {"api_search_errors": [], "rss_retrieval_failures": 0,
             "episode_text_samples_by_feed": {"show:private": [private_episode_text]}},
        ),
        audit_run_id="episode-text-run",
    )
    with sqlite3.connect(database) as connection:
        metadata = connection.execute("SELECT metadata_json FROM audit_runs").fetchone()[0]
    assert private_episode_text not in database.read_bytes().decode("latin1")
    assert private_episode_text not in metadata
    assert json.loads(metadata)["collection_report"]["episode_language_evidence"]["show:private"]["raw_episode_text_saved"] is False


def test_rss_parser_keeps_bounded_episode_text_only_for_in_memory_analysis():
    body = b"""<rss><channel><title>Show</title><item><title>Episode one</title>
    <description>A detailed episode summary for language analysis.</description></item></channel></rss>"""
    metadata = parse_rss_metadata(body)
    assert len(metadata.episode_text_samples) == 1
    assert "Episode one" in metadata.episode_text_samples[0]
    assert len(metadata.episode_text_samples[0]) <= 1600


def test_structured_location_claims_validate_codes_keep_types_and_never_set_origin(monkeypatch):
    monkeypatch.setattr(geo, "_country_reference", lambda: _Reference())
    row = _record(feed_id="show:geo", country="", language="en")
    item = geo._observations_for_record(
        row, {}, {},
        [
            {"rel": "creator", "text": "Toronto", "country": "CA"},
            {"rel": "subject", "text": "New York", "country": "US"},
            {"rel": "creator", "text": "Mystery", "country": "ZZ"},
        ], "checked", "US", 0, "2026-10-24T00:00:00Z",
    )
    identity = item["identity"]
    assert [claim["location"] for claim in identity["structured_creator_locations"]] == ["Toronto", "Mystery"]
    assert identity["structured_creator_locations"][0]["country_name"] == "Canada"
    assert identity["structured_creator_locations"][1]["country_code_valid"] is False
    assert identity["structured_subject_locations"][0]["country_code"] == "US"
    assert identity["structured_creator_locations"][0]["observed_at"] == "2026-10-24T00:00:00Z"
    assert identity["podcast_country_of_origin"]["value"] is None
    assert identity["api_storefront"]["value"] == "US"
    assert item["geographic_identity_conflict"]["potential_conflict"] is False


def test_geo_evidence_availability_distinguishes_checked_missing_failed_and_legacy(monkeypatch):
    monkeypatch.setattr(geo, "_country_reference", lambda: _Reference())
    checked = geo._observations_for_record(_record(), {}, {}, (), "checked")
    missing = geo._observations_for_record(_record(), {}, {}, (), "unavailable")
    legacy = geo._observations_for_record(_record(), {}, {}, (), "legacy_unavailable")
    malformed = geo._observations_for_record(_record(), {}, {}, (), "checked", location_parse_errors=1)
    assert checked["identity"]["evidence_availability"]["structured_creator_location"] == "no_claim_found"
    assert missing["identity"]["evidence_availability"]["structured_creator_location"] == "source_unavailable"
    assert legacy["identity"]["evidence_availability"]["structured_creator_location"] == "legacy_unavailable"
    assert malformed["identity"]["evidence_availability"]["structured_creator_location"] == "extraction_failed"


def test_language_evidence_reports_usable_sample_count_and_sufficiency():
    result = geo.classify_episode_languages(["short", "a" * 150], detector=lambda _text: ("en", .96))
    assert result["usable_episode_samples"] == 1
    assert result["evidence_sufficient"] is False


def test_geo_coverage_summary_counts_only_saved_evidence():
    rows = [
        {"identity": {"evidence_availability": {"rss": "checked", "structured_creator_location": "claim_found", "structured_subject_location": "no_claim_found", "declared_language": "claim_found"}}, "geographic_identity_conflict": {"potential_conflict": False}},
        {"identity": {"evidence_availability": {"rss": "unavailable", "structured_creator_location": "source_unavailable", "structured_subject_location": "source_unavailable", "declared_language": "source_unavailable"}}, "geographic_identity_conflict": {"potential_conflict": True}},
    ]
    assert geo.summarize_geo_evidence(rows) == {
        "podcasts_analyzed": 2, "rss_checked": 1, "rss_unavailable": 1,
        "rss_legacy_unrecorded": 0,
        "creator_location_claims": 1, "creator_location_no_claim": 0,
        "creator_location_unavailable": 1, "creator_location_extraction_failed": 0,
        "subject_location_claims": 0, "subject_location_no_claim": 1,
        "subject_location_unavailable": 1, "subject_location_extraction_failed": 0,
        "declared_language_claims": 1, "declared_language_no_claim": 0,
        "declared_language_unavailable": 1, "potential_conflicts": 1,
    }


def test_legacy_geo_snapshot_remains_readable_and_reports_unrecorded_coverage(tmp_path):
    from src.database import persist_audit_run
    from src.validation import ValidatedRecord

    database = tmp_path / "legacy-geo.sqlite"
    record = ValidatedRecord(1, _record(), _record(), ())
    persist_audit_run(
        database, "legacy.csv", [record], audit_run_id="legacy",
        metadata={"geo_intelligence": {"version": 1, "records": []}},
    )
    loaded = dashboard_data.get_geo_intelligence(database, "legacy")
    assert loaded["version"] == 1
    summary = geo.summarize_geo_evidence(loaded["records"])
    assert summary["podcasts_analyzed"] == 0
    assert summary["rss_checked"] == 0


def test_structured_location_drift_uses_history_and_missing_is_not_a_change(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_country_reference", lambda: _Reference())
    database = tmp_path / "location-history.sqlite"
    record = _record(feed_id="itunes:geo-history")

    def save(run_id, locations):
        report = {
            "api_search_errors": [], "rss_retrieval_failures": 0,
            "rss_evidence_status_by_feed": {record["feed_id"]: "checked"},
            "structured_locations_by_feed": {record["feed_id"]: locations},
        }
        run_daily_pipeline(
            database,
            collector=lambda *_args, **_kwargs: CollectionResult((record,), report),
            audit_run_id=run_id,
        )

    creator = [{"rel": "creator", "text": "Toronto", "country": "CA"}]
    save("geo-one", creator)
    save("geo-two", [])
    save("geo-three", creator)
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT audit_run_id FROM audit_runs ORDER BY started_at, audit_run_id").fetchall()
        metadata = json.loads(connection.execute(
            "SELECT metadata_json FROM audit_runs WHERE audit_run_id='geo-three'"
        ).fetchone()[0])
    assert len(rows) == 3
    drift = {row["field"]: row for row in metadata["geo_intelligence"]["drift"]}
    assert drift["structured_creator_locations"]["change_classification"] == "unchanged"
    assert drift["structured_creator_locations"]["previous_value"] == [["CA", "toronto"]]
