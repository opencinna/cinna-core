# Git-Backed Agent Versioning

## Purpose

Version-control the durable source of an agent — scripts, internal docs, prompts, SDK configuration, schedules — while preserving per-user runtime state (App Data) and platform-managed secrets (credentials). An agent is treated like a desktop application: **checkout** from a remote git repository (or a subdirectory of one), **connect** an existing agent to a new git repo (initial export push), **pull** updates when the remote advances, **push** local workspace changes back to the remote, and optionally wire a **push-webhook** so a repository push automatically triggers a pull. No platform account is required on the git-host side.

## Core Concepts

| Concept | Definition |
|---------|-----------|
| **Git source** | An `AgentGitSource` row binding one agent install to one remote git repository (optionally a `subdir` within it) so the install can be checked out, pulled, and pushed |
| **Sync direction** | Per-source policy: `pull` (read-from-remote only), `push` (write-to-remote only), or `bidirectional` (both) |
| **Canonical git tree** | `cinna.agent.json` + `workspace/` subtree + `.gitignore` — byte-for-byte the schema_version-2 bundle snapshot layout, so git is a transport/interchange layer, not a separate format |
| **Last synced commit** | SHA of the last remote commit that was imported or pushed; the idempotency pin, analogous to `Agent.installed_revision_id` |
| **Update available** | `ls-remote HEAD != last_synced_commit`; surfaced by `GET /agents/{id}/git` (best-effort) and `GET /agents/{id}/git/check-updates` (strict) |
| **Dirty** | The live workspace or DB prompt fields have diverged from the last synced revision. `GET /agents/{id}/git/dirty` reports both axes separately: `workspace_dirty` (file content) and `prompts_dirty` (DB prompt fields) |
| **GitOps webhook** | A `git_source` type `AgentWebhook` whose token a git host (GitHub, GitLab, etc.) posts to; the platform responds with a pull |
| **Ownerless bundle** | An `AgentBundle` row with `publisher_user_id = NULL` created or reused during checkout or connect; private, unlisted, and never a catalog publish |
| **Status** | `pending` → `connected` (after successful sync) → `error` (genuine operational failure) → `disconnected` |
| **Connect** | Attaches a git source to an agent the user **already owns** and performs an initial export push (first commit = current live workspace). Distinct from checkout, which imports a foreign repo into a new install |
| **Disconnect** | Deletes the `AgentGitSource` row, severing the platform link. The external repo is not touched — it remains the durable version history |
| **Deploy key** | An SSH key (from the user's SSH Keys library) that the backend uses host-side to authenticate push/pull to private git remotes. Never reaches the container; the developer's own credentials are used from their local machine |
| **Two-writer model** | Backend (via deploy key) and developer's local machine (own git/SSH credentials) can both push to the same remote, both fast-forward-only. Conflicts surface fail-loud — no auto-merge |
| **CLI git coordinates** | `GET /api/v1/cli/git-coordinates` (CLI-token / agent-scoped) — tells a local `cinna` CLI whether this agent is VCS-enabled and provides `repo_url`, `subdir`, `ref`, and `auth_hint`. Never includes the deploy key (private key never leaves the backend) |

## How Git Relates to Bundles and Mutagen

### Git vs. Bundles

Git is an **external layer** over the `AgentBundleRevision` system the platform already uses. The relationship is:

```
remote git repo ──checkout──► AgentBundleRevision (internal SSOT)
                                       │
                          install + env + workspace seeding
                                       │
push ◄── git commit/push ◄─────────────┤ (live workspace captured)
                                       │
pull ──► git pull ──► replace_bundle_content (advance last_synced_commit)
```

A git tree is byte-for-byte a schema_version-2 bundle snapshot. Every git operation reduces to an operation the platform already performs — with git as the wire. `AgentBundleRevision` rows remain the internal runtime source of truth backing App Data keying, install counts, credential specs, and schedule materialization; git is the portable, version-history face. Every push and connect now also persists an `AgentBundleRevision`, giving the dirty check a stable baseline across rebuilds.

### Git vs. Mutagen (Local Dev Sync)

Git and Mutagen are **orthogonal**. Mutagen is the live runtime sync layer: it keeps the developer's local folder mirrored to the running Docker container in near-real-time. Git is the preservation layer: it captures durable versioned snapshots on demand (via push / commit) and allows pull or rollback to any prior state. A local agent folder can be **both** a Mutagen sync target and a git working tree simultaneously — Mutagen continues keeping the container up to date while git tracks history independently.

## On-Disk Format (git tree layout)

```
<repo>/<subdir>/
├── cinna.agent.json   # manifest: schema_version, bundle_id, prompts, SDK per-mode,
│                      #   model overrides, required_credential_specs (metadata only),
│                      #   schedules, plugin_specs, version, release_notes, content_hash
├── workspace/         # the BUNDLE_OWNED subtree verbatim
│   ├── scripts/
│   ├── docs/
│   ├── files/
│   └── ...            # any bundle-owned workspace folder
└── .gitignore         # auto-generated from BUNDLE_EXCLUDED_TOPLEVEL + runtime denylist
│                      #   + recursive cache denylist (__pycache__/, *.pyc, …)
```

The `cinna.agent.json` filename is the only difference between a git tree and bundle storage (which uses `manifest.json`). `RevisionFormat` handles both transparently on read.

**Regenerated caches are never committed.** Beyond the top-level denylist, a recursive cache denylist (`__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `*.pyc`, `*.pyo`) is stripped at **every depth** of the captured tree — these appear nested inside agent code dirs (e.g. `agent_api/__pycache__/`) and are regenerated, so they must never reach a snapshot or commit. The same names are emitted into the generated `.gitignore`. This is enforced in the shared copy walk (`safe_copytree`), so it applies to publish, install seed, env migration, and git push alike.

## Operations

### Checkout

`POST /agents/checkout` (developer-gated). Imports a remote repo into a **new** agent install. Use this to get a copy of an existing git-versioned agent:

1. Egress guard runs on the repo URL; SSH key (if set) is decrypted host-side and placed in a chmod-600 temp file.
2. Clone (shallow) into a temp directory; record the commit SHA.
3. Validate layout and parse `cinna.agent.json`. An oversize workspace file (per `GIT_SOURCE_MAX_FILE_BYTES`) rejects before any row is created.
4. Resolve or create the `AgentBundle` row using the manifest's `bundle_id`:
   - Ownerless (no `publisher_user_id`) existing row — reuse. Different users checking out the same repo share one bundle row; App Data (keyed on the `bundle_id` string) reattaches across checkouts.
   - Row owned by the checking-out user — reuse (the publisher can also consume their own git mirror as a consumer install).
   - Row owned by a different real publisher — **409 Conflict** (cross-tenant injection guard).
   - No row — create ownerless/private/unlisted.
5. Persist the cloned tree as an `AgentBundleRevision` into bundle storage via the same denylist + symlink guards publish uses (credentials, app-data, logs, databases, uploads can never enter the install through checkout).
6. Install from the revision via the existing `InstallService._install_from_revision` path (creates `Agent` row, starts env, seeds workspace, materializes schedules/plugins/credential specs).
7. Apply `name_override` if provided.
8. Record `AgentGitSource` (status `connected`, `last_synced_commit` = cloned SHA).

A same-user re-checkout of the same repo → **409** (detected before any bundle/revision row is created so no half-state is left). A failed install after bundle/revision rows are committed triggers automatic orphan cleanup.

### Connect (Enable Git Versioning on an Existing Agent)

`POST /agents/{id}/git/connect` (developer-gated). Attaches a git source to an agent you **already own** and performs an **initial export push** (current live workspace → first commit on the remote):

1. Per-agent lock acquired.
2. Resolve the owned install (404 if missing or not owned).
3. **No-existing-source guard:** if a `AgentGitSource` row already exists → **409** ("disconnect first"). An `IntegrityError` race backstop also catches the concurrent-connect edge.
4. **Environment readable guard:** a running environment with a readable workspace is required (the initial push needs live files) → **400** if absent.
5. **Direction guard:** `sync_direction` must be `push` or `bidirectional`; `pull`-only at connect → **400** (can't push the first commit with pull-only direction).
6. SSH key resolved and temp-keyed (same as push).
7. Resolve backing bundle (same `_resolve_or_create_bundle` logic as checkout, keyed on `agent.bundle_id`). `Agent.bundle_uuid` is not mutated.
8. **Remote-state probe** (`ls_remote_head`, egress-guarded) — three branches:
   - **Empty remote / ref absent** → `init_repo_with_remote` path: init a fresh local repo, write the agent tree, commit, push (creates the branch on the remote).
   - **Ref exists but subdir is empty/absent** → full-history clone, write tree into `subdir`, commit, fast-forward push on top of existing history.
   - **Subdir already contains a `cinna.agent.json`** → **recoverable 409** with the machine-readable code `existing_agent_folder`. The UI then offers to **adopt** that folder (see below) instead of failing. Re-send the connect with `adopt_existing=true` to take the adopt path.
9. Capture + commit (shared `_capture_and_push` helper — same body as push).
10. Persist `AgentGitSource` (`status=connected`, `last_synced_commit=new_sha`). Persists an `AgentBundleRevision` as the dirty-check baseline.

#### Adopt an Existing Remote Folder

When the target subdir already holds an agent, the default connect returns the recoverable 409 above. Passing `adopt_existing=true` (the UI sends this after a confirmation dialog) instead **links the agent to the existing remote folder without overwriting it** — the git analog of pointing a remote at an existing local folder:

1. The remote folder's tree is read and validated, and recorded as the synced baseline `AgentBundleRevision` (so the dirty check has a baseline). **Nothing is committed or pushed.**
2. `last_synced_commit` is set to the remote HEAD (so `update_available` is `false`).
3. From there the dirty check surfaces local-vs-remote differences, which the user resolves by **committing** (push local → remote) or **pulling** (remote → local).

A `bundle_id` mismatch between the remote folder and the agent is **permitted** (logged as a warning, not an error) — adoption is an explicit, user-opted-in action, so a user may deliberately point an agent at a folder published under a different `bundle_id`.

### Disconnect

`DELETE /agents/{id}/git` (developer-gated). Deletes the `AgentGitSource` row, severing the versioning link. The external remote is not modified — it remains the durable record. After disconnect, the card returns to its disabled state and git operations (push, pull, commits, dirty check) return 404. Re-enable by connecting again.

### Pull (Update from Remote)

`POST /agents/{id}/git/pull` (developer-gated). Pulls the latest remote revision onto the install:

1. Per-agent lock acquired (serializes concurrent pull/push on one agent).
2. Direction guard: `sync_direction` must be `pull` or `bidirectional`; otherwise **400**.
3. Environment must exist (pull requires an active workspace); otherwise **400**.
4. `ls-remote HEAD` compared to `last_synced_commit`. If already up to date: touch `last_sync_at`, return without cloning — idempotent for webhook fires.
5. **Dirty guard** (DB side): if the install's prompt fields differ from the last synced revision, **409** "push or discard local changes first". The file side is protected by the `replace_bundle_content` denylist (App Data, credentials, logs, databases, uploads are never touched); only the DB prompt fields are checked here.
6. Clone the repo (shallow) into a temp directory, validate the tree, parse `cinna.agent.json`.
7. Persist as a new `AgentBundleRevision` (same bundle, next `revision_number`).
8. Stop the environment (if running), apply `replace_bundle_content` from the new snapshot (bundle-owned dirs replaced/pruned; App Data, credentials, plugins preserved), reset prompt-sync baselines, write DB prompt/SDK fields from the manifest.
9. Restart the environment.
10. Advance `last_synced_commit` and set `status = connected`.

**Error handling:** only genuine operational failures (egress-blocked, clone/network errors, filesystem errors) stamp `status = ERROR` on the source. Expected, user-actionable outcomes (dirty guard → 409, wrong direction → 400, missing env → 400) leave the source status unchanged.

### Push (Commit Agent to Remote)

`POST /agents/{id}/git/push` (developer-gated). Captures the live workspace and fast-forward-pushes it. This operation is also surfaced in the UI as "Commit Agent" (enabled only when the dirty check reports changes):

1. Per-agent lock acquired.
2. Direction guard: `sync_direction` must be `push` or `bidirectional`; otherwise **400**.
3. Requires a started environment with a readable workspace; otherwise **400**.
4. `also_publish_bundle` (optional, default `false`) requires a publisher install; validated before the push so a failed precondition never wastes the push.
5. **Fast-forward precheck:** `ls-remote HEAD` vs `last_synced_commit`. If the remote advanced — **409** "pull first". No clone or commit is attempted.
6. Full-history clone (not shallow — fast-forward push needs merge-base ancestry).
7. Delete the stale `workspace/` subtree in the clone (so deletions propagate). Capture the live `app/workspace/` tree via `RevisionFormat.write_tree` — the same denylist + symlink guards publish uses, so credentials, app-data, logs, databases, and uploads can never reach the git tree even via a symlink. Write `cinna.agent.json` and refresh `.gitignore`.
8. Size guard: reject any individual workspace file exceeding `GIT_SOURCE_MAX_FILE_BYTES` before committing.
9. `commit_all` (no-op safe if working tree unchanged). Fast-forward push — rejects server-side if the remote advanced between the precheck and the push (`GitNonFastForwardError` → **409**).
10. Advance `last_synced_commit`, set `status = connected`. Persists an `AgentBundleRevision` as the new dirty-check baseline.
11. If `also_publish_bundle` was set: `PublishService.publish` is called as a best-effort secondary action; a publish failure is logged but does not roll back the git push.

**Error handling:** same as pull — expected outcomes leave status unchanged; genuine failures stamp `ERROR`.

### Update Check

`GET /agents/{id}/git/check-updates` (owner-resolved, no developer gate). Runs `ls-remote HEAD` against the remote (cheap, no clone) and returns `{update_available, remote_commit, last_synced_commit}`. Any network or auth failure surfaces as a service error.

`GET /agents/{id}/git` (owner-resolved) returns `AgentGitSourcePublic` with a best-effort `update_available` — network/auth failures silently leave it `false` rather than failing the read.

### Commit History

`GET /agents/{id}/git/commits` (owner-resolved, no developer gate). Returns up to `limit` (default 50, clamped to 1..200) commits touching the configured `subdir`, newest first. Each commit carries `sha`, `short_sha`, `author_name`, `author_email`, `date`, `message`, and a `commit_url` (browser link to that single commit on the host, when the provider is supported — see Web Links below; `null` otherwise). Uses a bounded shallow clone so only the most recent history is visible. Commits touching other subdirs in the same repo do not appear — the list is scoped to the agent's subdir.

### Web Links (Provider-Aware)

The platform generates browser URLs into the remote host for hosts whose web layout it knows — **GitHub only today**, designed to extend to others (Bitbucket, GitLab) by adding one registry entry. Three links are produced, all scoped to the agent's branch + subdir, and all `null` for unsupported hosts (so the UI hides the link rather than rendering a broken one):

- **`web_history_url`** (on `AgentGitSourcePublic`) — commit history of the subdir: `…/commits/<ref>/<subdir>`. Surfaced as the "View history" link.
- **`web_tree_url`** (on `AgentGitSourcePublic`) — browse the folder tree: `…/tree/<ref>/<subdir>`. Surfaced by making the repo name clickable.
- **`commit_url`** (per `GitCommit`) — a single commit: `…/commit/<sha>`. Surfaced by making each commit's short SHA clickable.

Both SSH (`git@host:owner/repo.git`) and HTTPS repo URLs resolve to the same web URL.

### Change Detection (Dirty Check)

`GET /agents/{id}/git/dirty` (owner-resolved, no developer gate). Read-only comparison of the live workspace against the last synced revision. Returns:

- `dirty`: `true` if either axis is dirty.
- `prompts_dirty`: DB prompt fields (`workflow_prompt`, `entrypoint_prompt`, `refiner_prompt`, `router_trigger_prompt`) differ from the last synced revision.
- `workspace_dirty`: live workspace file content (hash) differs from the last synced revision.
- `has_env`: `false` if no environment exists (workspace dirty check is skipped; `workspace_dirty` will be `false`).
- `last_synced_commit`: the SHA the comparison was made against.

This endpoint is kept separate from `GET /agents/{id}/git` deliberately — the dirty check copies the entire workspace tree to a temp dir and must not slow or fail the cheap status read. The UI gates the "Commit Agent" button on `dirty=true`; the pull guard uses the same `_prompts_dirty` logic internally.

### Commit Status Preview (git-status Style)

`GET /agents/{id}/git/status` (owner-resolved, no developer gate). The detailed sibling of the dirty check: instead of booleans it returns the actual per-prompt and per-file changes the next commit would capture, so the commit dialog can render a `git status`-style preview before the user commits. Returns:

- `dirty`, `has_env`, `last_synced_commit` (as in the dirty check).
- `prompt_changes`: list of `{field, change_type}` for the changed prompt fields.
- `file_changes`: list of `{path, change_type}` for the changed workspace files.

`change_type` is `added` / `modified` / `deleted`. The workspace side compares the **same post-denylist capture a push produces** against the last synced revision's `workspace/` snapshot, so the preview matches the eventual commit exactly (e.g. `__pycache__` never appears). It is fetched lazily — only while the commit dialog is open — because it does a full workspace snapshot + per-file diff.

### GitOps Webhook (Auto-Pull on Push)

`POST /agents/{id}/webhooks/git-source` (developer-gated) creates a `git_source` webhook on the existing `agent_webhooks` infrastructure. The git host (GitHub, GitLab, etc.) POST to `{host}/agent-hooks/{webhook_id}` with the bearer token; the platform responds with a `GitSourceService.pull_update` call. The webhook payload body is ignored.

- Uses the same Fernet-encrypted bearer token, `hmac.compare_digest` validation, 64 KB payload cap, and immutable invocation log contract as session and script webhooks.
- After auth, the pull is always attempted (and always succeeds with 200 from the webhook's perspective — pull success or failure lands in the log row, not the HTTP response code).
- If the remote did not advance since `last_synced_commit`, pull is a no-op and the log reflects that.
- Genuine pull failures stamp `status = ERROR` on the git source and are logged in the invocation record.

## GIT Versioning Card (UI)

The "GIT Versioning" card lives in the agent's **Integrations** tab alongside the Local Dev, Agent REST API, and MCP Connectors cards. It is visible to the install owner (and hidden from `agent-user` role visitors).

The card's toggle reflects the **real connected state from first paint**: the enabled flag (`git_versioning_enabled`) rides the already-loaded agent payload (a computed capability flag — see Integration Points), so the switch never flashes "off" then flips on. The card's own git-source query fills the internals afterward, behind a "Loading git versioning…" spinner.

The card has two states:

**Disabled (no `AgentGitSource` row).** A toggle header and a one-line description. Toggling on reveals the connect form.

**Connect form** (toggle on, not yet connected):
- Repo URL, optional subdir, ref (default `main`), sync direction.
- **Deploy key picker** (`DeployKeySelect` component): choose an existing SSH key from the user's SSH Keys library, use no key (public repo), or generate a new key inline. Quick-generate shows only the **public key** with copy-to-clipboard and deploy-key guidance ("Add this as a Deploy key in your GitHub/GitLab repo settings and check 'Allow write access'"). Private key material is never displayed.
- Commit message for the initial export (default "Initial export from Cinna").
- **Connect** button submits `POST /agents/{id}/git/connect`. If the subdir already holds an agent, the recoverable 409 (`existing_agent_folder`) opens a **"Folder already exists" confirmation dialog**; confirming re-sends the connect with `adopt_existing=true` (adopt the remote folder and re-check status — nothing is overwritten).

**Connected view:**
- The repo URL is a **clickable link** (opens `web_tree_url` — the repo tree at the branch + subdir) when the host is supported; plain text otherwise.
- Coordinates line: **subdir and branch as inline code chips**, and the **sync direction as an icon** with a hover tooltip (bidirectional / pull only / push only) rather than a text label. Plus a status badge (`connected` / `error` with `last_error` detail).
- **Update banner** when `update_available` → **Pull** button.
- **Latest commits** — the 3 most recent from `GET /agents/{id}/git/commits`: message, clickable short SHA (`commit_url`), author, relative date. A **"View history"** link (→ `web_history_url`) sits beside the section title. Both the SHA link and View history appear only when the host is supported.
- **Card footer** holds the actions: on the left, **Commit Agent** (enabled only when `dirty=true`; clicking opens the commit dialog with the git-status preview, then `POST /agents/{id}/git/push`) with its status reason ("No local changes" / "Start the environment to commit") and a **Refresh** icon button that re-checks dirty/status/commits. On the right, an icon-only **Disconnect** button → confirm dialog → `DELETE /agents/{id}/git`. Disconnect resets the card to disabled (the cached git queries are removed, not just invalidated, so the 404 refetch can't leave stale connected state).

## Two-Writer Model

Backend (deploy key, via `push`/`connect`) and the developer's local machine (own git/SSH credentials, plain `git push`) can both write to the same external remote on the same ref. Both are fast-forward-only:

- Backend: enforced by `fast_forward_push` — **409** "pull first" if the remote advanced.
- Developer: enforced by normal git (`[rejected] non-fast-forward`).

Conflict resolution is fail-loud on both sides. The human reconciles via standard git (`git pull --rebase` locally; "Pull" button in the UI for the backend). No auto-merge.

## cinna-cli Sparse-Checkout Integration (Forward-Looking)

The cinna-cli local development tool (separate repo, `/Users/evgenyl/dev/ml-llm/cinna-cli`) uses `GET /api/v1/cli/git-coordinates` to discover whether a given agent is VCS-enabled. When it is and the local agent folder is not yet a git working tree, the planned CLI-side behavior is:

1. Copy live files into the agent folder first (so the backend's uncommitted in-flight changes are present).
2. `git init` + `git remote add origin <repo_url>`.
3. `git fetch --depth=1 origin <ref>` (developer's own credentials).
4. `git sparse-checkout set <subdir>` (one local repo, many agent subdirs).
5. `git reset --mixed origin/<ref>` (NOT `--hard`) — points HEAD at the remote tree without overwriting the already-copied live files. The backend's uncommitted changes now appear as local uncommitted changes for the developer to review and push.

If the local folder already exists but has no `.git` (Mutagen sync predated VCS enablement), the CLI warns and does nothing: "This agent is now git-versioned, but this local folder isn't a git working tree. Run `cinna disconnect` here and re-sync." No auto-conversion.

The deploy key is **never** provided by this endpoint — the developer authenticates to the remote with their own git/SSH client.

## Security Model

- **SSH keys host-side only.** The private key is decrypted in memory by `SSHKeyService.get_decrypted_private_key` (ownership-checked), written to a chmod-600 temp file for `GIT_SSH_COMMAND`, and deleted in `finally`. The key material never reaches the container, is never logged, and never appears in any API response — including `CliGitCoordinates`.
- **Egress / SSRF guard on every network call.** `assert_git_url_allowed` runs on every clone, pull, push, `ls-remote`, `init_repo_with_remote`, and `git_log_subdir` call, before any network socket is opened. For HTTPS URLs it combines a static scheme/host-shape check with a DNS-resolving range check (DNS-rebind defense); for SSH URLs (`git@host:owner/repo.git`) it extracts the host and runs the range check directly. The private-host policy is controlled by `GIT_SOURCE_ALLOW_PRIVATE_HOSTS` (for self-hosted git on private LANs).
- **`.gitignore` and symlink guards are structural.** The `.gitignore` is derived from the same constants as the publish denylist (`BUNDLE_EXCLUDED_TOPLEVEL` + `RUNTIME_NAME_DENYLIST` + `PLUGIN_DERIVED_FILES` + the recursive cache denylist `NESTED_EXCLUDED_DIRS` / `NESTED_EXCLUDED_FILE_GLOBS`). The workspace capture on push and connect uses `_snapshot_workspace_tree` + `safe_copytree` + `_copytree_ignore` (which drops both symlinks and nested cache artifacts at every depth), so a symlink pointing at `../credentials`, or a nested `__pycache__/`, cannot be committed. The denylist on checkout/pull prevents an untrusted repo from injecting `credentials/`, `app-data/`, `logs/`, or `databases/` into the install workspace.
- **`required_credential_specs` is metadata only.** The manifest carries credential spec metadata (name, type, `provided_by`, `template_private_fields`) but never secret values — the same rule `PublishService._template_payload_for` enforces for catalog bundles.
- **Per-agent ownership; no monorepo.** `AgentGitSource.owner_id` scopes access; `_resolve_source_owned` returns 404 for non-owners (no existence leak). The `subdir` field allows several agents per repository without collapsing tenants into one history or one ACL.
- **Per-agent lock.** An in-process `asyncio.Lock` (keyed by `agent_id`) serializes concurrent pull/push/connect on one agent, mirroring the per-bundle publish lock.

## Conflict Model

| Situation | Behavior |
|-----------|----------|
| Same-user re-checkout of same bundle_id | **409** before any row is created |
| Another user's catalog bundle has the same bundle_id | **409** before any row is created |
| Connect when a git source already exists | **409** "disconnect first" |
| Connect with sync_direction="pull" | **400** (can't export with pull-only direction) |
| Connect when subdir already contains a `cinna.agent.json` | **Recoverable 409** (`existing_agent_folder`) — UI offers to adopt; re-send with `adopt_existing=true` to link to the existing folder (no overwrite, records it as the baseline) |
| Pull when prompts differ from last synced revision (DB dirty) | **409** "push or discard changes first" — source status unchanged |
| Push when remote advanced since last_synced_commit | **409** "pull first" — no clone or commit attempted — source status unchanged |
| Push rejected by remote side as non-ff | **409** via `GitNonFastForwardError` — source status unchanged |
| Genuine clone/network/filesystem error | Source stamped `status = ERROR` + `last_error`; original exception re-raised |

Planned but not yet implemented: 3-way reconcile for manifest/prompt fields on dirty pull (analogous to the prompt-sync `decide()` pattern). The current behavior is fail-loud only.

## Integration Points

| Feature | Relationship |
|---------|-------------|
| [Agent Bundles](../agent_bundles/agent_bundles.md) | Git is an external layer over `AgentBundleRevision`. Every git operation persists/reads a revision; checkout and pull reuse `InstallService._install_from_revision` and `replace_bundle_content`; push and connect reuse `RevisionFormat.write_tree` (which calls `PublishService._snapshot_workspace_tree`). `publisher_user_id = NULL` on git-imported `AgentBundle` rows is enabled by migration `c8a4f1e09b27_agent_bundle_publisher_nullable`. Push and connect now also persist an `AgentBundleRevision` as the dirty-check baseline |
| [Agent Environment Data Management](../agent_environment_data_management/agent_environment_data_management.md) | Pull uses `replace_bundle_content` verbatim; App Data, credentials, plugins, and runtime dirs are preserved; stale bundle-owned dirs are pruned. Push and connect use `_snapshot_workspace_tree` + symlink guards; the `.gitignore` is derived from the same denylist constants |
| [SSH Keys](../../application/ssh_keys/ssh_keys.md) | Private-repo authentication and deploy-key management. `AgentGitSource.ssh_key_id` FK references `user_ssh_keys.id`. `SSHKeyService.get_decrypted_private_key` (ownership-checked) provides the key; `create_ssh_key_file` writes a chmod-600 temp file; deleted in `finally`. The `DeployKeySelect` UI component reuses `SshKeysService.listSshKeys` and `generateSshKey` from the same feature |
| [Agent Webhooks](../agent_webhooks/agent_webhooks.md) | The `GIT_SOURCE` webhook type rides the existing webhook infrastructure (Fernet token, immutable invocation log, public `agent-hooks/` dispatch). `POST /agents/{id}/webhooks/git-source` creates the webhook; `AgentWebhookService.fire_webhook` dispatches `git_source` type to `GitSourceService.pull_update` |
| [Knowledge Sources](../../application/knowledge_sources/knowledge_sources.md) | Both features share `services/knowledge/git_operations.py` (GitPython primitives, SSH temp-key pattern, HTTPS/SSH URL converters). Knowledge sources share the same `services/common/egress_guard.py` SSRF chokepoint. `AgentGitSource` is modeled on `AIKnowledgeGitRepo` |
| [Agent Credentials](../agent_credentials/agent_credentials.md) | Checkout materializes credential specs from `cinna.agent.json` via `InstallService` (PBP/PBU/PBT rules apply normally). Credential values are never stored in or read from git trees — `required_credential_specs` carries metadata only |
| [User Roles](../../application/user_roles/user_roles.md) | Checkout, connect, disconnect, pull, push, and creating a git-source webhook require the `agent-developer` role (`require_developer` dependency). Reading git source status, checking updates, listing commits, and checking dirty state are owner-gated with no developer requirement |
| [cinna CLI Integration](../../application/cinna_cli_integration/cinna_cli_integration.md) | `GET /api/v1/cli/git-coordinates` (CLI-token / agent-scoped) exposes VCS coordinates to the local cinna CLI, enabling sparse-checkout git-linked local development. The deploy key is never included; the developer authenticates with their own credentials |
| [Agent Management](../agent_management/agent_management.md) | `git_versioning_enabled` is a computed capability flag on `AgentPublic` (presence of an `AgentGitSource` row), batched in `compute_capability_flags` alongside `has_email_integration` / `has_mcp_connectors` / `has_webhooks` to stay off the N+1 path. It lets the card's toggle render the real state from the already-loaded agent before its own git-source query resolves |
