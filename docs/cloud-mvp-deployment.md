# Cloud MVP deployment

The cloud MVP keeps SQLite as the audit store. GitHub Actions runs the existing daily pipeline and publishes one versioned SQLite file to the dedicated `audit-snapshots` branch. The Streamlit app reads that branch through GitHub's authenticated Contents API; it does not rely on Community Cloud checking out or syncing the snapshot branch.

## 1. Publish the first snapshot

Merge the workflow and application changes to `master`, then open **Actions → Publish daily podcast audit → Run workflow**. The first successful run initializes `audit-snapshots`; later runs restore its database, append one run, validate it, and publish a new commit. The scheduled run is daily at 13:17 UTC. Manual and scheduled runs are serialized, and the publisher refuses to stage anything except `data/real_podcast_audits.sqlite`.

The workflow needs the repository's default `GITHUB_TOKEN` with `contents: write`. It calls Apple Search/Lookup and the RSS feeds returned by Apple using the existing bounded collector, then runs the bounded publisher website-check stage. Each stage keeps its request, size, and time limits. Failed audits are not published. An RSS-partial audit may be published only when it meets the established partial-publication eligibility thresholds and passes validation, persistence, snapshot-structure, and SQLite integrity checks. Other partial or inconsistent results are rejected. Existing snapshot-branch content other than the single expected database file causes a fail-closed stop.

## 2. Deploy the dashboard

1. Connect the GitHub account to [Streamlit Community Cloud](https://share.streamlit.io/) and authorize access to this private repository. Community Cloud requires repository admin permission to deploy from a private repository.
2. Create a **fine-grained personal access token** limited to this repository, with **Contents: read** permission only. Use a short expiration and rotate it when needed. Do not add it to GitHub repository contents, the snapshot database, workflow output, or a committed secrets file.
3. Create a Community Cloud app from this repository, branch `master`, entrypoint `app.py`, and select Python 3.11 in **Advanced settings**.
4. In the app's **Secrets** settings, add:

   ```toml
   [dashboard]
   mode = "published"

   [github_snapshot]
   token = "<read-only fine-grained token>"
   ```

5. Keep the app private until its published audit output is approved for public viewing. If making it public, anyone who can view the app can see the audit data displayed by it, though the source repository and snapshot branch remain private.

The app retrieves the raw SQLite file over HTTPS from the GitHub Contents API, validates its SQLite signature and project schema, then deserializes it into a read-only in-memory connection. No machine-local path is rendered in published mode. Snapshot data is cached for up to five minutes; **Refresh saved data** clears the cache. Missing snapshots, missing secrets, denied access, and invalid database files produce fixed, actionable messages without displaying tokens, local paths, or raw API errors.

## 3. Verify operation

- Confirm the first workflow run publishes exactly one file to `audit-snapshots`.
- Confirm the Streamlit app shows the latest nonempty real audit and its actual execution status, including partial status when applicable.
- Run the workflow manually a second time and confirm the branch has a new commit and the dashboard history contains both runs.
- Confirm the workflow rejects failed or ineligible partial collection, validation or persistence failure, empty results, SQLite integrity/schema errors, unexpected branch files, or a branch-tip change during the audit. A qualifying RSS-partial audit may publish only after all configured eligibility and integrity checks pass.

The SQLite snapshot is versioned in Git, so each published commit retains the previous database blob in branch history. This is a compact MVP suitable for the current audit volume. Keep the snapshot below GitHub's 100 MB single-file limit; the dashboard applies a stricter 50 MiB download limit. If history approaches that limit, this storage layout will need revisiting.
