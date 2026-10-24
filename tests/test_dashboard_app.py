"""Static guards for the dashboard's read-only, no-network boundary."""
import ast
from pathlib import Path

from src import dashboard_data
from src.database import persist_audit_run
from src.validation import ValidatedRecord, ValidationIssue

APP = Path(__file__).resolve().parents[1] / "app.py"


def test_dashboard_is_read_only_and_does_not_call_website_checker():
    source = APP.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    referenced_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "check_website_url" not in referenced_names
    assert "run_audit" not in referenced_names
    assert "persist_audit_run" not in referenced_names
    assert "requests" not in imported_modules
    assert "get_url_outcomes(db_path, selected_run)" in source
    assert "No websites checked yet" in source


def test_selected_database_views_are_read_only_and_empty_checks_are_not_failures(tmp_path):
    database = tmp_path / "read-only.sqlite"
    record = ValidatedRecord(
        2,
        {"feed_id": "one", "title": "One", "website_url": "https://one.test"},
        {"feed_id": "one", "title": "One", "website_url": "https://one.test", "normalized_website_url": "https://one.test"},
        (ValidationIssue("missing_optional_field", "Optional country is blank.", "country", "warning"),),
    )
    persist_audit_run(database, "one.csv", [record], audit_run_id="one")
    before = database.read_bytes()
    assert dashboard_data.get_url_outcomes(database, "one") == []
    assert dashboard_data.get_run_summary(database, "one")["url_check_count"] == 0
    assert len(dashboard_data.get_podcast_records(database, "one", filter_mode="with_findings")) == 1
    assert database.read_bytes() == before
