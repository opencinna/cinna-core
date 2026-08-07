# Git-Backed Agent Versioning — Technical Reference

## File Locations

### Models
- `backend/app/models/bundles/agent_git_source.py` — `AgentGitSourceBase`, `AgentGitSource` (table), `AgentGitSourcePublic`, `AgentGitSourceCreate`, `AgentGitSourceUpdate`, `GitSyncDirection`, `GitSourceStatus`

### Services
- `backend/app/services/bundles/git_source_service.py` — `GitSourceService` (checkout / connect / disconnect / pull / push / check-updates / get-source / compute_dirty / compute_status / list_commits / `_connect_adopt_existing` / `_file_hashes`); module constant `_PROMPT_FIELDS`; typed errors `GitSourceError`, `GitSourceNotFoundError`, `GitSourceValidationError`, `GitSourceConflictError`, `GitSourceExistingAgentError` (new, subclass of conflict)
- `backend/app/services/bundles/revision_format.py` — `RevisionFormat` (de)serializer; `RevisionFormatError`; `generate_gitignore` (now also emits the recursive cache denylist); constants `BUNDLE_MANIFEST_FILENAME`, `GIT_MANIFEST_FILENAME`, `REVISION_SCHEMA_VERSION`, `SUPPORTED_SCHEMA_VERSIONS`
- `backend/app/services/bundles/publish_service.py` — `PublishService._snapshot_workspace_tree`, `PublishService.hash_workspace_tree` (workspace-only stable digest for dirty check)
- `backend/app/services/environments/workspace_classification.py` — denylist single source of truth: `NESTED_EXCLUDED_DIRS` / `NESTED_EXCLUDED_FILE_GLOBS` / `is_nested_excluded` (new — recursive cache exclusion), `safe_copytree` + `_copytree_ignore` (renamed from `_ignore_symlinks`; now drops symlinks AND nested cache artifacts), `is_bundle_owned_toplevel` (also rejects nested-excluded names)
- `backend/app/services/knowledge/git_operations.py` — git primitives (extended for git versioning): `clone_repository`, `clone_repository_context`, `pull_repository`, `ls_remote_head`, `commit_all`, `fast_forward_push`, `get_current_commit_hash`, `create_ssh_key_file`, `verify_repository_access`, URL converters, `assert_git_url_allowed`, `init_repo_with_remote`, `git_log_subdir`; provider-aware web URLs: `GitWebProvider` dataclass + `_WEB_PROVIDERS` registry + `_resolve_web_provider` + `_split_host_path`, and builders `build_web_history_url` / `build_web_commit_url` / `build_web_tree_url` (new); typed errors `GitOperationError`, `GitAuthenticationError`, `GitConnectionError`, `GitNonFastForwardError`
- `backend/app/services/common/egress_guard.py` — `assert_url_allowed`, `assert_host_allowed`, `is_host_blocked`, `validate_external_endpoint_url`, `EgressBlockedError`; generalized from the original MCP provider module; honors `GIT_SOURCE_ALLOW_PRIVATE_HOSTS` per call
- `backend/app/services/agents/agent_webhook_service.py` — `create_git_source_webhook`, `fire_webhook` dispatch for `AgentWebhookType.GIT_SOURCE`
- `backend/app/services/agents/agent_service.py` — `compute_capability_flags` (now also computes `git_versioning_enabled` from `AgentGitSource` presence) + `to_public_with_clone_info` (sets it on `AgentPublic`)

### API Routes
- `backend/app/api/routes/agent_git.py` — all git-versioning routes (see table below); request/response models `AgentCheckoutRequest`, `AgentCheckoutResponse`, `AgentGitConnectRequest` (now with `adopt_existing`), `GitPushRequest`, `GitUpdateStatus`, `GitCommit` (now with `commit_url`), `GitCommitList`, `GitDirtyStatus`, `GitStatus` / `GitPromptChange` / `GitFileChange` (new — commit preview); helper `_git_source_to_public` (sets `web_history_url` + `web_tree_url`); error mapping `_map_git_error` (plus a dedicated structured-409 branch for `GitSourceExistingAgentError`)
- `backend/app/api/routes/agent_webhooks.py` — `POST /agents/{id}/webhooks/git-source` (developer-gated)
- `backend/app/api/routes/cli.py` — `GET /cli/git-coordinates` → `CliGitCoordinates`; auth via `CLIContextDep`

