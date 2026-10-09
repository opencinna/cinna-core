# Team Library — Technical Reference

## File Locations

- Models: `backend/app/models/library/git_repo.py` (`GitRepo`, `GitRepoGrant`, `GitRepoKind`, `LibraryPrincipalKind`, `LibraryAccess`, API schemas)
- Migration: `backend/app/alembic/versions/0e62eb9df5d4_add_git_repo_tables.py`
- Routes: `backend/app/api/routes/library.py` (`/api/v1/library`), `backend/app/api/routes/git_http.py` (`/git`, hidden from OpenAPI)
- Services in `backend/app/services/library/`: `library_service.py`, `library_access_service.py`, `git_repo_storage.py`, `git_http_service.py`, `push_policy.py`, `library_events.py`, `exceptions.py`
- Hook: `backend/app/git_hooks/pre-receive` (entry) calling `app.services.library.push_policy.main`
- Auth: `resolve_git_http_user` in `backend/app/api/deps.py`
- Events/audit constants: `backend/app/models/events/event.py`, `backend/app/models/events/security_event.py`
- Frontend: `frontend/src/routes/_layout/admin/team-library.tsx`, `frontend/src/components/Admin/TeamLibrary/` (`TeamLibraryTable`, `teamLibraryColumns`, `TeamLibraryRowActions`, `LibraryAccessDialog`, `LibraryGrantRow`, `LibraryPrincipalPicker`, `RemoveLibraryAgentDialog`), menu entry in `frontend/src/components/Sidebar/AdminMenu.tsx`

## Database (migrations `0e62eb9df5d4`, `08bfa6ee8d9f`)

`git_repo`: `id`, `kind` (`agent`/`mirror`, plain string), `slug`, `name`, `description`, `owner_user_id` (FK user, SET NULL), `marketplace_id` (FK `llm_plugin_marketplace`, SET NULL, mirrors only), `default_branch` (`main`), `head_sha` (NULL until first accepted push), `last_pushed_by_user_id` (FK user, SET NULL; who made the last accepted push), `manifest_cache` (JSON projection `{id, name, description, runtime.engine, contract_version}`), `created_at`, `updated_at`. Partial unique index `uq_git_repo_agent_slug` on `slug` where `kind='agent'`.

`git_repo_grant`: `repo_id` (FK CASCADE), `principal_kind` (`user`/`role`/`everyone`), `principal_id` (user uuid text / role value / `*`), `user_id` (FK user CASCADE, user grants only), `access` (`read`/`write`/`maintain`), `granted_by_user_id` (SET NULL), `created_at`. Unique `(repo_id, principal_kind, principal_id)`.

## API (`/api/v1/library`, JWT; errors map via `LibraryError.http_status` 404/403/409/422)

| Route | Notes |
|-------|-------|
| `GET /library` | `LibraryListing {agents, mirrors}`; agents the caller can read, with `access`; `mirrors` always `[]`. Follows grants only, also for superusers |
| `POST /library/agents` | `LibraryAgentCreate {slug, name, description, grants?}`; validates `grants` like the PUT before writing anything, then creates row, bare repo and grants (creator `maintain` + `grants`; the creator named again folds into `maintain`; omitted = creator only) in one transaction; emits `library.granted` to other readers; audit `LIBRARY_AGENT_CREATED` carries the grant labels |
| `GET /library/agents/{repo_id}` | One agent |
| `GET /library/agents/{repo_id}/grants` | Maintainer or superuser |
| `PUT /library/agents/{repo_id}/grants` | Body `list[LibraryGrantIn]`, full replace; validates roles/users, rejects duplicates, requires a person at `maintain`; emits `library.granted` to users who gained access; audit `LIBRARY_GRANTS_CHANGED` |
| `DELETE /library/agents/{repo_id}` | 204; 409 while another person's `AgentGitSource.repo_url` points at the repo; repo dir moves to `.deleted/<repo_id>-<utc>` |
| `GET /library/admin/agents` | Superuser only; `LibraryAgentAdmin` with `owner` and `grants` |

`LibraryAgent.pushed_by` is `{user_id, display_name}` of the last accepted push (set by `LibraryEvents.after_receive`), null before it or once that user is deleted.

Git access (`GitHttpService.authorize`): `read` grant for upload-pack, or any superuser; `write` grant for receive-pack (superusers included); mirrors never take a push.

`LibraryAgent.clone_url` is `<backend_base_url>/git/<repo_id>.git`; `cloud_install_id` is computed from the caller's `AgentGitSource.repo_url` matching it (null in practice until cloud twins). Principal wire shapes: `{"user_id"}`, `{"role"}`, `"everyone"`.

Audit `SecurityEvent` types: `LIBRARY_PUSH_ACCEPTED`, `LIBRARY_GRANTS_CHANGED`, `LIBRARY_AGENT_CREATED`, `LIBRARY_AGENT_DELETED` (severity low).

## Git transport (`/git/<repo_id>.git/...`)

