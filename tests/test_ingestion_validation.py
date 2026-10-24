from pathlib import Path

import pandas as pd
import pytest

from src.ingestion import IngestionError, load_podcast_file
from src.validation import normalize_website_url, validate_records

REQUIRED = ["feed_id", "title", "website_url"]


def write_csv(path: Path, rows, columns=REQUIRED):
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)
    return path


def test_load_valid_csv_preserves_identifier_and_source_values(tmp_path):
    path = write_csv(tmp_path / "podcasts.csv", [["00123", " Show ", " https://example.test/a "]])
    imported = load_podcast_file(path)
    assert imported.total_rows == 1
    assert imported.records[0]["feed_id"] == "00123"
    assert imported.records[0]["title"] == " Show "
    assert imported.records[0]["source_row"] == 2


def test_load_valid_excel(tmp_path):
    path = tmp_path / "podcasts.xlsx"
    pd.DataFrame([["00123", "Show", "https://example.test"]], columns=REQUIRED).to_excel(path, index=False)
    imported = load_podcast_file(path)
    assert imported.records[0]["feed_id"] == "00123"
    assert imported.total_rows == 1


def test_missing_required_columns_reported_without_dropping_row(tmp_path):
    path = write_csv(tmp_path / "missing.csv", [["Show"]], ["title"])
    imported = load_podcast_file(path)
    report = validate_records(imported.records, imported.columns)
    assert report.total_rows == 1
    assert report.valid_rows == 0
    assert {issue.field for issue in report.schema_issues} == {"feed_id", "website_url"}


@pytest.mark.parametrize("content, message", [("", "empty"), ('"feed_id","title","website_url"\n', "no data rows")])
def test_empty_csv_is_reported(tmp_path, content, message):
    path = tmp_path / "empty.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(IngestionError, match=message):
        load_podcast_file(path)


def test_missing_path_and_unsupported_format(tmp_path):
    with pytest.raises(IngestionError, match="does not exist"):
        load_podcast_file(tmp_path / "missing.csv")
    path = tmp_path / "podcasts.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(IngestionError, match="Unsupported"):
        load_podcast_file(path)


def test_duplicate_feed_ids_are_flagged_on_every_row(tmp_path):
    path = write_csv(tmp_path / "dupes.csv", [["a", "One", "https://a.test"], ["a", "Two", "https://b.test"]])
    imported = load_podcast_file(path)
    report = validate_records(imported.records, imported.columns)
    assert report.duplicate_feed_ids == ("a",)
    assert report.issue_counts["duplicate_feed_id"] == 2
    assert report.rows_with_issues == 2


def test_blank_and_malformed_urls_remain_distinct(tmp_path):
    path = write_csv(tmp_path / "urls.csv", [["a", "A", ""], ["b", "B", "ht!tp://[bad"]])
    imported = load_podcast_file(path)
    report = validate_records(imported.records, imported.columns)
    assert report.issue_counts["blank_website_url"] == 1
    assert report.issue_counts["malformed_url"] == 1


def test_url_normalization_trims_but_does_not_invent_scheme_or_host():
    assert normalize_website_url("  https://Example.test/a  ") == ("https://Example.test/a", None)
    assert normalize_website_url("http://example.test") == ("http://example.test", None)
    assert normalize_website_url("ftp://example.test") == ("ftp://example.test", "unsupported_url_scheme")
    assert normalize_website_url("example.test/path") == ("example.test/path", "malformed_url")
    assert normalize_website_url("ht!tp://[bad") == ("ht!tp://[bad", "malformed_url")


def test_whitespace_and_missing_optional_fields_are_reported_without_mutating_source(tmp_path):
    path = write_csv(tmp_path / "fields.csv", [[" a ", " Title ", " https://example.test "]])
    imported = load_podcast_file(path)
    report = validate_records(imported.records, imported.columns)
    row = report.records[0]
    assert row.original["title"] == " Title "
    assert row.normalized["title"] == "Title"
    assert row.original["website_url"] == " https://example.test "
    assert row.normalized["normalized_website_url"] == "https://example.test"
    assert report.issue_counts["surrounding_whitespace"] == 2
    assert report.issue_counts["missing_optional_field"] == 4


def test_multiple_issues_and_summary_counts(tmp_path):
    path = write_csv(tmp_path / "multi.csv", [["", "  ", "ftp://bad.test"]])
    report = validate_records(load_podcast_file(path).records, REQUIRED)
    row = report.records[0]
    assert {issue.category for issue in row.issues} >= {
        "missing_required_value", "unsupported_url_scheme", "missing_optional_field"
    }
    assert report.total_rows == 1
    assert report.valid_rows == 0
    assert report.rows_with_issues == 1
    assert sum(report.issue_counts.values()) > report.rows_with_issues


def test_unexpected_columns_are_preserved_and_reported(tmp_path):
    columns = [*REQUIRED, "source_extra"]
    path = write_csv(tmp_path / "extra.csv", [["a", "A", "https://a.test", "kept"]], columns)
    imported = load_podcast_file(path)
    report = validate_records(imported.records, imported.columns)
    assert report.unexpected_columns == ("source_extra",)
    assert report.records[0].original["source_extra"] == "kept"


def test_unreadable_excel_is_reported(tmp_path):
    path = tmp_path / "broken.xlsx"
    path.write_text("not an Excel workbook", encoding="utf-8")
    with pytest.raises(IngestionError, match="Could not read"):
        load_podcast_file(path)