### Frontend
- `frontend/src/components/Agents/GitVersioningCard.tsx` — the "GIT Versioning" card in the Integrations tab; manages disabled/connect-form/connected states; react-query hooks for source, dirty, status (commit preview), commits, push, pull, connect, disconnect. Takes a `gitVersioningEnabled` prop for the instant toggle state. Footer actions (Commit Agent + Refresh + icon Disconnect), Latest-commits (3) + View-history/clickable-SHA links, clickable repo name, icon-bearing code-chip coordinates (folder/branch) + sync-direction icon, green-check `connected` status icon, `CommitPreview` (git-status dialog), and the adopt-existing-folder confirm dialog. Helpers: `isExistingFolderError`, `CodeChip` (optional leading icon), `SyncDirectionIcon`, `CommitPreview`, `toGitSshUrl` (HTTP→SSH repo-URL normalizer)
- `frontend/src/components/Agents/DeployKeySelect.tsx` — deploy-key picker reusing `SshKeysService.listSshKeys`; generate/import open the shared `GenerateKeyModal` / `ImportKeyModal` dialogs (auto-selecting the new key via their `onGenerated`/`onImported` callbacks)
- `frontend/src/components/UserSettings/GenerateKeyModal.tsx`, `ImportKeyModal.tsx` — the shared SSH-key dialogs; each accepts an optional `onGenerated`/`onImported` callback so callers (the deploy-key picker) can auto-select the created key
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` — mounts `<GitVersioningCard>` in the card grid (now positioned **before** the Webhooks / Local Dev / Email Integration cards); owner-gated; passes `gitVersioningEnabled={agent.git_versioning_enabled}`
- `frontend/src/components/Agents/AgentCard.tsx` — agents-list card; renders a **GIT** capability badge (`GitBranch` icon) when `agent.git_versioning_enabled`

### Migrations
- `backend/app/alembic/versions/391a6285d8ff_add_agent_git_source.py` — creates `agent_git_source` table; `down_revision = b2d1f4c6a8e3`
- `backend/app/alembic/versions/c8a4f1e09b27_agent_bundle_publisher_nullable.py` — makes `agent_bundle.publisher_user_id` nullable (ownerless git-imported bundles); `down_revision = 391a6285d8ff`

**No additional migration was required for the extension.** "Enabled" is modeled by row presence (connect creates the row, disconnect deletes it); commit history and dirty state are computed live; `status` and `last_synced_commit` columns already existed.

## Configuration Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `GIT_SOURCE_ALLOW_PRIVATE_HOSTS` | `false` | When `true`, egress guard allows git URLs resolving to private/loopback/link-local addresses (for self-hosted git on private LANs) |
| `GIT_SOURCE_MAX_FILE_BYTES` | (configured in settings) | Maximum size (bytes) for any single workspace file accepted during checkout or pushed to the remote; `_assert_no_oversized_files` enforces this before committing or before any bundle/revision row is created |
| `GIT_SOURCE_NETWORK_TIMEOUT_SECONDS` | `30` | Timeout applied to remote-probe operations on read paths (`ls_remote_head`, `git_log_subdir`, `subdir_changed_between`) via GitPython `kill_after_timeout`. Also sets `GIT_HTTP_LOW_SPEED_LIMIT`/`GIT_HTTP_LOW_SPEED_TIME` for HTTP transports and SSH `ConnectTimeout`/`ServerAliveInterval`/`ServerAliveCountMax`. Not applied to full-history clones on write paths |

## Database Schema

### `agent_git_source`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | `default_factory=uuid.uuid4` |
| `repo_url` | varchar(2048) NOT NULL | HTTPS or SSH; normalized by URL converters |
| `subdir` | varchar(1024) NULLABLE | Path within the repo; NULL = repo root |
| `ref` | varchar(255) DEFAULT 'main' | Branch or tag |
| `ssh_key_id` | UUID FK → `user_ssh_keys.id` ON DELETE SET NULL NULLABLE | Private-repo auth; host-side only |
| `sync_direction` | varchar(32) DEFAULT 'bidirectional' | `pull` / `push` / `bidirectional` |
| `agent_id` | UUID FK → `agent.id` ON DELETE CASCADE NOT NULL | The install this source backs |
| `owner_id` | UUID FK → `user.id` ON DELETE CASCADE NOT NULL | Per-agent ownership scope |
| `bundle_uuid` | UUID FK → `agent_bundle.id` ON DELETE SET NULL NULLABLE | Git analog of `Agent.bundle_uuid` |
| `last_synced_commit` | varchar(64) NULLABLE | SHA of last imported/pushed commit; idempotency pin |
| `last_sync_at` | timestamp NULLABLE | Last successful sync time |
| `status` | varchar(32) DEFAULT 'pending' | `pending` / `connected` / `error` / `disconnected` |
| `last_error` | text NULLABLE | Last failure detail (free-form) |
| `created_at` | timestamp NOT NULL | |
| `updated_at` | timestamp NOT NULL | |

Indexes: `ix_agent_git_source_agent_id` (unique — one git source per install); `ix_agent_git_source_owner_id`.

### `agent_bundle` (modified)

`publisher_user_id` is now nullable (migration `c8a4f1e09b27`). A NULL value marks an ownerless git-imported bundle — private, unlisted, not a catalog publish. The FK remains with `ON DELETE RESTRICT` (unchanged). The downgrade deletes ownerless rows before re-imposing the NOT NULL constraint.

## Model Variants

### `AgentGitSourceBase(SQLModel)` — shared, user-editable fields
- `repo_url: str` (max 2048)
- `subdir: str | None` (max 1024)
- `ref: str` default `"main"` (max 255)
- `ssh_key_id: uuid.UUID | None` FK with `ondelete="SET NULL"`
- `sync_direction: str` default `GitSyncDirection.BIDIRECTIONAL` (max 32)

### `AgentGitSource(AgentGitSourceBase, table=True)` — DB table
Adds `id`, `agent_id`, `owner_id`, `bundle_uuid`, `last_synced_commit`, `last_sync_at`, `status`, `last_error`, `created_at`, `updated_at`.

### `AgentGitSourcePublic(AgentGitSourceBase)` — API response
Adds all identity/status fields plus `update_available: bool = False`, `web_history_url: str | None = None`, `web_tree_url: str | None = None`. SSH key material is never included. `update_available` is not stored on the row: `GET /agents/{id}/git` always returns it as `false` (remote-free); `GET /agents/{id}/git/check-updates` computes it via `_compute_update_available_remote` (see Service Layer). The two web URLs are set by the route helper `_git_source_to_public` from `build_web_history_url` / `build_web_tree_url` (both `None` for unsupported hosts). None are stored on the row.

### `AgentGitSourceCreate(AgentGitSourceBase)` — API input for checkout
Inherits Base fields verbatim (max_length validation applies to user input).

### `AgentGitSourceUpdate(SQLModel)` — all-optional patch body
Optional variants of Base fields. No `agent_id` or `owner_id` (not caller-settable).

### `GitSyncDirection` — string constants (not a pg enum)
`PULL = "pull"`, `PUSH = "push"`, `BIDIRECTIONAL = "bidirectional"`

### `GitSourceStatus` — string constants (not a pg enum)
`PENDING = "pending"`, `CONNECTED = "connected"`, `ERROR = "error"`, `DISCONNECTED = "disconnected"`

## Request / Response Models (in `agent_git.py`)

**`AgentCheckoutRequest`**
- `repo_url: str`
- `subdir: str | None = None`
- `ref: str = "main"`
- `ssh_key_id: uuid.UUID | None = None`
- `sync_direction: str = GitSyncDirection.BIDIRECTIONAL`
- `name_override: str | None = None`

**`AgentCheckoutResponse`**
- `agent: AgentPublic`
- `git_source: AgentGitSourcePublic`

**`AgentGitConnectRequest`** (connect flow)
- `repo_url: str`
- `subdir: str | None = None`
- `ref: str = "main"`
- `ssh_key_id: uuid.UUID | None = None`
- `sync_direction: str = GitSyncDirection.BIDIRECTIONAL`
- `commit_message: str = "Initial export from Cinna"`
- `adopt_existing: bool = False` — re-sent as `true` after the recoverable `existing_agent_folder` 409 to adopt the existing remote folder instead of failing

**`GitPushRequest`**
- `commit_message: str`
- `version: str | None = None`
- `also_publish_bundle: bool = False`

**`GitUpdateStatus`**
- `update_available: bool`
- `remote_commit: str | None = None`
- `last_synced_commit: str | None = None`

**`GitCommit`**
- `sha: str`
- `short_sha: str`
- `author_name: str`
- `author_email: str`
- `date: datetime`
- `message: str`
- `commit_url: str | None = None` — single-commit browser URL (from `build_web_commit_url`); `None` for unsupported hosts

**`GitCommitList`**
- `commits: list[GitCommit]`

**`GitDirtyStatus`**
- `dirty: bool`
- `prompts_dirty: bool`
- `workspace_dirty: bool`
- `has_env: bool`
- `last_synced_commit: str | None = None`

**`GitStatus`** (new — commit preview, response of `GET /git/status`)
- `dirty: bool`
- `has_env: bool`
- `last_synced_commit: str | None = None`
- `prompt_changes: list[GitPromptChange] = []`
- `file_changes: list[GitFileChange] = []`

**`GitPromptChange`** (new)
- `field: str` (human label, e.g. "Workflow prompt")
- `change_type: str` — `added` | `modified` | `deleted`

**`GitFileChange`** (new)
- `path: str` (workspace-relative POSIX path)
- `change_type: str` — `added` | `modified` | `deleted`

## Request / Response Model (in `cli.py`)

**`CliGitCoordinates`** (new — CLI-token auth)
- `vcs_enabled: bool`
- `repo_url: str | None = None`
- `subdir: str | None = None`
- `ref: str | None = None`
- `sync_direction: str | None = None`
- `last_synced_commit: str | None = None`
- `auth_hint: str | None = None` — `"ssh"` or `"https"` derived from `repo_url` shape; tells the CLI which credentials the developer needs locally. Never includes key material.

## API Routes

All agent-git routes are registered on `APIRouter(prefix="/agents", tags=["agent-git"])` in `backend/app/api/routes/agent_git.py`. The CLI coordinates route is in `backend/app/api/routes/cli.py` (prefix `/cli`, mounted at `/api/v1`).

| Method | Path | Auth | Request body | Response | Notes |
|--------|------|------|--------------|----------|-------|
| `POST` | `/agents/checkout` | `require_developer` | `AgentCheckoutRequest` | `AgentCheckoutResponse` | Creates install + git source |
| `POST` | `/agents/{agent_id}/git/connect` | `require_developer` | `AgentGitConnectRequest` | `AgentGitSourcePublic` | Attaches git source to existing install + initial export push |
| `DELETE` | `/agents/{agent_id}/git` | `require_developer` | — | `Message` | Deletes the `AgentGitSource` row; does not touch the remote |
| `GET` | `/agents/{agent_id}/git` | `CurrentUser` (owner-resolved) | — | `AgentGitSourcePublic` | Returns stored `AgentGitSource` instantly; no remote call; `update_available` always `false` |
| `GET` | `/agents/{agent_id}/git/check-updates` | `CurrentUser` (owner-resolved) | — | `GitUpdateStatus` | Strict `ls-remote`; surfaces network errors |
| `GET` | `/agents/{agent_id}/git/commits` | `CurrentUser` (owner-resolved) | `limit: int = 50` (query) | `GitCommitList` | Bounded shallow clone; `limit` clamped 1..200; subdir-scoped |
| `GET` | `/agents/{agent_id}/git/dirty` | `CurrentUser` (owner-resolved) | — | `GitDirtyStatus` | Full workspace tree copy to temp; best-effort on env/revision sides |
| `GET` | `/agents/{agent_id}/git/status` | `CurrentUser` (owner-resolved) | — | `GitStatus` | Per-file/per-prompt commit preview; same post-denylist capture a push produces |
| `POST` | `/agents/{agent_id}/git/pull` | `require_developer` | — | `AgentPublic` | Developer-gated; per-agent locked |
| `POST` | `/agents/{agent_id}/git/push` | `require_developer` | `GitPushRequest` | `AgentGitSourcePublic` | Developer-gated; per-agent locked; persists `AgentBundleRevision` |
| `POST` | `/agents/{agent_id}/webhooks/git-source` | `require_developer` | `AgentWebhookCreateGitSource` | `AgentWebhookPublicWithToken` | Registers GitOps trigger; token shown once |
| `GET` | `/cli/git-coordinates` | `CLIContextDep` (agent-scoped CLI token) | — | `CliGitCoordinates` | No deploy key in response; no developer-role gate |

### Error Mapping (`_map_git_error`)

| Exception | HTTP status |
|-----------|-------------|
| `GitSourceNotFoundError` | 404 |
| `GitSourceExistingAgentError` | 409 with structured `detail={"code": "existing_agent_folder", "message": ...}` (caught explicitly in the connect route **before** the generic conflict branch, since it subclasses `GitSourceConflictError`) |
| `GitSourceConflictError` | 409 |
| `GitNonFastForwardError` | 409 |
| `RevisionFormatError` | 422 |
| `GitAuthenticationError` | 400 |
| `EgressBlockedError` | 400 |
| `GitConnectionError` | 400 |
| `GitSourceValidationError` | 400 |
| other `GitOperationError` | 400 |

> ⚠️ **`GitAuthenticationError` must never map to 401 or 403.** It reports that
> *the backend's git client* was rejected by *the remote host* (wrong or
> unselected deploy key) — it says nothing about the caller's own session. The
> frontend's global API error handler (`main.tsx handleApiError`) treats 401/403
> as "this session is dead", so the original 401 mapping logged the user out and
> bounced them to `/login` the moment they clicked **Connect** with the wrong SSH
> key — and did the same on the passive `check-updates` read, so merely opening
> the Integrations tab with a since-revoked key ended the session. It is a
> user-fixable input error and rides the same 400 bucket as `GitConnectionError`.
> Guarded by `test_git_remote_auth_failure_is_not_unauthorized`.

## Service Layer

### `GitSourceService` (all static methods)

**`checkout(*, session, user, repo_url, subdir, ref, ssh_key_id, sync_direction, name_override=None) -> tuple[Agent, AgentGitSource]`**

Full checkout flow. Uses `_resolve_ssh_key` context manager for temp key lifecycle. Calls `clone_repository_context` (shallow). Validates tree via `_read_and_validate_tree` and `_require_bundle_id`. Asserts not oversized (`_assert_no_oversized_files`). Calls `_assert_not_already_checked_out` (409 on duplicate before any row is created). Calls `_resolve_or_create_bundle` (409 on cross-user real-publisher collision). Persists revision via `_persist_revision`. Calls `InstallService._install_from_revision`. On any `IntegrityError` or `InstallError`, calls `_cleanup_orphan_import` to remove stranded bundle/revision rows and re-raises.

**`connect(*, session, agent_id, user, repo_url, subdir, ref, ssh_key_id, sync_direction, commit_message="Initial export from Cinna", adopt_existing=False) -> tuple[AgentGitSource, Agent]`**

Attaches git source to an existing owned install and performs an initial export push. Per-agent locked. Flow: ownership resolve → no-existing-source guard (409) → env-readable guard (400) → direction guard (400 if pull-only) → SSH key resolve → backing bundle resolve → remote-state probe via `_connect_capture` (branches: empty-remote/init path, ref-exists-subdir-empty/ff path, subdir-has-agent → `GitSourceExistingAgentError` unless `adopt_existing` → `_connect_adopt_existing`) → persist `AgentGitSource` + `AgentBundleRevision`. `Agent.bundle_uuid` is not mutated. Raises `GitSourceExistingAgentError`/`GitSourceConflictError` on 409 cases and `GitSourceValidationError` on 400 cases; an `IntegrityError` from a concurrent connect is caught at the route layer as a race backstop. `adopt_existing` threads through `_connect_locked` → `_connect_capture`.

**`_connect_adopt_existing(session, *, source, owner, src, repo) -> str`** (new)

The adopt path: links the agent to an existing remote folder **without pushing**. Reads + validates the remote tree (`_read_and_validate_tree` + `_require_bundle_id`), resolves the bundle (`GitSourceValidationError` if missing), `_assert_no_oversized_files`, then `_persist_revision` records the remote tree as the synced baseline. Returns the remote HEAD SHA (becomes `last_synced_commit`, so `update_available` is `False`). A `bundle_id` mismatch between the remote manifest and the agent's bundle is **logged as a warning and allowed** (adoption is explicit/user-opted-in), not raised.

**`disconnect(session, agent_id, owner) -> None`** (new)

`_resolve_source_owned` → `session.delete(source)` → commit. Does not touch the remote. Returns via `Message("Git source disconnected")` at the route layer.

**`get_source(session, agent_id, owner) -> tuple[AgentGitSource, bool]`**

Returns `(source, update_available)` where `update_available` is always `False`. Makes no remote network call: releases the DB connection (`session.commit()`), then returns the stored row immediately. The `False` value is a sentinel — callers that need freshness must call `check_updates` instead.

**`check_updates(session, agent_id, owner) -> dict`**

Strict update check via `_compute_update_available_remote` — raises on network/auth failure. Releases the DB connection before the remote call (see Pool-Safety Contract below). Returns `{update_available, remote_commit, last_synced_commit}`.

**`_remote_change_is_relevant(*, repo_url, ref, subdir, last_synced_commit, remote_sha, ssh_key_path) -> bool`** (new — shared helper)

Whether a remote HEAD advance actually concerns this agent's `subdir`, given the caller has already established `remote_sha != last_synced_commit`. No `subdir` or no `last_synced_commit` baseline → always relevant (unchanged root-repo behavior). `subdir` + baseline → relevant only when `subdir_changed_between` reports the subdir tree changed between `last_synced_commit` and `remote_sha`. Single source of truth shared by `_compute_update_available_remote` (drives the "update available" banner) and `_push_locked`'s fast-forward precheck (drives the 409 "pull first" guard) — fixes a prior disagreement where the banner reported no update while push still 409'd on an advance confined to another folder of a shared repo.

**`_compute_update_available_remote(repo_url, ref, subdir, last_synced_commit, key_material) -> tuple[bool, str]`** (private helper, used only by `check_updates`)

Takes captured primitives and in-memory SSH key material rather than a live `(session, source)` pair — so the DB connection is already released before this function is entered. Returns `(update_available, remote_head_sha)`.

Logic (cheap-first):
1. `ls_remote_head` to fetch the current remote HEAD SHA. If HEAD == `last_synced_commit`, returns `(False, HEAD)` with no further work.
2. If no `subdir` (repo root), returns `(True, HEAD)` — every commit touches the root, so a HEAD advance is conclusive.
3. If a `subdir` and `last_synced_commit` baseline are both present, calls `subdir_changed_between` in `git_operations.py`. Equal tree hash ⇒ `(False, HEAD)`; unequal or indeterminate ⇒ `(True, HEAD)`.
4. If `last_synced_commit` is absent and HEAD advanced, returns `(True, HEAD)` conservatively.

**`compute_dirty(session, agent_id, owner) -> dict`** (new)

Read-only comparison of live workspace + DB prompts against the last synced revision. Never pushes. Returns `{dirty, prompts_dirty, workspace_dirty, has_env, last_synced_commit}`. If no env exists: `has_env=False`, `workspace_dirty=False`. Workspace dirty: snapshots live env to temp via `PublishService._snapshot_workspace_tree`, runs `PublishService.hash_workspace_tree(temp/workspace)`, compares against `hash_workspace_tree(revision.snapshot_path/workspace)` resolved via `_resolve_synced_revision`. Prompts dirty: calls `_prompts_dirty(session, install)`. Temp dir removed in `finally`. Best-effort on both axes.

**`compute_status(session, agent_id, owner) -> dict`** (new)

Detailed sibling of `compute_dirty` — the per-file/per-prompt commit preview. Returns `{dirty, has_env, last_synced_commit, prompt_changes, file_changes}` where each change is `{... , change_type}` (`added`/`modified`/`deleted`). Prompts: iterates `_PROMPT_FIELDS` comparing the install vs `_resolve_synced_revision`. Workspace: snapshots the live env via `PublishService._snapshot_workspace_tree` (same post-denylist capture a push produces, so the preview matches the commit — e.g. `__pycache__` never appears), hashes each file via `_file_hashes`, and set-diffs against the synced revision's `workspace/`. Read-only; best-effort (empty lists with no env / no baseline).

**`_file_hashes(workspace_root) -> dict[str, str]`** (new)

Maps each file under a `workspace/` subtree to its SHA-256 (relative POSIX path → hex). Skips symlinks/non-files; missing root → empty. The per-file analogue of `hash_workspace_tree`, used by `compute_status` to classify added/modified/deleted.

**`list_commits(session, agent_id, owner, limit=50) -> list[dict]`** (new)

Delegates to `git_log_subdir(repo_url, ref, subdir, ssh_key_path, max_count=limit)` after resolving the owned source, then attaches a per-commit `commit_url` via `build_web_commit_url(source.repo_url, sha)` (`None` for unsupported hosts). Returns newest-first list of `{sha, short_sha, author_name, author_email, date, message, commit_url}` dicts. `limit` is clamped to 1..200 at the route layer.

**`pull_update(*, session, agent_id, owner) -> Agent`**

Acquires per-agent lock, delegates to `_pull_locked`. On `GitSourceConflictError`, `GitSourceValidationError`, or `GitSourceNotFoundError` — re-raises without stamping `ERROR` (user-actionable, pre-mutation). On any other exception — calls `_mark_source_error` then re-raises.

**`_pull_locked(session, agent_id, owner) -> Agent`**

Direction guard → env existence check → `ls_remote_head` (no-op return if already up to date) → `_assert_not_dirty` (calls `_prompts_dirty` internally) → clone → `_persist_revision` → `_apply_revision_to_install` → advance `last_synced_commit`.

**`push(*, session, agent_id, owner, commit_message, version=None, also_publish_bundle=False) -> AgentGitSource`**

Acquires per-agent lock, delegates to `_push_locked`. Same error-class split as pull.

**`_push_locked(...) -> AgentGitSource`**

Direction guard → env + workspace readable → `also_publish_bundle` precondition check → `ls_remote_head` precheck → full-history clone (`depth=None`) → delete stale `workspace/` subtree → `_capture_and_push` helper → advance `last_synced_commit`. If `also_publish_bundle`: `PublishService.publish` best-effort.

The precheck is subdir-aware: a remote HEAD advance only raises `GitSourceConflictError` (409 "pull first") when `_remote_change_is_relevant` (below) says the advance concerns this agent — a repo-root install (no `subdir`) always blocks; a `subdir` install blocks only when the subdir tree changed since `last_synced_commit`. When the advance is subdir-irrelevant, the precheck does not raise and control falls through to the full-history clone + `_capture_and_push` + `fast_forward_push` exactly as if the remote had not advanced; `fast_forward_push`'s own merge-base ancestor check is unaffected and still raises `GitNonFastForwardError` (409) on a genuine non-fast-forward, so a real conflict the subdir check couldn't see is still caught. This is the same helper `_compute_update_available_remote` uses for the update-check banner, so the two can no longer disagree.

**`_capture_and_push(session, *, install, env, source_like, owner, key, repo, repo_path, commit_message, version, revision_number_hint) -> str`** (new shared helper)

Extracted from the `_push_locked` capture body; also used by `connect`. Builds manifest (`RevisionFormat.build_manifest` with cred/schedule/plugin specs from `PublishService._collect_*`), calls `RevisionFormat.write_tree` (manifest + workspace capture), writes `.gitignore`, asserts no oversized files, `commit_all` (no-op safe), `fast_forward_push`. Persists an `AgentBundleRevision` on every changed push/connect via `_persist_revision` — this gives `compute_dirty` a stable baseline. `source_like` carries `repo_url/subdir/ref/bundle_uuid`; for connect it is the in-memory unsaved source; for push it is the persisted row.

**`_PROMPT_FIELDS: tuple[tuple[str, str], ...]`** (module-level constant)

Maps the four DB prompt field names to their UI labels: `("workflow_prompt", "Workflow prompt")`, `("entrypoint_prompt", "Entrypoint prompt")`, `("refiner_prompt", "Refiner prompt")`, `("router_trigger_prompt", "Router trigger prompt")`. Shared by `_prompts_dirty` (pull guard + dirty endpoint) and `compute_status` (commit preview) so neither can diverge.

**`_prompts_dirty(session, source, install) -> bool`**

Compares the four prompt fields enumerated by `_PROMPT_FIELDS` (`workflow_prompt`, `entrypoint_prompt`, `refiner_prompt`, `router_trigger_prompt`) between the `Agent` row and the revision resolved by `_resolve_synced_revision(session, source, install)`. Returns `True` if any field differs, `False` when there is no synced revision baseline. Shared by `_assert_not_dirty` (pull guard) and `compute_dirty` (dirty endpoint) so they cannot disagree.

**Known limitation — metadata-only edits do not light up dirty.** The 8 definitional metadata fields (`description`, `example_prompts`, `status_refresh_command`, etc.) are NOT compared here, and the workspace dirty check compares file content only. A git-versioned agent where the user edits only `description` or `example_prompts` (without changing any prompt file or workspace file) will show `dirty=false` and `prompts_dirty=false`. The values ARE captured correctly the next time the user pushes (via `build_manifest`), so the data is never lost — the indicator simply does not fire proactively for metadata-only changes. Future work could widen `_PROMPT_FIELDS` or add a dedicated metadata drift check.

**`_resolve_synced_revision(session, source, install) -> AgentBundleRevision | None`**

The dirty-check baseline: prefers the **latest** `AgentBundleRevision` on `source.bundle_uuid` (newest revision, since every sync appends one — using `installed_revision_id` would give the stale checkout revision for a checkout-then-push install). Falls back to `install.installed_revision_id`; returns `None` when no baseline exists. The `source` parameter is the reliable FK — `source.bundle_uuid` may differ from `install.bundle_uuid` for connected installs with no catalog bundle. **Not filtered by `origin`** — both `"publish"` and `"git"` revisions are eligible as the dirty-check baseline; only the Revisions UI listing is filtered.

**`_assert_not_already_checked_out(session, *, user_id, bundle_id) -> None`**

Raises `GitSourceConflictError` (→ 409) if a consumer install (`is_publisher_install=False`) with the same `(owner_id, bundle_id)` already exists. Runs before any bundle/revision row is created.

**`_resolve_or_create_bundle(session, *, bundle_id, user, display_name) -> tuple[AgentBundle, bool]`**

- Ownerless existing row → reuse (returns `created=False`).
- Row owned by requesting user → reuse.
- Row owned by another real publisher and caller is not superuser → `GitSourceConflictError` (409).
- No row → create with `publisher_user_id=None` (ownerless), private, unlisted (returns `created=True`).

**`_persist_revision(session, *, bundle, src, manifest, published_by_user_id) -> AgentBundleRevision`**

Copies `src/workspace/` into `<BUNDLE_STORAGE_DIR>/<bundle_id>/<revision_number>/workspace/` via `iter_bundle_toplevel` + `safe_copytree` (same denylist + symlink guards as publish). Writes `manifest.json`. Computes `content_hash` via `PublishService._hash_tree_with_manifest`. Creates and commits the `AgentBundleRevision` row with `origin="git"` (module constant `REVISION_ORIGIN_GIT` from `backend/app/models/bundles/agent_bundle_revision.py`). The `revision_number` comes from the same global monotonic counter as catalog publishes (`uq_revision_bundle_number` unique constraint and the `<bundle_id>/<rev>/` snapshot path both depend on it). The bundle Revisions UI (`list_revisions_with_install_counts`) filters to `origin="publish"` only, so git baselines are invisible there and do not affect the publish dialog's next-version suggestion. As a consequence, revision numbers in the Revisions UI may show gaps when git operations interleave with catalog publishes — this is expected. `bundle.latest_revision_id` is never updated by `_persist_revision`; it remains publish-set.

**`_assert_not_dirty(session, source, install) -> None`**

Thin raising wrapper over `_prompts_dirty(session, source, install)`; raises `GitSourceConflictError` (→ 409) if it returns `True`.

**`_apply_revision_to_install(session, install, revision) -> None`**

Stop env (if running) → `replace_bundle_content(revision.snapshot_path, env.id)` → reset prompt-sync baselines (`*_synced_hash = None`) → write DB prompt fields from manifest → call `InstallService._apply_revision_metadata(install, revision)` to overwrite the 8 definitional metadata fields (publisher-authoritative, missing-key-tolerant — `NULL` revision column skips that field) → update `installed_revision_id`, `last_sync_at`, `last_update_status = "synced"` → restart env (if it was running).

**`_mark_source_error(session, source_id, exc) -> None`**

Best-effort rollback of any poisoned transaction (uses `get_nested_transaction().rollback()` inside test savepoints, `session.rollback()` in production), then re-fetches the source row and stamps `status = ERROR` + `last_error = str(exc)`. Swallows its own errors so it never masks the original exception.

**`_cleanup_orphan_import(session, revision, bundle) -> None`**

Best-effort removal of a stranded revision row + on-disk snapshot, and — when `bundle` is non-None (this checkout created the row) — the bundle row. Swallows its own errors.

### Per-Agent Locks

`_git_locks: dict[str, asyncio.Lock]` keyed by `str(agent_id)`. `_lock_for(agent_id)` initializes on first access. Serializes concurrent pull/push/connect on one agent, mirroring `PublishService._publish_locks`.

### Pool-Safety Contract

All five read endpoints release the pooled DB connection before any blocking work:

1. **Read phase** (connection open): `_resolve_source_owned` fetches the source row, `_read_ssh_key_material` decrypts the SSH key (if set) into an in-memory `(private_key_bytes, passphrase)` tuple. Both require the DB.
2. **Release** (`session.commit()`): returns the connection to the pool before any remote-git or heavy-filesystem operation starts.
3. **Work phase** (connection released): `_ssh_key_file` context manager writes the in-memory key material to a chmod-600 temp file only for the duration of the git call, then deletes it in `finally`.

The four write paths (checkout, connect, pull, push) retain the connection across git I/O by design — they are low-frequency mutating POSTs and need commit/rollback semantics around the full operation.

### Module-level helpers in `git_source_service.py`

**`_read_ssh_key_material(session, ssh_key_id, owner_id) -> tuple[bytes, str] | None`** — fetches and decrypts the SSH private key via `SSHKeyService.get_decrypted_private_key` (ownership-checked) while the DB connection is open, returning an in-memory `(private_key_bytes, passphrase)` tuple. Returns `None` when `ssh_key_id` is `None`. Must be called **before** `session.commit()` (the pool-release point). Raises `GitSourceValidationError` if the key is not found/owned.

**`_ssh_key_file(key_material: tuple[bytes, str] | None)`** — context manager: if `key_material` is provided, writes a chmod-600 temp file via `create_ssh_key_file` and yields its path; yields `None` otherwise. Used on read paths where the DB connection is already released, so the temp file exists only around the git call.

**`_resolve_ssh_key(session, ssh_key_id, owner_id)`** — context manager retained for the four WRITE paths (checkout, connect, pull, push): decrypts the private key via `SSHKeyService.get_decrypted_private_key` (ownership-checked), writes a chmod-600 temp file via `create_ssh_key_file`, yields the path or `None`. Raises `GitSourceValidationError` if the key is not found/owned. These paths still hold the DB connection across git I/O.

**`_resolve_subdir(repo_path, subdir) -> Path`** — resolves `<repo>/<subdir>`, raises `GitSourceValidationError` if the path escapes the repo root (path traversal guard).

**`_read_and_validate_tree(src) -> dict`** — asserts `snapshot_layout(src) == "v2_workspace"` then calls `RevisionFormat.read_manifest(src)`.

**`_require_bundle_id(manifest) -> str`** — raises `GitSourceValidationError` if `bundle_id` is absent or not a string.

**`_assert_no_oversized_files(workspace_root) -> None`** — walks `workspace_root` via `rglob("*")`, skips symlinks and non-files; raises `GitSourceValidationError` for any file exceeding `settings.GIT_SOURCE_MAX_FILE_BYTES`.

**`_persist_clone_as_snapshot(src, snapshot_dir, manifest) -> str`** — copies cloned workspace into bundle storage (denylist + symlink guards); computes and stamps `content_hash`; writes `manifest.json`.

## `PublishService` — Additions

**`hash_workspace_tree(workspace_root: Path) -> str`** (new static method in `backend/app/services/bundles/publish_service.py`)

SHA-256 over files under a `workspace/` subtree only, with no manifest body included. Stable across rebuilds (excludes `revision_number`, `published_at`, `content_hash`), so two captures of identical files produce equal hashes — the dirty-check primitive. Uses the same walk/sort/byte-feed approach as `_hash_tree_with_manifest`. Used by `GitSourceService.compute_dirty` to compare the live workspace snapshot against the last synced revision's snapshot.

## `RevisionFormat` — (De)Serializer

`backend/app/services/bundles/revision_format.py`. All methods are static; the format is stateless.

### Write side

**`build_manifest(*, install, env, cred_specs, schedule_specs, plugin_specs, revision_number, version, release_notes) -> dict`**

Builds the canonical schema_version-2 manifest dict. `content_hash` is absent at this point (added by `write_tree` after the workspace is captured). When `env` is `None` the SDK and model-override slots are `None`.

Key fields in the manifest:
- `schema_version: 2`
- `bundle_id: str`
- `revision_number: int`
- `version: str | None`
- `published_at: ISO-8601 datetime`
- `prompts: {workflow, entrypoint, refiner, router_trigger}`
- `sdk: {building, conversation, model_override_building, model_override_conversation}`
- `required_credential_specs: [...]` — metadata only; private values stripped by `PublishService._template_payload_for`
- `schedules: [...]`
- `plugin_specs: [...]`
- `metadata: {description, example_prompts, status_refresh_command, agent_api_enabled, agent_api_identity_enabled, a2a_config, agent_sdk_config, webapp_enabled}` — agent-row definitional fields read directly off the publisher's `Agent` row. Enablement flags travel; per-install tokens / grants / UI prefs do NOT. All values may be `null` when the agent has not configured the corresponding feature.
- `release_notes: str | None`

Because both bundle publish and git push (`_capture_and_push`) route through `build_manifest`, both inherit the `metadata` block automatically.

**`write_tree(*, env_workspace_root, dest, manifest, manifest_filename=BUNDLE_MANIFEST_FILENAME) -> str`**

Calls `PublishService._snapshot_workspace_tree(env_workspace_root, dest)` (denylist + symlink guards), computes `content_hash` via `_hash_tree_with_manifest`, stamps it on `manifest`, writes `dest/<manifest_filename>`. Returns the bare hex digest. `manifest_filename` is `"cinna.agent.json"` for git trees, `"manifest.json"` for bundle storage.

### Read side

**`read_manifest(snapshot_path) -> dict`**

Dispatches filename: tries `manifest.json` then `cinna.agent.json`. Validates `schema_version` against `SUPPORTED_SCHEMA_VERSIONS` ({1, 2}). Raises `RevisionFormatError` on missing file, invalid JSON, non-object, or unsupported version.

**`manifest_to_revision_fields(manifest) -> dict`**

Maps manifest keys to `AgentBundleRevision` constructor kwargs: `workflow_prompt`, `entrypoint_prompt`, `refiner_prompt`, `router_trigger_prompt`, `agent_sdk_building`, `agent_sdk_conversation`, `model_override_building`, `model_override_conversation`, `required_credential_specs`, `schedules`, `plugin_specs`, `version`, `release_notes`, and the 8 metadata fields: `description`, `example_prompts`, `status_refresh_command`, `agent_api_enabled`, `agent_api_identity_enabled`, `a2a_config`, `agent_sdk_config`, `webapp_enabled`. Does not produce `bundle_id` (FK uuid), `revision_number`, `snapshot_path`, `content_hash`, or `published_by_user_id` — those are caller-supplied.

Missing-key-tolerant: the `metadata` sub-dict is read with `.get()` defaults — a manifest written before this block was added yields `None` for all 8 metadata kwargs, mapping to `NULL` columns on the revision row, which the restore side treats as "do not overwrite the consumer's current value".

### Git-ignore generation

**`generate_gitignore() -> str`**

Derives `.gitignore` lines from `BUNDLE_EXCLUDED_TOPLEVEL | RUNTIME_NAME_DENYLIST` (toplevel workspace dirs), `PLUGIN_DERIVED_FILES` (scoped under `workspace/plugins/`), and the recursive cache denylist `NESTED_EXCLUDED_DIRS` (`__pycache__/`, …) + `NESTED_EXCLUDED_FILE_GLOBS` (`*.pyc`, `*.pyo`) emitted unscoped so they match at any depth. Sorted for stable diffs. Single source of truth; derived from the same constants as the snapshot denylist, so the `.gitignore` and the copy walk can never disagree.

## `workspace_classification.py` — Recursive Cache Denylist

`backend/app/services/environments/workspace_classification.py` is the single source of truth for what the workspace copy walk captures. New for this work:

- `NESTED_EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}` and `NESTED_EXCLUDED_FILE_GLOBS = ("*.pyc", "*.pyo")` — regenerated caches that must never be snapshotted/committed.
- `is_nested_excluded(name)` — exact-name match against the dirs set + `fnmatch` against the globs.
- `_copytree_ignore` (renamed from `_ignore_symlinks`) — the `shutil.copytree(ignore=...)` callback used by `safe_copytree`; now drops both symlinks AND nested-excluded names at **every depth**.
- `is_bundle_owned_toplevel` also returns `False` for nested-excluded names (so a top-level `__pycache__` is dropped too).

Because `safe_copytree` is the shared copy primitive, this exclusion applies uniformly to publish, install seed, env migration, and git push. It was the fix for the original bug (a nested `agent_api/__pycache__/*.pyc` was being committed to the remote).

## `git_operations.py` — Provider-Aware Web URLs

Browser-link builders for the agent's remote, kept beside the host-parsing helpers. **GitHub only today**, designed to extend by one registry entry.

- `_split_host_path(git_url) -> (host, owner/repo)` — parses schemeless SSH (`git@host:…`), `ssh://`, and HTTP(S) URLs; strips a trailing `.git`; `(None, None)` if unparseable.
- `GitWebProvider` (frozen dataclass) — `{name, hosts, history_path(ref, subdir), commit_path(sha), tree_path(ref, subdir)}`; each `*_path` returns the path after the repo root.
- `_WEB_PROVIDERS` — the registry tuple (GitHub entry + commented Bitbucket/GitLab examples). `_ref_subdir_path("/commits"|"/tree", ref, subdir)` is the shared GitHub/GitLab path shape.
- `_resolve_web_provider(repo_url) -> (provider, repo_web_base)` — the single place that decides whether web-URL generation is available; matches the host against the registry; `(None, None)` otherwise.
- `build_web_history_url(repo_url, ref, subdir)` → `…/commits/<ref>[/<subdir>]`
- `build_web_tree_url(repo_url, ref, subdir)` → `…/tree/<ref>[/<subdir>]`
- `build_web_commit_url(repo_url, sha)` → `…/commit/<sha>` (singular); `None` for empty `sha`.

All return `None` for unsupported hosts so callers hide the link rather than render a broken one.

## `git_operations.py` — Git Primitives (Extended)

`backend/app/services/knowledge/git_operations.py`. Uses GitPython. All network calls are egress-guarded via `assert_git_url_allowed`.

### Extensions added for git versioning

**`assert_git_url_allowed(git_url) -> None`**

SSRF chokepoint for every outbound git network call. For HTTPS: delegates to `assert_url_allowed(url, allow_private_hosts=settings.GIT_SOURCE_ALLOW_PRIVATE_HOSTS)`. For SSH (`git@host:...`): extracts the host with `_SSH_HOST_PATTERN` and calls `assert_host_allowed(host, allow_private_hosts=...)`. Raises `EgressBlockedError` if blocked.

**`ls_remote_head(git_url, ref="main", ssh_key_path=None) -> str`**

Resolves the SHA a remote ref points at without cloning. Tries `refs/heads/<ref>`, then `refs/tags/<ref>`, then the raw ref string. For annotated tags, prefers the `^{}` dereferenced commit SHA over the tag object SHA (avoids spurious "update available" mismatches). Applies `GIT_SOURCE_NETWORK_TIMEOUT_SECONDS` via `kill_after_timeout` (SIGKILL on overrun). Raises `GitOperationError` if the ref is not found, `GitAuthenticationError` on auth failures, `GitConnectionError` on network failures.

**`commit_all(repo, message, author_name, author_email) -> str`**

Stages all changes (`git add -A`), checks if the index matches HEAD (no-op safe — returns current HEAD SHA without an empty commit), then commits with the given author. Returns the new commit SHA.

**`fast_forward_push(repo, ref="main", ssh_key_path=None) -> None`**

Fetches remote `ref`, asserts the local branch is an ancestor-or-equal of the remote (merge-base check). Raises `GitNonFastForwardError` if the remote advanced. Pushes `ff-only` (no `--force`); remote-side rejection also maps to `GitNonFastForwardError`. Requires a full-history clone (not shallow) for the merge-base check. An absent remote ref (first push to an empty remote or new branch) is treated as ancestor-OK — the merge-base check passes and the push creates the ref on the remote.

**`clone_repository(git_url, destination, branch, ssh_key_path, depth=1) -> Repo`**

`depth=None` = full-history clone (required for push). Applies egress guard before cloning. Handles URL conversion (SSH key present → SSH URL, no key → HTTPS URL).

**`clone_repository_context(git_url, branch, ssh_key_path=None, base_dir=None, depth=1)`**

Context manager wrapping `clone_repository`; yields `(repo_path, repo)`; removes the temp directory in `finally`.

**`init_repo_with_remote(*, workdir, repo_url, ref="main", ssh_key_path=None) -> Repo`** (new)

`git init` a fresh working tree, add `origin`, create branch `ref`. For the empty-remote / absent-ref bootstrap case where there is nothing to clone. Egress-guarded via `assert_git_url_allowed` before the remote is added. Returns a `Repo` with no commits yet; the caller writes the tree, calls `commit_all`, then `fast_forward_push` (which creates the branch on the remote).

**`git_log_subdir(*, repo_url, ref="main", subdir=None, ssh_key_path=None, max_count=50) -> list[dict]`** (new)

Returns up to `max_count` commits touching `subdir`, newest first. Each dict: `{sha, short_sha, author_name, author_email, date (ISO-8601), message}`. Egress-guarded. Uses a bounded shallow clone (`depth=max_count`) into a temp dir, then `git log --max-count=N -- <subdir>/` with a `%x1f`/`%x1e`-delimited format for safe field splitting. Applies `GIT_SOURCE_NETWORK_TIMEOUT_SECONDS` via `kill_after_timeout` on the clone. Temp dir removed in `finally`. Errors map through the existing typed git errors.

**`subdir_changed_between(*, repo_url, ref, subdir, base_commit, ssh_key_path=None) -> bool`** (new)

Compares the tree-object hash of `<subdir>/` at the tip of `ref` against its hash at `base_commit`. Returns `True` if the subdir changed (or the outcome is indeterminate), `False` if the tree hashes match (subdir untouched). Used by `_compute_update_available_remote` as the second-stage check for subdir-scoped installs.

Implementation:
1. `depth=1` shallow clone of `ref` into a temp dir. Egress-guarded via `assert_git_url_allowed` before the clone. `GIT_SOURCE_NETWORK_TIMEOUT_SECONDS` applied via `kill_after_timeout`. For SSH transports: `ConnectTimeout`, `ServerAliveInterval`, `ServerAliveCountMax` are injected via `GIT_SSH_COMMAND`. For HTTP(S) transports: `GIT_HTTP_LOW_SPEED_LIMIT`/`GIT_HTTP_LOW_SPEED_TIME` are set in the subprocess env.
2. `git fetch --depth=1 <base_commit>` to make the base commit reachable. A second `assert_git_url_allowed` call re-asserts the same URL before this follow-up fetch (DNS-rebind defense). Same timeout settings apply.
3. `git rev-parse HEAD:<subdir>` → tree hash at tip; `git rev-parse <base_commit>:<subdir>` → tree hash at base.
4. Equal hashes → return `False` (subdir unchanged). Unequal → return `True`.
5. Any exception (server disallows fetch-by-reachable-SHA, base commit GC'd or rewritten, subdir missing at one revision, auth failure) → return `True` conservatively. On git hosts that disallow fetch-by-SHA (some self-hosted Gitea/GitLab without `uploadpack.allowReachableSHA1InWant`), this degrades gracefully to the legacy always-`True` behavior.

Call sites: `_remote_change_is_relevant`, invoked from both `_compute_update_available_remote` (the update-check path) and `_push_locked`'s fast-forward precheck (previously only the update check used it — the push precheck now shares the same subdir-relevance decision).

### Existing primitives (unchanged, now egress-guarded)

- `verify_repository_access` — `ls-remote` check without cloning; also egress-guarded.
- `pull_repository` — pull into an existing working copy; egress-guarded.
- `get_current_commit_hash(repo) -> str` — returns `repo.head.commit.hexsha`.
- `create_ssh_key_file(private_key, passphrase)` — context manager; chmod-600 temp file; deleted in `finally`.
- `convert_https_to_ssh_url` / `convert_ssh_to_https_url` — URL format converters.

### Typed errors

- `GitOperationError` — base; maps to 400.
- `GitAuthenticationError(GitOperationError)` — maps to 400 (never 401/403 — see the error-mapping note above).
- `GitConnectionError(GitOperationError)` — maps to 400.
- `GitNonFastForwardError(GitOperationError)` — maps to 409.

## Egress Guard (`services/common/egress_guard.py`)

Promoted from `services/mcp_providers/egress_guard.py` (re-exported from the old path for backward compatibility). The private-host policy is now a per-call argument rather than a module constant:

- MCP callers: `allow_private_hosts=None` → defaults to `settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS` (unchanged behavior).
- Git callers: `allow_private_hosts=settings.GIT_SOURCE_ALLOW_PRIVATE_HOSTS` (explicit).

Functions:
- `validate_external_endpoint_url(url, *, allow_private_hosts=None) -> str` — static (no DNS): scheme + host shape + literal-IP private-range check.
- `is_host_blocked(host, *, allow_private_hosts=None) -> bool` — DNS-resolving range check (DNS-rebind defense; checks every resolved address).
- `assert_url_allowed(url, *, allow_private_hosts=None) -> str` — combines both; the chokepoint for HTTPS targets.
- `assert_host_allowed(host, *, allow_private_hosts=None) -> str` — host-only variant for SSH git URLs.

## GIT_SOURCE Webhook Type

`AgentWebhookType.GIT_SOURCE = "git_source"` in `backend/app/models/agents/agent_webhook.py`.

**`AgentWebhookCreateGitSource(SQLModel)`** — `name: str`, `payload_template: str | None`. No type-specific fields; the existing token + `agent_id` on `AgentWebhook` are sufficient.

**`AgentWebhookService.create_git_source_webhook(db_session, agent_id, user_id, data)`** — mirrors `create_session_webhook` (Fernet token, unique `webhook_id`, one-time reveal). Sets `type=AgentWebhookType.GIT_SOURCE`; all other webhook columns not relevant to git source are `None`.

**`AgentWebhookService.fire_webhook` dispatch for `git_source`** — calls `GitSourceService.pull_update(session, webhook.agent_id, owner=<webhook owner>)`. Reuses the "always 200 with log_id post-auth" contract and the immutable invocation-log write. Payload body is ignored (pull always fetches `ls-remote HEAD`).

**Route:** `POST /agents/{agent_id}/webhooks/git-source` in `agent_webhooks.py` — `dependencies=[Depends(require_developer)]`. Returns `AgentWebhookPublicWithToken` with the plaintext token shown once.

**Public dispatch:** `backend/app/api/routes/agent_hooks.py` — unchanged. The git-host posts to `{host}/agent-hooks/{webhook_id}` with the bearer token; `fire_webhook` routes by `webhook.type` to the git-source pull path.

## Frontend Components

### `GitVersioningCard.tsx`

`frontend/src/components/Agents/GitVersioningCard.tsx`. The primary UI for the entire feature. Mounted in `AgentIntegrationsTab` alongside `LocalDevCard`, `AgentRestApiCard`, `McpConnectorsCard`. Owner-gated (hidden from `agent-user` role).

**Props.** `agentId`, `agentName`, and `gitVersioningEnabled` (from `agent.git_versioning_enabled`). The toggle's checked state is `effectiveConnected = sourceResolved ? connected : gitVersioningEnabled` — it shows the real status from first paint (no off→on flash); the internals load behind a "Loading git versioning…" spinner (`showInternalsSpinner`).

**Connected layout.** Repo URL is a link to `web_tree_url` when present; coordinates render as `CodeChip`s — each carries a leading icon to distinguish them at a glance: a `Folder` icon on the subdir chip and a `GitBranch` icon on the branch chip — plus a `SyncDirectionIcon` (icon + tooltip, replacing the text direction). The `connected` status renders as a green `CheckCircle2` icon (tooltip "Connected") instead of a text badge; only the `error` status still renders a destructive badge (with `last_error`). "Latest commits" shows the 3 most recent with clickable `commit_url` SHAs and a "View history" link (`web_history_url`). All actions live in a `CardFooter`: left = Commit Agent + status reason + a **Refresh** icon button (`handleRefresh` invalidates git-dirty/status/source/commits); right = icon-only Disconnect. The commit dialog renders `CommitPreview` (from `getGitStatus`, fetched only while the dialog is open).

**Connect form.** Fields render top-to-bottom: repo URL, a two-column row with **Branch / ref first, Subdirectory second**, then sync direction and deploy key. Sync direction and deploy key use a right-aligned row layout (`flex items-center justify-between` with a `w-[200px] shrink-0` control), mirroring the **Communication & Locale** settings card. The repo URL input normalizes a pasted HTTP(S) URL to the SSH (`git@host:owner/repo.git`) form on blur via the module-level `toGitSshUrl` helper (mirrors the backend `convert_https_to_ssh_url`); SSH URLs and unrecognized strings pass through unchanged.

**Adopt flow.** `connectMutation` takes an `adoptExisting` boolean. On a connect error, `isExistingFolderError` (checks `status===409 && body.detail.code==="existing_agent_folder"`) opens an adopt confirm dialog; confirming re-runs `mutate(true)`.

React Query hooks:

| Hook | Service call | Query key | Notes |
|---|---|---|---|
| source status | `AgentGitService.getGitSource` | `["git-source", agentId]` | drives connected/disabled + web URLs; `staleTime: 30 000 ms`; `update_available` always `false` (use check-updates for freshness) |
| check-updates | `AgentGitService.checkGitUpdates` | `["git-check-updates", agentId]` | drives the update banner; `staleTime: 30 000 ms`; enabled when connected |
| dirty | `AgentGitService.getGitDirty` | `["git-dirty", agentId]` | `refetchOnWindowFocus` disabled; `staleTime: 30 000 ms`; gates "Commit Agent"; `isFetching` spins Refresh |
| status (preview) | `AgentGitService.getGitStatus` | `["git-status", agentId]` | enabled only while the commit dialog is open |
| commits | `AgentGitService.listGitCommits` | `["git-commits", agentId, LATEST_COMMITS_COUNT]` | enabled when connected; limit 3 |
| connect | `AgentGitService.connectGitSource` | — | `mutate(adoptExisting)`; invalidates git-source/commits/dirty/status + `["agent", agentId]` |
| push | `AgentGitService.pushGitSource` | — | invalidates `git-commits`, `git-dirty`, `git-source`, `git-status` |
| pull | `AgentGitService.pullGitSource` | — | invalidates all + `["agent", agentId]` |
| disconnect | `AgentGitService.disconnectGitSource` | — | **removeQueries** git-source/dirty/status/commits + invalidate `["agent", agentId]` → card resets to disabled |
| ssh keys | `SshKeysService.listSshKeys` / `generateSshKey` | `["sshKeys"]` | deploy-key picker |

### `DeployKeySelect.tsx`

`frontend/src/components/Agents/DeployKeySelect.tsx`. Deploy-key picker used inside the connect form, laid out as a right-aligned row (label + guidance left, `w-[200px] shrink-0` select right).

- Lists the user's SSH keys via `SshKeysService.listSshKeys`.
- Options: pick an existing key, "None (public repo)", "Generate a new key…", "Import an existing key…".
- Key creation reuses the **same modal dialogs as the Settings → SSH Keys management screen** — `GenerateKeyModal` and `ImportKeyModal` (`frontend/src/components/UserSettings/`), rather than inline forms. Each modal gained an optional callback (`onGenerated` / `onImported`, both non-breaking) so the picker can auto-select the freshly created key by id. The modals own the public-key reveal, copy, and deploy-key guidance.
- Returns the chosen `ssh_key_id | null` to the connect form. Private-key material is never displayed.

## Migrations

### `391a6285d8ff_add_agent_git_source` (first migration)

- `down_revision = b2d1f4c6a8e3`
- `upgrade`: `create_table("agent_git_source", ...)` with all columns, FKs, and indexes. Enum-like columns stored as plain `String` (not pg enum) to avoid migration churn.
- `downgrade`: drop indexes, drop table.

### `c8a4f1e09b27_agent_bundle_publisher_nullable` (second migration)

- `down_revision = 391a6285d8ff`
- `upgrade`: `alter_column("agent_bundle", "publisher_user_id", nullable=True)` — enables ownerless git-imported bundle rows.
- `downgrade`: `DELETE FROM agent_bundle WHERE publisher_user_id IS NULL` (removes git-imported rows), then re-imposes `nullable=False`. This is destructive — ownerless rows must be absent before downgrading.

### `878bc3f6579f_add_revision_origin` (bundle migration, not git-versioning-specific)

- `down_revision = d9b3e1a7c45f` (follows the `agent_bundle_revision_metadata` migration)
- `upgrade`: adds `origin varchar(32) NOT NULL server_default 'publish'` to `agent_bundle_revision`. Existing rows (including any git baselines) are backfilled to `'publish'`; they are indistinguishable from catalog publishes and that is acceptable — the origin discriminator is new.
- `downgrade`: drops the column.
- See [Agent Bundles — Technical Reference](../agent_bundles/agent_bundles_tech.md) for the full migration table.
