# Data Volumes

The backend keeps durable files under `/app/data/*`. These mounts must persist across container recreation. They are mounted into the backend only (the git volume is never handed to agent containers).

| Mount | Setting | Holds |
|-------|---------|-------|
| `/app/data/uploads` | `UPLOAD_BASE_PATH` | Uploaded files |
| `/app/data/app-data` | `HOST_APP_DATA_DIR` (host side) | Per-user agent App Data |
| `/app/data/bundles` | `BUNDLE_STORAGE_DIR` | Agent bundle revisions, see [Agent Bundles](../agents/agent_bundles/agent_bundles.md) |
| `/app/data/skills` | `HOST_SKILL_STORAGE_DIR` (host side) | Skill packages |
| `/app/data/git` | `GIT_STORAGE_DIR` | Bare git repositories of desktop agents, see [Desktop Agents in the Catalog](../application/catalog/desktop_agents.md) |
| `/app/data/artifacts` | `ARTIFACT_STORAGE_DIR` (host side `HOST_ARTIFACT_STORAGE_DIR`, default `./backend/data/artifacts`) | Published artifact revisions (`<artifact_id>/<revision_id>/...`, cached `<revision_id>.zip`, ingest scratch in `.tmp/`), see [Artifacts](../application/artifacts/artifacts.md) |

**Cleanup.** Deleting a bundle removes `data/bundles/<bundle_id>`. Deleting a user removes their environment instances and containers, `data/app-data/<user_id>` and `uploads/<user_id>` (background, after commit, via the guarded helper `backend/app/services/common/data_dirs.py`, orchestrated by `backend/app/services/users/user_data_cleanup_service.py`). Known limit: a re-publish from another worker between the delete commit and folder removal can lose files. Agent App Data is kept on uninstall (garbage-collected after a day).

**Artifacts.** The files ARE the artifact (Postgres holds only the manifest), so the mount must persist and, with several backend replicas, be shared. Deleting an artifact or revision removes its folder; deleting a user removes every artifact folder they owned (via `user_data_cleanup_service.py`). Stale `.tmp/` scratch is swept after an hour.

**Dev server.** `docker-compose.override.yml` runs the backend as `uvicorn app.main:app --reload --reload-dir app`, so writes under `data/` do not restart it.

Host paths come from `docker-compose.yml` (for git: `HOST_GIT_STORAGE_DIR`, default `./backend/data/git`).

## Git volume specifics

- Layout: `<repo_id>.git` per desktop agent; `.deleted/<repo_id>-<utc>` for removed ones, purged by the operator.
- The repos ARE the content. Postgres holds only metadata, grants and a cached `head_sha`; losing the directory loses every shared agent.
- **Multi-replica**: `GIT_STORAGE_DIR` (like `BUNDLE_STORAGE_DIR`) must be a shared volume visible to every backend replica.

## Backup

There is no automated backup. Back up each mount while the backend is stopped, or for git repos run `git bundle create <file> --all` per repository. After a restore, Postgres `head_sha` reconciles on the next accepted push.
