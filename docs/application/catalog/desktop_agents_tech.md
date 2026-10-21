# Desktop Agents in the Catalog — Technical Reference

## File Locations

- Models: `backend/app/models/bundles/agent_bundle.py` (`runs_on`, `git_repo_id`, `manifest_id`, `slug`), `backend/app/models/bundles/agent_bundle_revision.py` (`origin='share'`, `git_commit_sha`), `backend/app/models/library/git_repo.py` (`GitRepo`, `GitRepoKind`, `KIT_SLUG_PATTERN`), `backend/app/models/catalog/catalog_item.py` (`CatalogItem`, `CatalogItemDetail`, `CatalogRevision`, `CatalogDesktop`, `DesktopAgentCreate`, `CatalogItemUpdate`)
- Migrations: `backend/app/alembic/versions/0e62eb9df5d4_add_git_repo_tables.py` (repos), `backend/app/alembic/versions/6b4297a50868_desktop_bundles.py` (conversion)
- Routes: `backend/app/api/routes/catalog_items.py` (`/api/v1/catalog`), `backend/app/api/routes/git_http.py` (`/git`, hidden from OpenAPI)
- Services: `backend/app/services/catalog/desktop_agent_service.py`, `desktop_agent_revisions.py`, `catalog_item_service.py`, `catalog_events.py`; transport in `backend/app/services/library/`: `git_repo_storage.py`, `git_http_service.py`, `push_policy.py`, `exceptions.py`
- Hook: `backend/app/git_hooks/pre-receive` (entry) calling `app.services.library.push_policy.main`
- Auth: `resolve_git_http_user` in `backend/app/api/deps.py`
- Events/audit constants: `backend/app/models/events/event.py`, `backend/app/models/events/security_event.py`

## Database

`agent_bundle` (see [Agent Bundles tech](../../agents/agent_bundles/agent_bundles_tech.md)) gains `runs_on` (`cloud` default / `desktop`), `git_repo_id` (FK `git_repo`, RESTRICT, unique), `manifest_id` (UUID) and `slug` (varchar 64). Partial unique indexes `uq_agent_bundle_desktop_manifest_id` and `uq_agent_bundle_desktop_slug` where `runs_on='desktop'`; check `ck_agent_bundle_desktop_fields` requires all three for desktop rows. `agent_bundle_revision` gains `git_commit_sha` (varchar 40) and a nullable `snapshot_path`; `content_hash` holds the git tree id for share revisions.

`git_repo`: `id`, `kind` (`agent`/`mirror`), `marketplace_id` (FK `llm_plugin_marketplace`, SET NULL, mirrors only), `default_branch` (`main`), `head_sha` (NULL until first accepted push), `last_pushed_by_user_id` (FK user, SET NULL), `manifest_cache` (JSON `{id, name, description, runtime.engine, contract_version}`), `created_at`, `updated_at`. Slug, name, description, owner and the old `git_repo_grant` table are gone; those live on the bundle and `bundle_access_grant`.

Migration `6b4297a50868` converted existing repos: bundle id from `BundleIdService.desktop_bundle_id`, grants mapped `read/write/maintain` to `use/edit/maintain`, `everyone` to `public` visibility, owner to `maintain`, revision 1 (origin `share`) when a head existed. A missing or duplicate stored manifest id got a fresh one (logged). Downgrade is lossy.

## API (JWT; `CatalogError.http_status` maps 403/404/409/422)

| Route | Notes |
|-------|-------|
| `POST /api/v1/catalog/desktop-agents` | `DesktopAgentCreate {slug, display_name, description, manifest_id, visibility, grants}` -> `CatalogItem`; any active user. `DesktopAgentService.create`: validate slug and manifest id, mint `bundle_id`, create `GitRepo`, bundle, bare repo and grants in one transaction (creator `maintain`); on failure rollback and remove the repo dir; audit `CATALOG_DESKTOP_AGENT_CREATED` |
| `GET /api/v1/catalog/items` | Query `kind` (`agent`/`skill`), `runs_on` (`cloud`/`desktop`), `scope=all` (superuser, else 403) -> `CatalogItemsPublic {data, count}`. Ownerless cloud bundles are excluded; a desktop bundle whose publisher is gone stays |
| `GET /api/v1/catalog/items/{item_uuid}` | `CatalogItemDetail` = item + `revisions: CatalogRevision[]`; needs `use` (else 404) |
| `PATCH /api/v1/catalog/items/{item_uuid}` | `CatalogItemUpdate {display_name?, description?, visibility?, is_listed?}`; maintainer or superuser |
| `DELETE /api/v1/catalog/items/{item_uuid}` | 204; 409 while other people use it; desktop: bundle (revisions and grants cascade), `git_repo` row, repo dir moved to `.deleted/<repo_id>-<utc>`; audit `CATALOG_ITEM_DELETED` |
| `GET /api/v1/catalog/items/{item_uuid}/grants`, `PUT .../grants` | Full replace, maintainer or superuser; see [Agent Bundles](../../agents/agent_bundles/agent_bundles.md) |

