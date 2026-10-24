"""Read-only, plain-language Streamlit view of saved podcast audit snapshots."""
from __future__ import annotations

from importlib import reload

import pandas as pd
import streamlit as st

from src import dashboard_data as data
from src.published_snapshot import SnapshotValidationError, validate_publishable_snapshot
from src.geo_intelligence import summarize_geo_evidence

# The database access module is project code; reload it with the app so changes
# to its read-only queries are available after Streamlit hot reloads this file.
data = reload(data)

st.set_page_config(page_title="Podcast Data Quality", layout="wide")

SECTIONS = ["Overview", "What needs attention", "Podcast records", "Website checks", "Geo Intelligence", "Audit history", "Evaluation"]
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
        parsed = pd.to_datetime(value, utc=True)
        return parsed.strftime("%b %d, %Y · %I:%M %p UTC")
    except (TypeError, ValueError):
        return value


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


def _render_findings(database_path: str | bytes, run_id: str) -> list[dict]:
    findings = data.get_issue_field_counts(database_path, run_id)
    if not findings:
        st.success("No data quality findings were saved for this audit.", icon=":material/check_circle:")
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
    st.caption("A record may have more than one finding. Warnings describe incomplete or untidy data; they do not by themselves make a record invalid.")
    for index, issue in enumerate(findings):
        name = _finding_label(issue)
        count = issue["affected_record_count"]
        suffix = "record" if count == 1 else "records"
        with st.expander(f"{name} · {count} {suffix}", key=f"finding_{run_id}_{index}"):
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
                _show_records_table(affected, key_prefix=f"finding_{run_id}_{index}")
            else:
                st.caption("This finding applies to the input file rather than an individual podcast record.")
    file_issues = data.get_schema_issues(database_path, run_id)
    if file_issues:
        st.subheader("Input file findings")
        file_rows = [{"Finding": FINDING_LABELS.get(r["category"], r["category"].replace("_", " ").title()), "Field": _field_label(r.get("field")), "Detail": r["message"], "Priority": _severity_label(r["severity"])} for r in file_issues]
        st.dataframe(pd.DataFrame(file_rows), hide_index=True, alt="Findings that apply to the input file")
    return findings


with st.sidebar:
    st.markdown("### Podcast data")
    dashboard_settings = st.secrets.get("dashboard", {})
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
        row["audit_run_id"]: f"{_audit_date(row['started_at'])} · {row['input_filename']} · {STATUS_LABELS.get(row['run_status'], row['run_status'])}"
        for row in runs
    }
    run_ids = list(run_labels)
    selected_run = st.selectbox("Selected audit", run_ids, format_func=lambda value: run_labels[value], key="selected_audit")

try:
    summary = data.get_run_summary(db_path, selected_run)
    history = data.get_history_summary(db_path)
except data.DashboardDataError as exc:
    st.error(str(exc), icon=":material/database_off:")
    st.stop()
if summary is None:
    st.error("The selected audit is no longer available. Refresh saved data and choose another audit.")
    st.stop()

st.title("Podcast Data Quality")
st.markdown("A clearer view of your podcast data—what looks good, what needs attention, and what to fix next.")
status = STATUS_LABELS.get(summary["run_status"], summary["run_status"].replace("_", " ").title())
status_color = {"Completed": "green", "Partially completed": "orange", "Failed": "red", "In progress": "blue"}.get(status, "gray")
st.badge(f"Audit status · {status}", color=status_color, help="Status saved by the audit process.")
st.caption(f"File reviewed: {summary['input_filename']} · Audit date: {_audit_date(summary['started_at'])} · Saved snapshot, shown read-only")
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
    st.subheader("At a glance")
    with st.container(horizontal=True):
        st.metric("Records reviewed", summary["stored_records"], help="Podcast records saved in this audit. Each source record is retained.")
        st.metric("Records needing attention", summary["records_with_issues"], help="Distinct records with one or more saved findings. A warning does not automatically mean the record is invalid.")
        st.metric("Total findings", summary["issue_occurrences"], help="Count of saved finding occurrences; one record can have several.")
        st.metric("Missing publisher websites", summary["missing_website_urls"], help="Records with a blank website value. Podcast feed links are not counted as publisher websites.")
        st.metric("Website checks completed", summary["url_check_count"], help="Only saved website-check results are counted. No checks are run from this dashboard.")
    st.subheader("What needs attention?")
    st.write("Findings include both action items and completeness notes. Review the priority and affected records before deciding what to change.")
    _render_findings(db_path, selected_run)
    st.subheader("Website checks")
    if summary.get("website_check_status") == "partial":
        st.warning(f"Website checking completed partially. {summary.get('website_check_unique_urls', 0)} unique URLs were checked; additional URLs may have been skipped after reaching the request limit or encountering checker errors. Review saved outcomes in Website checks.")
    if summary["url_check_count"] == 0:
        if summary.get("website_check_status") == "not_requested":
            st.info("**No websites checked yet**\n\nWebsite checks were not requested for this audit.", icon=":material/info:")
        else:
            st.warning("Website checks were requested, but no results were saved.")
    else:
        st.caption(f"{summary['url_check_count']} saved website checks are available. Open Website checks for results and details.")

