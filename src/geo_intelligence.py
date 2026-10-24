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
        "usable_episode_samples": supplied,
        "evidence_sufficient": result in {"identified", "multilingual"},
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
    structured_locations: Iterable[Mapping[str, Any]] = (),
    rss_status: str = "legacy_unavailable",
    apple_storefront: str | None = None,
    location_parse_errors: int = 0,
    observed_at: str | None = None,
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
    normalized_locations: list[dict[str, Any]] = []
    for claim in structured_locations if isinstance(structured_locations, (list, tuple)) else ():
        if not isinstance(claim, Mapping):
            continue
        rel = str(claim.get("rel") or "").strip().lower()
        text = str(claim.get("text") or "").strip()[:128]
        if rel not in {"creator", "subject"} or not (text or claim.get("country")):
            continue
        supplied_country = str(claim.get("country") or "").strip()
        country_code = _country_from_value(supplied_country) if len(supplied_country) == 2 else None
        country_reference = _country_reference()
        country_obj = country_reference.countries.get(alpha_2=country_code) if country_code and country_reference else None
        normalized_locations.append({
            "type": f"podcast_location_{rel}", "rel": rel, "location": text or None,
            "country_code": country_code,
            "country_name": country_obj.name if country_obj else None,
            "country_code_valid": not supplied_country or country_code is not None,
            "source": "publisher RSS podcast:location", "confidence": "publisher-provided; unverified",
            "observed_at": observed_at,
        })
    creator_locations = [item for item in normalized_locations if item["rel"] == "creator"]
    subject_locations = [item for item in normalized_locations if item["rel"] == "subject"]
    mismatch = bool(
        declared_base and episode_status in {"identified", "multilingual"} and episode_languages
        and declared_base not in episode_languages
    )
    location_claims = [item for item in explicit if item["type"] == "publisher_location_claim"]
    comparable_creator_countries = {item["country_code"] for item in creator_locations if item["country_code"]}
    comparable_creator_countries.update(item["country_code"] for item in location_claims)
    country_conflict = bool(
        len(comparable_creator_countries) > 1
        or declared_country and any(code != declared_country for code in comparable_creator_countries)
    )
    source_available = rss_status == "checked"
    return {
        "feed_id": feed_id,
        "title": str(_record_value(record, "title") or "Untitled podcast"),
        "identity": {
            "podcast_country_of_origin": {"value": None, "status": "unknown", "reason": "No source establishes podcast origin."},
            "api_storefront": {"value": apple_storefront or "US", "source": "Apple Search API result/request country", "confidence": "explicit catalog metadata", "meaning": "Catalog queried; not origin, publisher location, or intended audience."},
            "declared_publisher_country": {"value": declared_country, "source": "record country field" if declared_country else None, "declared_value_present": bool(declared_country_value), "confidence": "unverified declaration" if declared_country else "unknown"},
            "publisher_location_claims": location_claims,
            "intended_audience_claims": [item for item in explicit if item["type"] == "intended_audience_claim"],
            "structured_creator_locations": creator_locations,
            "structured_subject_locations": subject_locations,
            "language": {"declared": declared_language or None, "declared_source": record_sources.get("language") if declared_language else None, "episode_observation": dict(evidence) if evidence else {"status": "undetermined", "languages": [], "usable_episode_samples": 0, "evidence_sufficient": False, "evidence_source": "RSS episode titles/descriptions", "reason": "No qualifying episode text was available."}},
            "evidence_availability": {
                "rss": "checked" if source_available else "unavailable" if rss_status == "unavailable" else "legacy_or_unrecorded",
                "structured_creator_location": "claim_found" if creator_locations else "extraction_failed" if source_available and location_parse_errors else "no_claim_found" if source_available else "source_unavailable" if rss_status == "unavailable" else "legacy_unavailable",
                "structured_subject_location": "claim_found" if subject_locations else "extraction_failed" if source_available and location_parse_errors else "no_claim_found" if source_available else "source_unavailable" if rss_status == "unavailable" else "legacy_unavailable",
                "declared_language": "claim_found" if declared_language else "no_claim_found" if source_available else "source_unavailable" if rss_status == "unavailable" else "legacy_unavailable",
            },
        },
        "geographic_identity_conflict": {
            "status": "review" if country_conflict else "none_supported",
            "potential_conflict": country_conflict,
            "evidence": "Comparable publisher-country and creator-location claims disagree." if country_conflict else None,
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
            "structured_creator_locations": sorted([[item["country_code"] or "", (item["location"] or "").casefold()] for item in creator_locations]),
            "structured_subject_locations": sorted([[item["country_code"] or "", (item["location"] or "").casefold()] for item in subject_locations]),
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


def summarize_geo_evidence(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Count coverage and findings from saved observations, without inference."""
    rows = list(records)
    coverage = [item.get("identity", {}).get("evidence_availability", {}) for item in rows]
    return {
        "podcasts_analyzed": len(rows),
        "rss_checked": sum(item.get("rss") == "checked" for item in coverage),
        "rss_unavailable": sum(item.get("rss") == "unavailable" for item in coverage),
        "rss_legacy_unrecorded": sum(item.get("rss") == "legacy_or_unrecorded" for item in coverage),
        "creator_location_claims": sum(item.get("structured_creator_location") == "claim_found" for item in coverage),
        "creator_location_no_claim": sum(item.get("structured_creator_location") == "no_claim_found" for item in coverage),
        "creator_location_unavailable": sum(item.get("structured_creator_location") in {"source_unavailable", "legacy_unavailable"} for item in coverage),
        "creator_location_extraction_failed": sum(item.get("structured_creator_location") == "extraction_failed" for item in coverage),
        "subject_location_claims": sum(item.get("structured_subject_location") == "claim_found" for item in coverage),
        "subject_location_no_claim": sum(item.get("structured_subject_location") == "no_claim_found" for item in coverage),
        "subject_location_unavailable": sum(item.get("structured_subject_location") in {"source_unavailable", "legacy_unavailable"} for item in coverage),
        "subject_location_extraction_failed": sum(item.get("structured_subject_location") == "extraction_failed" for item in coverage),
        "declared_language_claims": sum(item.get("declared_language") == "claim_found" for item in coverage),
        "declared_language_no_claim": sum(item.get("declared_language") == "no_claim_found" for item in coverage),
        "declared_language_unavailable": sum(item.get("declared_language") in {"source_unavailable", "legacy_unavailable"} for item in coverage),
        "potential_conflicts": sum(bool(item.get("geographic_identity_conflict", {}).get("potential_conflict")) for item in rows),
    }


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
    structured_locations = collection_report.get("structured_locations_by_feed", {})
    rss_statuses = collection_report.get("rss_evidence_status_by_feed", {})
    apple_storefronts = collection_report.get("apple_storefront_by_feed", {})
    location_parse_errors = collection_report.get("location_parse_errors_by_feed", {})
    if not isinstance(structured_locations, Mapping):
        structured_locations = {}
    if not isinstance(rss_statuses, Mapping):
        rss_statuses = {}
    if not isinstance(apple_storefronts, Mapping):
        apple_storefronts = {}
    if not isinstance(location_parse_errors, Mapping):
        location_parse_errors = {}
    current = []
    for row in records:
        feed_id = str(_record_value(row, "feed_id") or "")
        current.append(_observations_for_record(
            row, language_evidence, field_sources,
            structured_locations.get(feed_id, ()),
            str(rss_statuses.get(feed_id, "legacy_unavailable")),
            str(apple_storefronts.get(feed_id) or "US"),
            int(location_parse_errors.get(feed_id, 0) or 0), observed_at,
        ))
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
        "version": 2,
        "storefront_context": {"value": "US", "source": "Apple Search API country parameter", "not_a_claim_about": ["podcast origin", "publisher location", "intended audience"]},
        "records": current,
        "evidence_coverage": summarize_geo_evidence(current),
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