`CatalogItem.desktop` is `{repo_id, clone_url, default_branch, head_sha, manifest_id, slug, engine, contract_version}`; `clone_url` is `GitRepoStorage.clone_url(repo_id)` = `<backend_base_url>/git/<repo_id>.git`. Routes `/items` are registered before `/catalog/{bundle_id}`.

Cloud paths answer 409 `{"code":"desktop_only","message":"This agent runs on your computer in Cinna Desktop."}` via `DesktopOnlyError` and `BundleService.require_cloud`: `/catalog/{bundle_id}` (get, install-context, install, admin-install), `InstallService` install and `apply_update`, `/bundles/{uuid}` routes, first publish onto a desktop `bundle_id`, and `POST /agents/checkout` with a core repo URL. `CatalogService.user_can_install` is false for desktop; `GET /bundles/` and `GET /catalog/` list cloud bundles only.

Git access (`GitHttpService.authorize(session, repo, bundle, user, service)`): upload-pack needs `use` or a superuser; receive-pack needs `edit` (superusers too); mirrors never take a push. Audit types (severity low): `CATALOG_DESKTOP_PUSH_ACCEPTED`, `CATALOG_DESKTOP_AGENT_CREATED`, `CATALOG_GRANTS_CHANGED`, `CATALOG_ITEM_DELETED`.

## Revision on push (`DesktopAgentRevisions.after_receive`)

Runs as a Starlette `BackgroundTask` after receive-pack; counts the push only if `refs/heads/main` equals the requested new sha and differs from the pre-push head. In one transaction it locks the bundle row (`FOR UPDATE`), compare-and-sets `git_repo.head_sha` and `manifest_cache`, then inserts `AgentBundleRevision(origin='share', revision_number=max+1, git_commit_sha, content_hash=<tree id>, snapshot_path=None, published_by_user_id=pusher, release_notes, manifest, required_credential_specs, skills_summary)`, moves `latest_revision_id`, writes the audit row and, after commit, emits `catalog.updated`. Inputs: `GitRepoStorage.commit_messages(repo_id, prev_sha, new_sha)` (release notes, capped at 10k chars), `GitRepoStorage.export_paths(...)` (`cinna-agent.json` and `skills/` into a temp dir), `project_revision_manifest` (`{id, name, slug, description, contract_version, runtime, credentials, catalog_skills}`), `credential_spec.build_spec` per manifest credential, and `parse_skill_dir` per authored skill plus pinned catalog skills. `BundleService.list_revisions_with_install_counts` and `delete_revision` accept origins `publish` and `share`.

## Git transport (`/git/<repo_id>.git/...`)

`GET info/refs?service=`, `POST git-upload-pack`, `POST git-receive-pack`. Dumb protocol not served (403). Unknown repo 404; no access 403 (`use` for upload-pack, `edit` for receive-pack, mirrors never writable); auth failure 401 with `WWW-Authenticate: Basic realm="cinna"`. The DB session is held only while authorizing.

**Auth resolver** (`resolve_git_http_user`): token from `Bearer`, or Basic with any username and the token as password. The JWT is decoded, then by `token_type`: `cli` resolves to the agent owner, `cli-account` to its user, none (web or desktop access JWT, desktop revocation honoured) via the platform resolver. `agent_env`, `agent`, `guest_share`, `webapp_share` and any other type are refused. Inactive accounts refused.

