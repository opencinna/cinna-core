# Git-Backed Agent Versioning — Technical Reference

## File Locations

### Models
- `backend/app/models/bundles/agent_git_source.py` — `AgentGitSourceBase`, `AgentGitSource` (table), `AgentGitSourcePublic`, `AgentGitSourceCreate`, `AgentGitSourceUpdate`, `GitSyncDirection`, `GitSourceStatus`

### Services
- `backend/app/services/bundles/git_source_service.py` — `GitSourceService` (checkout / connect / disconnect / pull / push / check-updates / get-source / compute_dirty / compute_status / list_commits / `_connect_adopt_existing` / `_file_hashes` / `_settings_changes` / `_pull_blocking_changes` (new — shared blocking-set helper) / `_capture_backup_revision` (new — pre-pull backup) / `_build_live_manifest` (new — shared by push capture and the backup) / `_discard_backup_revision` (new — rolls back an orphaned backup)); module constants `_PROMPT_FIELDS`, `_METADATA_FIELDS`, `_SDK_FIELDS`, `_SPEC_FIELDS`, `_SETTING_SECTIONS`, `_PULL_OVERWRITTEN_SECTIONS`, `GIT_PULL_TAKE_REMOTE` / `GIT_PULL_KEEP_LOCAL` / `GIT_PULL_RESOLUTIONS` (new — the two `conflict_resolution` values, validated service-side), `_PULL_LOCAL_CHANGES_MESSAGE` (new — the 409 fallback message); normalizers `_canonical_json_value` / `_sorted_json` / `_normalize_setting_value` / `_classify_change`; typed errors `GitSourceError`, `GitSourceNotFoundError`, `GitSourceValidationError`, `GitSourceConflictError`, `GitSourceExistingAgentError` (subclass of conflict), `GitSourceLocalChangesError` (new — subclass of conflict, carries the `blocking` list for the structured pull 409), `GitBaselineUnavailableError`
- `backend/app/services/bundles/revision_format.py` — `RevisionFormat` (de)serializer; `RevisionFormatError`; `generate_gitignore` (now also emits the recursive cache denylist); constants `BUNDLE_MANIFEST_FILENAME`, `GIT_MANIFEST_FILENAME`, `REVISION_SCHEMA_VERSION`, `SUPPORTED_SCHEMA_VERSIONS`
- `backend/app/services/bundles/publish_service.py` — `PublishService._snapshot_workspace_tree`, `PublishService.hash_workspace_tree` (workspace-only stable digest for dirty check)
- `backend/app/services/environments/workspace_classification.py` — denylist single source of truth: `NESTED_EXCLUDED_DIRS` / `NESTED_EXCLUDED_FILE_GLOBS` / `is_nested_excluded` (new — recursive cache exclusion), `safe_copytree` + `_copytree_ignore` (renamed from `_ignore_symlinks`; now drops symlinks AND nested cache artifacts), `is_bundle_owned_toplevel` (also rejects nested-excluded names)
- `backend/app/services/knowledge/git_operations.py` — git primitives (extended for git versioning): `clone_repository`, `clone_repository_context`, `pull_repository`, `ls_remote_head`, `commit_all`, `fast_forward_push`, `get_current_commit_hash`, `create_ssh_key_file`, `verify_repository_access`, URL converters, `assert_git_url_allowed`, `init_repo_with_remote`, `git_log_subdir`; provider-aware web URLs: `GitWebProvider` dataclass + `_WEB_PROVIDERS` registry + `_resolve_web_provider` + `_split_host_path`, and builders `build_web_history_url` / `build_web_commit_url` / `build_web_tree_url` (new); typed errors `GitOperationError`, `GitAuthenticationError`, `GitConnectionError`, `GitNonFastForwardError`
- `backend/app/services/common/egress_guard.py` — `assert_url_allowed`, `assert_host_allowed`, `is_host_blocked`, `validate_external_endpoint_url`, `EgressBlockedError`; generalized from the original MCP provider module; honors `GIT_SOURCE_ALLOW_PRIVATE_HOSTS` per call
- `backend/app/services/agents/agent_webhook_service.py` — `create_git_source_webhook`, `fire_webhook` dispatch for `AgentWebhookType.GIT_SOURCE`
- `backend/app/services/agents/agent_service.py` — `compute_capability_flags` (now also computes `git_versioning_enabled` from `AgentGitSource` presence) + `to_public_with_clone_info` (sets it on `AgentPublic`)

### API Routes
- `backend/app/api/routes/agent_git.py` — all git-versioning routes (see table below); request/response models `AgentCheckoutRequest`, `AgentCheckoutResponse`, `AgentGitConnectRequest` (with `adopt_existing`), `GitPushRequest`, `GitPullRequest` (new — optional pull body carrying `conflict_resolution`), `GitUpdateStatus`, `GitCommit` (with `commit_url`), `GitCommitList`, `GitDirtyStatus`, `GitStatus` / `GitPromptChange` / `GitSettingChange` (both now carry `blocks_pull`) / `GitFileChange` — commit preview and pull-conflict preview; helper `_git_source_to_public` (sets `web_history_url` + `web_tree_url`); error mapping `_map_git_error` (plus dedicated structured-409 branches for `GitSourceExistingAgentError` and, inline in the pull handler, `GitSourceLocalChangesError`)
- `backend/app/api/routes/agent_webhooks.py` — `POST /agents/{id}/webhooks/git-source` (developer-gated)
- `backend/app/api/routes/cli.py` — `GET /cli/git-coordinates` → `CliGitCoordinates`; auth via `CLIContextDep`