elif section == "What needs attention":
    st.header("What needs attention?")
    st.write("Each item below comes from the selected saved audit. A warning records an issue under the current rules; it does not mean the podcast is broken or invalid.")
    _render_findings(db_path, selected_run)

elif section == "Podcast records":
    st.header("Podcast records")
    categories = data.get_issue_categories(db_path, selected_run)
    options = ["All records", "Records with findings", "Records without findings", "Missing publisher websites", *[f"Finding: {FINDING_LABELS.get(category, category.replace('_', ' ').title())}" for category in categories]]
    filter_label = st.selectbox("Show", options, key="record_filter")
    search = st.text_input("Search podcast, ID, or publisher website", key="record_search")
    issue_category = None
    if filter_label == "All records":
        mode = "all"
    elif filter_label == "Records with findings":
        mode = "with_findings"
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

elif section == "Website checks":
    st.header("Website checks")
    st.write("Website reachability checks are separate from reviewing the data fields. Opening this dashboard never sends a request to a publisher website.")
    outcomes = data.get_url_outcomes(db_path, selected_run)
    if summary.get("website_check_status") == "partial":
        st.warning(f"Website checking completed partially. {summary.get('website_check_unique_urls', 0)} unique URLs were checked; {summary.get('website_check_unexpected_errors', 0)} checker error(s) were safely recorded as inconclusive. Additional URLs may have been skipped after reaching the request limit or stage deadline.")
    if not outcomes:
        if summary.get("website_check_status") == "not_requested":
            st.info("**No websites checked yet**\n\nWebsite checks were not requested for this audit.", icon=":material/info:")
        else:
            st.warning("Website checks were requested, but no results were saved. This can happen when there are no eligible website URLs.")
    else:
        outcome_rows = [{"Website check result": CLASSIFICATION_LABELS.get(row["classification"], row["classification"].replace("_", " ").title()), "Checks": row["record_count"], "Share of checks (%)": row["checked_pct"]} for row in outcomes]
        st.bar_chart(pd.DataFrame(outcome_rows), x="Website check result", y="Checks", horizontal=True, sort="-Checks", alt="Saved website check results by outcome")
        st.dataframe(pd.DataFrame(outcome_rows), hide_index=True, alt="Website check results and their share of saved checks")
        status_rows = data.get_http_status_distribution(db_path, selected_run)
        if status_rows:
            st.subheader("Server responses")
            friendly_status = [{"Response": "No response" if row["status_bucket"] == "no_http_response" else f"HTTP {row['status_bucket']}", "Checks": row["check_count"], "Share of checks (%)": row["checked_pct"]} for row in status_rows]
            st.bar_chart(pd.DataFrame(friendly_status), x="Response", y="Checks", horizontal=True, alt="HTTP status codes and checks without a response")
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