`GET info/refs?service=`, `POST git-upload-pack`, `POST git-receive-pack`. Dumb protocol not served (403). Unknown repo 404; no grant 403 (`read` for upload-pack, `write` for receive-pack, mirrors never writable); auth failure 401 with `WWW-Authenticate: Basic realm="cinna"`. The DB session is held only while authorizing.

**Auth resolver** (`resolve_git_http_user`): token from `Bearer`, or Basic with any username and the token as password. The JWT is decoded, then by `token_type`: `cli` resolves to the agent owner, `cli-account` to its user, none (web or desktop access JWT, desktop revocation honoured) via the platform resolver. `agent_env`, `agent`, `guest_share`, `webapp_share` and any other type are refused. Inactive accounts refused.

**Process model** (`GitHttpService`): `git -c core.hooksPath=<GIT_HOOKS_DIR> [-c receive.fsckObjects=true] upload-pack|receive-pack --stateless-rpc <repo>` via `asyncio.create_subprocess_exec`. The request body streams into stdin by a feeder (gzip bodies inflated incrementally, capped at `GIT_MAX_PUSH_BYTES` -> 413); stdout streams back in 64 KiB chunks; the exchange is bounded by `GIT_HTTP_TIMEOUT_SECONDS` (504). On timeout, disconnect or error the process gets SIGTERM (receive-pack, 5 s grace so git drops locks and quarantine) then SIGKILL; upload-pack is killed. The first chunk is pulled before responding so errors can still become status codes. Environment is hardened by `GitRepoStorage.git_env()` (`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=/dev/null`, `HOME=` storage dir, no prompts); receive-pack adds `CINNA_REPO_KIND`, `CINNA_KIT_DIR`, `CINNA_GIT_MAX_BLOB_BYTES`, `CINNA_GIT_MAX_TREE_BYTES`, `CINNA_PUSHER_ID`.

**Bare repo creation** (`GitRepoStorage.init_bare`): `receive.denyNonFastForwards`, `receive.denyDeletes`, `receive.fsckObjects`, `core.hooksPath`, `cinna.kind`, `uploadpack.allowFilter`, initial branch `main`.

## Hook (`pre-receive` -> `push_policy.main`)

`core.hooksPath` is stored on the repo and also forced with `-c` on every spawn, so a stale path in the volume is never used and no in-volume hook runs. If the hook file is missing or not executable (`GitHttpService.hook_ready()`), receive-pack answers 503 (git would silently skip it); the state is logged at startup (`main.py`). The hook has no DB access: repo kind comes from env or `cinna.kind`, kit rules from `CINNA_KIT_DIR` (`layout.json`, `CONTRACT_VERSION`, `schema/cinna-agent.schema.json`). Rules: only `refs/heads/main`; each new blob and the tip tree checked against `never_travels` and `secret_files` (case-insensitive via `casefold`), blob and tree size caps; symlinks (mode 120000) refused; for `agent` repos `cinna-agent.json` must exist, validate (jsonschema) and have an acceptable `contract_version`. Pushed objects are only visible through git quarantine variables; nothing is checked out or executed. A refusal prints one sentence on stderr, exit 1; any unexpected error also refuses (fail safe). `jsonschema` is an explicit backend dependency.

## Bookkeeping and events

receive-pack exits 0 even when the hook refuses, so acceptance is decided from the ref. `ReceiveCommands` parses the request's command section for the SHA `main` was asked to become (`pushed_new`); after the stream ends a Starlette `BackgroundTask` calls `LibraryEvents.after_receive`, which counts the push only if `refs/heads/main` now equals `pushed_new` and differs from the pre-push head. It updates `head_sha`, `manifest_cache`, `updated_at` (compare-and-set on the previous `head_sha`; on a lost race the disk value is stored), writes the audit row, then emits `library.updated` to each reader's `user_{id}` room (never a broadcast, so guest/webapp sockets are not reached). Socket.IO has no cross-worker manager, so events are hints only.

## Settings (`backend/app/core/config.py`)

`GIT_STORAGE_DIR` (`/app/data/git`), `GIT_HOOKS_DIR` (`backend/app/git_hooks`), `GIT_MAX_BLOB_BYTES` (10 MiB), `GIT_MAX_TREE_BYTES` (200 MiB), `GIT_MAX_PUSH_BYTES` (256 MiB), `GIT_HTTP_TIMEOUT_SECONDS` (600), `GIT_EXPORT_TREE_V3` (false; see [Git-Backed Agent Versioning](../../agents/agent_git_versioning/agent_git_versioning_tech.md)). Compose host override: `HOST_GIT_STORAGE_DIR`.

## Deployment

- `docker-compose.yml` mounts `${HOST_GIT_STORAGE_DIR:-./backend/data/git}:/app/data/git` on the backend; it must persist because the bare repos are the content (Postgres only caches `head_sha`). `backend/data/git/` is gitignored.
- `frontend/nginx.conf` and the production proxy need the `/git/` block, see [Nginx Setup](../../infrastructure/nginx_setup.md).
- Volume, multi-replica and backup notes: [Data Volumes](../../infrastructure/data_volumes.md).
- Clients scope `http.<origin>/.extraHeader` by the clone URL's origin, which is `backend_base_url` and can differ from the client's API origin.