**Process model** (`GitHttpService`): `git -c core.hooksPath=<GIT_HOOKS_DIR> [-c receive.fsckObjects=true] upload-pack|receive-pack --stateless-rpc <repo>` via `asyncio.create_subprocess_exec`. The request body streams into stdin by a feeder (gzip bodies inflated incrementally, capped at `GIT_MAX_PUSH_BYTES` -> 413); stdout streams back in 64 KiB chunks; the exchange is bounded by `GIT_HTTP_TIMEOUT_SECONDS` (504). On timeout, disconnect or error the process gets SIGTERM (receive-pack, 5 s grace so git drops locks and quarantine) then SIGKILL; upload-pack is killed. The first chunk is pulled before responding so errors can still become status codes. Environment is hardened by `GitRepoStorage.git_env()` (`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=/dev/null`, `HOME=` storage dir, no prompts); receive-pack adds `CINNA_REPO_KIND`, `CINNA_KIT_DIR`, `CINNA_GIT_MAX_BLOB_BYTES`, `CINNA_GIT_MAX_TREE_BYTES`, `CINNA_PUSHER_ID`, `CINNA_BOUND_MANIFEST_ID` (the bundle's `manifest_id`).

**Bare repo creation** (`GitRepoStorage.init_bare`): `receive.denyNonFastForwards`, `receive.denyDeletes`, `receive.fsckObjects`, `core.hooksPath`, `cinna.kind`, `uploadpack.allowFilter`, initial branch `main`.

## Hook (`pre-receive` -> `push_policy.main`)

`core.hooksPath` is stored on the repo and also forced with `-c` on every spawn, so a stale path in the volume is never used and no in-volume hook runs. If the hook file is missing or not executable (`GitHttpService.hook_ready()`), receive-pack answers 503 (git would silently skip it); the state is logged at startup (`main.py`). The hook has no DB access: repo kind comes from env or `cinna.kind`, kit rules from `CINNA_KIT_DIR` (`layout.json`, `CONTRACT_VERSION`, `schema/cinna-agent.schema.json`). Rules: only `refs/heads/main`; each new blob and the tip tree checked against `never_travels` and `secret_files` (case-insensitive via `casefold`; a `never_travels` entry ending `/` is a folder, any other entry an exact file, e.g. `.claude/settings.local.json`; `_NEVER_TRAVELS_SENTENCES` maps `credentials/`, `app-data/` and that file to dedicated sentences, others use `MSG_NEVER_TRAVELS`; a version whose tip tree still contains the file is refused even if the change did not touch it; deleting the file is accepted), blob and tree size caps; symlinks (mode 120000) refused; for `agent` repos `cinna-agent.json` must exist, validate (jsonschema), have an acceptable `contract_version` and, when `CINNA_BOUND_MANIFEST_ID` is set, carry that `id` (`check_manifest(..., bound_id)`; refusal `MSG_MANIFEST_ID_CHANGED`). Pushed objects are only visible through git quarantine variables; nothing is checked out or executed. A refusal prints one sentence on stderr, exit 1; any unexpected error also refuses (fail safe). `jsonschema` is an explicit backend dependency.

## Bookkeeping and events

receive-pack exits 0 even when the hook refuses, so acceptance is decided from the ref. `ReceiveCommands` parses the request's command section for the SHA `main` was asked to become (`pushed_new`); after the stream ends a Starlette `BackgroundTask` calls `DesktopAgentRevisions.after_receive`, which counts the push only if `refs/heads/main` now equals `pushed_new` and differs from the pre-push head. It updates `head_sha`, `manifest_cache`, `updated_at` (compare-and-set on the previous `head_sha`; on a lost race the disk value is stored), writes the audit row, then emits `catalog.updated` to each reader's `user_{id}` room (never a broadcast, so guest/webapp sockets are not reached). Socket.IO has no cross-worker manager, so events are hints only.

## Settings (`backend/app/core/config.py`)

`GIT_STORAGE_DIR` (`/app/data/git`), `GIT_HOOKS_DIR` (`backend/app/git_hooks`), `GIT_MAX_BLOB_BYTES` (10 MiB), `GIT_MAX_TREE_BYTES` (200 MiB), `GIT_MAX_PUSH_BYTES` (256 MiB), `GIT_HTTP_TIMEOUT_SECONDS` (600), `GIT_EXPORT_TREE_V3` (false; see [Git-Backed Agent Versioning](../../agents/agent_git_versioning/agent_git_versioning_tech.md)). Compose host override: `HOST_GIT_STORAGE_DIR`.

## Deployment

- `docker-compose.yml` mounts `${HOST_GIT_STORAGE_DIR:-./backend/data/git}:/app/data/git` on the backend; it must persist because the bare repos are the content (Postgres only caches `head_sha`). `backend/data/git/` is gitignored.
- `frontend/nginx.conf` and the unified production proxy config carry the `/git/` block, see [Nginx Setup](../../infrastructure/nginx_setup.md).
- Volume, multi-replica and backup notes: [Data Volumes](../../infrastructure/data_volumes.md).
- Clients scope `http.<origin>/.extraHeader` by the clone URL's origin, which is `backend_base_url` and can differ from the client's API origin.
