# Data Volumes

The backend keeps durable files under `/app/data/*`. These mounts must persist across container recreation. They are mounted into the backend only (the git volume is never handed to agent containers).

| Mount | Setting | Holds |
|-------|---------|-------|
| `/app/data/uploads` | `UPLOAD_BASE_PATH` | Uploaded files |
| `/app/data/app-data` | `HOST_APP_DATA_DIR` (host side) | Per-user agent App Data |
| `/app/data/bundles` | `BUNDLE_STORAGE_DIR` | Agent bundle revisions, see [Agent Bundles](../agents/agent_bundles/agent_bundles.md) |
| `/app/data/skills` | `HOST_SKILL_STORAGE_DIR` (host side) | Skill packages |
| `/app/data/git` | `GIT_STORAGE_DIR` | Team library bare repositories, see [Team Library](../application/team_library/team_library.md) |

Host paths come from `docker-compose.yml` (for git: `HOST_GIT_STORAGE_DIR`, default `./backend/data/git`).

## Git volume specifics

- Layout: `<repo_id>.git` per library entry; `.deleted/<repo_id>-<utc>` for removed ones, purged by the operator.
- The repos ARE the content. Postgres holds only metadata, grants and a cached `head_sha`; losing the directory loses every shared agent.
- **Multi-replica**: `GIT_STORAGE_DIR` (like `BUNDLE_STORAGE_DIR`) must be a shared volume visible to every backend replica.

## Backup

There is no automated backup. Back up each mount while the backend is stopped, or for git repos run `git bundle create <file> --all` per repository. After a restore, Postgres `head_sha` reconciles on the next accepted push.