### Frontend
- `frontend/src/components/Agents/GitVersioningCard.tsx` — the "GIT Versioning" card in the Integrations tab; manages disabled/connect-form/connected states; react-query hooks for source, dirty, status (commit preview + pull-conflict preview), commits, push, pull, connect, disconnect. Takes a `gitVersioningEnabled` prop for the instant toggle state. Footer actions (Commit Agent + Refresh + icon Disconnect), Latest-commits (3) + View-history/clickable-SHA links, clickable repo name, icon-bearing code-chip coordinates (folder/branch) + sync-direction icon, green-check `connected` status icon, `CommitPreview` (git-status dialog, three sections: Prompts / Agent settings / Workspace, now importing `CHANGE_META`/`ChangeRow` from `GitChangeList.tsx`), the adopt-existing-folder confirm dialog, and the **update banner** (switches to "Review & pull" copy + opens `GitPullConflictDialog` when `update_available && dirty`, plain "Pull" otherwise — `pullNeedsReview` derived state). `pullMutation` sends `requestBody: resolution ? { conflict_resolution: resolution } : undefined` (a bodiless call on the plain-Pull path, preserving fail-loud semantics) and its `onSuccess` toast copy branches on `resolution` (`keep_local` / `take_remote` / plain). Helpers: `isExistingFolderError`, `isLocalChangesError` (new — both built on a shared `isRecoverableConflict(error, code)`), `CodeChip` (optional leading icon), `SyncDirectionIcon`, `CommitPreview`, `toGitSshUrl` (HTTP→SSH repo-URL normalizer)
- `frontend/src/components/Agents/GitPullConflictDialog.tsx` (new) — the pull-conflict dialog: "Incoming" / "Blocks the pull" (Prompts + Agent settings, from `blocks_pull`) / "Will be replaced by this pull" (workspace files) / "Not touched" sections, plus a footer of **Discard my changes and take remote** (left) / **Keep my changes** (right), each behind its OWN `AlertDialog` confirmation with per-action copy. There is deliberately no Cancel button: both actions replace the workspace and restart the environment, so a dismissal sitting beside them implied a symmetry that does not exist; Esc / the close affordance / the confirmation's own Cancel are the exits. Every change row is clickable, opening `GitDiffDialog` for that item (rows whose payload carries no raw `key` — an older 409 — stay plain text, since the diff endpoint could not be addressed). Reached pre-emptively (opened by the card's update banner, seeded empty) or reactively (opened from the pull mutation's `onError`, seeded from a 409's `detail.blocking`). Fetches its own `["git-status", agentId]` query while open (`staleTime: 0`, `retry: false` — a destructive choice must be made against a fresh read, not a value cached from before whatever just changed); a seeded 409 blocking list always wins over a fresher-but-still-stale-relative-to-it status read. Exports `localChangesBlocking(error)` (reads `detail.blocking` off a `local_changes` 409, used by the card) and the `GitBlockingChange` / `GitPullResolution` types.
- `frontend/src/components/Agents/GitChangeList.tsx` (new) — `CHANGE_META` (change_type → single-letter tag + color, `git status --short` style) and the `ChangeRow` / `ChangeGroup` presentational components, extracted so `CommitPreview` (in `GitVersioningCard.tsx`) and `GitPullConflictDialog.tsx` render the same change-list visuals from opposite directions (what a commit would capture vs. what a pull would overwrite) without drifting apart. `ChangeRow` takes an optional `onOpenDiff`; when given, the label renders as a real `<button>` (not a clickable `<span>` — these rows live inside dialogs, where keyboard reachability is the only mouse-free path to reviewing a change).
- `frontend/src/components/Agents/GitDiffDialog.tsx` (new) — the per-item diff modal behind every clickable change row. Fetches `["git-diff", agentId, section, key]` (`staleTime: 0`, `retry: false`) and renders the unified diff as a `<pre>` of per-line-colored rows. Guards against React Query serving the *previous* key's body during a target switch (`isCurrent` compares the response's `section`/`key` against the open target) — otherwise the last file's diff flashes under this file's title. Header-line coloring is ordered so `---`/`+++` match before the `-`/`+` add/remove cases, or the file headers render as a deletion and an insertion. Exports the `GitDiffTarget` type. Mounted twice, independently: inside `GitPullConflictDialog` and inside the card's commit dialog (the two can be open at different times and must not share open-state).
- `frontend/src/components/Agents/DeployKeySelect.tsx` — deploy-key picker reusing `SshKeysService.listSshKeys`; generate/import open the shared `GenerateKeyModal` / `ImportKeyModal` dialogs (auto-selecting the new key via their `onGenerated`/`onImported` callbacks)
- `frontend/src/components/UserSettings/GenerateKeyModal.tsx`, `ImportKeyModal.tsx` — the shared SSH-key dialogs; each accepts an optional `onGenerated`/`onImported` callback so callers (the deploy-key picker) can auto-select the created key
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` — mounts `<GitVersioningCard>` in the card grid (now positioned **before** the Webhooks / Local Dev / Email Integration cards); owner-gated; passes `gitVersioningEnabled={agent.git_versioning_enabled}`
- `frontend/src/components/Agents/AgentCard.tsx` — agents-list card; renders a **GIT** capability badge (`GitBranch` icon) when `agent.git_versioning_enabled`
- `frontend/src/utils.ts` — `getErrorMessage(error, fallback)` (shared, pre-existing helper used well beyond this card) gained an object-`detail` guard: a structured 409 (`existing_agent_folder`, `local_changes`) carries `body.detail` as an object rather than a string, and the un-guarded version rendered it as `[object Object]` in a toast; it now falls through to `detail.message` when `detail` is an object

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
- `settings_dirty: bool = False` — any **non-prompt** `cinna.agent.json` field (metadata, SDK, schedules, plugins) diverging from the baseline
- `workspace_dirty: bool`
- `has_env: bool`
- `last_synced_commit: str | None = None`

**`GitStatus`** (commit preview AND pull-conflict preview, response of `GET /git/status`)
- `dirty: bool`
- `has_env: bool`
- `last_synced_commit: str | None = None`
- `pull_blocked: bool = False` — whether a bodiless `POST /git/pull` would 409 right now (any change below carries `blocks_pull`). Computed by `_pull_blocking_changes`, the same helper `_assert_not_dirty` raises from, so this preview and the 409 it explains can never disagree
- `prompt_changes: list[GitPromptChange] = []`
- `setting_changes: list[GitSettingChange] = []`
- `file_changes: list[GitFileChange] = []`

**`GitPromptChange`**
- `field: str` (human label, e.g. "Workflow prompt")
- `key: str = ""` — raw column name (`workflow_prompt`); the diff endpoint's key
- `section: str = "prompt"`
- `change_type: str` — `added` | `modified` | `deleted`
- `blocks_pull: bool = False` — always `true` for a reported prompt change; a pull rewrites all four prompt columns wholesale

**`GitSettingChange`**
- `field: str` (human label, e.g. "Example prompts", "Schedules")
- `key: str = ""` — raw attribute name (`example_prompts`, `agent_sdk_conversation`, …)
- `section: str = ""` — owning registry: `metadata` / `sdk` / `specs`. Required alongside `key` to address a diff: the registries are what resolve the *live* side (an `Agent` column, an `AgentEnvironment` column, or a collector call), so the key alone is not enough
- `change_type: str` — `added` | `modified` | `deleted`
- `blocks_pull: bool = False` — `true` only for the `metadata`-section fields the pull guard actually narrows on (`_PULL_OVERWRITTEN_SECTIONS` + the non-NULL-baseline guard); schedules, plugins, and env SDK selections are always `false`

**`GitDiff`** (response of `GET /agents/{agent_id}/git/diff`)
- `section: str`, `key: str` — echoed back, so a client switching targets can tell whose body it is holding
- `label: str` — human label (the path itself, for a file)
- `change_type: str` — `added` | `modified` | `deleted` | `unchanged`
- `diff: str = ""` — unified-diff text; `""` when the sides are equal or `binary`
- `binary: bool = False`, `truncated: bool = False`

**`GitFileChange`**
- `path: str` (workspace-relative POSIX path)
- `change_type: str` — `added` | `modified` | `deleted`
- No `blocks_pull` field — workspace files never block a pull, they are *replaced* by it wholesale whenever an env exists (a property of the operation, not of any one file)

**`GitPullRequest`** (optional body of `POST /agents/{agent_id}/git/pull`)
- `conflict_resolution: str | None = None` — one of `GIT_PULL_RESOLUTIONS` (`keep_local` / `take_remote`). Omitting the field, or the whole body, keeps the historical fail-loud 409 the GitOps webhook path relies on. Shaped as a single scalar so a future per-field resolution (`keep_fields: [...]`) can be added without a breaking change. Validated in the service, not the route — the service is the sole enforcement point for every caller

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
| `GET` | `/agents/{agent_id}/git/diff` | `CurrentUser` (owner-resolved) | `section`, `key` (query) | `GitDiff` | Unified diff of ONE prompt / setting / workspace file; `key` is allowlisted against the field registries + workspace denylist |
| `POST` | `/agents/{agent_id}/git/pull` | `require_developer` | `GitPullRequest \| None` (optional) | `AgentPublic` | Developer-gated; per-agent locked; blocking local drift with no `conflict_resolution` → recoverable 409 (`local_changes`); see Error Mapping |
| `POST` | `/agents/{agent_id}/git/push` | `require_developer` | `GitPushRequest` | `AgentGitSourcePublic` | Developer-gated; per-agent locked; persists `AgentBundleRevision` |
| `POST` | `/agents/{agent_id}/webhooks/git-source` | `require_developer` | `AgentWebhookCreateGitSource` | `AgentWebhookPublicWithToken` | Registers GitOps trigger; token shown once |
| `GET` | `/cli/git-coordinates` | `CLIContextDep` (agent-scoped CLI token) | — | `CliGitCoordinates` | No deploy key in response; no developer-role gate |

### Error Mapping (`_map_git_error`)

| Exception | HTTP status |
|-----------|-------------|
| `GitSourceNotFoundError` | 404 |
| `GitSourceExistingAgentError` | 409 with structured `detail={"code": "existing_agent_folder", "message": ...}` (caught explicitly in the connect route **before** the generic conflict branch, since it subclasses `GitSourceConflictError`) |
| `GitSourceLocalChangesError` | 409 with structured `detail={"code": "local_changes", "message": ..., "blocking": [{"section", "field", "change_type"}, ...]}` (caught explicitly in the pull route handler, inline rather than via `_map_git_error`, **before** the generic conflict branch — same ordering rule as `GitSourceExistingAgentError`, since it also subclasses `GitSourceConflictError`) |
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

Read-only comparison of the live install against the last synced revision across the **three axes of a git tree**. Never pushes. Returns `{dirty, prompts_dirty, settings_dirty, workspace_dirty, has_env, last_synced_commit}`; `dirty` is the OR of the three. If no env exists: `has_env=False`, `workspace_dirty=False`.

Resolves the sync baseline once via `_resolve_synced_revision` and passes it to all three axis checks:

- *Prompts* (`manifest["prompts"]`): `_prompts_changed(install, synced_rev)` — the pure, already-resolved-baseline variant, so the one `_resolve_synced_revision` call above is reused rather than re-resolved.
- *Settings* (the rest of `cinna.agent.json`): `bool(_settings_changes(session, install, synced_rev, env, stop_early=True))` — stops at the first detected change since only a boolean is needed here.
- *Workspace* (`workspace/`): snapshots live env to temp via `PublishService._snapshot_workspace_tree`, runs `PublishService.hash_workspace_tree(temp/workspace)`, compares against `hash_workspace_tree(revision.snapshot_path/workspace)` resolved from the same `synced_rev`. Temp dir removed in `finally`.

Best-effort on every axis.

**`compute_status(session, agent_id, owner) -> dict`**

Detailed sibling of `compute_dirty` — the per-file/per-prompt/per-setting commit preview, doubling as the pull-conflict preview. Returns `{dirty, has_env, last_synced_commit, pull_blocked, prompt_changes, setting_changes, file_changes}` where each prompt/setting change is `{field, change_type, blocks_pull}` and each file change is `{path, change_type}`. Calls `_pull_blocking_changes` once (the same helper `_assert_not_dirty` raises from) to get the blocking `{(section, field)}` set; every prompt change is trivially blocking (a pull rewrites all four columns) so `prompt_changes` is projected directly from the blocking list rather than recomputed; `setting_changes` comes from the full (unnarrowed) `_settings_changes` call and joins each entry's raw `(section, name)` against the blocking-key set to set `blocks_pull`. This means `_settings_changes` runs twice per request (once narrowed inside `_pull_blocking_changes`, once in full) — accepted, since the narrowed pass only touches the cheap `metadata` columns and re-deriving the blocking set from the full diff would re-implement the per-field `skip_null_baseline_metadata` narrowing here. Prompts: the blocking-list projection described above (backed by `_PROMPT_FIELDS` comparing the install vs `_resolve_synced_revision`). Workspace: snapshots the live env via `PublishService._snapshot_workspace_tree` (same post-denylist capture a push produces, so the preview matches the commit — e.g. `__pycache__` never appears), hashes each file via `_file_hashes`, and set-diffs against the synced revision's `workspace/` (no `blocks_pull` — files never block a pull). Read-only; best-effort (empty lists with no env / no baseline).

Each prompt/setting change also carries the raw `key` (+ `section`) alongside the human `field` label — the stable pair `compute_diff` addresses. The label is UI copy; making it the identifier would break every diff link the moment someone rewords it.

**`compute_diff(session, agent_id, owner, *, section, key) -> dict`** (new)

The per-item drill-down behind every status row. Returns `{section, key, label, change_type, diff, binary, truncated}`; `a/` is the last synced revision, `b/` the live agent (git's baseline→working-copy convention). No synced baseline → `GitSourceValidationError` (400), not an empty diff — "nothing to compare against" is a different answer from "no differences".

- **Prompts / settings** (`_diff_sides_for_field`): resolves `(label, live, baseline)` through the SAME registries and collectors `_settings_changes` uses, so a diff can never disagree with the row that opened it. `metadata` reads the `Agent` row, `sdk` the active `AgentEnvironment`, `specs` re-runs the publish collector. Both sides pass through `_normalize_setting_value` before rendering, so a row reported as changed always yields a non-empty diff and an "unset" shape never diffs against its twin. A collector that raises is surfaced as a 400 (after `_clear_poisoned_transaction`) rather than 500-ing a read-only endpoint.
- **Files**: live env workspace vs. the baseline snapshot's `workspace/`, with the same lost-baseline re-materialization `compute_dirty` / `compute_status` use — a wiped snapshot dir is re-cloned rather than diffed against nothing, which would render every file as newly added.
- Rendering (`_render_setting_text`): strings verbatim (JSON-quoting a multi-line prompt makes every line unreadable), everything else as `indent=2, sort_keys=True` JSON so re-serialization order never churns the diff.
- Caps: `_DIFF_MAX_BYTES` (512 KB) per side, `_DIFF_MAX_LINES` (2,000) on output, both reported via `truncated`.
- Pool-safe: all DB reads (source, install, baseline, env, collectors, SSH key material) complete before `session.commit()` releases the connection; the filesystem reads and any re-clone happen after.

**`_resolve_diff_file_key(key) -> str`** / **`_read_diff_side(root, rel) -> tuple[str | None, bool]`** (new)

The security boundary for the file variant, which takes caller-supplied input straight to the filesystem. `_resolve_diff_file_key` allowlists rather than sanitizes: relative-only, no `.`/`..` segments, first segment must satisfy `is_bundle_owned_toplevel`, no segment may be `is_nested_excluded` — calling `workspace_classification`'s own helpers, so the endpoint cannot drift from what a commit captures. `credentials/`, `app-data/`, `logs/`, `databases/` are therefore unreachable.

**`_read_diff_side` re-checks containment on the fully resolved path, and that is the check that actually holds.** String-level validation cannot see a symlinked intermediate *directory* (`scripts/x -> /etc`): it contains no `..`, every segment passes the denylist, and the final component is an ordinary file. Resolving both root and target and requiring `is_relative_to` catches it; the per-segment denylist above is defense in depth, not the boundary. Symlinks are refused outright (the capture walk drops them, so one here would never be committed), a missing file returns `(None, False)` — deliberately distinct from an empty file `("", False)`, since that distinction is what makes a change classify as added/deleted rather than modified — and undecodable bytes report as binary instead of raising.

**`_file_hashes(workspace_root) -> dict[str, str]`** (new)

Maps each file under a `workspace/` subtree to its SHA-256 (relative POSIX path → hex). Skips symlinks/non-files; missing root → empty. The per-file analogue of `hash_workspace_tree`, used by `compute_status` to classify added/modified/deleted.

**`list_commits(session, agent_id, owner, limit=50) -> list[dict]`** (new)

Delegates to `git_log_subdir(repo_url, ref, subdir, ssh_key_path, max_count=limit)` after resolving the owned source, then attaches a per-commit `commit_url` via `build_web_commit_url(source.repo_url, sha)` (`None` for unsupported hosts). Returns newest-first list of `{sha, short_sha, author_name, author_email, date, message, commit_url}` dicts. `limit` is clamped to 1..200 at the route layer.

**`pull_update(*, session, agent_id, owner, conflict_resolution=None) -> Agent`**

Acquires per-agent lock, delegates to `_pull_locked`. `conflict_resolution` is one of `GIT_PULL_RESOLUTIONS` (`"keep_local"` / `"take_remote"`); `None` (the default — never given an implicit default anywhere in this call chain) keeps the historical fail-loud behavior the GitOps webhook dispatch (`AgentWebhookService.fire_webhook`) relies on, since it always calls this with no resolution. On `GitSourceConflictError` (including its `GitSourceLocalChangesError` subclass), `GitSourceValidationError`, or `GitSourceNotFoundError` — re-raises without stamping `ERROR` (user-actionable, pre-mutation). On any other exception — calls `_mark_source_error` then re-raises.

**`_pull_locked(session, agent_id, owner, *, conflict_resolution=None) -> Agent`**

Unknown `conflict_resolution` (not `None` and not in `GIT_PULL_RESOLUTIONS`) → `GitSourceValidationError` (400) — validated FIRST, before the direction guard, so every caller (route, webhook, CLI) gets the same validation regardless of entry point. Then: direction guard → env **existence** check (`env is None` → 400; this does NOT check readability — see below) → `ls_remote_head` (no-op return if already up to date — this also means the dirty guard below never even runs on an unadvanced remote, so an idempotent webhook fire against a locally-dirty install stays quiet). Once the remote HAS advanced:

- `conflict_resolution is None` → `_assert_not_dirty` (raises `GitSourceLocalChangesError` → 409 on blocking drift).
- otherwise → `_pull_blocking_changes` computes the same blocking set **without raising** (needed for `keep_local`'s `preserve_fields` and for the audit log line).

Clone + validate the tree + oversize check run next (so a request that would fail those checks never gets a backup taken for nothing). If `conflict_resolution is not None`, `_capture_backup_revision` runs at this point — deliberately as late as possible, after everything that can still abort the pull, but before any mutation. **This is where the workspace-readability guard actually lives** (`PublishService._assert_workspace_readable`, called from inside `_capture_backup_revision` — see its entry below): it is not an upfront precheck alongside the existence check above, it fires only on a resolved pull, after the clone, right before the backup snapshot. **New user-visible failure mode:** a resolved pull now 400s here if the env's `app/workspace/` is missing/unreadable on disk — a case where a bodiless pull previously proceeded unguarded (a bodiless pull never calls `_capture_backup_revision`, so it never hits this check). Both resolutions are affected identically, since the backup is unconditional on either one. `_persist_revision` persists the incoming revision next; if that raises, `_discard_backup_revision` rolls back the just-taken backup (so a backup is never left as the newest revision on a pull that did not complete) before re-raising. `preserve_fields` is then derived (`{c["field"] for c in blocking}` when `conflict_resolution == "keep_local"`, else `None`) and passed into `_apply_revision_to_install`. Finally: advance `last_synced_commit`, and (only when a resolution was supplied) log an INFO audit line naming the agent, resolution, preserved/discarded field labels, and the backup's revision number — not a `SecurityEvent`, since this is an owner acting on their own agent and no other git operation emits one either.

**`push(*, session, agent_id, owner, commit_message, version=None, also_publish_bundle=False) -> AgentGitSource`**

Acquires per-agent lock, delegates to `_push_locked`. Same error-class split as pull.

**`_push_locked(...) -> AgentGitSource`**

Direction guard → env + workspace readable → `also_publish_bundle` precondition check → `ls_remote_head` precheck → full-history clone (`depth=None`) → delete stale `workspace/` subtree → `_capture_and_push` helper → advance `last_synced_commit`. If `also_publish_bundle`: `PublishService.publish` best-effort.

The precheck is subdir-aware: a remote HEAD advance only raises `GitSourceConflictError` (409 "pull first") when `_remote_change_is_relevant` (below) says the advance concerns this agent — a repo-root install (no `subdir`) always blocks; a `subdir` install blocks only when the subdir tree changed since `last_synced_commit`. When the advance is subdir-irrelevant, the precheck does not raise and control falls through to the full-history clone + `_capture_and_push` + `fast_forward_push` exactly as if the remote had not advanced; `fast_forward_push`'s own merge-base ancestor check is unaffected and still raises `GitNonFastForwardError` (409) on a genuine non-fast-forward, so a real conflict the subdir check couldn't see is still caught. This is the same helper `_compute_update_available_remote` uses for the update-check banner, so the two can no longer disagree.

**`_capture_and_push(session, *, install, env, source_like, owner, key, repo, repo_path, commit_message, version) -> str`** (shared helper)

Extracted from the `_push_locked` capture body; also used by `connect`. Builds the manifest via `_build_live_manifest` (shared with `_capture_backup_revision` so a pull backup and a real push describe the live agent identically), calls `RevisionFormat.write_tree` (manifest + workspace capture), writes `.gitignore`, asserts no oversized files, `commit_all` (no-op safe), `fast_forward_push`. Persists an `AgentBundleRevision` on every changed push/connect via `_persist_revision` — this gives `compute_dirty` a stable baseline. `source_like` carries `repo_url/subdir/ref/bundle_uuid`; for connect it is the in-memory unsaved source; for push it is the persisted row.

**`_PROMPT_FIELDS: tuple[tuple[str, str], ...]`** (module-level constant)

Maps the four DB prompt field names to their UI labels: `("workflow_prompt", "Workflow prompt")`, `("entrypoint_prompt", "Entrypoint prompt")`, `("refiner_prompt", "Refiner prompt")`, `("router_trigger_prompt", "Router trigger prompt")`. Shared by `_prompts_changed` (`compute_dirty`), `_pull_blocking_changes` (pull guard's blocking set + `compute_status`'s `blocks_pull`), and `_apply_revision_to_install`'s `keep_local` field-preservation narrowing, so none of the three can diverge.

**`_prompts_changed(install, rev) -> bool`**

Compares the four prompt fields enumerated by `_PROMPT_FIELDS` (`workflow_prompt`, `entrypoint_prompt`, `refiner_prompt`, `router_trigger_prompt`) between the `Agent` row and an **already-resolved** baseline revision. Returns `True` if any field differs, `False` when `rev is None`. Pure — takes the resolved revision rather than re-resolving it, so `compute_dirty` (which also needs the baseline for the settings and workspace diffs) resolves it **once per request** instead of once per check; each resolve is a full-row `SELECT` carrying the revision's `manifest` blob, on a polled endpoint. Used only by `compute_dirty` — the pull guard instead goes through `_pull_blocking_changes` below, which needs the per-field labels and change types, not just a boolean.

**`_METADATA_FIELDS` / `_SDK_FIELDS` / `_SPEC_FIELDS`** (module-level constants)

The registries for the **non-prompt** half of `cinna.agent.json` — the settings drift check. Every attribute name is deliberately identical on the live row and on `AgentBundleRevision`, so one `getattr` pair covers both sides:

| Registry | Manifest block | Live source | Entries |
|---|---|---|---|
| `_METADATA_FIELDS` | `metadata` | `Agent` row | `description`, `example_prompts`, `status_refresh_command`, `agent_api_enabled`, `agent_api_identity_enabled`, `a2a_config`, `agent_sdk_config`, `webapp_enabled` |
| `_SDK_FIELDS` | `sdk` | active `AgentEnvironment` | `agent_sdk_building`, `agent_sdk_conversation`, `model_override_building`, `model_override_conversation` |
| `_SPEC_FIELDS` | top-level lists | `PublishService._collect_schedule_specs` / `_collect_plugin_specs` | `schedules`, `plugin_specs` |

`_SPEC_FIELDS` carries the collector callable itself, so the live side is re-collected with the **same helpers `_capture_and_push` uses to build the manifest** — the diff can never disagree with what a commit would write.

`required_credential_specs` is deliberately **not** in `_SPEC_FIELDS` — do not re-add it. Its live collector (`PublishService._collect_credential_specs`) reads the install-local `Credential` rows, and on any install that did not author the baseline (a `checkout`, or a `pull` from a repo another install pushed) those rows are placeholders (`name="<spec> (placeholder)"`, locally re-resolved `provided_by="user"`) that can never reproduce the publisher's spec values — the comparison would report drift permanently with no user edit behind it, not a trustworthy signal. Credential-spec staleness already has a purpose-built detector: `PublishService.compute_credential_spec_drift` behind `GET /bundle-credential-drift`, surfaced as the republish nudge on the Bundle tab (see [Agent Bundles tech](../agent_bundles/agent_bundles_tech.md)). The manifest itself still carries `required_credential_specs` unchanged — only this diff stopped reading it.

**That detector does not cover git-connected installs, so the gap left by dropping `required_credential_specs` from `_SPEC_FIELDS` has no other surface for them.** `PublishService.compute_credential_spec_drift` early-returns `BundleCredentialDrift(stale=False, drift=[])` in two cases (`publish_service.py:780-781`, `:787-788`): when `not install.is_publisher_install`, and when the bundle has no `latest_revision`. A `checkout` always creates a consumer install (`is_publisher_install=False` — `install_service.py:257`), so the detector is a permanent no-op for every checked-out agent. `bundle.latest_revision_id` is only ever written by `PublishService.push` (`publish_service.py:346`) — git `connect`/`push` deliberately never call it — so a `connect`-based install that is never separately published to the catalog is equally uncovered. Net effect: for both git install shapes that never publish to the catalog, a rename of a linked credential or an `allow_sharing` flip is invisible to both `settings_dirty` and `compute_credential_spec_drift`, even though the next push *will* rewrite `required_credential_specs` in `cinna.agent.json`. Accepted trade-off, not a bug — see the Conflict Model table in the business doc — the push still captures the change whenever the user commits for any other reason.

Supporting constants: `_SETTING_SECTIONS` (`"metadata"`, `"sdk"`, `"specs"`), `_PULL_OVERWRITTEN_SECTIONS` (`("metadata",)` — see `_pull_blocking_changes`), `_UNORDERED_LIST_FIELDS` (the two spec lists, compared as multisets), `_SET_LIKE_DICT_FIELDS` (`agent_sdk_config`, whose `sdk_tools` / `allowed_tools` are written via `list(set(...))` by tool discovery and therefore have non-deterministic order).

**`_settings_changes(session, install, rev, env=None, *, sections=_SETTING_SECTIONS, skip_null_baseline_metadata=False, stop_early=False) -> list[dict]`**

Per-field diff of the non-prompt `cinna.agent.json` fields against `rev` — the **already-resolved** baseline from `_resolve_synced_revision` (passed in, not re-resolved, so a caller resolves once). Returns `[{field, change_type}]` (the same shape as the prompt preview); `[]` when `rev is None`. Iterates the three registries above, normalizing both sides through `_normalize_setting_value` before `_classify_change`.

**Absent manifest sections are never compared.** The baseline's raw `revision.manifest` is consulted: a missing `metadata` / `sdk` block, or a missing top-level spec key, skips that group entirely — the same missing-key-tolerant rule `InstallService._apply_revision_metadata` applies on the restore side, so a pre-metadata snapshot cannot fabricate drift. The `sdk` group is additionally skipped when the install has no env row.

Flags (all narrow the default full comparison the indicator endpoints use):

| Flag | Used by | Effect |
|---|---|---|
| `sections` | `_pull_blocking_changes` (via `_assert_not_dirty` / `compute_status`) | Restricts which registries are compared (`_PULL_OVERWRITTEN_SECTIONS`). |
| `skip_null_baseline_metadata` | `_pull_blocking_changes` (via `_assert_not_dirty` / `compute_status`) | Skips a metadata field whose **raw** baseline column is `None`, mirroring `_apply_revision_metadata`'s per-field `is not None` guard. Matched on the raw column, **not** on `change_type == "added"`: a baseline of `[]` / `""` normalizes to `None` (so it classifies as `added`) yet still passes `is not None`, meaning the pull does overwrite it and the guard must still block. |
| `stop_early` | `compute_dirty` | Returns as soon as the first change is found, skipping the remaining (query-heavy) spec collectors. The list is then partial by design — only valid for callers reducing it to a bool. |

A collector that raises is reported as `modified` rather than propagating (conservative-on-indeterminate). Because a SQLAlchemy-level failure leaves the transaction poisoned — which would otherwise resurface as `PendingRollbackError` at the caller's `session.commit()` and 500 the polled endpoint — the handler first calls `_clear_poisoned_transaction`.

Touches the DB (the three collectors), so the pool-safe read paths call it **before** releasing their connection.

**`_clear_poisoned_transaction(session, *, context) -> None`**

Shared, never-throwing rollback used by any handler that swallows a DB error (`_settings_changes`' collector guard, `_mark_source_error`). Nested-transaction-aware: `get_nested_transaction().rollback()` when inside a savepoint (`ROLLBACK TO SAVEPOINT`, preserving the outer transaction's committed rows under the test suite's savepoint isolation), plain `session.rollback()` in production where no savepoint is active.

**Normalization helpers** (module-level)

- `_canonical_json_value(value)` — collapses `None` / `""` / `[]` / `{}` to `None` (all mean "unset") and drops empty dict entries, recursing into containers. `False` / `0` are values, not emptiness. Stops an omitted manifest key from reading as a change against a live empty default.
- `_sorted_json(items)` — deterministic list ordering by canonical JSON encoding.
- `_normalize_setting_value(field, value)` — `_canonical_json_value`, then multiset ordering for `_UNORDERED_LIST_FIELDS` and per-key list ordering for `_SET_LIKE_DICT_FIELDS`.
- `_classify_change(live, baseline)` — `added` / `modified` / `deleted`, or `None` when unchanged.

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

**`_pull_blocking_changes(session, install, rev, env=None) -> list[dict]`**

The changes a pull would OVERWRITE — the single source of truth for both the pull guard (`_assert_not_dirty`) and the `blocks_pull` flags `compute_status` stamps onto its response, so the 409 and the preview that explains it can never disagree (the same discipline `_remote_change_is_relevant` enforces for the update banner vs. the push guard). Returns `[]` when `rev is None`. Each entry is `{section, field, label, change_type}` where `section` is `"prompt"` or one of `_SETTING_SECTIONS`, `field` is the raw attribute name (the stable key — also what `keep_local` passes as `preserve_fields` to `_apply_revision_to_install`), and `label` is the UI string.

Two halves: (1) the prompt half — always blocking, since a pull rewrites all four `_PROMPT_FIELDS` columns wholesale; (2) the settings half — `_settings_changes(..., sections=_PULL_OVERWRITTEN_SECTIONS, skip_null_baseline_metadata=True)`, i.e. the exact narrowed call `_assert_not_dirty` made before this extraction. **Deliberately narrower than the `settings_dirty` indicator**, on both axes: only the sections a pull actually rewrites — the prompt columns and the definitional metadata (via `InstallService._apply_revision_metadata`) — and within `metadata` only the fields `_apply_revision_metadata` actually assigns (non-NULL baseline column). Schedules, plugin links, credential links and env SDK selections survive a pull untouched, so blocking on them would protect nothing while deadlocking the user — push answers a remote advance with "pull first" while pull answers with 409, and a pull is exactly what an advanced remote demands first. A publisher who never set `status_refresh_command` must not permanently lock out every installer who does. The drift still shows in `GET /git/dirty` and `GET /git/status` (with `blocks_pull: false`). No `stop_early` passthrough — a partial list is unusable here: the guard needs the full set for its 409 payload, and the preview needs it for the `blocks_pull` join key set.

**Caveat on the `skip_null_baseline_metadata` mirroring:** it tests the BASELINE revision's column while the overwrite is driven by the INCOMING one. They are the same revision on the common (already-synced) path, but when the baseline is `NULL` and the incoming revision carries a value, the field is (correctly) absent from this blocking set — so `keep_local` does not preserve it — yet `_apply_revision_metadata` still assigns it from the incoming revision. The alternative (blocking on a `NULL` baseline) is the documented deadlock this narrowing exists to prevent, so the gap is accepted, not fixed.

Touches the DB via `_settings_changes`, so pool-safe read paths (`compute_status`) must call it before releasing their connection.

**`_assert_not_dirty(session, source, install, env=None) -> None`**

The pull guard. Thin wrapper: resolves the synced baseline, calls `_pull_blocking_changes`, and raises `GitSourceLocalChangesError(_PULL_LOCAL_CHANGES_MESSAGE, blocking)` (→ a structured, recoverable 409) when the blocking list is non-empty. `_PULL_LOCAL_CHANGES_MESSAGE` is a module-level constant — *"This agent has local changes that a pull would overwrite. Review them to choose whether to keep or discard them."* — deliberately not telling the user to push or discard by hand: pushing is impossible in exactly this state (the push precheck itself demands a pull first), and there was no discard action before this feature shipped the `conflict_resolution` modes below.

**`_build_live_manifest(session, *, install, env, bundle, version, release_notes) -> dict`**

Builds the `cinna.agent.json` manifest describing the LIVE agent: allocates the next `revision_number` and runs the three `PublishService._collect_*` spec collectors, then hands them to `RevisionFormat.build_manifest`. Shared by `_capture_and_push` (which writes it into a clone and commits) and `_capture_backup_revision` (which persists it straight to bundle storage), so a pre-pull backup can never describe the live agent differently from the way a real push would — a backup built off a divergent manifest shape would be a broken restore point. The git half of `_capture_and_push` (writing the tree into the clone directory so the commit picks it up) is deliberately NOT folded into this helper — reusing it for a backup would mean either a second full workspace snapshot on the push path or a callback-shaped seam, more disruption than the duplication it removes.

**`_capture_backup_revision(session, *, install, env, bundle, owner, release_notes) -> AgentBundleRevision`**

The safety net behind BOTH pull resolutions, called from `_pull_locked` when `conflict_resolution is not None`. `_capture_and_push` minus the git half: `PublishService._assert_workspace_readable(env, env_workspace_root)` (must run before the snapshot — without it a missing/unreadable workspace root silently captures an EMPTY `workspace/` rather than raising; a `ValueError` from it is caught and re-raised as `GitSourceValidationError` → 400), `_build_live_manifest`, `PublishService._snapshot_workspace_tree` into a temp dir, then `_persist_revision` through the same denylist + symlink guards every sync uses. Taken **unconditionally on any resolution, even when no field blocks the pull** — `replace_bundle_content` replaces the whole workspace either way, so this snapshot is also the only record of locally edited workspace files (this is the plan §3.4 deviation — see the module/route docstrings and `_pull_locked`'s inline comment for the full rationale). Costs one `revision_number` from the shared counter, widening the numbering gaps the Revisions tab already tolerates. **Never swallows** — any failure propagates to the caller, and the caller must invoke this BEFORE mutating anything (silently discarding the user's work after promising a backup is the one outcome this feature must never produce). The caller also owns the ordering contract: the backup must land BELOW the incoming revision (`_persist_revision`'s monotonic `revision_number` counter enforces this as long as the backup is persisted first) and must be rolled back via `_discard_backup_revision` if the incoming persist then fails.

**Behavior change worth flagging separately:** `_assert_workspace_readable` is the same guard `connect` and `push` already required (env-readable guard, business doc) — pull is simply the third caller now. Its `ValueError` message is literally shared with the publish path and says "Cannot publish…", which reads oddly on a pull; that is a known, accepted quirk of reusing the guard verbatim rather than a bug. Before this feature, pull only ever checked env *existence* (`env is None`), never workspace *readability* — so a missing/unreadable `app/workspace/` dir used to let a bodiless pull through untouched (the workspace-file side has no guard at all on that path — see the Conflict Model / known-gap notes in the business doc). A **resolved** pull (`keep_local` or `take_remote`) now 400s in that same situation, because it always attempts a backup first.

**`_discard_backup_revision(session, revision) -> None`**

Rolls back a pre-pull backup whose subsequent incoming-revision persist failed. A no-op when `revision is None`. Best-effort **only because it runs while an exception is already in flight** and must never mask it — not because a leftover backup is harmless: left in place, it becomes the newest revision on the bundle (`_resolve_synced_revision` takes the max `revision_number`), so the install would report clean while still holding unpushed work, and the next pull (including an unguarded bodiless one) would discard that work silently. Clears any poisoned transaction first (`_clear_poisoned_transaction`) since `revision` is an expired ORM instance by this point. On its own failure, logs at WARNING with the operational consequence spelled out, since that log line is the only remaining signal that the baseline may now be wrong.

**`_apply_revision_to_install(session, install, revision, *, preserve_fields=None) -> None`**

Stop env (if running) → `replace_bundle_content(revision.snapshot_path, env.id)` (workspace files replaced wholesale — `preserve_fields` never applies here, one tree has one baseline) → reset prompt-sync baselines (`*_synced_hash = None`, which is what makes a preserved DB value actually win over the file the snapshot just wrote) → write DB prompt fields from manifest, skipping any field named in `preserve_fields` → call `InstallService._apply_revision_metadata(install, revision, skip_fields=preserve_fields or None)` to overwrite the 8 definitional metadata fields (publisher-authoritative, missing-key-tolerant — `NULL` revision column skips that field; `skip_fields` additionally skips whatever the caller named regardless of the revision's value) → update `installed_revision_id`, `last_sync_at`, `last_update_status = "synced"` → restart env (if it was running). `preserve_fields` is the `keep_local` resolution's narrowing set — raw prompt/metadata attribute names from `_pull_blocking_changes`, passed by `_pull_locked`.

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
