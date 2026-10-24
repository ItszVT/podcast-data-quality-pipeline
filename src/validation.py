"""Schema and record-level checks for imported podcast records."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .ingestion import REQUIRED_COLUMNS

OPTIONAL_COLUMNS = ("description", "categories", "language", "country")


@dataclass(frozen=True)
class ValidationIssue:
    category: str
    message: str
    field: str | None = None
    severity: str = "error"


@dataclass(frozen=True)
class ValidatedRecord:
    source_row: int
    original: Mapping[str, Any]
    normalized: Mapping[str, Any]
    issues: tuple[ValidationIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


@dataclass(frozen=True)
class ValidationReport:
    records: tuple[ValidatedRecord, ...]
    schema_issues: tuple[ValidationIssue, ...]
    input_columns: tuple[str, ...]
    missing_optional_columns: tuple[str, ...]
    unexpected_columns: tuple[str, ...]
    duplicate_identifiers: tuple[str, ...]

    @property
    def total_rows(self) -> int:
        return len(self.records)

    @property
    def valid_rows(self) -> int:
        if self.schema_issues:
            return 0
        return sum(record.is_valid for record in self.records)

    @property
    def rows_with_issues(self) -> int:
        return sum(bool(record.issues) for record in self.records)

    @property
    def issue_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter(issue.category for issue in self.schema_issues)
        counts.update(issue.category for record in self.records for issue in record.issues)
        return dict(sorted(counts.items()))

    @property
    def duplicate_feed_ids(self) -> tuple[str, ...]:
        return self.duplicate_identifiers


def normalize_website_url(value: Any) -> tuple[str | None, str | None]:
    """Trim outer whitespace and validate an absolute HTTP(S) URL.

    Returns (normalized candidate, error category). This deliberately does not
    add a scheme, rewrite the hostname, or otherwise guess a destination.
    """
    if value is None or not str(value).strip():
        return None, "blank_website_url"

    candidate = str(value).strip()
    try:
        parts = urlsplit(candidate)
        # Accessing .port validates malformed/out-of-range port values.
        _ = parts.port
    except ValueError:
        return candidate, "malformed_url"

    if not parts.netloc or not parts.hostname:
        return candidate, "malformed_url"
    if parts.scheme.lower() not in {"http", "https"}:
        return candidate, "unsupported_url_scheme"
    if any(character.isspace() for character in candidate):
        return candidate, "malformed_url"
    return candidate, None


def validate_records(
    records: Iterable[Mapping[str, Any]],
    columns: Iterable[str] | None = None,
) -> ValidationReport:
    """Validate rows while retaining originals and attaching all row issues."""
    source_records = list(records)
    if columns is None:
        columns = source_records[0].keys() if source_records else ()
    input_columns = tuple(str(column).strip() for column in columns)
    present = set(input_columns)

    schema_issues = tuple(
        ValidationIssue(
            category="missing_required_column",
            message=f"Required column '{column}' is missing from the input file.",
            field=column,
        )
        for column in REQUIRED_COLUMNS
        if column not in present
    )
    missing_optional = tuple(column for column in OPTIONAL_COLUMNS if column not in present)
    unexpected = tuple(column for column in input_columns if column not in (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS))

    prepared: list[dict[str, Any]] = []
    ids_to_rows: dict[str, list[int]] = defaultdict(list)
    for index, source in enumerate(source_records):
        original = {key: value for key, value in source.items() if key != "source_row"}
        row_number = source.get("source_row", index + 2)
        feed_value = original.get("feed_id")
        feed_id = "" if feed_value is None else str(feed_value).strip()
        if feed_id:
            ids_to_rows[feed_id].append(index)
        normalized = dict(original)
        if "feed_id" in normalized and feed_value is not None:
            normalized["feed_id"] = feed_id
        title_value = original.get("title")
        if title_value is not None:
            normalized["title"] = str(title_value).strip()
        raw_url = original.get("website_url")
        normalized_url, url_error = normalize_website_url(raw_url)
        if "website_url" in original:
            normalized["normalized_website_url"] = normalized_url
        prepared.append({
            "source_row": row_number,
            "original": original,
            "normalized": normalized,
            "feed_id": feed_id,
            "url_error": url_error,
            "url_had_outer_whitespace": isinstance(raw_url, str) and raw_url != raw_url.strip(),
        })

    duplicates = tuple(sorted(identifier for identifier, indexes in ids_to_rows.items() if len(indexes) > 1))
    duplicate_set = set(duplicates)
    validated: list[ValidatedRecord] = []

    for row in prepared:
        issues: list[ValidationIssue] = []
        source = row["original"]
        feed_id = row["feed_id"]
        if not str(source.get("feed_id") or "").strip():
            issues.append(ValidationIssue("missing_required_value", "Feed ID is missing or blank.", "feed_id"))
        elif feed_id in duplicate_set:
            issues.append(ValidationIssue("duplicate_feed_id", f"Feed ID '{feed_id}' occurs more than once in this file.", "feed_id"))

        if not str(source.get("title") or "").strip():
            issues.append(ValidationIssue("missing_required_value", "Podcast title is missing or blank.", "title"))
        elif isinstance(source.get("title"), str) and source["title"] != source["title"].strip():
            issues.append(ValidationIssue("surrounding_whitespace", "Title has leading or trailing whitespace; normalized value is trimmed.", "title", "warning"))

        if row["url_error"]:
            category = row["url_error"]
            messages = {
                "blank_website_url": "Website URL is blank; the record cannot be checked online.",
                "malformed_url": "Website URL is not a valid absolute HTTP(S) URL.",
                "unsupported_url_scheme": "Website URL uses a scheme other than HTTP or HTTPS.",
            }
            issues.append(ValidationIssue(category, messages[category], "website_url"))
        elif row["url_had_outer_whitespace"]:
            issues.append(ValidationIssue("surrounding_whitespace", "Website URL has leading or trailing whitespace; normalized candidate is trimmed.", "website_url", "warning"))

        for field in OPTIONAL_COLUMNS:
            value = source.get(field)
            if value is None or not str(value).strip():
                issues.append(ValidationIssue("missing_optional_field", f"Optional field '{field}' is missing or blank.", field, "warning"))

        validated.append(ValidatedRecord(
            source_row=row["source_row"],
            original=row["original"],
            normalized=row["normalized"],
            issues=tuple(issues),
        ))

    return ValidationReport(
        records=tuple(validated),
        schema_issues=schema_issues,
        input_columns=input_columns,
        missing_optional_columns=missing_optional,
        unexpected_columns=unexpected,
        duplicate_identifiers=duplicates,
    )



