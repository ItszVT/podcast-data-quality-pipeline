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
    assert dashboard_data.get_geo_intelligence(database, "run-two")["version"] == 1
    assert dashboard_data.get_geo_intelligence(database.read_bytes(), "run-two")["version"] == 1


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
