"""Read-only, plain-language Streamlit view of saved podcast audit snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
from importlib import reload

import pandas as pd
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from src import dashboard_data as data
from src.published_snapshot import SnapshotValidationError, validate_publishable_snapshot
from src.geo_intelligence import summarize_geo_evidence

# The database access module is project code; reload it with the app so changes
# to its read-only queries are available after Streamlit hot reloads this file.
data = reload(data)

st.set_page_config(page_title="Podcast Data Quality", layout="wide")

SECTIONS = ["Overview", "Data quality", "Website health", "Geo intelligence", "Podcast records", "History"]
FIELD_LABELS = {
    "feed_id": "Podcast ID",
    "title": "Podcast title",
    "website_url": "Publisher website",
    "description": "Description",
    "categories": "Categories",
    "language": "Language",
    "country": "Publisher country",
}
FINDING_LABELS = {
    "blank_website_url": "Missing publisher website",
    "malformed_url": "Malformed publisher website",
    "unsupported_url_scheme": "Unsupported website link",
    "missing_required_value": "Missing required information",
    "duplicate_feed_id": "Duplicate podcast ID",
    "surrounding_whitespace": "Extra spaces",
    "missing_required_column": "Missing required column",
    "missing_optional_field": "Missing optional information",
}
FIELD_EXPLANATIONS = {
    "country": ("No publisher country is recorded.", "Leave this blank unless a reliable source confirms the publisher's country; the Apple storefront is not enough."),
    "description": ("No podcast description is recorded.", "Add a publisher-provided description when one is available."),
    "language": ("No language is recorded.", "Add the language from a reliable source when available."),
    "website_url": ("No publisher website is recorded for this podcast.", "Review the podcast's publisher information. Keep a feed URL separate from a publisher website."),
    "feed_id": ("This podcast has no identifier, or its identifier is repeated.", "Check the source identifier and resolve missing or repeated IDs in the source file."),
    "title": ("This podcast has no title.", "Add the podcast's title from the source record."),
}
CATEGORY_EXPLANATIONS = {
    "malformed_url": ("The website value is present but is not a valid web address.", "Check the original value for typos. The pipeline does not guess or rewrite its destination."),
    "unsupported_url_scheme": ("The link uses a scheme other than HTTP or HTTPS.", "Confirm the intended website link. Only change it when a reliable source gives the correct address."),
    "duplicate_feed_id": ("More than one record uses this podcast identifier.", "Review the affected records and decide whether they represent duplicate entries."),
    "surrounding_whitespace": ("The source value has spaces at the beginning or end.", "Trim the value in the source if appropriate; the original is retained in the audit."),
    "missing_required_value": ("A required podcast value is blank.", "Check the source and add the missing value when it can be verified."),
    "missing_required_column": ("The input file is missing a column required by the documented format.", "Add the required column to the input file and rerun the audit."),
}
CLASSIFICATION_LABELS = {
    "likely_working": "Likely reachable",
    "http_error": "Server returned an error",
    "suspected_soft_404": "Possible not-found page",
    "inconclusive": "Not enough information",
    "invalid_input": "Could not check this link",
}
STATUS_LABELS = {"succeeded": "Completed", "partial": "Partially completed", "failed": "Failed", "running": "In progress"}


@st.cache_data(ttl=300, show_spinner=False)
def _load_published_snapshot() -> bytes:
    settings = st.secrets.get("github_snapshot", {})
    token = settings.get("token", "") if hasattr(settings, "get") else ""
    snapshot = data.fetch_published_snapshot(token)
    try:
        validate_publishable_snapshot(snapshot, allow_recoverable_rss_partial=True)
    except SnapshotValidationError as exc:
        raise data.DashboardDataError(
            "The published snapshot does not contain a verified, nonempty real audit. Run the audit workflow again."
        ) from exc
    return snapshot


def _audit_date(value: str | None) -> str:
    if not value:
        return "Not recorded"
    try:
        parsed = pd.to_datetime(value, utc=True).to_pydatetime()
        local = parsed.astimezone()
        zone = local.tzname() or "local time"
        return local.strftime("%b %d, %Y · %I:%M %p") + f" {zone}"
    except (TypeError, ValueError):
        return value


def _audit_choice_label(row: dict) -> str:
    """Keep audit choices scannable; the full context is shown on the page."""
    try:
        started_at = row.get("started_at")
        if not started_at:
            raise ValueError("Audit start time is unavailable")
        started = pd.to_datetime(started_at, utc=True).to_pydatetime().astimezone()
        date_label = started.strftime("%b %d · %I:%M %p")
    except (TypeError, ValueError):
        date_label = "Date not recorded"
    status = STATUS_LABELS.get(row.get("run_status"), "Status unavailable")
    status = "Partial" if status == "Partially completed" else status
    audit_id = str(row.get("audit_run_id") or "Unknown ID")
    return f"{date_label} · {status} · {audit_id[:8]}"


def _field_label(field: str | None) -> str:
    return FIELD_LABELS.get(field or "", (field or "File-level finding").replace("_", " ").title())


def _finding_label(issue: dict) -> str:
    category, field = issue.get("category"), issue.get("field")
    if category == "missing_optional_field":
        return f"Missing {_field_label(field).lower()}"
    return FINDING_LABELS.get(category, (category or "Finding").replace("_", " ").title())


def _finding_explanation(issue: dict) -> tuple[str, str]:
    if issue.get("category") == "missing_optional_field" and issue.get("field") in FIELD_EXPLANATIONS:
        return FIELD_EXPLANATIONS[issue["field"]]
    if issue.get("category") == "blank_website_url":
        return FIELD_EXPLANATIONS["website_url"]
    return CATEGORY_EXPLANATIONS.get(
        issue.get("category"),
        (issue.get("message", "A data quality finding was recorded."), "Review the source value and its meaning in the input contract."),
    )


def _severity_label(value: str) -> str:
    return {"error": "Action needed", "warning": "Completeness note", "info": "Information"}.get(value, "Review")


def _exact_utc(value: str | None) -> str:
    if not value:
        return "Not recorded"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return "Timestamp unavailable"


def _duration(started_at: str | None, completed_at: str | None) -> str:
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        seconds = max(0, int((end - start).total_seconds()))
        return f"{seconds // 60}m {seconds % 60}s"
    except (AttributeError, TypeError, ValueError):
        return "Not recorded"


def _render_stages(summary: dict) -> None:
    stages = summary.get("execution_stages", {})
    names = (("preflight", "Preflight"), ("collection", "Collection"), ("validation", "Validation"), ("persistence", "Save"))
    if not stages:
        st.caption("Stage-by-stage execution details are unavailable for this older audit.")
        return
    colors = {"succeeded": "green", "partial": "orange", "failed": "red", "skipped": "gray", "not_started": "gray", "not_requested": "gray"}
    with st.container(horizontal=True):
        for key, label in names:
            value = stages.get(key)
            display = STATUS_LABELS.get(value, value.replace("_", " ").title()) if value else "Not recorded"
            st.badge(f"{label} · {display}", color=colors.get(value, "gray"))


def _render_website_coverage(summary: dict) -> None:
    status_value = summary.get("website_check_status", "unknown")
    unique_count = summary.get("website_check_unique_urls")
    result_count = summary.get("website_records_with_results", summary.get("url_check_count", 0))
    if status_value == "not_requested":
        st.info("No websites checked yet. This audit records that website checking was not requested.")
    elif status_value == "unknown":
        if result_count:
            st.info(f"Request status is not recorded for this audit. {result_count} per-record result(s) were saved.")
        else:
            st.info("Website-check request status is unavailable for this audit; no result rows were saved.")
    elif status_value == "partial":
        reasons = {
            "unique_url_limit_reached": "The unique URL request limit was reached.",
            "stage_budget_exhausted": "The website-check time budget was reached.",
            "checker_error": "One or more checks could not be completed by the checker.",
        }
        reason = reasons.get(summary.get("website_check_stop_reason"), "Some eligible sites may not have been reached within the configured limits.")
        st.warning(f"Website checking was partial. {reason}")
    elif status_value == "succeeded" and result_count == 0:
        st.info("Website checking completed, but no per-record results were saved. There may have been no eligible website links.")
    if unique_count is not None:
        source = "pipeline metadata" if summary.get("website_unique_count_source") == "pipeline_metadata" else "distinct saved targets"
        st.caption(f"{unique_count} unique URL(s) tested · {result_count} per-record result row(s) · unique URL count from {source}.")
    elif result_count == 0:
        st.caption("Unique URL coverage cannot be recovered from this audit.")


def _show_records_table(records: list[dict], *, key_prefix: str = "records") -> None:
    if not records:
        st.info("No podcast records match these filters.")
        return
    display = pd.DataFrame(records)
    columns = [
        ("title", "Podcast"), ("website_url", "Publisher website"),
        ("feed_id", "Podcast ID"), ("finding_count", "Findings"),
    ]
    visible = {source: label for source, label in columns if source in display.columns}
    st.dataframe(
        display[list(visible)].rename(columns=visible),
        hide_index=True,
        height=min(440, 38 + 36 * min(len(display), 10)),
        alt="Podcast records matching the selected filters",
        key=f"{key_prefix}_table",
    )


def _render_findings(
    database_path: str | bytes,
    run_id: str,
    *,
    key_prefix: str = "findings",
    severity: str | None = None,
    category: str | None = None,
    exclude_category: str | None = None,
) -> list[dict]:
    findings = data.get_issue_field_counts(database_path, run_id)
    if severity is not None:
        findings = [issue for issue in findings if issue["severity"] == severity]
    if category is not None:
        findings = [issue for issue in findings if issue["category"] == category]
    if exclude_category is not None:
        findings = [issue for issue in findings if issue["category"] != exclude_category]
    if not findings:
        message = "No actionable errors were saved for this audit." if severity == "error" else "No completeness notes were saved for this audit." if severity == "warning" or category == "missing_optional_field" else "No informational findings were saved for this audit." if severity == "info" else "No findings were saved for this audit."
        st.success(message, icon=":material/check_circle:")
        return []
    summary_rows = []
    for issue in findings:
        meaning, next_step = _finding_explanation(issue)
        summary_rows.append({
            "Finding": _finding_label(issue),
            "Records affected": issue["affected_record_count"],
            "Priority": _severity_label(issue["severity"]),
        })
    st.dataframe(
        pd.DataFrame(summary_rows),
        hide_index=True,
        height=min(300, 38 + 42 * len(summary_rows)),
        alt="Saved data quality findings, affected record counts, and priorities",
    )
    st.caption("Counts are from the selected audit. One podcast may have multiple findings; completeness notes are not failures.")
    for index, issue in enumerate(findings):
        name = _finding_label(issue)
        count = issue["affected_record_count"]
        suffix = "record" if count == 1 else "records"
        finding_key = f"{key_prefix}_{run_id}_{index}"
        with st.expander(f"{name} · {count} {suffix}", key=f"{finding_key}_detail"):
            meaning, next_step = _finding_explanation(issue)
            st.markdown(f"**What it means:** {meaning}")
            st.markdown(f"**Suggested next step:** {next_step}")
            st.caption(f"{_severity_label(issue['severity'])} · Field: {_field_label(issue.get('field'))}")
            if issue.get("message"):
                st.caption(f"Recorded detail: {issue['message']}")
            if issue["affected_record_count"]:
                affected = data.get_affected_records(
                    database_path, run_id,
                    issue_category=issue["category"], issue_field=issue.get("field"),
                )
                _show_records_table(affected, key_prefix=f"{finding_key}_records")
            else:
                st.caption("This finding applies to the input file rather than an individual podcast record.")
    file_issues = [
        issue for issue in data.get_schema_issues(database_path, run_id)
        if (severity is None or issue["severity"] == severity)
        and (category is None or issue["category"] == category)
        and (exclude_category is None or issue["category"] != exclude_category)
    ]
    if file_issues:
        st.subheader("Input file findings")
        file_rows = [{"Finding": FINDING_LABELS.get(r["category"], r["category"].replace("_", " ").title()), "Field": _field_label(r.get("field")), "Detail": r["message"], "Priority": _severity_label(r["severity"])} for r in file_issues]
        st.dataframe(pd.DataFrame(file_rows), hide_index=True, alt="Findings that apply to the input file")
    return findings


with st.sidebar:
    st.markdown("### Podcast data")
    try:
        dashboard_settings = st.secrets.get("dashboard", {})
    except StreamlitSecretNotFoundError:
        # Local read-only mode needs no secrets; deployed mode is configured
        # explicitly through Streamlit secrets.
        dashboard_settings = {}
    dashboard_mode = dashboard_settings.get("mode", "local") if hasattr(dashboard_settings, "get") else "local"
    dashboard_mode = str(dashboard_mode).strip().lower()
    if dashboard_mode not in {"local", "published"}:
        st.error("Dashboard mode is invalid. Set it to `local` or `published` in app settings.")
        st.stop()

    if dashboard_mode == "published":
        try:
            db_path = _load_published_snapshot()
        except data.DashboardDataError as exc:
            st.error(str(exc), icon=":material/database_off:")
            st.stop()
        st.caption("Latest published real audit · read-only")
    else:
        db_path = st.text_input(
            "Existing audit database",
            value="data/real_podcast_audits.sqlite",
            key="database_path",
        )
    st.caption("Read-only view. Opening this page never changes saved audits or checks websites.")
    if st.button("Refresh saved data", width="stretch", key="refresh_data"):
        if dashboard_mode == "published":
            _load_published_snapshot.clear()
        st.rerun()
    section = st.radio("Explore", SECTIONS, key="dashboard_section")
    with st.expander("Methodology / About these checks"):
        st.write("The dashboard reads saved SQLite snapshots only. Opening it does not run collection or contact publisher websites.")
        st.write("A successful audit means the pipeline completed; it does not mean every podcast field is complete or every website is reachable.")
        st.caption("Website classifications use bounded HTTP observations. Inconclusive results are not proof of failure. Geographic origin is never inferred from language, domain, or Apple storefront.")
        st.subheader("Evaluation evidence")
        st.write("Automated tests cover pipeline logic, validation, database behavior, URL-checking safety, and dashboard views. Controlled cases are examples, not a representative sample of the web.")
        reference_file = data.PROJECT_ROOT / "data" / "reference_url_outcomes.csv"
        if reference_file.is_file():
            reference = pd.read_csv(reference_file)
            st.caption(f"{len(reference)} controlled local cases are available.")
            cases = [{"Case": row["case_id"].replace("_", " ").title(), "Expected result": CLASSIFICATION_LABELS.get(row["expected_classification"], row["expected_classification"])} for _, row in reference.iterrows()]
            st.dataframe(pd.DataFrame(cases), hide_index=True, alt="Controlled website cases and expected outcomes")
        st.caption("Real-world evaluation metrics are not stored; no accuracy score or confusion matrix is claimed.")
        st.code("python -m pytest tests/test_evaluation.py -v", language="powershell")
    try:
        runs = data.get_runs(db_path)
    except data.DashboardDataError as exc:
        st.error(str(exc), icon=":material/database_off:")
        st.stop()
    if not runs:
        if dashboard_mode == "published":
            st.error("The published snapshot contains no real audit data. Run the audit workflow first.")
        else:
            st.info("This database has no saved audits yet.")
        st.stop()
    run_labels = {
        row["audit_run_id"]: _audit_choice_label(row)
        for row in runs
    }
    run_ids = list(run_labels)
    selected_run = st.selectbox("Selected audit", run_ids, format_func=lambda value: run_labels[value], key="selected_audit")

try:
    summary = data.get_run_summary(db_path, selected_run)
except data.DashboardDataError as exc:
    st.error(str(exc), icon=":material/database_off:")
    st.stop()
if summary is None:
    st.error("The selected audit is no longer available. Refresh saved data and choose another audit.")
    st.stop()

st.title("Podcast intelligence")
st.caption("A focused view of audit execution, data quality, website observations, and geographic evidence.")
status = STATUS_LABELS.get(summary["run_status"], summary["run_status"].replace("_", " ").title())
status_color = {"Completed": "green", "Partially completed": "orange", "Failed": "red", "In progress": "blue"}.get(status, "gray")
with st.container(border=True):
    with st.container(horizontal=True):
        st.badge(f"Audit execution · {status}", color=status_color, help="Pipeline execution status only; data-quality findings are shown separately.")
        st.metric("Podcasts analyzed", summary["stored_records"])
        st.caption(f"Source: {summary.get('source_label', 'Source not recorded')} · Catalog/storefront: {summary.get('storefront_label', 'Not recorded')}")
    st.caption(f"Audit ID: `{summary['audit_run_id']}`")
    st.caption(f"Started {_audit_date(summary.get('started_at'))} · Completed {_audit_date(summary.get('completed_at'))} · Duration {_duration(summary.get('started_at'), summary.get('completed_at'))} · times shown in dashboard-local time")
    with st.expander("Exact timestamps (UTC)"):
        st.text(f"Started:   {_exact_utc(summary.get('started_at'))}\nCompleted: {_exact_utc(summary.get('completed_at'))}")
    _render_stages(summary)
if summary["run_status"] == "partial":
    rss_failures = summary.get("rss_retrieval_failures")
    if isinstance(rss_failures, int) and rss_failures > 0:
        st.warning(
            f"RSS metadata could not be fetched for {rss_failures} podcast feed(s). "
            "Records returned by successful searches were retained, validated, and saved; "
            "this audit remains marked partially completed."
        )
    else:
        st.warning(
            "This audit is partially completed. Some collection requests failed, so "
            "the saved results may not include the full search coverage."
        )

if section == "Overview":
    st.header("Overview")
    st.write("Execution status describes whether the audit pipeline completed. It is separate from the issues found in podcast data.")
    with st.container(horizontal=True):
        st.metric("Podcasts analyzed", summary["stored_records"])
        st.metric("Records with actionable errors", summary["records_with_actionable_errors"], help="Distinct podcast records with at least one error-severity finding. File-level errors are listed separately.")
        st.metric("Completeness notes", summary["completeness_issue_occurrences"], help="Optional metadata omissions; these are not pipeline failures.")
        st.metric("Unique website URLs tested", summary["website_check_unique_urls"] if summary["website_check_unique_urls"] is not None else "Unknown")
    st.subheader("What was checked")
    rss_success = summary.get("rss_retrieval_successes")
    rss_failures = summary.get("rss_retrieval_failures")
    if rss_success is not None or rss_failures is not None:
        st.caption(f"RSS metadata: {rss_success if rss_success is not None else 'unknown'} retrieved · {rss_failures if rss_failures is not None else 'unknown'} failed · {summary.get('rss_feeds_missing_url') if summary.get('rss_feeds_missing_url') is not None else 'unknown'} feeds had no URL.")
    else:
        st.caption("RSS retrieval coverage was not recorded for this audit.")
    _render_website_coverage(summary)
    st.subheader("Action needed")
    st.caption(f"{summary['records_with_actionable_errors']} podcast record(s) have error-severity findings. Optional missing fields are shown separately under Data quality.")
    _render_findings(db_path, selected_run, key_prefix="overview_errors", severity="error")
    st.subheader("Geographic and language changes")
    overview_geo = data.get_geo_intelligence(db_path, selected_run)
    overview_drift = overview_geo.get("drift", []) if isinstance(overview_geo, dict) else []
    changed_count = sum(row.get("change_classification") == "changed" for row in overview_drift)
    first_count = sum(row.get("change_classification") == "first_observation" for row in overview_drift)
    missing_count = sum(row.get("change_classification") == "missing_or_unavailable" for row in overview_drift)
    if overview_drift:
        st.caption(f"{changed_count} confirmed value change(s) · {first_count} first observation(s) · {missing_count} previously known value(s) now missing or unavailable.")
    else:
        st.caption("No historical Geo Intelligence comparison is available for this audit.")

elif section == "Data quality":
    st.header("Data quality")
    st.write("Errors need review. Completeness notes describe optional information that was not recorded; they do not mean a podcast is invalid.")
    with st.container(horizontal=True):
        st.metric("Records with errors", summary["records_with_actionable_errors"])
        st.metric("Error findings", summary["actionable_issue_occurrences"])
        st.metric("Optional fields missing", summary["completeness_issue_occurrences"])
        st.metric("Other warnings", summary["other_warning_occurrences"])
        st.metric("File-level errors", summary["file_level_error_count"])
    tab_errors, tab_notes, tab_warnings, tab_info = st.tabs(["Actionable errors", "Completeness notes", "Other warnings", "Other information"])
    with tab_errors:
        _render_findings(db_path, selected_run, key_prefix="quality_errors", severity="error")
    with tab_notes:
        if summary["completeness_issue_occurrences"]:
            st.caption("A missing publisher country is not treated as an error. Leave country unknown unless a reliable source confirms it.")
        _render_findings(db_path, selected_run, key_prefix="quality_completeness", severity="warning", category="missing_optional_field")
    with tab_warnings:
        st.caption("Formatting and other review warnings are separate from optional-field completeness notes and error-severity findings.")
        _render_findings(db_path, selected_run, key_prefix="quality_warnings", severity="warning", exclude_category="missing_optional_field")
    with tab_info:
        _render_findings(db_path, selected_run, key_prefix="quality_information", severity="info")

elif section == "Podcast records":
    st.header("Podcast records")
    categories = data.get_issue_categories(db_path, selected_run)
    options = ["All records", "Records with actionable errors", "Records with completeness notes", "Records without findings", "Missing publisher websites", *[f"Finding: {FINDING_LABELS.get(category, category.replace('_', ' ').title())}" for category in categories]]
    filter_label = st.selectbox("Show", options, key="record_filter")
    search = st.text_input("Search podcast, ID, or publisher website", key="record_search")
    issue_category = None
    if filter_label == "All records":
        mode = "all"
    elif filter_label == "Records with actionable errors":
        mode = "with_actionable_errors"
    elif filter_label == "Records with completeness notes":
        mode = "with_completeness_notes"
    elif filter_label == "Records without findings":
        mode = "without_findings"
    elif filter_label == "Missing publisher websites":
        mode = "missing_website"
    else:
        mode = "category"
        selected_label = filter_label.removeprefix("Finding: ")
        issue_category = next((category for category in categories if FINDING_LABELS.get(category, category.replace("_", " ").title()) == selected_label), None)
    records = data.get_podcast_records(db_path, selected_run, filter_mode=mode, issue_category=issue_category, search=search)
    st.caption(f"{len(records)} records match this view. The table is read-only; source rows are not changed here.")
    _show_records_table(records, key_prefix="all_podcasts")
    if records:
        by_id = {row["record_id"]: row for row in records}
        chosen_id = st.selectbox("Inspect a record", list(by_id), format_func=lambda value: f"{by_id[value]['title'] or 'Untitled podcast'} · row {by_id[value]['source_row']}", key="record_detail")
        chosen = by_id[chosen_id]
        st.subheader(chosen["title"] or "Untitled podcast")
        st.caption(f"Podcast ID: {chosen['feed_id'] or 'Not recorded'} · Source row: {chosen['source_row']}")
        with st.expander("Podcast details", expanded=True):
            detail_rows = [{"Field": label, "Value": chosen.get(field) or "Not recorded"} for field, label in (("website_url", "Publisher website"), ("description", "Description"), ("categories", "Categories"), ("language", "Language"), ("country", "Publisher country"))]
            st.dataframe(pd.DataFrame(detail_rows), hide_index=True, alt="Saved podcast metadata for the selected record")
        record_findings = data.get_record_findings(db_path, selected_run, chosen_id)
        if record_findings:
            st.markdown("**Findings for this podcast**")
            for issue in record_findings:
                meaning, _ = _finding_explanation(issue)
                st.markdown(f"- **{_finding_label(issue)}** — {meaning} ({_severity_label(issue['severity'])})")
        else:
            st.success("No findings were saved for this podcast.", icon=":material/check_circle:")
        url_observation = data.get_record_url_check(db_path, selected_run, chosen_id)
        with st.expander("Website observation"):
            if url_observation:
                st.write(f"**Result:** {CLASSIFICATION_LABELS.get(url_observation['classification'], url_observation['classification'])}")
                st.write(f"**HTTP status:** {url_observation['http_status_code'] if url_observation['http_status_code'] is not None else 'No response'}")
                st.write(f"**Checked URL:** {url_observation['original_url'] or 'Not recorded'}")
                st.write(f"**Final URL:** {url_observation['final_url'] or 'Not reached'}")
                if url_observation.get("evidence"):
                    st.caption(url_observation["evidence"])
            elif summary.get("website_check_status") == "not_requested":
                st.caption("Website checking was not requested for this audit.")
            else:
                st.caption("No website result is saved for this record; the request state may be unavailable in older audits.")
        geo_payload = data.get_geo_intelligence(db_path, selected_run)
        geo_row = next((item for item in geo_payload.get("records", []) if item.get("feed_id") == chosen.get("feed_id")), None) if isinstance(geo_payload, dict) else None
        with st.expander("Geographic and language evidence"):
            if not geo_row:
                st.caption("No geographic evidence was saved for this record in the selected audit.")
            else:
                identity = geo_row.get("identity", {})
                st.write(f"**Podcast origin:** {identity.get('podcast_country_of_origin', {}).get('value') or 'Unknown'}")
                availability = identity.get("evidence_availability", {})
                rss_state = availability.get("rss", "legacy_or_unrecorded")
                st.write(f"**RSS source:** {rss_state.replace('_', ' ').title()}")
                description_state = "Present in saved record" if chosen.get("description") else "No description saved"
                if not chosen.get("description") and rss_state == "unavailable":
                    description_state += "; RSS retrieval failed, so absence is not confirmed"
                elif not chosen.get("description") and rss_state == "legacy_or_unrecorded":
                    description_state += "; source availability is unknown for this older audit"
                elif not chosen.get("description") and rss_state == "checked":
                    description_state += "; RSS retrieval succeeded"
                st.write(f"**Description:** {description_state}")
                language_state = "Declared" if chosen.get("language") else "Not recorded"
                if not chosen.get("language") and rss_state == "unavailable":
                    language_state += "; RSS retrieval failed, so absence is not confirmed"
                elif not chosen.get("language") and rss_state == "legacy_or_unrecorded":
                    language_state += "; source availability is unknown for this older audit"
                st.write(f"**Declared language:** {language_state}")
                claims = identity.get("structured_creator_locations", []) + identity.get("structured_subject_locations", []) + identity.get("publisher_location_claims", [])
                if claims:
                    st.dataframe(pd.DataFrame(claims), hide_index=True, alt="Geographic claims saved for this podcast")
                language = identity.get("language", {})
                st.write(f"**Declared language:** {language.get('declared') or 'Not recorded'}")
                st.write(f"**Episode language evidence:** {language.get('episode_observation', {}).get('status', 'Unavailable')}")

elif section == "Website health":
    st.header("Website health")
    st.write("Website reachability checks are separate from reviewing the data fields. Opening this dashboard never sends a request to a publisher website.")
    _render_website_coverage(summary)
    outcomes = data.get_url_outcomes(db_path, selected_run)
    if summary.get("website_check_status") == "partial":
        st.warning(f"{summary.get('website_check_unexpected_errors', 0)} checker error(s) were recorded as inconclusive. Other sites may have been skipped at the request or stage limit.")
    if not outcomes:
        st.caption("No outcome chart is shown without saved result rows.")
    else:
        outcome_rows = [{"Website check result": CLASSIFICATION_LABELS.get(row["classification"], row["classification"].replace("_", " ").title()), "Record results": row["record_count"], "Share of result rows (%)": row["checked_pct"]} for row in outcomes]
        st.bar_chart(pd.DataFrame(outcome_rows), x="Website check result", y="Record results", horizontal=True, sort="-Record results", alt="Saved per-record website results by outcome")
        st.dataframe(pd.DataFrame(outcome_rows), hide_index=True, alt="Website outcomes and their share of saved per-record result rows")
        status_rows = data.get_http_status_distribution(db_path, selected_run)
        if status_rows:
            st.subheader("Server responses")
            friendly_status = [{"Response": "No response" if row["status_bucket"] == "no_http_response" else f"HTTP {row['status_bucket']}", "Record results": row["check_count"], "Share of result rows (%)": row["checked_pct"]} for row in status_rows]
            st.bar_chart(pd.DataFrame(friendly_status), x="Response", y="Record results", horizontal=True, alt="HTTP status codes and per-record checks without a response")
        redirects = data.get_redirect_summary(db_path, selected_run)
        st.subheader("Redirects")
        with st.container(horizontal=True):
            st.metric("Checks with redirects", redirects["records_with_redirects"], help="Saved checks that followed at least one validated redirect. A redirect alone is not a failure.")
            st.metric("Redirect steps", redirects["redirect_hops"], help="Total recorded hops across checked websites.")
            st.metric("Average steps per check", round(redirects["avg_redirect_hops"], 2) if redirects["avg_redirect_hops"] is not None else "—", help="Includes checks with no redirects.")
        destinations = data.get_redirect_destinations(db_path, selected_run)
        if destinations:
            st.dataframe(pd.DataFrame(destinations).rename(columns={"destination_url": "Redirect destination", "observed_hops": "Times recorded"}), hide_index=True, alt="Recorded redirect destinations")
        checks = data.get_url_check_rows(db_path, selected_run)
        display_checks = []
        for row in checks:
            display_checks.append({"Podcast": row["title"] or "Untitled podcast", "Podcast ID": row["feed_id"] or "Not recorded", "Website": row["original_url"], "Result": CLASSIFICATION_LABELS.get(row["classification"], row["classification"]), "HTTP status": row["http_status_code"] if row["http_status_code"] is not None else "No response", "Redirects": row["redirect_count"] or 0})
        st.subheader("Checked websites")
        st.dataframe(pd.DataFrame(display_checks), hide_index=True, alt="Saved website checks for the selected audit")
        with st.expander("Technical details"):
            st.caption("Timeout, connection, safety, and HTTP evidence are stored with each result. An inconclusive check is not treated as proof that a website is broken.")
            detail_rows = [{"Podcast": r["title"], "Original website": r["original_url"], "Final website": r["final_url"] or "Not reached", "Evidence": r["evidence"], "Reason code": r["error_reason"] or "—"} for r in checks]
            if detail_rows:
                st.dataframe(pd.DataFrame(detail_rows), hide_index=True, alt="Technical evidence for saved website checks")

elif section == "Geo intelligence":
    st.header("Geo intelligence")
    st.write("This view reports explicit metadata claims and repeated language evidence. It does not infer podcast origin from language, website domains, or the Apple storefront.")
    geo = data.get_geo_intelligence(db_path, selected_run)
    if not geo:
        st.info("No geographic observations were saved for this audit. Run a new audit to build evidence from available metadata.")
    else:
        storefront = geo.get("storefront_context", {})
        st.caption(f"Apple storefront: {storefront.get('value', 'Not recorded')} — {storefront.get('source', 'source not recorded')}. This identifies the catalog searched only.")
        records_geo = geo.get("records", [])
        coverage = geo.get("evidence_coverage") or summarize_geo_evidence(records_geo)
        metric_columns = st.columns(4)
        metric_columns[0].metric("Podcasts analyzed", coverage.get("podcasts_analyzed", len(records_geo)))
        metric_columns[1].metric("RSS checked", coverage.get("rss_checked", 0), help=f"{coverage.get('rss_unavailable', 0)} unavailable; legacy audits may not record RSS coverage.")
        metric_columns[2].metric("Creator locations", coverage.get("creator_location_claims", 0), help=f"{coverage.get('creator_location_no_claim', 0)} checked with no claim; {coverage.get('creator_location_unavailable', 0)} unavailable/legacy; {coverage.get('creator_location_extraction_failed', 0)} extraction failures.")
        metric_columns[3].metric("Subject locations", coverage.get("subject_location_claims", 0), help=f"{coverage.get('subject_location_no_claim', 0)} checked with no claim; {coverage.get('subject_location_unavailable', 0)} unavailable/legacy; {coverage.get('subject_location_extraction_failed', 0)} extraction failures.")
        metric_columns = st.columns(3)
        metric_columns[0].metric("Declared languages", coverage.get("declared_language_claims", 0), help=f"{coverage.get('declared_language_no_claim', 0)} checked with no declaration; {coverage.get('declared_language_unavailable', 0)} unavailable/legacy.")
        metric_columns[1].metric("Potential conflicts", coverage.get("potential_conflicts", 0), help="Only comparable, explicit country claims are counted.")
        metric_columns[2].metric("RSS unavailable", coverage.get("rss_unavailable", 0))
        identity_rows = []
        language_rows = []
        for item in records_geo:
            identity = item.get("identity", {})
            language = identity.get("language", {})
            episode = language.get("episode_observation", {})
            location = identity.get("publisher_location_claims", [])
            audience = identity.get("intended_audience_claims", [])
            for claim in identity.get("structured_creator_locations", []) + identity.get("structured_subject_locations", []) + location + audience:
                identity_rows.append({
                    "Podcast": item.get("title", "Untitled podcast"),
                    "Claim type": claim.get("type", claim.get("rel", "explicit text")),
                    "Location": claim.get("location") or claim.get("country_name") or "Text claim",
                    "Country": claim.get("country_name") or claim.get("country_code") or "Not supplied/invalid",
                    "Source": claim.get("source", "Source provenance unavailable"),
                    "Confidence": claim.get("confidence", "publisher-provided; unverified"),
                    "Observed": claim.get("observed_at") or "Historical timestamp unavailable",
                })
            language_rows.append({
                "Podcast": item.get("title", "Untitled podcast"),
                "Declared language": language.get("declared") or "Not recorded",
                "Episode language observation": ", ".join(episode.get("languages", [])) or episode.get("status", "undetermined").replace("_", " ").title(),
                "Usable episode samples": episode.get("usable_episode_samples", episode.get("episode_samples_seen", 0)),
                "Evidence sufficient": "Yes" if episode.get("evidence_sufficient", episode.get("status") in {"identified", "multilingual"}) else "No",
                "Evidence": episode.get("evidence_source", "No episode language evidence"),
            })
        if identity_rows:
            st.subheader("Geographic claims found")
            st.caption("Creator locations describe publisher-declared production places; subject locations describe content focus. Neither establishes podcast origin. Country codes are validated, but claims remain unverified.")
            st.dataframe(pd.DataFrame(identity_rows), hide_index=True, alt="Publisher-provided geographic claims with provenance and confidence")
        else:
            st.info("No structured location or explicit geographic text claims were found in the available evidence.")
        conflicts = geo.get("identity_conflicts", [])
        if conflicts:
            st.subheader("Evidence conflicts to review")
            st.warning("These are conflicting source claims, not verified errors.")
            st.dataframe(pd.DataFrame([{"Podcast": item.get("title"), "Evidence": item.get("geographic_identity_conflict", {}).get("evidence"), "Creator claims": item.get("identity", {}).get("structured_creator_locations", []), "Publisher country": item.get("identity", {}).get("declared_publisher_country", {}).get("value")} for item in conflicts]), hide_index=True, alt="Potential geographic identity conflicts with comparable source evidence")
        else:
            st.info("No supported geographic identity conflict was found in the available records.")
        if language_rows:
            st.subheader("Language intelligence")
            st.dataframe(pd.DataFrame(language_rows), hide_index=True, alt="Declared language compared with RSS episode language evidence")
        inconsistencies = geo.get("language_inconsistencies", [])
        if inconsistencies:
            st.warning("Some repeated episode-language observations differ from declared metadata; review the source before changing it.")
            st.dataframe(pd.DataFrame([{"Podcast": item.get("title"), "Evidence": item.get("language_inconsistency", {}).get("evidence")} for item in inconsistencies]), hide_index=True, alt="Potential declared and observed language differences")
        st.subheader("Confirmed changes")
        drift = geo.get("drift", [])
        changes = [row for row in drift if row.get("change_classification") == "changed"]
        if changes:
            st.dataframe(pd.DataFrame([{
                "Podcast": row.get("title"), "Field": row.get("field", "").replace("_", " ").title(),
                "Previous": row.get("previous_value") if row.get("previous_value") not in (None, [], "") else "Not recorded",
                "Current": row.get("current_value") if row.get("current_value") not in (None, [], "") else "Not recorded",
                "First observation": row.get("first_observation"), "Latest observation": row.get("latest_observation"),
            } for row in changes]), hide_index=True, alt="Geographic and language values that differ from prior available evidence")
        else:
            st.info("No values differ from prior available evidence in this audit.")
        first_observations = [row for row in drift if row.get("change_classification") == "first_observation"]
        if first_observations:
            with st.expander(f"First observations · {len(first_observations)}"):
                st.dataframe(pd.DataFrame([{
                    "Podcast": row.get("title"), "Field": row.get("field", "").replace("_", " ").title(),
                    "Observed value": row.get("current_value"), "First observed": row.get("first_observation"),
                } for row in first_observations]), hide_index=True, alt="Values seen for the first time, not classified as changes")
        unavailable = [row for row in drift if row.get("change_classification") == "missing_or_unavailable"]
        if unavailable:
            with st.expander(f"Previously observed, missing now · {len(unavailable)}"):
                st.caption("A missing observation is not evidence that the value changed.")
                st.dataframe(pd.DataFrame([{
                    "Podcast": row.get("title"), "Field": row.get("field", "").replace("_", " ").title(),
                    "Last known value": row.get("previous_value"), "Last observed": row.get("latest_observation"),
                } for row in unavailable]), hide_index=True, alt="Prior values with no current comparable observation")
        st.caption("Historical comparisons use prior SQLite snapshots. First observations and missing evidence are not counted as value changes.")
        st.subheader("Regional website reliability")
        regional = geo.get("regional_website_reliability", {})
        observations = regional.get("observations", [])
        if observations:
            st.dataframe(pd.DataFrame(observations), hide_index=True, alt="Verified regional website reliability observations")
        else:
            st.info("Not tested. No authorized regional probe with a verified exit country is configured. The standard audit runner's location is not treated as a country observation.")
            st.caption("A future probe observation must include: " + ", ".join(regional.get("required_observation_fields", [])))

elif section == "History":
    st.header("Audit history")
    st.write("Use rates with the record count beside each audit; a percentage can move simply because the collection size changed.")
    metrics = data.get_history_metrics(db_path)
    source_by_run = {row["audit_run_id"]: row.get("source_label", "Source not recorded") for row in runs}
    history_rows = [{
        "Started": _audit_date(row["started_at"]),
        "Status": STATUS_LABELS.get(row["run_status"], row["run_status"]),
        "Records analyzed": row["stored_records"],
        "Error rate (%)": row["actionable_error_rate_pct"],
    } for row in metrics]
    if history_rows:
        st.dataframe(pd.DataFrame(history_rows), hide_index=True, alt="Audit history with execution status, sample size, and error rate")
        st.caption("Open an audit below for its source, finding counts, website results, and full ID.")
        for row in metrics:
            run_id = row["audit_run_id"]
            with st.expander(_audit_choice_label(row)):
                details = [
                    {"Detail": "Source", "Value": str(source_by_run.get(run_id) or "Source not recorded")},
                    {"Detail": "Records analyzed", "Value": str(row["stored_records"])},
                    {"Detail": "Records with errors", "Value": str(row["records_with_actionable_errors"])},
                    {"Detail": "Error rate", "Value": f"{row['actionable_error_rate_pct']}%" if row["actionable_error_rate_pct"] is not None else "Not available"},
                    {"Detail": "Completeness notes", "Value": str(row["completeness_issue_occurrences"])},
                    {"Detail": "Other warnings", "Value": str(row["other_warning_occurrences"])},
                    {"Detail": "Website result rows", "Value": str(row["url_checks"])},
                    {"Detail": "Audit ID", "Value": run_id},
                ]
                st.dataframe(pd.DataFrame(details), hide_index=True, alt=f"Detailed saved metrics for audit {_audit_choice_label(row)}")
    if len(metrics) >= 2:
        frame = pd.DataFrame(metrics)
        frame["Audit date"] = pd.to_datetime(frame["started_at"], utc=True)
        eligible = frame[frame["run_status"].isin({"succeeded", "partial"})]
        if len(eligible) >= 2:
            eligible = eligible.sort_values(["Audit date", "audit_run_id"]).set_index("Audit date")
            st.subheader("Actionable error rate over time")
            st.line_chart(eligible[["actionable_error_rate_pct"]].rename(columns={"actionable_error_rate_pct": "Records with error-severity findings (%)"}), alt="Actionable error rate per completed or partially completed audit")
            st.caption("Denominator: podcast records saved in each audit. The history table shows each sample size. File-level errors are separate; failed and in-progress runs are excluded from trends.")
            st.subheader("Collection size by audit")
            st.bar_chart(eligible[["stored_records"]].rename(columns={"stored_records": "Podcasts analyzed"}), alt="Saved podcast record count for each completed or partially completed audit")
        else:
            st.info("At least two completed or partially completed audits are needed for a trend. Failed and in-progress runs remain in the history table but are excluded from trend calculations.")
    else:
        st.info(f"A trend needs at least two saved audits. This database contains {len(metrics)} audit.")
    st.subheader("Compare two audits")
    if len(run_ids) < 2:
        st.info("Save a second audit to compare podcast IDs and website results across snapshots.")
    else:
        a, b = st.columns(2)
        earlier_id = a.selectbox("Earlier audit", run_ids, index=min(1, len(run_ids) - 1), format_func=lambda value: run_labels[value], key="earlier_audit")
        later_id = b.selectbox("Later audit", run_ids, index=0, format_func=lambda value: run_labels[value], key="later_audit")
        if earlier_id == later_id:
            st.warning("Choose two different audits to compare.")
        else:
            comparison = data.compare_runs(db_path, earlier_id, later_id)
            with st.container(horizontal=True):
                st.metric("IDs in both", len(comparison["present_in_both"]), help="Only IDs that are unique in both selected audits are matched.")
                st.metric("New IDs", len(comparison["newly_observed"]))
                st.metric("Not in later audit", len(comparison["absent_from_later"]))
                st.metric("Ambiguous IDs", len(comparison["ambiguous_identifiers"]))
            if comparison["present_in_both"]:
                matched = [{"Podcast ID": row["feed_id"], "Earlier title": row["earlier"]["title"], "Later title": row["later"]["title"], "Website result changed": "Yes" if row["url_outcome_changed"] else "No" if row["url_outcome_changed"] is False else "Not checked in both"} for row in comparison["present_in_both"]]
                st.dataframe(pd.DataFrame(matched), hide_index=True, alt="Podcast records uniquely matched between selected audits")
            with st.expander("New podcasts"):
                _show_records_table(comparison["newly_observed"], key_prefix="new_ids")
            with st.expander("Podcasts absent from the later audit"):
                _show_records_table(comparison["absent_from_later"], key_prefix="absent_ids")
            with st.expander("IDs that are repeated and cannot be matched"):
                _show_records_table(comparison["ambiguous_identifiers"], key_prefix="ambiguous_ids")
            with st.expander("Records without a podcast ID"):
                _show_records_table(comparison["unidentified_records"], key_prefix="unidentified")