elif section == "Geo Intelligence":
    st.header("Geo Intelligence")
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
            st.subheader("Identity conflicts to review")
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
        st.subheader("Geographic and language changes")
        drift = geo.get("drift", [])
        changes = [row for row in drift if row.get("change_classification") in {"changed", "missing_or_unavailable", "first_observation"}]
        if changes:
            labels = {"changed": "Changed", "missing_or_unavailable": "Missing or unavailable now", "first_observation": "First observation"}
            st.dataframe(pd.DataFrame([{
                "Podcast": row.get("title"), "Field": row.get("field", "").replace("_", " ").title(),
                "Previous": row.get("previous_value") if row.get("previous_value") not in (None, [], "") else "Not recorded",
                "Current": row.get("current_value") if row.get("current_value") not in (None, [], "") else "Not recorded",
                "Change": labels.get(row.get("change_classification"), "Not observed"),
                "First observation": row.get("first_observation"), "Latest observation": row.get("latest_observation"),
            } for row in changes]), hide_index=True, alt="Historical changes in geographic and language metadata")
        else:
            st.info("No supported changes are available yet. Missing data is not counted as a change.")
        st.subheader("Regional website reliability")
        regional = geo.get("regional_website_reliability", {})
        observations = regional.get("observations", [])
        if observations:
            st.dataframe(pd.DataFrame(observations), hide_index=True, alt="Verified regional website reliability observations")
        else:
            st.info("Not tested. No authorized regional probe with a verified exit country is configured. The standard audit runner's location is not treated as a country observation.")
            st.caption("A future probe observation must include: " + ", ".join(regional.get("required_observation_fields", [])))

elif section == "Audit history":
    st.header("Audit history")
    st.write("Each audit is a saved snapshot of one input file. Results from earlier audits remain available for comparison.")
    history_rows = []
    summary_by_run = {}
    for run in runs:
        run_summary = data.get_run_summary(db_path, run["audit_run_id"])
        if not run_summary:
            continue
        summary_by_run[run["audit_run_id"]] = run_summary
        history_rows.append({
            "Audit date": _audit_date(run["started_at"]), "File reviewed": run["input_filename"],
            "Records reviewed": run_summary["stored_records"], "Records needing attention": run_summary["records_with_issues"],
            "Total findings": run_summary["issue_occurrences"], "Audit status": STATUS_LABELS.get(run["run_status"], run["run_status"]),
            "Audit ID": run["audit_run_id"][:8],
        })
    st.dataframe(pd.DataFrame(history_rows), hide_index=True, alt="Saved audits and their record and finding totals")
    if len(history_rows) >= 2:
        metrics = data.get_history_metrics(db_path)
        frame = pd.DataFrame(metrics)
        frame["Audit date"] = pd.to_datetime(frame["started_at"], utc=True)
        frame = frame.sort_values(["Audit date", "audit_run_id"])
        frame = frame.set_index("Audit date")
        chart_frame = frame[["records_with_issues", "stored_records"]].rename(columns={"records_with_issues": "Records needing attention", "stored_records": "Records reviewed"})
        st.subheader("Records across saved audits")
        st.line_chart(chart_frame, alt="Records reviewed and records needing attention across saved audits")
        st.caption("These are saved snapshot totals. They do not explain why a count changed.")
    else:
        st.info(f"A trend needs at least two saved audits. This database contains {len(history_rows)} audit, so no trend chart is shown.")
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

else:
    st.header("How was this tested?")
    st.write("The project has automated software tests and a small controlled reference set. These are different from checking whether real publisher websites can be reached.")
    st.subheader("Automated software tests")
    st.write("Tests check the pipeline's reading, validation, URL-checking safety rules, database behavior, SQL queries, and dashboard data views. Most network behavior is mocked or tested on a local fixture server.")
    st.subheader("Controlled reference cases")
    reference_file = data.PROJECT_ROOT / "data" / "reference_url_outcomes.csv"
    if reference_file.is_file():
        reference = pd.read_csv(reference_file)
        st.caption(f"{len(reference)} labeled local cases cover selected known outcomes. They are designed branch examples, not a representative sample of the web.")
        case_rows = [{"Case": row["case_id"].replace("_", " ").title(), "Expected result": CLASSIFICATION_LABELS.get(row["expected_classification"], row["expected_classification"])} for _, row in reference.iterrows()]
        st.dataframe(pd.DataFrame(case_rows), hide_index=True, alt="Controlled local reference cases and their expected outcomes")
    else:
        st.info("The controlled reference file is not present in this project folder.")
    st.subheader("Real-world website checks")
    st.write("The selected audit contains the number of saved website checks shown in the overview. These results are observations from that audit only; they do not establish broad classification accuracy.")
    st.warning("Evaluation metrics are not saved in the database, so this dashboard does not display an accuracy score or confusion matrix. No evaluation is run by opening this page.")
    st.code("python -m pytest tests/test_evaluation.py -v", language="powershell")
