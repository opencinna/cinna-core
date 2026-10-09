# Team Sharing (cinna-core) — Implementation Plan

Spec, source of truth (gitignored in cinna-desktop; cite by path here, never link from committed docs):
`/Users/evgenyl/dev/ml-llm/cinna-desktop/plans/team-sharing/README.md` (S1–S13, §4 interfaces are FIXED) and
`/Users/evgenyl/dev/ml-llm/cinna-desktop/plans/team-sharing/core.md` (§2 P0, §3 P2). This run: P0 + P2 + the §6 admin page.

## 0. Index

### 0.1 Working-tree rules
- Branch `main`, shared tree. Do not commit, stash, reset or checkout; the manager commits. Leave `cinna.log` alone.
- Never edit anything under `/Users/evgenyl/dev/ml-llm/cinna-desktop`.
- Kit edits go to `docs/local_agent_kit/` only, then `make sync-platform-knowledge` (mirrors into `backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit/`). Never hand-edit the mirror.
- After Phase D backend changes: `source ./backend/.venv/bin/activate && make gen-client`. Never hand-edit `frontend/src/client/`.
- Wire strings are contract: `library.updated`, `library.granted`, `/git/<repo_id>.git/...`, LibraryAgent/LibraryMirror field names. Do not rename.

### 0.2 Decisions taken (recommendation marked ★ where alternatives existed)
| # | Decision |
|---|---|
| D1 | ★ Tree v3 = kit folder as-is (`cinna-agent.json` at root, content beside). v3 is a **git wire format only**: internal bundle storage (`manifest.json` + `workspace/`) stays v2, so seeding/install code is untouched. Readers accept v3 always; git export writes v3 only when `GIT_EXPORT_TREE_V3=true` (default false). Deviation from core.md §2.2 "publish": publish-to-storage keeps v2 because storage is not a shared tree |
| D2 | ★ Transport = `git upload-pack/receive-pack --stateless-rpc` via `asyncio.create_subprocess_exec`, body from `request.stream()`, output via `StreamingResponse`. Not Dulwich: backend already runs real git (image has it), `receive.denyNonFastForwards`/`denyDeletes`/`core.hooksPath`/quarantine are git-core semantics Dulwich re-implements partially, and no new dependency. Async subprocess never blocks the uvicorn loop (`fastapi run --workers 4`, `backend/Dockerfile:55`) |
| D3 | Auth = one resolver in `deps.py` reusing existing ones, no new token type: `token_type=="cli"` → `_resolve_cli_context(...).user` (the agent owner, `cli_token.owner_id`); `"cli-account"` → `_resolve_account_cli_context(...).user`; else `_resolve_platform_user_from_token` (web + desktop JWTs, honours desktop revocation, rejects `agent_env`/guest/webapp; `aud`-bearing agent-api tokens fail decode). Bearer, or Basic with any username and the token as password |
| D4 | Grants: `principal_kind` `user`/`role`/`everyone`; `role` = `User.role` values (`agent-user`, `agent-developer`, `admin`; `admin` also matches `is_superuser`). Effective access = max over matching grants. Any user may create; creator gets `maintain`. Superusers get **management** (edit grants, delete, admin listing) on every repo but read/write over git and `GET /library` follow grants only (an admin self-grants if needed) |
| D5 | ★ `POST /library/agents` grants creator `maintain` **and** `everyone: read` (README §5 "Share with the team" means the team sees it). Maintainer narrows with PUT. See §15 Q1 |
| D6 | Slug: kit pattern `^[a-z0-9][a-z0-9-]{1,62}$`, unique per instance among `kind='agent'` (partial unique index) |
| D7 | Hook = `backend/app/git_hooks/pre-receive`, a 10-line Python entry calling pure `app.services.library.push_policy.main()`. Runs under the image venv (`PATH=/app/.venv/bin`, `PYTHONPATH=/app`, `backend/Dockerfile:20,37`); `jsonschema` (already in `uv.lock`) becomes an explicit dependency. No DB access in the hook: repo kind comes from git config `cinna.kind` on the bare repo; kit dir and caps come from env set by the handler |
| D8 | `core.hooksPath` is set on the bare repo at creation (spec) **and** passed as `git -c core.hooksPath=<GIT_HOOKS_DIR>` on every spawn, so an image path change never leaves stale paths in the data volume and no in-volume hook can run |
| D9 | Post-receive bookkeeping runs in a Starlette `BackgroundTask` after the stream ends, with its own `Session(engine)`: compare `git rev-parse refs/heads/main` with stored `head_sha` (exit code of receive-pack is 0 even when the hook refuses, so the ref is the truth) |
| D10 | Events go to `user_{id}` rooms of every reader (resolved audience), never a no-room broadcast (that would reach guest/webapp sockets). Pre-existing limit: Socket.IO has no Redis manager across 4 workers; events stay refresh hints, `GET /library` is recovery |
| D11 | Audit = `SecurityEvent` rows (`LIBRARY_PUSH_ACCEPTED`, `LIBRARY_GRANTS_CHANGED`, `LIBRARY_AGENT_CREATED`, `LIBRARY_AGENT_DELETED`, severity `low`) |
| D12 | `/git` gets its own nginx block (`proxy_request_buffering off`, `client_max_body_size 256m`, 600 s timeouts). No `/api/...` alias: the generic `/api/` block's 1 MB body default would break pushes anyway |
| D13 | `cloud_install_id` computed (one query: caller's `AgentGitSource.repo_url` matching the clone URL); `mirrors` is always `[]`; no `git_repo_lease` table |

### 0.3 Phase table
| Phase | Layer | Files (create **C** / modify **M**) | Plan § | Depends | Tests (§12) |
|---|---|---|---|---|---|
| A | backend + kit | M `docs/local_agent_kit/{CONTRACT_VERSION,kit.json,layout.json,CHANGELOG.md,templates/root/gitignore,schema/cinna-agent.schema.json}`, C `docs/local_agent_kit/schema/cinna-marketplace.schema.json`, M `backend/app/services/bundles/revision_format.py`, M `backend/app/services/bundles/git_source_service.py`, M `backend/app/core/config.py` | §2, §3 | — | 12.1 |
| B | backend | C `backend/app/models/library/{__init__,git_repo}.py`, M `backend/app/models/__init__.py`, C migration, M `backend/app/core/config.py`, M `backend/pyproject.toml`, C `backend/app/services/library/{__init__,git_repo_storage,library_access_service}.py` | §4, §5 | — (A optional) | 12.2 |
| C | backend + infra | C `backend/app/api/routes/git_http.py`, C `backend/app/services/library/{git_http_service,push_policy,library_events}.py`, C `backend/app/git_hooks/pre-receive` (mode 755), M `backend/app/api/deps.py`, M `backend/app/main.py`, M `backend/app/models/events/security_event.py`, M `frontend/nginx.conf`, M `docker-compose.yml`, M `.gitignore` | §6–§9 | B, A (manifest schema 1.8.0) | 12.3 |
| D | backend + client | C `backend/app/api/routes/library.py`, C `backend/app/services/library/library_service.py`, M `backend/app/api/main.py`, regen `frontend/src/client/` | §10 | C | 12.4 |
| E | frontend | C `frontend/src/routes/_layout/admin/team-library.tsx`, C `frontend/src/components/Admin/TeamLibrary/*`, M `frontend/src/components/Sidebar/AdminMenu.tsx` | §11 (+ UI Specification appended by `cinna-core-ui-designer`) | D | typecheck + ui.review |

A developer reads §0 + its phase sections + the §12 sub-list for that phase.

## 1. Contract anchors (do not drift)
- Transport README §4.1; Library API README §4.2; events README §4.3. Clone URL `"{settings.backend_base_url}/git/{repo_id}.git"` (`backend/app/core/config.py:206`), default branch `main`.
- S7 policy, S9 vocabulary (refusal sentences never say branch/commit/push/pull/merge/rebase/remote/repository/conflict/SHA), S13 plaintext on the server.

## 2. Phase A — Kit contract 1.8.0
Current `CONTRACT_VERSION` is `1.7.0`, carried in `CONTRACT_VERSION`, `kit.json`, `layout.json` (`contract_version`) and `CHANGELOG.md`; `LocalAgentKitService._contract_defect_reason` 503s the contract when they disagree, so bump all four together to **1.8.0** (minor, additive).
1. `layout.json` → new top-level `shared_remote` section:
   - `tree: "agent_folder"` (tree v3: the agent folder is the git tree, manifest `cinna-agent.json` at root), `default_branch: "main"`, `one_branch: true`.
   - `never_travels`: the paths of the `credentials` and `runtime_data` roles (`credentials/`, `app-data/`) plus `secret_rule: "secret_files"`. Hosts and the server read these, not prose.
   - `local_state`: `{ "file": "app-data/desktop.json", "key": "shared_remote", "fields": ["kind", "repo_id", "clone_url"], "kind_values": ["cinna", "external"] }` — per-machine, never in the tree (it lives under `app-data/`).
2. `layout.json` → `workshop.roles` gains `{ path: "Marketplaces", kind: "directory", role: "marketplaces", survives_update: true, description: external marketplace checkouts, never edited by the workshop assistant }`; `templates/root/gitignore` gains `Marketplaces/`.
3. `schema/cinna-marketplace.schema.json` (draft 2020-12) for `.cinna-plugin/marketplace.json`: Claude-compatible top level (`name` pattern `^[A-Za-z0-9][A-Za-z0-9._-]*$`, `owner{name,email?,url?}`, `plugins[]`, `description`, `version`, `metadata.pluginRoot`, `renames`), entry (`name`, `source` oneOf relative `./…` without `..` / `github` / `url` / `git-subdir`, `description`, `version`, `dependencies`, `metadata.cinna{kind: agent|skill|plugin, minContract}`), `additionalProperties: true` everywhere (unknown keys warn, never reject). Source: `/Users/evgenyl/dev/ml-llm/cinna-desktop/plans/agent-marketplaces.md` §3.1–3.2.
4. `schema/cinna-agent.schema.json`: register `bundle` (object, `x-scope: "host:cinna-core"`, `x-import: "ignored"`) — the core-only revision block written by v3 export (§3). List the new schema in `kit.json` wherever schemas are enumerated.
5. `CHANGELOG.md` `## 1.8.0 — shared agents` entry: Added `shared_remote`, `Marketplaces` role, marketplace schema, `bundle` field; "every 1.x folder stays valid".
6. Run `make sync-platform-knowledge`; then `tests/unit/test_local_kit_conformance.py`, `test_local_kit_tool.py`, `tests/api/cli/test_local_agent_kit.py`.

## 3. Phase A — `RevisionFormat` v3 (reader + writer)
File `backend/app/services/bundles/revision_format.py` (327 lines; `REVISION_SCHEMA_VERSION` :60, `_MANIFEST_FILENAMES` :73, `read_manifest` :200, `write_tree` :163).
- Constants: `KIT_MANIFEST_FILENAME = "cinna-agent.json"`, `SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3}`; `REVISION_SCHEMA_VERSION` stays 2 (storage).
- `detect_tree_layout(src) -> "v2" | "v3"`: v2 when `workspace/` dir + `cinna.agent.json`; v3 when `cinna-agent.json` at root and no `workspace/`.
- **Reader** `read_v3_tree(src, dest_workspace) -> dict`: copies the tree into `dest_workspace` applying the kit's `cloud_import_excludes` (read from `platform_knowledge_assets.local_kit_dir()/layout.json`, the same rule `cinna agent import` uses) and returns a **v2-equivalent manifest**: the `bundle` block's fields (bundle_id, revision_number, version, published_at, content_hash, sdk, required_credential_specs, schedules, plugin_specs, metadata, release_notes) overlaid with kit top-level fields (description, example_prompts, router_trigger_prompt, status_refresh_command) and prompt texts read from the files named in `prompts` (default `layout.agent.prompt_files`). A tree without `bundle` (desktop-made) yields a manifest without `bundle_id`; existing `_require_bundle_id` (`git_source_service.py:3324`) still refuses checkout of it (that is P7).
- **Writer** `write_v3_tree(*, env_workspace_root, dest, manifest, existing_kit_manifest: dict | None) -> str`: prunes from `dest` only paths the reader would import (paths matching `cloud_import_excludes` — `AGENTS.md`, `README.md`, `Makefile`, `publications.json`, … — that exist in the clone are left untouched so a desktop teammate's files survive a cloud export), snapshots the workspace to the root via `PublishService._snapshot_workspace_tree`, writes `docs/WORKFLOW_PROMPT.md`/`ENTRYPOINT_PROMPT.md`/`REFINER_PROMPT.md` from manifest prompt text, writes `cinna-agent.json` = existing kit manifest (unknown keys preserved) updated with `contract_version` 1.8.0, `name`, `slug`, `description`, `example_prompts`, `router_trigger_prompt`, `prompts`, `runtime.engine` (`claude-code`→`claude`, else `opencode`), and `bundle` = rest of v2 manifest. `id`: keep existing; else mint `uuid4` once. `.gitignore` = kit `templates/agent/gitignore`. Returns content hash (stored in `bundle.content_hash`).
- `git_source_service.py` call sites: `_read_and_validate_tree` (:3310, used at :391, :771, :1684, :2667) becomes layout-aware — for v3 it materialises into a temp sibling `…/_v2view/workspace` via `read_v3_tree` and returns `(manifest, workspace_root)`; replace the literal `src / "workspace"` at :393, :791, :1685, :2630/:2686 and in `_persist_clone_as_snapshot` (:3353) with the returned root. Never mutate the clone itself (a later commit would record moves).
- `_capture_and_push` (:2417–:2485): if `settings.GIT_EXPORT_TREE_V3` **or** the remote tree is already v3 (never downgrade a v3 remote) → `write_v3_tree`; else today's v2 path. `_assert_no_oversized_files` targets the written root.
- Setting `GIT_EXPORT_TREE_V3: bool = False` next to `GIT_SOURCE_*` (`config.py:110`).
- Invariants: v2 trees behave byte-identically with the flag off; round trip v2 manifest → v3 → reader equals the input (minus `content_hash`); `cinna agent import` untouched.

## 4. Phase B — Models, migration, settings
`backend/app/models/library/git_repo.py` (re-export in `models/__init__.py`). Pattern: `models/bundles/bundle_permissions.py`.

`git_repo`
| column | type | notes |
|---|---|---|
| id | uuid pk | repo_id |
| kind | varchar(16) NOT NULL | `agent` \| `mirror` (constants class, not pg enum) |
| slug | varchar(64) NOT NULL | partial unique index `uq_git_repo_agent_slug` on (slug) WHERE kind='agent' |
| name | varchar(255) NOT NULL | |
| description | text NOT NULL default '' | |
| owner_user_id | uuid FK user SET NULL, nullable | creator; repo survives user deletion |
| marketplace_id | uuid FK llm_plugin_marketplace SET NULL, nullable | mirrors (P5) |
| default_branch | varchar(64) default 'main' | |
| head_sha | varchar(64) nullable | null until first accepted push |
| manifest_cache | JSON nullable | `{id,name,description,runtime:{engine},contract_version}` from HEAD |
| created_at / updated_at | timestamptz | `updated_at` moves on push and metadata edit |

`git_repo_grant`
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| repo_id | uuid FK git_repo CASCADE | index |
| principal_kind | varchar(16) | `user` \| `role` \| `everyone` |
| principal_id | varchar(64) NOT NULL | user uuid str / role value / `*` (not NULL so the unique key works) |
| user_id | uuid FK user CASCADE, nullable | set only for kind=user, so deleting a user drops the grant |
| access | varchar(16) | `read` \| `write` \| `maintain` (ordered) |
| granted_by_user_id | uuid FK user SET NULL | |
| created_at | timestamptz | |
Unique `(repo_id, principal_kind, principal_id)`; index on `user_id`.

API schemas (same file): `LibraryPrincipalUser{user_id}`, `LibraryPrincipalRole{role}`, `LibraryPrincipal = LibraryPrincipalUser | LibraryPrincipalRole | Literal["everyone"]`, `LibraryGrantIn{principal, access}`, `LibraryGrantPublic{principal, access, display_name, granted_by, created_at}`, `LibraryMaintainer{user_id, display_name}`, `LibraryManifest{id, name, description, runtime{engine}, contract_version}` (nullable on `LibraryAgent` until the first share), `LibraryAgent` (README §4.2 exactly + nullable `cloud_install_id`), `LibraryMirrorEntry`, `LibraryMirror` (README §4.2), `LibraryListing{agents, mirrors}`, `LibraryAgentCreate{slug, name, description}`, `LibraryAgentAdmin(LibraryAgent minus access) + owner + grants[]`.

Migration `backend/app/alembic/versions/<rev>_add_git_repo_tables.py` (autogenerate via `make migration`, then hand-check the partial index and FKs); downgrade drops both tables. Bare repos on disk are not touched by downgrade.

Settings (`config.py`, beside `BUNDLE_STORAGE_DIR` :1017, same "must be a persisted mount" comment): `GIT_STORAGE_DIR="/app/data/git"`, `HOST_GIT_STORAGE_DIR` not needed (never bind-mounted into agent containers), `GIT_HOOKS_DIR` (default `Path(app).parent/"git_hooks"`), `GIT_MAX_BLOB_BYTES=10 MiB`, `GIT_MAX_TREE_BYTES=200 MiB`, `GIT_MAX_PUSH_BYTES=256 MiB`, `GIT_HTTP_TIMEOUT_SECONDS=600`. `pyproject.toml`: add `jsonschema` (version already in `uv.lock:1355`).

## 5. Phase B — Storage and access services
`backend/app/services/library/git_repo_storage.py` (`GitRepoStorage`, static, sync, subprocess with timeout, env hardened: `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `HOME` = storage dir):
- `repo_path(repo_id) -> Path` (`<GIT_STORAGE_DIR>/<repo_id>.git`, uuid-validated, never from user strings).
- `init_bare(repo_id, kind)`: `git init --bare --initial-branch=main`; config `receive.denyNonFastForwards=true`, `receive.denyDeletes=true`, `core.hooksPath=<GIT_HOOKS_DIR>`, `cinna.kind=<kind>`, `uploadpack.allowFilter=true`; dirs 0o755.
- `head_sha(repo_id) -> str | None` (`rev-parse --verify -q refs/heads/main`), `read_manifest_at(repo_id, sha) -> dict | None` (`git show <sha>:cinna-agent.json`, JSON-parse, project to `LibraryManifest` fields, tolerate garbage → None).
- `remove(repo_id)`: move to `<GIT_STORAGE_DIR>/.deleted/<repo_id>-<utc>` (operator purges; backup-window safety).

`backend/app/services/library/library_access_service.py` (`LibraryAccessService`):
- `effective_access(session, repo, user) -> "read"|"write"|"maintain"|None` — max over grants: user (`user_id`), role (`user.role`, plus `admin` when `is_superuser`), everyone.
- `accessible_repos(session, user) -> list[(GitRepo, access)]` — one query: `git_repo` join grants filtered by the three principal predicates, grouped, max access.
- `can_manage(repo, user, access)` = access == maintain or `user.is_superuser`.
- `readers(session, repo) -> set[UUID]` — active users holding ≥ read (everyone → all active users; role → users with that role; admin role → superusers).
- `maintainers(session, repo) -> list[LibraryMaintainer]` — user grants with `maintain`, `display_name = full_name or email`.
- `replace_grants(session, repo, grants, actor)` — validates (role in `VALID_USER_ROLES`, user exists and active, no duplicate principals), **refuses a result with no `maintain` user grant** (409 "An agent in the team library needs at least one maintainer"), replaces rows in one transaction, returns `(before_readers, after_readers)`.

## 6. Phase C — Smart HTTP transport + auth
Routes `backend/app/api/routes/git_http.py`, `router = APIRouter(prefix="/git", include_in_schema=False)`, mounted in `backend/app/main.py` next to `agent_hooks_router` (:499):
```
GET  /git/{repo_id}.git/info/refs?service=git-upload-pack|git-receive-pack
POST /git/{repo_id}.git/git-upload-pack
POST /git/{repo_id}.git/git-receive-pack
```
Path param: declare `/{repo_file}/info/refs` and parse `<uuid>.git` in the service (404 otherwise).

Auth — `deps.py` new `resolve_git_http_user(db, authorization: str | None) -> User` raising `GitHttpAuthError` (D3). Bearer, or Basic where the password is the token (username ignored). The route maps any auth failure (missing, malformed, expired, revoked device, revoked CLI token, inactive user) to **401** with `WWW-Authenticate: Basic realm="cinna"`; unknown repo → 404; missing grant → **403** (`read` for upload-pack and its info/refs, `write` for receive-pack and its info/refs; mirrors never writable). Do not use `CurrentUser` here: `get_current_user` returns 403/404 on bad tokens and would look up a CLI token id as a user id.

`backend/app/services/library/git_http_service.py` (`GitHttpService`):
- `advertise(repo_id, service, git_protocol) -> bytes`: run `git -c core.hooksPath=… <service> --stateless-rpc --advertise-refs <path>`; prefix `# service=git-<service>\n` pkt-line + flush **unless** `GIT_PROTOCOL` contains `version=2` (mirror `http-backend.c`). Content type `application/x-git-<service>-advertisement`, `Cache-Control: no-cache`.
- `run_rpc(repo_id, service, body: AsyncIterator[bytes], gzip: bool, git_protocol, env_extra) -> AsyncIterator[bytes]`: spawn with `asyncio.create_subprocess_exec` (stdin/stdout pipes, stderr captured to log); a feeder task streams `request.stream()` → `zlib.decompressobj(16 + zlib.MAX_WBITS)` when `Content-Encoding: gzip` → `stdin.write`/`drain`, counting bytes and aborting (kill, 413) past `GIT_MAX_PUSH_BYTES`; the generator yields `stdout.read(65536)` chunks concurrently (no deadlock with large packs). Whole operation bounded by `asyncio.timeout(GIT_HTTP_TIMEOUT_SECONDS)`; on timeout or client disconnect, kill the process and `await proc.wait()` in `finally`. Response type `application/x-git-<service>-result`.
- Env for receive-pack: `CINNA_REPO_KIND`, `CINNA_KIT_DIR=local_kit_dir()`, `CINNA_GIT_MAX_BLOB_BYTES`, `CINNA_GIT_MAX_TREE_BYTES`, `CINNA_PUSHER_ID`, plus the hardened git env of §5; `GIT_PROTOCOL` from the `Git-Protocol` request header.
- Receive-pack: read `head_sha` before spawning; the route returns `StreamingResponse(..., background=BackgroundTask(LibraryEvents.after_receive, repo_id, before_sha, pusher_id))` (§8).
- Nothing from the pushed tree is ever executed or checked out on the server.

## 7. Phase C — `pre-receive` policy hook
- `backend/app/git_hooks/pre-receive` (committed with mode 755; verify `git ls-files -s` shows `100755`): `#!/usr/bin/env python3` → `from app.services.library.push_policy import main; sys.exit(main(sys.stdin, os.environ))`.
- `push_policy.py` is pure (stdlib + `jsonschema`, no `app.core` import, no DB) so unit tests drive it directly. Reads stdin lines `<old> <new> <ref>`; runs git plumbing (objects are visible through git's quarantine env vars, which it inherits):
  1. ref ≠ `refs/heads/main` → refuse. (Deletes are already refused by `receive.denyDeletes`; non-fast-forward by `receive.denyNonFastForwards` — the client maps git's non-ff message to its S8 dialog.)
  2. every **new** object: `git rev-list --objects <new> --not --all` (covers secrets added then removed in a later commit) piped to `git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)'`; for each blob path: secret rule (load `secret_files.rules` from `$CINNA_KIT_DIR/layout.json`, same fail-safe clause semantics as `services/agents/skill_manifest.py:733` `_secret_clause_hits`), path under a `never_travels` root, size > `CINNA_GIT_MAX_BLOB_BYTES`.
  3. tip tree total (`git ls-tree -r -l <new>`) > `CINNA_GIT_MAX_TREE_BYTES`.
  4. `kind=agent`: `git show <new>:cinna-agent.json` missing / not JSON / fails `schema/cinna-agent.schema.json` (first error only); contract major ≠ server `CONTRACT_VERSION` major → refuse; minor newer → warning line on stderr, accepted.
  - Missing/unreadable `layout.json` → refuse everything ("The team library can't check this change right now. Ask your admin to look at the Cinna server.") — fail-safe.
- Refusal = print the sentence to stderr, exit 1. Sentences (S9; desktop shows verbatim under "Couldn't share"):
  - secret: `This change includes {path}, a file that can hold a password or key, so it can't be shared.`
  - credentials/: `This change includes the agent's credentials folder, which never leaves your computer.`
  - app-data/: `This change includes the agent's app-data folder (its private working files), which can't be shared.`
  - blob: `The file {path} is larger than {N} MB, too big to share with the team.`
  - tree: `This agent is larger than {N} MB in total, too big to share with the team.`
  - manifest missing / invalid: `This agent has no cinna-agent.json, so it can't be shared.` / `This agent's cinna-agent.json isn't valid ({detail}), so it can't be shared.`
  - contract newer major / older major: `This agent was made with a newer version of Cinna than this server. Ask your admin to update Cinna.` / `This agent was made with an older version of Cinna. Open it in Cinna Desktop to update it, then share again.`
  - other ref: `Only the agent's main version can be shared to the team library.`

## 8. Phase C — Bookkeeping, events, audit
`backend/app/services/library/library_events.py` (`LibraryEvents`):
- `after_receive(repo_id, before_sha, pusher_id)` (async; DB work via `run_in_threadpool` with `Session(engine)`): `new = GitRepoStorage.head_sha`; if `new is None or new == before_sha` → return (refused or no-op). Else compare-and-set `head_sha` (UPDATE … WHERE head_sha IS NOT DISTINCT FROM before_sha; a lost race just re-reads), refresh `manifest_cache`, `updated_at`; write `SecurityEvent LIBRARY_PUSH_ACCEPTED` (user = pusher, details `{repo_id, slug, before, after}`); emit `library.updated` to every `readers()` user: `event_service.emit_event(event_type="library.updated", model_id=repo_id, user_id=<reader>, meta={repo_id, head_sha, pushed_by})` (`pushed_by` = `{user_id, display_name}`).
- `granted(repo, user_ids)`: emits `library.granted {repo_id}` per newly-gained reader; called by grants PUT with `after - before` (creator excluded on create).
- Event-type constants `LIBRARY_UPDATED = "library.updated"`, `LIBRARY_GRANTED = "library.granted"` added to `EventType` (`models/events/event.py:27`); security constants added in `models/events/security_event.py`.

## 9. Phase C — Deployment
- `frontend/nginx.conf`: `location /git/` before `location /` (copy the `/api/v1/cli/` block, :12): `proxy_buffering off`, `proxy_request_buffering off`, `client_max_body_size 256m`, `proxy_read_timeout/proxy_send_timeout 600s`, forwarded headers. Same block documented for the production reverse proxy in `docs/infrastructure/nginx_setup.md`.
- `docker-compose.yml` backend volumes (:53–:72): `- ${HOST_GIT_STORAGE_DIR:-./backend/data/git}:/app/data/git` with the "must persist" comment; add `backend/data/git/` to the root `.gitignore` (beside `backend/data/bundles/`, :15). `docker-compose.override.yml` needs nothing (path is not handed to agent containers).
- Desktop/CLI: they use the `clone_url` returned by the API; its origin is `backend_base_url`, which may differ from the desktop's API origin (`api.domain` vs `domain`), so clients key `http.<origin>/.extraHeader` by the clone URL origin (note for the desktop/CLI sessions, §15 Q4).
- Multi-replica: `GIT_STORAGE_DIR` must be a shared volume, same caveat as `BUNDLE_STORAGE_DIR`.
- Backup: no runbook exists today; create `docs/infrastructure/data_volumes.md` listing every `/app/data/*` mount (uploads, app-data, bundles, skills, git) with "back up while the backend is stopped, or `git bundle create --all` per repo"; Postgres `head_sha` reconciles on next push.

## 10. Phase D — Library API (`backend/app/api/routes/library.py`, tag `library` → `LibraryService` in the client)
Thin routes; logic in `backend/app/services/library/library_service.py` (`LibraryService`, typed errors mapped to HTTP in the route, like `agent_git.py::_map_git_error`).
| Method + path | Auth | Behaviour |
|---|---|---|
| `GET /api/v1/library` | CurrentUser | `{agents: accessible_repos → LibraryAgent[], mirrors: []}`; maintainers in one batched query; `cloud_install_id` one query over the caller's `AgentGitSource` (`repo_url` equal to clone URL, with/without `.git`) |
| `POST /api/v1/library/agents` | CurrentUser | validate slug (D6), 409 duplicate (`An agent called {slug} is already in the team library.`); insert row (flush) → `init_bare` → grants (creator maintain + everyone read, D5) → commit; on commit failure `remove`; on init failure rollback. `LIBRARY_AGENT_CREATED`. Returns `LibraryAgent` (head_sha/manifest null) |
| `GET /api/v1/library/agents/{repo_id}` | CurrentUser | 404 when unknown **or** no read (do not advertise) |
| `GET /api/v1/library/agents/{repo_id}/grants` | maintain or superuser | `LibraryGrantPublic[]` (additive to README, needed by grant editors) |
| `PUT /api/v1/library/agents/{repo_id}/grants` | maintain or superuser | body `LibraryGrantIn[]`, full replace (§5), `LIBRARY_GRANTS_CHANGED`, `library.granted` to new readers, returns grants |
| `DELETE /api/v1/library/agents/{repo_id}` | maintain or superuser | 409 while any **other** user's `AgentGitSource.repo_url` matches (`{N} people use this agent in Cinna, so it can't be removed until they remove it.`); else delete row (grants cascade), `remove` dir, `LIBRARY_AGENT_DELETED`; 204 |
| `GET /api/v1/library/admin/agents` | superuser | every `kind=agent` repo as `LibraryAgentAdmin` (owner, maintainers, grants, head_sha, updated_at) for the §11 page |
Register in `backend/app/api/main.py`; then `make gen-client`.

## 11. Phase E — Frontend surfaces (inventory only; composition by `cinna-core-ui-designer`, append `## UI Specification` below)
**S1 Team library admin page** — NEW route `frontend/src/routes/_layout/admin/team-library.tsx` (admin route boilerplate as `admin/routing-tuning.tsx`), entry "Team library" in `components/Sidebar/AdminMenu.tsx` beside "Addon Marketplaces".
- Intent: the admin wants to see which agents are shared through the team library, who maintains each, and who can see or change it.
- Data: per agent name, slug, description, maintainers, grants (principal + access), last updated, whether it has been shared yet (`head_sha` null → not yet), owner.
- Actions: view; open one agent's grants; edit grants; remove an agent (shows the 409 sentence when refused).
- API: `LibraryService.listLibraryAdminAgents`, `getLibraryAgentGrants`, `putLibraryAgentGrants`, `deleteLibraryAgent` (names per generated client). Query keys `["library","admin"]`, `["library","grants",repoId]`; invalidate both after PUT/DELETE.
**S2 Grant editor** — NEW (dialog per §4 guidelines "edit in dialogs"), launched from S1.
- Intent: the admin changes who can see or change one agent.
- Data/actions: list grants; add a person (reuse the existing user search picker, `GET /users/search`), a role (`agent-user`/`agent-developer`/`admin`) or "Everyone"; set access read/write/maintain; remove a grant; save (full replace). Server refusal "needs at least one maintainer" shown inline.
- Copy uses S9 words (Team library, maintainer, can see / can change); labels for access: read = "Can get", write = "Can share changes", maintain = "Maintainer".
- Guideline hot spots (`ui_ux_guidelines.md` §4): long lists (cap 5 + Show all), ≤ 2 inline row actions.

## 12. Tests (checklist by file; read `backend/tests/README.md` first)
### 12.1 Phase A
- [ ] `tests/unit/test_revision_format.py`: v3 detect; v2→v3→read round trip; reader applies `cloud_import_excludes`; prompts from files; tree without `bundle` → no bundle_id; writer preserves unknown kit keys and existing `id`; prune leaves `AGENTS.md`.
- [ ] `tests/unit/test_local_kit_conformance.py`: 1.8.0 coherent across 4 carriers; `shared_remote` keys; `Marketplaces` role; marketplace schema validates a Claude-shaped sample and accepts unknown keys.
- [ ] `tests/api/agents/git/agents_git_tree_v3_test.py`: checkout of a v3 tree (via `_fake_clone_ctx` pattern) creates the agent with prompts/workspace; pull of v3; push with flag off writes v2; flag on writes v3; v3 remote stays v3 with flag off.
### 12.2 Phase B
- [ ] `tests/migrations/`: up/down of the new revision (partial unique index, cascades). Access matrix is covered through the API in 12.4.
### 12.3 Phase C — real git client
- [ ] `tests/utils/git_http_bridge.py`: single-threaded `http.server` on 127.0.0.1:<ephemeral> forwarding each request (headers, raw body, status, headers back) to the test `client` — so git subprocesses hit the app with the test DB session (savepoint fixture); fixture sets `GIT_STORAGE_DIR` to `tmp_path`. Git env for clients: `GIT_CONFIG_COUNT` extraHeader injection as README §4.4.
- [ ] `tests/utils/library.py`: `create_library_agent`, `put_grants`, `git(…)` helper; `bind_cloud_install_seam` (documented Rule-1 DB-seam exemption, inserts an `AgentGitSource` row).
- [ ] `tests/api/library/library_git_http_test.py`: clone empty → commit valid v3 agent → push → clone elsewhere sees it → second push → fetch; no token → 401 + `WWW-Authenticate: Basic realm="cinna"`; Basic `x-access-token` works; cli-account and per-agent cli tokens work (per-agent resolves to owner); revoked desktop token → 401; reader push → 403; stranger clone → 403; non-fast-forward refused; branch delete refused; gzip request body (raw POST of gzip pkt-lines to upload-pack) → 200; protocol v2 and v0 both clone.
- [ ] `tests/api/library/library_push_policy_test.py`: one push per refusal sentence (`.env`, `.env.local`, `.env.example` accepted, `credentials/x`, `app-data/x`, blob > cap with monkeypatched small cap, tree > cap, missing manifest, invalid manifest, major mismatch, minor newer accepted with warning, `refs/heads/other`, secret added then removed in a later commit); asserts stderr sentence appears in git output and `head_sha` unchanged.
- [ ] `tests/unit/test_library_push_policy.py`: rule loader fail-safe (unknown clause), missing layout → refuse; parity with `skill_manifest.SECRET_FILE_RULES`.
- [ ] `tests/api/library/library_events_test.py` (socketio stub): accepted push → `library.updated` to each reader room only, not to a non-reader; refused push → none; `SecurityEvent` visible via security-events API.
### 12.4 Phase D
- [ ] `tests/api/library/library_api_test.py`: create (creator maintain + everyone read, slug pattern 422, duplicate 409); listing grants matrix (user / role / everyone, max access, non-reader absent, superuser without grant absent from `/library` but present in `/library/admin/agents`); GET one 404 for non-reader; PUT by maintainer, by superuser, by writer → 403; PUT without maintainer → 409; `library.granted` only to new readers; DELETE refused with other user's cloud install (seam), allowed otherwise and repo no longer clonable; `cloud_install_id` set for caller's own bound agent; `mirrors == []`.
- Regression scope: `tests/api/agents/git/`, `tests/api/cli/`, `tests/api/desktop_auth/`, `tests/unit/`.

## 13. Docs (files and the sentence each needs)
- C `docs/application/team_library/team_library.md` + `team_library_tech.md` (frontmatter `feature: team_library`, `domain: application`, `affects: [agent_git_versioning, local_agent_kit, realtime_events, user_roles]`): what the library is, grants, policy sentences, events, routes; then `make docs-registry`.
- M `docs/agents/agent_git_versioning/agent_git_versioning_tech.md`: "RevisionFormat reads v3 kit-folder trees; git export writes v3 when `GIT_EXPORT_TREE_V3` or the remote is already v3; bundle storage stays v2."
- M `docs/application/local_agent_kit/*`: "Contract 1.8.0 adds `shared_remote`, the `Marketplaces` role and the marketplace schema."
- M `docs/application/realtime_events/event_bus_system.md`: "`library.updated` / `library.granted` go to each reader's user room."
- M `docs/application/app_sync/app_sync.md`: "Team library trees are plaintext on the server by design; the zero-knowledge guarantee does not apply to them."
- M `docs/infrastructure/nginx_setup.md`: the `/git/` block and why. C `docs/infrastructure/data_volumes.md` (§9).
- Then `make sync-platform-knowledge` and `make check-docs`.

## 14. Out of scope / deferred
- **P5 mirrors** — core.md §4 requires sign-off from the resilient-plugins rework owner before a persistent clone returns. Ships now only: `kind='mirror'` and `marketplace_id` columns, the `LibraryMirror` schema, `mirrors: []`, receive-pack 403 for mirrors.
- **P7** — suggestions, editing lease (`git_repo_lease` not created), cloud twins (`AgentGitSource` pointing at a library repo with a service principal). `cloud_install_id` is computed but stays null in practice until P7.
- Web history/blame/branches UI, LFS, submodules, multiple branches, force pushes (core.md §6). Mirror sync status on the admin page (P5).

## 15. Open decisions for the user
1. **Default visibility on create** (D5): `everyone: read` (recommended, matches "Share with the team") vs creator-only until the maintainer adds grants.
2. **Superuser reach** (D4): management only, git follows grants (recommended), vs implicit read on every repo.
3. **Secret coverage**: hook enforces the kit `secret_files` rule + `credentials/`/`app-data/` (recommended; same rule the clients apply) vs also refusing key-material names (`*.pem`, `*.key`, `credentials.json`) from `cloud_import_excludes`.
4. **Clone URL origin**: `backend_base_url`; desktop/CLI must scope the extraHeader by the clone URL's origin, not their API origin. Needs a line in the desktop/CLI briefs.
5. **README §4.2 additions**: `GET …/grants` and `GET /library/admin/agents`, plus `manifest` nullable before the first share. Additive; confirm before the desktop session codes against §4.2.

## 16. Risks
- `git_source_service.py` has ~10 `src / "workspace"` literals; missing one in v3 paths silently snapshots nothing. Grep every literal; the v3 API test must assert workspace files land.
- Hook exec bit lost in a copy → every push refused with a git "cannot run hook" error. Test 12.3 catches it; Dockerfile `COPY` preserves mode.
- Streaming + `BackgroundTask` + dependency-scoped session: do not use the request `SessionDep` after the stream starts; bookkeeping owns its session (pool-safety contract, `agent_git_versioning_tech.md` "Pool-Safety Contract").
- Event fan-out with `everyone` = one emit per active user; fine for team-sized instances, revisit with a Redis manager.

## UI Specification

_Produced by cinna-core.ui.design on 2026-10-09 (re-checked the same day: helper name corrected, Maintainer scoped to people). Composition decisions here override the plan's frontend section (§11); data and API decisions stay with the plan._

Vocabulary (README S9) for every string below: "team library", "agent", "maintainer", "access", "get", "share changes", "last updated", "not shared yet". Never "repository", "repo", "push", "commit", "branch", "SHA", "clone" in rendered text. The slug is shown as an identifier, not described.

### Surfaces

| # | Surface | Host | Story | Placement (§2 row) | Pattern | Budget | Verification |
|---|---------|------|-------|--------------------|---------|--------|--------------|
| S0 | "Team Library" menu entry | `Sidebar/AdminMenu.tsx` | — (navigation) | menu item after "Addon Marketplaces" | existing menu item | 1 item, `Library` icon | checklist |
| S1 | Team Library admin page | NEW `/admin/team-library` | Manage list | dedicated route (§1 Manage list) | P9 (reference `admin/marketplaces.tsx` + `marketplaceColumns.tsx`) | 5 columns, page size 30, 0 inline row actions besides the name link, `⋯` = 2 items | checklist + screenshots (new route) |
| S2 | Access dialog (grant editor) | Dialog launched from S1 | Edit | Dialog, one section (§1 Edit; §2 Disclosure depth) | P3 rows inside a form dialog + trigger/Popover picker (§2 Choosing one entity in a form) | rows uncapped but scroll-contained (`max-h-[40vh]`), 2 inline controls per row, 1 add control | checklist + screenshots (new composition: buffered grant rows + mixed principal picker) |
| S3 | Remove confirm | AlertDialog owned by S1 (list-level `deleteTarget`) | — (confirm) | §2 Confirmation | AlertDialog | 1 sentence + server refusal Alert | checklist |

Not specified, on purpose: **mirrors**. The admin endpoint returns `kind=agent` repos only and `mirrors` is always `[]` this run; the page renders **nothing** for mirrors — no section, no "coming soon" note. §2 Absent facts: an absence nobody asked about is the most repeated and least informative thing on a surface, and a placeholder section for a deferred feature is that absence at page scale. P5 adds its own surface when mirrors exist. No history, blame or per-version UI (plan §14).

### S0 — Menu entry
- `DropdownMenuItem asChild` → `RouterLink to="/admin/team-library"`, icon `Library` (`mr-2 h-4 w-4`), label **"Team Library"** — Title Case to match its siblings ("Addon Marketplaces", "AI Credentials"); the product noun is still "team library" in body copy. Placed directly after "Addon Marketplaces". `isActive` already covers `/admin/*`.

### S1 — Team Library admin page
- **Intent:** the admin wants to see which agents are shared through the team library, who maintains each, who can get it, and remove one that should not be there.
- **Layout:** route boilerplate copied from `admin/marketplaces.tsx` (head title `Team Library - Admin - ${APP_NAME}`, superuser `beforeLoad` + render guard as in `admin/routing-tuning.tsx`), `usePageHeader` content = `h1` "Team Library" + `p.text-xs.text-muted-foreground` "Agents shared with the team, who maintains them and who can get them". **No primary header button**: agents are added to the team library from Cinna Desktop or the CLI, not here (P9's Create button has no action to carry). Body `p-6 md:p-8 overflow-y-auto` → `mx-auto max-w-7xl` → `DataTable` (client-side paging, `DEFAULT_PAGE_SIZE` 30). No search/filter toolbar this run (team-sized lists; add per P9 when a list passes one page).
- **Columns** (`components/Admin/TeamLibrary/teamLibraryColumns.tsx`, typed `ColumnDef<LibraryAgentAdmin>[]`):
  1. **Agent** — name as a `<button type="button">` styled like the marketplaces name link (`font-medium text-primary hover:underline`, `truncate`) that opens S2; below it one `text-xs text-muted-foreground` line: `slug` in `font-mono` · `added by {owner display name}`. Owner null → slug only (§2 Absent facts). Description is not a column (it is S2's description).
  2. **Maintainers** — first 2 display names joined ", ", then a muted `+N` with a `Tooltip` listing the rest one per line. Always ≥ 1 (server invariant); if the array is empty render nothing.
  3. **Who can get it** — one summary string computed from `grants` (any access level counts, since every level includes get): `Everyone` when an everyone grant exists; otherwise the facts with units joined ` · ` — role labels by name when ≤ 2 roles (`Agent Developer`, from `utils/userRoles` `userRoleLabel`), else `N roles`; people as `N people` / `1 person`. Plain text, no badges.
  4. **Last updated** — `RelativeTime` of `updated_at` when `head_sha` is set; when `head_sha` is null, muted text **"Not shared yet"** (it is the column's answer, not a chip; no `Badge`).
  5. **Actions** (`sr-only` header) — `Common/RowActionsMenu` labelled `the agent {name}`, right-aligned. Items: `KeyRound` "Edit access" (opens S2) · separator · `Trash2` "Remove from team library" (`variant="destructive"`, sets `deleteTarget`). Do not copy `MarketplaceActionsMenu`'s hand-rolled trigger.
- **Sorting:** server order, else by `updated_at` desc with not-yet-shared last.
- **Interaction model:** none on the page; edits go through S2, removal through S3.
- **States:** loading — `PendingItems` (skeleton table, as marketplaces); use `useQuery` (not `useSuspenseQuery`) so error is its own branch: `QueryErrorAlert` with Retry in place of the table; empty — centred block, one sentence: "No agents have been shared with the team yet. People add one from Cinna Desktop or the CLI." (no button: there is no web create action — see Lessons); pending remove — `Loader2` in the `⋯` slot, `h-7 w-7`, Tooltip + `sr-only` "Removing…" (§2 Pending on rows), keyed by `deleteMutation.variables === repo_id`.
- **Facts & values:** timestamps → `RelativeTime` only (A11); no links; nothing click-to-copy (the slug is an identifier the admin reads, not pastes — add copy only on request); wire-string fallbacks with `||` (`display_name || "Unknown person"`).
- **Components:** `DataTable`, `RowActionsMenu`, `RelativeTime`, `QueryErrorAlert`, `Tooltip`. Query key `["library","admin"]`.
- **Anti-patterns:** none in touched files (`AdminMenu.tsx` is not in §4).
- **Screenshots:** `/admin/team-library` at 1440 and 1024, full page. If the dev DB has no library agents, capture the empty state and report the populated table as "required but not capturable" — do not create library agents to stage it.

### S2 — Access dialog
- **Intent:** the admin (or a maintainer, later) changes who can get one agent, who can share changes to it, and who maintains it.
- **Layout:** `Dialog`, `DialogContent className="sm:max-w-lg max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0"`. Header titled **by the agent** (§2 Wizard step header, by analogy): `DialogTitle` = agent name; `DialogDescription` = agent description `||` "Choose who can get this agent from the team library and who can change it." (`break-words`). Body: one `h3.text-sm.font-medium` "Access", then the grant list, then the add control. Footer: Cancel · **Save** (`LoadingButton`, "Saving…").
- **Rows:** `ListRowGroup` inside a `max-h-[40vh] overflow-y-auto pr-2` container (§2 Scrolling lists and the far edge). One `ListRow` per grant, **no status dot** (membership is access — `SkillPackageGrantRow` precedent), one line, no `meta`:
  - icon tile: `Globe` (everyone), `Users` (role), `User` (person), `h-3.5 w-3.5` in the `h-6 w-6` tile;
  - title: "Everyone" | role label (`userRoleLabel`) | person `display_name || "Unknown person"`;
  - inline control 1: access `Select` (`h-8 w-[10.5rem]`, `aria-label="Access for {title}"`) with **Can get** (read) · **Can share changes** (write) · **Maintainer** (maintain); labels live in a new `utils/libraryAccess.ts` (`LIBRARY_ACCESS_OPTIONS`), not in the component; **Maintainer is offered on person rows only** — `maintainers()` counts user grants only (plan §5), so a role or Everyone at maintain would hold maintain access without appearing as a maintainer anywhere; an existing non-person maintain grant (API-created) still shows its value as the selected option;
  - inline control 2: remove — ghost `h-7 w-7` `X` with Tooltip "Remove", hover-revealed (`opacity-0 group-hover:opacity-100 focus-visible:opacity-100`). Removal is buffered, so it does not confirm (it is undone by Cancel).
  - order: Maintainer → Can share changes → Can get; within a level Everyone → roles → people, then by title.
- **Add control:** below the list, one `Button variant="outline" size="sm"` with `Plus` "Add people or roles" as the trigger of a `Popover` (`LibraryPrincipalPicker`, new, in `Admin/TeamLibrary/`; popover/search skeleton taken from `Common/UserSelectField.tsx`). Popover body: a search `Input`; group "Everyone" (one item, hidden if already granted); group "Roles" (the 3 roles from `USER_ROLE_OPTIONS`, filtered by query, already-granted hidden); group "People" from `GET /users/search` (≥ 2 chars, `includeSelf` so an admin can self-grant per D4, already-granted user ids excluded). Choosing an item appends a row at **Can get**, closes the popover and returns focus to the trigger. Group headings `text-xs font-semibold uppercase tracking-wide text-muted-foreground`. List in the popover `max-h-[40vh] overflow-y-auto pr-2`. Never a nested `Dialog` (§2 Disclosure depth).
- **Interaction model:** form. Rows, adds and removes are buffered; Save sends the full list (`putLibraryAgentGrants`, full replace). Save disabled until dirty. Cancel / Escape / outside-click discard; all three are blocked while saving.
- **Validation:** when the buffered list holds no **person** at Maintainer, show an inline `Alert` above the footer — "An agent in the team library needs at least one maintainer." — and disable Save. A server 409 with the same or another sentence renders in the same `Alert` slot (server `detail`, `||` fallback "Could not save access"). Other errors → error toast, dialog stays open with the edits.
- **States:** loading — 3 `Skeleton` rows `h-[40px]` while `getLibraryAgentGrants` (`["library","grants",repoId]`) loads; error — `QueryErrorAlert` with Retry in the body, Save hidden; empty — cannot occur for a saved agent (≥ 1 maintainer); in the buffer, if every row is removed, the list area shows "No one has access yet. Add people or roles below." plus the maintainer Alert; pending — Save shows "Saving…", all row controls and the add trigger disabled. Success → toast "Access to {name} updated", close, invalidate `["library","admin"]` and `["library","grants",repoId]`.
- **Facts & values:** no timestamps rendered (`created_at`/`granted_by` are not shown — nobody scans grants by them; add a `RowInfo` only on request); no links; nothing click-to-copy; dialog overflow as in Layout; initial focus — first focusable is the first row's `Select` (no tooltip), so default Radix autofocus is fine.
- **Components:** `Dialog`, `ListRowGroup`/`ListRow`, `Select`, `Tooltip`, `Popover`, `Input`, `Alert`, `LoadingButton`, `QueryErrorAlert`, `Skeleton`; new: `LibraryAccessDialog.tsx`, `LibraryGrantRow.tsx`, `LibraryPrincipalPicker.tsx`, `utils/libraryAccess.ts`.
- **Screenshots:** `/admin/team-library` → click the first agent's name → capture `--element "[role=dialog]"` at 1440 and 1024 (one capture each). If no library agent exists in the dev DB: "required but not capturable", reviewed from JSX.

### S3 — Remove confirm
- **Owner:** the page, via `deleteTarget` set by the menu item (§2 Row actions — a `DropdownMenuItem` unmounts on select).
- **Copy:** title "Remove {name} from the team library?"; description "People can no longer get it or share changes to it. Copies already on their machines are not deleted." Buttons Cancel · **Remove agent** (destructive; "Removing…" while pending; `e.preventDefault()` so the dialog stays until the request settles).
- **Refusal (409):** the dialog stays open; the server sentence (`{N} people use this agent in Cinna, so it can't be removed until they remove it.`) renders as `Alert variant="destructive"` inside the dialog; the destructive button is hidden and Cancel reads "Close". 404 → toast "This agent was already removed", close, invalidate.
- **Success:** toast "{name} removed from the team library", invalidate `["library","admin"]`.

### Frontend phase order
All of S0–S3 belong to Phase E, after Phase D's `make gen-client`. Contract per surface: S1 `LibraryService.listLibraryAdminAgents` (`GET /library/admin/agents`); S2 `getLibraryAgentGrants` + `putLibraryAgentGrants`, plus existing `UsersService.searchUsers`; S3 `deleteLibraryAgent`. Build order: `utils/libraryAccess.ts` → S1 table + menu + S3 → S2 → S0.

### Open questions (contract gaps for the planner, not taste)
1. **`LibraryAgentAdmin.owner` shape** — S1 needs a display name, not only an id: `owner: { user_id, display_name } | null` (same shape as `LibraryMaintainer`).
2. **`LibraryAgentAdmin.grants` element type** — S1's "Who can get it" summary needs `principal` + `access` per grant; confirm it is `LibraryGrantPublic[]` (with `display_name`) so S2 could also seed from it.
3. **`LibraryGrantPublic.display_name` for role/everyone** — the UI labels roles from `utils/userRoles` and "Everyone" itself; the server value for those kinds is ignored. Confirm it is never relied on as the only label.
4. **`description` on `LibraryAgentAdmin`** — S2's header uses it; confirm "LibraryAgent minus access" keeps it (README §4.2 has it).
5. **Maintain for roles / Everyone** — S2 offers Maintainer to people only. Should `replace_grants` also refuse `maintain` on `role`/`everyone` principals, so the API cannot create a maintainer the UI never lists?

### Lessons for the guideline
- **Empty state with no web create action.** §2 Empty state requires a button/link to Create; this list is filled from Desktop/CLI only. Resolved as one sentence naming where it happens, no button. Proposed rule: "when the create action lives outside the web app, the sentence names the client; no dead button."
- **Menu/page title casing vs the product noun.** No rule says whether a feature noun ("Team library") follows sibling Title Case in menus/h1. Resolved: siblings' casing in navigation and page titles, sentence case in body copy.
- **Buffered grant editors.** No house pattern covers a form dialog whose body is an editable list (rows with a per-row `Select` + buffered remove + an add picker). If S2 passes review, name it as the reference for "access editors" so the next sharing surface does not re-decide it.
