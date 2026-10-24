"""Evidence-led geographic and language observations for saved audit runs.

Geographic claims are kept separate by meaning. The Apple storefront is never
used as a publisher or podcast country, and missing evidence stays unknown.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

REGIONAL_OBSERVATION_FIELDS = (
    "requested_country", "verified_probe_exit_country", "website_url",
    "observed_at", "http_status", "redirects", "soft_404_classification",
    "network_failure", "evidence", "uncertainty",
)
_MIN_LANGUAGE_TEXT = 120
_MIN_MODEL_SCORE = 0.85
_MAX_SAMPLES = 8


class LanguageDetectorUnavailable(RuntimeError):
    """The configured offline language model is not installed."""


def _country_reference():
    try:
        import pycountry
        return pycountry
    except ImportError:
        return None


def _country_from_value(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    reference = _country_reference()
    if reference is None:
        return None
    cleaned = value.strip()
    country = reference.countries.get(alpha_2=cleaned.upper()) if len(cleaned) == 2 else None
    if country is None and len(cleaned) == 3:
        country = reference.countries.get(alpha_3=cleaned.upper())
    if country is None:
        folded = cleaned.casefold()
        country = next((
            item for item in reference.countries
            if item.name.casefold() == folded
            or (getattr(item, "official_name", "") or "").casefold() == folded
        ), None)
    return country.alpha_2 if country is not None else None


def _explicit_country_references(text: str, *, source: str) -> list[dict[str, str]]:
    """Extract only country names preceded by explicit location/audience wording."""
    reference = _country_reference()
    if reference is None or not text:
        return []
    countries = sorted(reference.countries, key=lambda item: len(item.name), reverse=True)
    patterns = (
        ("publisher_location_claim", r"\b(?:based|located|headquartered)\s+in\s+"),
        ("intended_audience_claim", r"\b(?:for\s+)?(?:listeners|audiences|readers)\s+in\s+"),
    )
    found: dict[tuple[str, str], dict[str, str]] = {}
    for country in countries:
        names = {country.name}
        official_name = getattr(country, "official_name", None)
        if official_name:
            names.add(official_name)
        for name in sorted(names, key=len, reverse=True):
            for kind, prefix in patterns:
                match = re.search(prefix + re.escape(name) + r"\b", text, flags=re.IGNORECASE)
                if match:
                    found[(kind, country.alpha_2)] = {
                        "type": kind, "country_code": country.alpha_2,
                        "country_name": country.name,
                        "source": source,
                        "confidence": "explicit text claim; unverified",
                        "evidence": "Explicit location or intended-audience phrase detected.",
                    }
    return list(found.values())


def _detect_language(text: str) -> tuple[str | None, float]:
    try:
        from langdetect import DetectorFactory, detect_langs
    except ImportError as exc:
        raise LanguageDetectorUnavailable from exc
    try:
        DetectorFactory.seed = 0
        results = detect_langs(text)
    except Exception:  # Model/parser failures remain undetermined without raw details.
        return None, 0.0
    if not results:
        return None, 0.0
    return results[0].lang, float(results[0].prob)


def classify_episode_languages(samples: Iterable[str], *, detector=None) -> dict[str, Any]:
    """Summarize RSS episode text without persisting the text itself."""
    detect = detector or _detect_language
    candidates: list[tuple[str, float]] = []
    supplied = 0
    detector_available = True
    for text in list(samples)[:_MAX_SAMPLES]:
        if not isinstance(text, str) or len(text.strip()) < _MIN_LANGUAGE_TEXT:
            continue
        supplied += 1
        try:
            language, score = detect(text[:4000])
        except LanguageDetectorUnavailable:
            detector_available = False
            break
        if language and score >= _MIN_MODEL_SCORE:
            candidates.append((language.lower().split("-")[0], score))
    distribution = Counter(code for code, _ in candidates)
    supported = sorted(code for code, count in distribution.items() if count >= 2)
    if len(supported) >= 2:
        result, languages = "multilingual", supported
    elif len(supported) == 1:
        result, languages = "identified", supported
    else:
        result, languages = "undetermined", []
    return {
        "status": result,
        "languages": languages,
        "episode_samples_seen": supplied,
        "strong_model_matches": len(candidates),
        "detector_available": detector_available,
        "confidence_explanation": (
            "The offline language detector is unavailable in this environment."
            if not detector_available else
            "At least two episode samples agreed per reported language at model score >= 0.85; this is a review signal, not a calibrated probability."
            if result != "undetermined" else
            "Insufficient repeated high-scoring episode evidence to identify a language."
        ),
        "evidence_source": "RSS episode titles/descriptions",
        "method": "langdetect 1.0.9; samples under 120 characters or below 0.85 score excluded",
        "raw_episode_text_saved": False,
    }


def _record_value(record: Any, field: str) -> Any:
    if isinstance(record, Mapping):
        if isinstance(record.get("original"), Mapping):
            return record["original"].get(field)
        if isinstance(record.get("original_record"), Mapping):
            return record["original_record"].get(field)
        return record.get(field)
    return None


def _observations_for_record(
    record: Any,
    language_evidence: Mapping[str, Any],
    field_sources: Mapping[str, Any],
) -> dict[str, Any]:
    feed_id = str(_record_value(record, "feed_id") or "")
    declared_language = str(_record_value(record, "language") or "").strip().lower().replace("_", "-")
    declared_country_value = _record_value(record, "country")
    declared_country = _country_from_value(declared_country_value)
    record_sources = field_sources.get(feed_id, {}) if isinstance(field_sources, Mapping) else {}
    record_sources = record_sources if isinstance(record_sources, Mapping) else {}
    explicit = []
    for field in ("title", "description"):
        text = str(_record_value(record, field) or "")
        if text:
            source = record_sources.get(field, "historical record; source provenance unavailable")
            explicit.extend(_explicit_country_references(text, source=str(source)))
    evidence = language_evidence.get(feed_id, {}) if isinstance(language_evidence, Mapping) else {}
    evidence = evidence if isinstance(evidence, Mapping) else {}
    episode_languages = evidence.get("languages", [])
    if not isinstance(episode_languages, list):
        episode_languages = []
    declared_base = declared_language.split("-")[0] if declared_language else None
    episode_status = evidence.get("status", "undetermined")
    mismatch = bool(
        declared_base and episode_status == "identified" and episode_languages
        and declared_base not in episode_languages
    )
    location_claims = [item for item in explicit if item["type"] == "publisher_location_claim"]
    country_conflict = bool(
        declared_country and location_claims
        and any(item["country_code"] != declared_country for item in location_claims)
    )
    return {
        "feed_id": feed_id,
        "title": str(_record_value(record, "title") or "Untitled podcast"),
        "identity": {
            "podcast_country_of_origin": {"value": None, "status": "unknown", "reason": "No source establishes podcast origin."},
            "api_storefront": {"value": "US", "source": "Apple Search API country parameter", "confidence": "explicit request parameter", "meaning": "Catalog queried; not origin, publisher location, or intended audience."},
            "declared_publisher_country": {"value": declared_country, "source": "record country field" if declared_country else None, "declared_value_present": bool(declared_country_value), "confidence": "unverified declaration" if declared_country else "unknown"},
            "publisher_location_claims": location_claims,
            "intended_audience_claims": [item for item in explicit if item["type"] == "intended_audience_claim"],
            "language": {"declared": declared_language or None, "episode_observation": dict(evidence) if evidence else {"status": "undetermined", "languages": [], "evidence_source": "RSS episode titles/descriptions", "reason": "No qualifying episode text was available."}},
        },
        "geographic_identity_conflict": {
            "status": "review" if country_conflict else "none_supported",
            "potential_conflict": country_conflict,
            "evidence": "Structured country declaration differs from explicit publisher-location wording." if country_conflict else None,
        },
        "language_inconsistency": {
            "status": "review" if mismatch else "none_supported",
            "potential_inconsistency": mismatch,
            "evidence": "Declared language differs from repeated, high-scoring episode-text observations." if mismatch else None,
        },
        "drift_values": {
            "declared_publisher_country": declared_country,
            "declared_language": declared_base,
            "publisher_location_claims": sorted({item["country_code"] for item in location_claims}),
            "intended_audience_claims": sorted({item["country_code"] for item in explicit if item["type"] == "intended_audience_claim"}),
            "episode_language": sorted(set(episode_languages)) if episode_status in {"identified", "multilingual"} else None,
        },
    }


def _historical_observations(database_path: str | Path) -> tuple[dict[str, list[tuple[str, str, Any]]], list[str]]:
    history: dict[str, list[tuple[str, str, Any]]] = {}
    try:
        path = Path(database_path)
        if not path.is_file():
            return history, []
        uri = path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            runs = connection.execute("SELECT audit_run_id, started_at, metadata_json FROM audit_runs ORDER BY started_at, audit_run_id").fetchall()
            run_times = {run[0]: run[1] for run in runs}
            for run_id, started_at, metadata_json in runs:
                try:
                    metadata = json.loads(metadata_json or "{}")
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
                saved_geo = metadata.get("geo_intelligence", {}) if isinstance(metadata, dict) else {}
                items = saved_geo.get("records", []) if isinstance(saved_geo, dict) else []
                by_feed = {item.get("feed_id"): item for item in items if isinstance(item, dict) and item.get("feed_id")}
                rows = connection.execute("SELECT feed_id, record_json FROM podcast_records WHERE audit_run_id = ?", (run_id,)).fetchall()
                for feed_id, record_json in rows:
                    if not feed_id:
                        continue
                    if feed_id in by_feed:
                        values = by_feed[feed_id].get("drift_values", {})
                    else:
                        try:
                            record = json.loads(record_json or "{}")
                        except (TypeError, json.JSONDecodeError):
                            record = {}
                        values = {
                            "declared_publisher_country": _country_from_value(_record_value(record, "country")),
                            "declared_language": str(_record_value(record, "language") or "").lower().replace("_", "-").split("-")[0] or None,
                            "publisher_location_claims": sorted({item["country_code"] for field in ("title", "description") for item in _explicit_country_references(str(_record_value(record, field) or ""), source="historical record; source provenance unavailable") if item["type"] == "publisher_location_claim"}),
                            "intended_audience_claims": sorted({item["country_code"] for field in ("title", "description") for item in _explicit_country_references(str(_record_value(record, field) or ""), source="historical record; source provenance unavailable") if item["type"] == "intended_audience_claim"}),
                            "episode_language": None,
                        }
                    for field, value in values.items():
                        history.setdefault(f"{feed_id}:{field}", []).append((run_id, started_at, value))
            return history, list(run_times)
    except (sqlite3.Error, OSError, ValueError):
        return {}, []


def build_geo_intelligence(
    database_path: str | Path,
    records: Iterable[Any],
    collection_report: Mapping[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    """Build safe current observations and compare them with earlier snapshots."""
    language_evidence = collection_report.get("episode_language_evidence", {})
    if not isinstance(language_evidence, Mapping):
        language_evidence = {}
    field_sources = collection_report.get("geo_field_sources", {})
    if not isinstance(field_sources, Mapping):
        field_sources = {}
    current = [_observations_for_record(row, language_evidence, field_sources) for row in records]
    history, prior_runs = _historical_observations(database_path)
    drift = []
    for item in current:
        feed_id = item["feed_id"]
        for field, value in item["drift_values"].items():
            earlier = history.get(f"{feed_id}:{field}", [])
            observed = [(run_id, when, old) for run_id, when, old in earlier if old not in (None, [], "")]
            previous = observed[-1] if observed else None
            if value in (None, [], ""):
                classification = "missing_or_unavailable" if previous else "not_observed"
                previous_value = previous[2] if previous else None
                first_at = observed[0][1] if observed else None
                latest_at = previous[1] if previous else None
            else:
                previous_value = previous[2] if previous else None
                classification = ("first_observation" if previous is None else "unchanged" if value == previous_value else "changed")
                first_at = observed[0][1] if observed else observed_at
                latest_at = observed_at
            drift.append({
                "feed_id": feed_id, "title": item["title"], "field": field,
                "previous_value": previous_value, "current_value": value,
                "evidence_source": "current audit metadata and prior SQLite audit snapshots",
                "first_observation": first_at, "latest_observation": latest_at,
                "change_classification": classification,
            })
    conflicts = [item for item in current if item["geographic_identity_conflict"]["potential_conflict"]]
    language_findings = [item for item in current if item["language_inconsistency"]["potential_inconsistency"]]
    return {
        "version": 1,
        "storefront_context": {"value": "US", "source": "Apple Search API country parameter", "not_a_claim_about": ["podcast origin", "publisher location", "intended audience"]},
        "records": current,
        "identity_conflicts": conflicts,
        "language_inconsistencies": language_findings,
        "drift": drift,
        "regional_website_reliability": {
            "status": "not_tested", "observations": [],
            "required_observation_fields": list(REGIONAL_OBSERVATION_FIELDS),
            "reason": "No authorized regional probe with a verified exit country is configured.",
        },
        "history_runs_considered": len(prior_runs),
    }
