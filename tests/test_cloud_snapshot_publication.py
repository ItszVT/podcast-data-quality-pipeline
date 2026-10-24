from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


SNAPSHOT_FILE = "data/real_podcast_audits.sqlite"
WORKFLOW_FILE = Path(__file__).parents[1] / ".github/workflows/cloud-daily-audit.yml"
VERIFY_WORKFLOW_FILE = Path(__file__).parents[1] / ".github/workflows/verify-podcast-collection.yml"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def staged_files(repo: Path) -> list[str]:
    output = git(repo, "diff", "--cached", "--name-only")
    return output.splitlines() if output else []


def assert_snapshot_is_only_staged_file(repo: Path) -> None:
    files = staged_files(repo)
    if files != [SNAPSHOT_FILE]:
        raise AssertionError("The staged publication is not limited to the SQLite snapshot.")


def initialize_source_repo(repo: Path) -> None:
    git(repo, "init", "--quiet", "--initial-branch=master")
    git(repo, "config", "user.name", "Snapshot Test")
    git(repo, "config", "user.email", "snapshot-test@example.invalid")
    (repo / "pipeline.py").write_text("pipeline source\n", encoding="utf-8")
    snapshot = repo / SNAPSHOT_FILE
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"previous database")
    git(repo, "add", "pipeline.py", SNAPSHOT_FILE)
    git(repo, "commit", "--quiet", "-m", "source branch")


def publish_first_snapshot(repo: Path, preserved_snapshot: Path) -> None:
    (repo / SNAPSHOT_FILE).unlink()
    git(repo, "checkout", "--orphan", "audit-snapshots")
    git(repo, "read-tree", "--empty")
    snapshot = repo / SNAPSHOT_FILE
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(preserved_snapshot.read_bytes())
    git(repo, "add", "-f", "--", SNAPSHOT_FILE)
    assert_snapshot_is_only_staged_file(repo)
    git(repo, "commit", "--quiet", "-m", "first snapshot")


def test_first_and_subsequent_publications_track_only_snapshot(tmp_path: Path):
    workflow = WORKFLOW_FILE.read_text(encoding="utf-8")
    orphan_checkout = workflow.index('git checkout --orphan "$SNAPSHOT_BRANCH"')
    empty_index = workflow.index("git read-tree --empty", orphan_checkout)
    assert empty_index > orphan_checkout

    repo = tmp_path / "repo"
    repo.mkdir()
    initialize_source_repo(repo)
    # This copy stands in for the validated snapshot preserved under RUNNER_TEMP.
    preserved_snapshot = tmp_path / "publish-audits.sqlite"
    preserved_snapshot.write_bytes(b"first validated database")

    publish_first_snapshot(repo, preserved_snapshot)
    assert git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == [
        SNAPSHOT_FILE
    ]
    assert (repo / SNAPSHOT_FILE).read_bytes() == b"first validated database"

    first_snapshot_commit = git(repo, "rev-parse", "HEAD")
    # A later workflow run starts from a clean checkout of the source branch.
    git(repo, "clean", "-fd")
    git(repo, "checkout", "--quiet", "master")
    git(repo, "checkout", "--quiet", "--force", "-B", "audit-snapshots", first_snapshot_commit)
    (repo / SNAPSHOT_FILE).write_bytes(b"updated validated database")
    git(repo, "add", "-f", "--", SNAPSHOT_FILE)
    assert_snapshot_is_only_staged_file(repo)
    git(repo, "commit", "--quiet", "-m", "subsequent snapshot")

    assert git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == [
        SNAPSHOT_FILE
    ]
    assert (repo / SNAPSHOT_FILE).read_bytes() == b"updated validated database"


def test_unexpected_staged_file_rejects_publication(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    initialize_source_repo(repo)
    preserved_snapshot = tmp_path / "publish-audits.sqlite"
    preserved_snapshot.write_bytes(b"validated database")
    publish_first_snapshot(repo, preserved_snapshot)

    (repo / "unexpected.txt").write_text("must not publish", encoding="utf-8")
    git(repo, "add", "unexpected.txt")
    with pytest.raises(
        AssertionError, match="not limited to the SQLite snapshot"
    ):
        assert_snapshot_is_only_staged_file(repo)


def test_collection_verification_reads_run_scoped_report_and_rejects_missing_diagnostics():
    workflow = VERIFY_WORKFLOW_FILE.read_text(encoding="utf-8")

    assert '"SELECT metadata_json FROM audit_runs WHERE audit_run_id = ?"' in workflow
    assert 'parsed_metadata.get("collection_report")' in workflow
    assert 'result.get("collection_report")' not in workflow
    assert "missing or incomplete in this audit's SQLite metadata" in workflow
    assert 'return str(count) if count is not None else "unavailable"' in workflow
    assert "and diagnostics_complete" in workflow
    assert 'report.get("rss_response_size_diagnostics")' in workflow
    assert "declared Content-Length status (declaration, not verified transfer size)" in workflow
    assert "RSS response-size diagnostics: unavailable or inconsistent" in workflow
    assert "contents: read" in workflow
    assert "workflow_dispatch:" in workflow
    assert "git push" not in workflow

