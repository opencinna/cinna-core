# Agent Skills — workspace-native skill sets, engine projection, and a server skills catalog

**Feature name:** `agent-skills`
**Status:** Draft — implementation blueprint (2026-09-08)
**Decisions taken in this plan (confirm or override before Phase 1):**
D1 canonical location is a top-level, bundle-owned `skills/<name>/SKILL.md` in the agent workspace (never `.claude/skills/`) ·
D2 engines see agent-local skills through a runtime **projection** into `/root/.claude/skills/` (the per-instance `claude_sessions/` mount) that env-core refreshes before every message ·
D3 externally supplied skills always arrive through the existing **plugin install pipeline** (git marketplace today, server skills catalog in Phase 3) ·
D4 bundle-shipped `skills/` are publisher-owned (whole-dir replace on apply-update, like `scripts/`) ·
D5 the skill index shown to the model is the engine's native one; the platform only caches metadata for UI, commands and catalog cards ·
D6 the feature is strictly additive — an agent with no `skills/` directory behaves exactly as today.

---

## 1. Overview

Agents can carry a set of **skills** — folders shaped like the open Agent Skills standard (`skills/<name>/SKILL.md` with `name`/`description` frontmatter plus optional `scripts/`, `references/`, `assets/`). The workflow prompt stays the orchestration narrative and refers to skills by name; the engine (Claude Code or OpenCode) injects the name+description index itself and loads a skill's body only when it is invoked, which is the progressive-disclosure behaviour the feature is after. Skills travel with the agent through every existing seam (bundles, git versioning, env migration, Mutagen sync, local kit), are visible in the UI and the slash-command popup, and — in a later phase — can be published to and installed from a server-wide skills catalog through the plugin install pipeline.

Core capabilities:

- **Workspace convention** `skills/<name>/SKILL.md`, validated against the open standard (name regex, description cap, 50-skill cap), authored by the building agent or by the owner locally.
- **Engine projection** — env-core mirrors `skills/*` into `/root/.claude/skills/` (copy, hash short-circuited) before each message; Claude Code rescans per message (fresh CLI per turn), OpenCode gets an instance dispose when the tree hash changed. No container restart.
- **`Skill` tool enablement** in the Claude Code adapter; `permission.skill = allow` in generated OpenCode config.
- **Metadata cache + `/skills` command** — env-core `GET /config/skills` → `AgentSkillsService` cache on the environment row (CLI-commands pattern) → `/skills` slash command, popup entries, agent-page card.
- **Plugin-skill parity for OpenCode** — active plugins' `skills/` dirs are handed to OpenCode via its `skills` config paths; the existing "unsupported" report for `skills` is retired.
- **Bundle / git / kit propagation** — zero-change capture (denylist), derived `skills_summary` in the revision manifest, kit `layout.json` role and guide rewrite.
- **Skills catalog (Phase 3)** — `SkillPackage` + immutable `SkillPackageRevision` (directory snapshots, like bundle revisions), publish-from-agent, install-into-agent as `AgentPluginLink.source = catalog` fetched by the container as a signed archive.

High-level flow:

```
 building agent / owner ──writes──▶ /app/workspace/skills/<name>/SKILL.md (+ scripts/)
                                          │
        env-core (per message) ───────────┼──▶ project skills/* → /root/.claude/skills/*   (hash short-circuit)
                                          │      ├─ Claude Code: fresh CLI per message → rescans, Skill tool allowed
                                          │      └─ OpenCode:    tree hash changed → POST /instance/dispose → rescans
                                          │
        env-core GET /config/skills ──────┼──▶ backend AgentSkillsService → environment.skills_parsed (cache)
                                          │      ├─ /skills command · popup entries · Agent page card
                                          │      └─ bundle publish → revision.skills_summary (derived) → catalog card
                                          │
        bundles / git / migration / kit ──┴──▶ skills/ is captured by the existing denylist walk (no change)

 Phase 3:  agent skills/<name> ──publish──▶ SkillPackageRevision (dir snapshot) ──install──▶ AgentPluginLink(source=catalog)
                                                                                    └──▶ container fetches archive → plugins/cinna-skills/<slug>/skills/<name>/
```

---

## 2. Architecture Overview

### 2.1 Components

| Component | Location | Responsibility |
|---|---|---|
| Skill convention + validator | `backend/app/services/agents/skill_manifest.py` (new, pure) | Parse SKILL.md frontmatter, validate name/description/caps, reserved-name check. Shared by env-core (vendored copy, see 2.3), backend cache service, publish and catalog code. |
| Skills projection | `backend/app/env-templates/app_core_base/core/server/skills_projection.py` (new, env-core) | Compute the `skills/` tree hash; mirror into `/root/.claude/skills/`; prune stale projected dirs; return `changed`. Called from `sdk_manager` before every `send_message_stream`. |
| Claude Code adapter change | `.../adapters/claude_code_sdk_adapter.py` | Add `"Skill"` to `pre_allowed_tools`. |
| OpenCode adapter change | `.../adapters/opencode_sdk_adapter.py` | On `changed=True` dispose the workspace instance; plugin `skills/` dirs → `skills` config paths in `_materialize_opencode_config`; stop the per-mode server on plugin manifest change so it relaunches lazily with the new config. |
| OpenCode config generator | `backend/app/services/environments/environment_lifecycle.py::_generate_opencode_config_files` | Emit `permission.skill = "allow"` (and `tools.skill` left default). |
| env-core skills endpoint | `.../core/server/routes.py` `GET /config/skills` | Return the parsed index (local + plugin skills) with per-skill validation errors. |
| Backend cache service | `backend/app/services/agents/agent_skills_service.py` (new) | Fetch/parse/cache on `AgentEnvironment` (mirrors `CLICommandsService`), refresh triggers, rate limit. |
| Synced-file registry | `backend/app/services/environments/synced_files.py` + env-core `_WATCHED_FILES` | New `pull_only` entry `skills/` (directory entry — watcher gains directory-hash support). |
| Slash command | `backend/app/services/agents/commands/skills_command.py` (new) | `/skills` document listing; popup entries for `/<skill-name>`. |
| Prompt template | `.../core/prompts/BUILDING_AGENT.md` | New "Skills" authoring section; workflow-prompt guidance updated to reference skills by name. |
| Workspace classification | `backend/app/services/environments/workspace_classification.py` | Add `.claude` to `RUNTIME_NAME_DENYLIST` (defensive). |
| Revision format | `backend/app/services/bundles/revision_format.py`, `publish_service.py` | Derived `skills_summary` in manifest + nullable revision column. |
| Local kit | `docs/local_agent_kit/layout.json`, `guides/08-…`, `templates/agent/skills/` | New `skills` role, guide rewrite, contract version bump. |
| Catalog (Phase 3) | `backend/app/models/skills/`, `backend/app/services/skills/`, `backend/app/api/routes/skills.py`, `frontend/src/routes/_layout/catalog/skills.tsx` | Packages, revisions, publish, install via plugin link `source=catalog`, archive endpoint for containers. |

### 2.2 Data flow — one message

1. Backend `POST /chat/stream` → env-core `routes.chat_stream` → `sdk_manager.send_message_stream(mode, …)`.
2. **New:** `sdk_manager` calls `skills_projection.refresh()` (engine-agnostic). It hashes `skills/**` (relative path, size, mtime_ns; symlinks skipped), compares to the last hash persisted at `/root/.claude/.cinna_skills_hash`, and if different re-mirrors: for each valid skill dir copy the whole folder to `/root/.claude/skills/<name>/` (rm-then-copy), prune projected dirs that carry the `.cinna_projected` marker but no longer exist in `skills/`, write the new hash. Returns `changed: bool`. Never raises into the message path; failures log and return `False`.
3. Adapter proceeds. Claude Code: nothing further — the CLI subprocess spawned for this message rescans `~/.claude/skills` (user setting source, already enabled). OpenCode: if `changed`, `POST /instance/dispose` with `directory=/app/workspace` before creating/continuing the session so the memoized skill list is rebuilt.
4. Model invokes a skill via the `Skill` tool (Claude Code) or `skill` tool (OpenCode); the event transformer already normalises both to the unified `skill` tool name, so the chat renders it without change.

### 2.3 Two copies of the parser

env-core cannot import backend modules (it runs inside the container from `/app/core`). Follow the existing precedent for host⇄container shared constants (the `OPENCODE_RUNTIME_DIR_TEMPLATE` mirror): keep `skill_manifest.py` stdlib-only and vendor it into `app_core_base/core/server/skill_manifest.py`; a unit test asserts the two files are byte-identical (same guard style as the synced-files drift test).

### 2.4 Integration points (summary; details in §11)

Agent Environment Core (sdk_manager, adapters, routes) · Agent Prompts (BUILDING_AGENT.md) · Agent Plugins (OpenCode parity, catalog source) · Agent Bundles (manifest, publish, seed) · Agent Git Versioning (automatic) · Local Agent Kit (layout, guides, contract) · Agent Commands (`/skills`, popup) · Realtime Events (`WORKSPACE_FILES_CHANGED`, `AGENT_UPDATED`) · Catalog routes (Phase 3).

---

## 3. Data Models

### 3.1 Skill (filesystem entity — no table)

```
/app/workspace/skills/<name>/
├── SKILL.md            # required; YAML frontmatter + markdown body
├── scripts/            # optional; executed by the model via Bash
├── references/         # optional; read on demand
└── assets/             # optional
```

Frontmatter contract (validated by `skill_manifest.parse_skill_dir`):

| Field | Rule |
|---|---|
| `name` | required; `^[a-z0-9]+(-[a-z0-9]+)*$`, 1–64 chars, **must equal the directory name**; must not collide with a reserved platform command name (`files`, `files-all`, `run`, `run-list`, `skills`, `session-recover`, `session-reset`, `session-improve`, `webapp`, `rebuild-env`, `agent-status`) |
| `description` | required; 1–1024 chars after trim |
| `allowed-tools`, `disable-model-invocation`, `user-invocable`, `argument-hint`, `model`, `effort`, `context`, `agent`, `paths`, `metadata`, `license`, `compatibility` | optional, passed through untouched; the platform records only whether `disable-model-invocation` / `user-invocable` are set (drives popup availability) |
| body | ≤ 64 KB; larger files are still projected but flagged `warning: oversized` in the index |

Caps: 50 skills per agent (entries beyond 50, sorted by name, are excluded and reported); total projected bytes ≤ 16 MB (excess skills excluded with `error: budget`).

Parsed index entry shape (used by env-core response, env-row cache, revision summary and catalog):

```
{ name, description, source: "local" | "plugin", plugin_ref: "<mkt>/<plugin>" | null,
  path: "skills/<name>", has_scripts: bool, user_invocable: bool, model_invocable: bool,
  size_bytes: int, error: str | null, warning: str | null }
```

### 3.2 `agent_environment` — new cache columns (Phase 2)

Mirror the CLI-commands cache (`environment.py` lines ~107–110):

| Column | Type | Notes |
|---|---|---|
| `skills_parsed` | JSON, nullable | list of index entries above |
| `skills_hash` | String(64), nullable | tree hash reported by env-core; drives no-op short-circuit |
| `skills_fetched_at` | DateTime(tz), nullable | |
| `skills_error` | String(256), nullable | `env_not_running` / `adapter_error` / `parse_error` |

### 3.3 `agent_bundle_revision` — derived summary (Phase 2)

| Column | Type | Notes |
|---|---|---|
| `skills_summary` | JSON, nullable | `[{name, description, has_scripts}]` derived at publish from the snapshot tree. `None` = revision predates the feature (missing-key discipline of `manifest_to_revision_fields`). |

Manifest key `skills_summary` is **derived, never authored** — same discipline as `content_hash`. The kit's `cinna-agent.json` gets **no** `skills` key; the filesystem is the declaration.

### 3.4 Skills catalog (Phase 3)

**`skill_package`** — publisher-owned identity, one row per catalog entry.

| Column | Type | Constraints |
|---|---|---|
| `id` | UUID | PK |
| `package_id` | String(255) | reverse-DNS, unique, indexed (`uq_skill_package_package_id`); mirrors `AgentBundle.bundle_id` |
| `name` | String(64) | skill name inside the package (dir name); unique with `publisher_user_id` (`uq_skill_package_publisher_name`) |
| `display_name` | String(255) | |
| `description` | Text, nullable | copied from latest revision frontmatter at publish; editable |
| `publisher_user_id` | UUID FK `user.id` | `ON DELETE SET NULL`, nullable (ownerless after account deletion, like ownerless bundles) |
| `source_agent_id` | UUID FK `agent.id` | `ON DELETE SET NULL`, nullable — the agent it was published from |
| `latest_revision_id` | UUID FK `skill_package_revision.id` | `ON DELETE SET NULL`, nullable |
| `visibility` | String(16) | `private` \| `public`; default `private`; indexed |
| `is_listed` | bool | default true |
| `install_count` | int | maintained by the install service; excludes the publisher's own installs (bundle precedent) |
| `created_at`, `updated_at` | DateTime(tz) | |

**`skill_package_revision`** — append-only, immutable.

| Column | Type | Constraints |
|---|---|---|
| `id` | UUID | PK |
| `package_id` | UUID FK `skill_package.id` | `ON DELETE CASCADE`, indexed |
| `revision_number` | int | unique with `package_id` (`uq_skill_package_revision_number`) |
| `version` | String(64), nullable | semver string from publisher |
| `frontmatter` | JSON | parsed SKILL.md frontmatter (name, description, allowed-tools, …) |
| `snapshot_path` | String(1024) | `<SKILL_STORAGE_DIR>/<package uuid>/<revision_number>/skills/<name>/…` |
| `content_hash` | String(64) | SHA-256 over the snapshot tree (`publish_service.hash_workspace_tree` reused) |
| `size_bytes` | int | |
| `release_notes` | Text, nullable | |
| `published_by_user_id` | UUID FK `user.id` | `ON DELETE SET NULL` |
| `published_at` | DateTime(tz) | |

**`agent_plugin_link`** — extension.

| Change | Notes |
|---|---|
| `PluginSource` gains `catalog` | files fetched by the container as an archive from the backend; `plugin_id` NULL |
| `skill_package_revision_id` UUID FK `skill_package_revision.id`, nullable, `ON DELETE SET NULL`, indexed | identity + `has_update` detection (`package.latest_revision_id != link.skill_package_revision_id`) |
| dedupe | service-layer unique on `(agent_id, snapshot_marketplace_name="cinna-skills", snapshot_plugin_name=<package.name>)` — same NULL-tolerant rule the bundle source uses |

On-disk layout inside the agent (synthesised plugin so both engines load it through the plugin path):

```
/app/workspace/plugins/cinna-skills/<package name>/
├── .claude-plugin/plugin.json      # {name, version, description} generated by the container
├── .cinna_plugin_ref               # "<revision id>" idempotency marker (existing mechanism)
└── skills/<name>/SKILL.md …        # the revision snapshot, verbatim
```

Storage: `settings.SKILL_STORAGE_DIR` (default `/app/data/skills`), laid out `<package uuid>/<revision_number>/`; same atomic `<rev>.tmp` → move discipline as `publish_service._write_snapshot_to_disk`. Check the backend compose mount covers the new write path before Phase 3 lands (review-checklist item from the bundle storage work).

Lifecycle: package `visibility private → public` (owner toggle); revision has no state machine (immutable). An `AgentPluginLink(source=catalog)` follows the plugin link states: installed / disabled / per-mode flags; `has_update` computed.

---

## 4. Security Architecture

- **Trust boundary of a skill = trust boundary of `scripts/`.** A skill body is prompt text injected into every session's index and its scripts run with the agent's credentials. Agent-local skills are authored by the owner/building agent inside their own container — no new boundary. Catalog/marketplace skills are third-party content; they use the plugin pipeline on purpose so they inherit the plugin posture: explicit install, per-mode toggles, disable without delete, visibility rules (`private`/`public`), and the tools-approval flow for any tool a skill's `allowed-tools` would widen. The adapter's `can_use_tool` still denies interactive tools regardless of a skill's `allowed-tools`.
- **No secrets in snapshots.** `publish_skill` runs the kit's `secret_files` predicate (`docs/local_agent_kit/layout.json`) over the skill tree and rejects with 422 listing the offending relative paths; dotenv shapes and key material are blocked, `.example`/`.sample` allowed. The same predicate runs in `skill_manifest.validate_tree` so the agent-page card can warn before publish.
- **Projection writes are confined** to `/root/.claude/skills/` (rw mount `claude_sessions/`), never to `/app/core` (ro). The projector only copies directories whose name passed the regex and refuses symlinks and `..` components (same guards as `workspace_classification.safe_copytree`).
- **Archive endpoint for containers** (`GET /api/v1/skills/packages/{package_id}/revisions/{n}/archive`) authenticates with `AgentEnvContextDep` (scoped env token + `X-Agent-Env-Id`), and authorises by checking that the calling env's agent has an `AgentPluginLink(source=catalog)` for that revision. Tarball is built from the immutable snapshot; response carries `X-Content-SHA256` which the container verifies before extraction. Extraction uses a safe-extract (reject absolute paths, `..`, symlinks, device files).
- **Access control.** Publish: agent owner (publisher install or foreign install of their own) with the same role gate as bundle publish. Catalog read: `public` packages to any authenticated user; `private` to the publisher only. Install: the installing user must own the target agent; catalog visibility must allow them. Update/delete package: publisher only; a superuser may delist (`is_listed=false`) but not delete.
- **Input validation.** Name regex + reserved names; description cap; 64 KB SKILL.md; 16 MB per skill package; max 50 skills per agent; YAML parsed with `safe_load`; frontmatter unknown keys preserved but never interpreted.
- **Rate limiting.** Cache refresh shares the 30 s per-environment rate limit bucket pattern of `CLICommandsService` (independent bucket). Publish endpoints are user-actions; no extra limit beyond the global one.
- **Logging.** Never log skill bodies; log names, hashes and validation errors only. Archive download logs `(env_id, package_id, revision)`.

---

## 5. Backend Implementation

### 5.1 Phase 1 — convention, projection, engine enablement

**env-core (`app_core_base/core/server/`)**

- `skill_manifest.py` (vendored): `parse_skill_dir(path) -> SkillEntry | SkillError`, `scan_skills_root(root, *, max_skills=50, max_total_bytes=16 MiB) -> list[SkillEntry]`, `is_reserved_name(name)`, `tree_hash(root) -> str`.
- `skills_projection.py`: `refresh(workspace_dir, home=/root/.claude) -> ProjectionResult{changed: bool, projected: int, errors: [...]}`; marker file `.cinna_projected` written in each projected dir so pruning never touches a user-placed skill under `~/.claude/skills` (none exist today, but the guard is cheap); hash file `~/.claude/.cinna_skills_hash`.
- `sdk_manager.send_message_stream`: call `refresh()` before adapter dispatch; pass `skills_changed` into the adapter call (new keyword, default `False` so other callers are unaffected).
- `claude_code_sdk_adapter.py`: append `"Skill"` to `pre_allowed_tools`; on `skills_changed` nothing else (fresh CLI per message).
- `opencode_sdk_adapter.py`: on `skills_changed` call `POST /instance/dispose?directory=/app/workspace` (ignore 404 — older builds; log once). In `_materialize_opencode_config`, add each active plugin's `skills/` dir to `config["skills"]` (list of absolute paths, deduped) instead of reporting it unsupported; keep `agents`/`hooks` in `_OPENCODE_UNSUPPORTED_DIRS`. New `stop()` method that terminates the per-mode server; `routes.install_plugins` calls `sdk_manager.stop_opencode_servers()` after a successful manifest install so the next message relaunches with the new config (30 s startup cost, only on plugin change).
- `routes.py`: `GET /config/skills` → `SkillsIndexResponse{hash, skills: [SkillEntry], errors: [...]}` built from `scan_skills_root(workspace/skills)` plus, for each active plugin (any mode), `scan_skills_root(plugin/skills)` tagged `source=plugin`.
- `models.py`: `SkillEntry`, `SkillsIndexResponse`.

**Backend**

- `environment_lifecycle._generate_opencode_config_files`: add `"permission": {..., "skill": "allow"}`.
- `workspace_classification.RUNTIME_NAME_DENYLIST` += `".claude"`; `generate_gitignore` picks it up automatically.
- `prompt_generator.py`: no index injection when the engine supports skills (both do). Add `supports_skills: bool = True` on `BaseSDKAdapter` so a future adapter can opt out, in which case `generate_*_prompt` appends a `## Agent Skills` block listing `name — description` and the instruction to read `skills/<name>/SKILL.md` before use.
- `BUILDING_AGENT.md`: new `### skills/` entry under "Bundle-owned folders", a "Building Skills" subsection (when to split workflow logic into a skill, the SKILL.md contract, `${CLAUDE_SKILL_DIR}` for bundled scripts, keep body < 500 lines, reference skills from WORKFLOW_PROMPT.md by `/name`), and an example workflow prompt that acts as a skills index.
- Docs: `docs/agents/agent_skills/agent_skills.md` + `_tech.md`; registry row in `docs/README.md` (no counters).

### 5.2 Phase 2 — visibility

**Backend services**

- `backend/app/services/agents/agent_skills_service.py` — `AgentSkillsService` copied from `CLICommandsService`: `refresh_after_action(session, environment, *, force=False)`, `fetch_index(adapter) -> SkillsIndexResponse`, `handle_post_action_event(event)`, `get_cached(environment) -> list[dict]`. Short-circuit when `skills_hash` unchanged. Emits `AGENT_UPDATED` to the owner when the cached list changes (so the card re-renders).
- `synced_files.py`: `SyncedFile("skills", "skills/", "pull_only")`. env-core `_WATCHED_FILES` gains the same string; the watcher treats a trailing `/` entry as a directory and watches `tree_hash` instead of mtime. The existing drift test keeps both lists aligned.
- `app/main.py`: register `AgentSkillsService.handle_post_action_event` for `ENVIRONMENT_ACTIVATED`, `STREAM_COMPLETED`, `STREAM_ERROR`, `CRON_*`, `WORKSPACE_FILES_CHANGED` (derived from the registry like the other pull-only entries); `_sync_dynamic_data` calls `refresh_after_action(force=True)` in the start sweep.
- `DockerAdapter.get_skills_index()` → env-core `GET /config/skills`.

**Slash command** — `commands/skills_command.py`:
- `/skills` (`display="document"`, `include_in_llm_context=True`, `requires_running_environment=False`): markdown table `name | source | description | invoke` from the cache; falls back to "no skills" copy.
- `CommandService.list_for_session`: append dynamic entries `/<skill name>` for cached local+plugin skills with `user_invocable=True`, `description` as tooltip, `is_available=True`, `kind="skill"`. These are **not** registered handlers: `/name` passes through `is_command()` as an unregistered command and reaches the LLM, where Claude Code treats a leading `/<skill>` as an explicit skill invocation. Under OpenCode the text reaches the model as a normal message; the popup tooltip says "asks the agent to use this skill".

**Bundles**

- `publish_service.publish`: after `_snapshot_workspace_tree`, run `scan_skills_root(snapshot/workspace/skills)` and set `manifest["skills_summary"]`; hard-block publish on a skill with `error` (same posture as an unresolvable plugin) and on secret-predicate hits inside `skills/`.
- `revision_format.build_manifest` / `manifest_to_revision_fields`: `skills_summary` in/out; `AgentBundleRevision.skills_summary` column; `catalog_service._bundle_to_entry` adds `skills: [{name, description}]` from the latest revision.
- `git_source_service._build_live_manifest`: same derived key so `cinna.agent.json` and `manifest.json` stay byte-identical schemas.

**Routes** (`backend/app/api/routes/agents.py`, agent-owner scope via existing deps):
- `GET /agents/{agent_id}/skills` → `AgentSkillsPublic{skills: [SkillEntryPublic], fetched_at, error, hash}` from the cache of the active environment.
- `POST /agents/{agent_id}/skills/refresh` → forces `refresh_after_action(force=True)` (wakes a suspended env like `/agent-status`); returns the same shape.
- `GET /agents/{agent_id}/skills/{name}/content` → `SkillContentPublic{name, path, content}` — SKILL.md text only, read through the existing workspace download route on the adapter (no new file-serving surface).

### 5.3 Phase 3 — skills catalog

**Models** in `backend/app/models/skills/{skill_package.py, skill_package_revision.py}` + re-exports; `PluginSource.catalog`; `AgentPluginLink.skill_package_revision_id`.

**Service** `backend/app/services/skills/skill_catalog_service.py`:
- `publish_from_agent(session, *, agent, skill_name, version, release_notes, visibility, package_id?) -> SkillPackageRevision` — resolves the active env, pulls `skills/<name>/` via the adapter workspace download (tar) into a staging dir, validates (`skill_manifest`, secret predicate, size cap), writes the snapshot atomically, creates/extends the package (first publish creates it with `package_id = <reverse-dns of instance>.<publisher slug>.<name>` unless supplied), sets `latest_revision_id`, updates `description`.
- `list_catalog(session, user) -> [SkillPackageEntry]` — public listed packages + the user's own; each entry carries `latest_revision`, `install_count`, `installed_in_agent_ids` for the user.
- `get_package`, `update_package` (display_name, description, visibility, is_listed), `delist` (superuser).
- `install_into_agent(session, *, agent, package, revision=None) -> PluginSyncResponse` — creates `AgentPluginLink(source=catalog, snapshot_marketplace_name="cinna-skills", snapshot_plugin_name=package.name, skill_package_revision_id, installed_version=revision.version, installed_commit_hash=None)`, then `LLMPluginService.sync_plugins_to_agent_environments` (unchanged transport).
- `upgrade_link`, `uninstall` — reuse `LLMPluginService.uninstall_plugin_from_agent` (prune path is source-agnostic).
- `build_archive(revision) -> (bytes, sha256)` cached per revision on disk next to the snapshot.

**Manifest builder** `LLMPluginService.build_plugin_manifest`: for `source=catalog` emit `archive: {url: "<BACKEND_URL>/api/v1/skills/packages/{id}/revisions/{n}/archive", sha256, ref: "<revision id>"}` and `git: null`. env-core `PluginManifestEntry` gains `archive: PluginArchiveCoords | None`; `install_plugins` routes `source == "catalog"` to a new `_ensure_catalog_plugin` (download with the env token, verify sha256, safe-extract into `plugins/cinna-skills/<name>/`, write `.claude-plugin/plugin.json` and the `.cinna_plugin_ref` marker = revision id; idempotent by marker).

**Bundle interplay**: `_collect_plugin_specs` already snapshots `plugins/cinna-skills/*` and `plugin_sync.materialise` creates `source=bundle` links for consumers. Consumers therefore receive catalog skills as bundle plugins (updated with the bundle, no independent upgrade) — document, do not special-case.

**Routes** `backend/app/api/routes/skills.py` (tag `skills` → `SkillsService` in the generated client):

| Method + path | Deps | Purpose |
|---|---|---|
| `GET /skills/catalog` | `CurrentUser` | list entries (filter `visibility`, `installed`, `mine`) |
| `GET /skills/packages/{package_id}` | `CurrentUser` | detail + revisions |
| `PATCH /skills/packages/{package_id}` | owner | display_name / description / visibility / is_listed |
| `POST /skills/packages/{package_id}/delist` | superuser | |
| `GET /skills/packages/{package_id}/revisions/{n}/content` | `CurrentUser` + visibility | SKILL.md preview (text) |
| `GET /skills/packages/{package_id}/revisions/{n}/archive` | `AgentEnvContextDep` | container fetch (see §4) |
| `POST /agents/{agent_id}/skills/{name}/publish` | owner + developer gate | body `{version, release_notes, visibility, package_id?}` → `SkillPackageRevisionPublic` |
| `POST /agents/{agent_id}/skills/install` | owner | body `{package_id, revision_number?, conversation_mode, building_mode}` → `PluginSyncResponse` |
| `POST /agents/{agent_id}/plugins/{link_id}/upgrade` | existing route | extended to handle `source=catalog` |

Request/response schemas live in `backend/app/models/skills/schemas.py` (no tables): `SkillPackagePublic`, `SkillPackageEntry`, `SkillPackageRevisionPublic`, `SkillPublishRequest`, `SkillInstallRequest`, `SkillContentPublic`.

### 5.4 Background tasks

None new. Projection is synchronous and cheap (hash short-circuit; copies only on change). Catalog archive building is on-demand and cached. Cache refresh rides existing event handlers.

---

## 6. Frontend Surfaces (inventory, not design)

Composition is decided by `cinna-core.ui.design`, which appends a `## UI Specification` to this plan.

| Surface | Host / exists today | User intent | Data · actions (verbs) | API (generated service) | §4 anti-patterns touched |
|---|---|---|---|---|---|
| Agent skills list | Agent page › Config tab (`components/Agents/AgentConfigTab.tsx`, exists) — new sibling of the Prompts cards | "I want to see which skills this agent has and check one before I rely on it" | name, description, source (local / plugin / catalog), has_scripts, validation error/warning · view SKILL.md · refresh · publish (Phase 3) · open Plugins tab for plugin-sourced entries | `AgentsService.getAgentSkills`, `refreshAgentSkills`, `getAgentSkillContent`; contract gap: none (list is capped at 50 by the backend, so "Show all (N)" gets N from the array) | A9 (header icon on new card), A10 (row anatomy — build on `Common/ListRow` + `PreviewList`) |
| SKILL.md viewer | opened from the list; new | "read the skill as the model sees it" | markdown content · copy path | `getAgentSkillContent` | none |
| Slash-command popup entries | `components/Chat/SlashCommandPopup.tsx` (exists) | "invoke a skill by name from chat" | `/<name>` + description tooltip + "skill" kind badge | `SessionsService.listSessionCommands` (extended entry shape: `kind`) | none |
| `/skills` command output | chat system message, `display="document"` (exists pattern) | "list skills without leaving chat" | table | n/a | none |
| Bundle catalog card | `components/Catalog/CatalogCard.tsx` (exists) | "does this bundle come with skills?" | skill names line from `skills_summary` | `CatalogService.listCatalog` (entry gains `skills`) | none |
| Plugins tab rows | `components/Agents/AgentPluginsTab.tsx` (exists, **A10 open**) | Phase 3: "this installed skill came from the catalog; is there an update?" | source badge `catalog`, `has_update`, upgrade/uninstall/toggles (existing verbs) | existing `LlmPluginsService` + `upgrade` | **A10 — fix on touch** (rebuild rows on `ListRow`/`RowActionsMenu`) |
| Skills catalog route | `routes/_layout/catalog/skills.tsx` (new; parent `catalog.tsx` is a pass-through `<Outlet/>`; sibling `catalog/agents.tsx` exists) | "browse skills other people published and add one to my agent" | package name, description, publisher, version, install count, installed-in · preview SKILL.md · install into agent (agent picker) · filter mine/public/installed | `SkillsService.listSkillCatalog`, `getSkillPackage`, `getSkillPackageRevisionContent`, `installAgentSkill` | A9 (icons), A10 |
| Publish skill dialog | from the agent skills list; new | "share this skill on the server catalog" | version, release notes, visibility, package id (prefilled) · publish | `SkillsService.publishAgentSkill` | A2 (must not open from inside another dialog) |
| Package management | catalog › package detail (new) | publisher: "rename, hide, or make public" | display_name, description, visibility, is_listed, revisions · edit · delist | `updateSkillPackage`, `delistSkillPackage` | A5 |

**State management**
- Query keys: `["agent", agentId, "skills"]`, `["agent", agentId, "skills", name, "content"]`, `["skills-catalog", filter]`, `["skill-package", packageId]`. Existing `["agent", agentId]` invalidation on `AGENT_UPDATED` is extended to the skills key (the agent detail page already subscribes).
- Mutations: refresh, publish, install, upgrade, update package, delist — invalidate the keys above plus `["agent", agentId, "plugins"]` after install/upgrade.
- No context providers; no localStorage.

---

## 7. Database Migrations

| Phase | File (description) | Changes | Downgrade |
|---|---|---|---|
| 2 | `add_agent_environment_skills_cache` | `agent_environment`: `skills_parsed` JSON, `skills_hash` VARCHAR(64), `skills_fetched_at` TIMESTAMPTZ, `skills_error` VARCHAR(256) — all nullable | drop the four columns |
| 2 | `add_bundle_revision_skills_summary` | `agent_bundle_revision.skills_summary` JSON nullable | drop column |
| 3 | `add_skill_package_tables` | create `skill_package` (indexes: unique `package_id`; unique `(publisher_user_id, name)`; btree `visibility`; btree `publisher_user_id`), create `skill_package_revision` (unique `(package_id, revision_number)`; btree `package_id`), FK `skill_package.latest_revision_id → skill_package_revision.id ON DELETE SET NULL` added after both tables exist | drop FK, drop tables |
| 3 | `add_agent_plugin_link_catalog_source` | `agent_plugin_link.skill_package_revision_id` UUID nullable FK `skill_package_revision.id ON DELETE SET NULL`, btree index; `source` stays VARCHAR (enum value `catalog` is app-level) | drop column + index; app must first rewrite `source=catalog` rows (downgrade note in the migration docstring) |

Generate via `make migration`, review by hand (autogenerate does not see the app-level enum change), apply with `make migrate`.

---

## 8. Knowledge Repository Format

Not applicable to knowledge sources (they cannot index `skills/`). The **skill package format** is the open Agent Skills layout:

```
<name>/
  SKILL.md        # frontmatter: name (== dir), description; optional standard keys
  scripts/ references/ assets/   # optional
```

Validation rules are those in §3.1. A catalog archive is a gzip tarball rooted at `skills/<name>/` plus a generated `.claude-plugin/plugin.json` sibling, so the extracted tree is a valid Claude plugin and a valid OpenCode `skills` path.

---

## 9. Error Handling & Edge Cases

| Scenario | Behaviour |
|---|---|
| SKILL.md missing frontmatter / bad name / name ≠ dir | Skill excluded from projection; listed in the index with `error`; card shows the error; publish blocks on it |
| Skill name collides with a platform command | `error: reserved_name`; excluded from popup and projection |
| > 50 skills or > 16 MB | Overflow skills excluded with `error: budget`; index carries `warnings` |
| Projection write fails (disk, permission) | Logged, message proceeds; index reports `projection_error`; nothing else blocks |
| OpenCode dispose endpoint missing (older build) | 404 logged once per server; skills visible after the next server start; `/rebuild-env` is the user-facing fix |
| Skill invoked under OpenCode while `permission.skill` absent (pre-feature config) | OpenCode asks permission → surfaces as the existing tools-approval flow; regenerated config on next start fixes it |
| Pre-feature container (old `/app/core`) | No projection, no endpoint → cache error `adapter_error`; card copy says "rebuild the environment to enable skills" |
| `WORKSPACE_FILES_CHANGED` for `skills/` while env suspended | Handler exits early (`env_not_running`); refreshed on activation sweep |
| Bundle apply-update drops `skills/` | Whole dir pruned (D4); consumer-installed catalog skills under `plugins/cinna-skills/` survive (plugins merge rule) |
| Publish with secrets inside `skills/<name>` | 422 `skill_contains_secrets` with relative paths |
| Publish name already used by another publisher | Allowed (uniqueness is per publisher); `package_id` disambiguates |
| Archive sha256 mismatch on install | `PluginInstallResult(status=failed)`; existing amber banner + `PLUGIN_SYNC_FAILED` notification |
| Package deleted / revision missing when a container re-ensures | `failed` result with `catalog_revision_missing`; link kept (`SET NULL`), UI shows "source unavailable" like an orphaned marketplace link |
| Same skill name from local `skills/` and an installed plugin/catalog package | Both exist (plugin skills are namespaced `plugin:name` under Claude Code; OpenCode keeps first-loaded); index marks `warning: shadowed`; card shows both with source |
| User without developer role tries to publish | 403 from the role gate; the card hides the verb for that role (capability reply, not a role check in the client) |

---

## 10. UI/UX Considerations

- Status vocabulary for a skill row: `ok` (no colour), `warning` (`--warning`), `error` (destructive). Sources: `local`, `plugin`, `catalog` — plain metadata text, not badges on every row.
- Copy that carries meaning: "Skills are folders under `skills/` the agent can invoke by name; the model sees only the name and description until it uses one." Pre-feature environments: "Rebuild the environment to enable skills."
- Sequencing: the Publish verb appears only after the skill validates clean (no error, no secret hits). Install into an agent requires picking a target agent the user owns; a suspended target is woken by the plugin sync (existing behaviour) — the copy must say so.
- Accessibility: the popup's skill entries need a distinguishable `kind` label read by screen readers ("skill"), since they look like commands.

---

## 11. Integration Points

- **Agent Environment Core** — `sdk_manager`, both adapters, `routes.py`, `models.py`, new `skills_projection.py`, vendored `skill_manifest.py`. env-core ships in the per-environment `/app/core` copy: existing containers need `/rebuild-env` (or the admin bulk rebuild) before they project or report skills.
- **Multi-SDK** — `environment_lifecycle._generate_opencode_config_files` (`permission.skill`); OpenCode server stop-on-plugin-change.
- **Agent Prompts** — `BUILDING_AGENT.md` authoring guidance; no change to the three synced prompt docs or their reconcile.
- **Agent Plugins / Plugin Marketplaces** — `skills` removed from `_OPENCODE_UNSUPPORTED_DIRS`; new `PluginSource.catalog`; manifest `archive` coordinates; `_ensure_catalog_plugin`.
- **Agent Bundles** — denylist capture (no change), `skills_summary` in manifest/revision/catalog entry, publish hard-block on invalid skills or secrets.
- **Agent Git Versioning** — automatic; `.claude` joins the generated `.gitignore` through the denylist.
- **Local Agent Kit** — `layout.json` gains `{path: "skills", kind: "directory", role: "skills", survives_update: true}`; `docs/` role text drops "one doc per local skill"; guide `08-knowledge-and-local-skills.md` rewritten to the folder convention (the `docs/skill_*.md` form is documented as legacy, still imported); `templates/agent/skills/README.md` explains the layout; `CONTRACT_VERSION` bump + `CHANGELOG.md` entry; `cloud_import_excludes` unchanged (`skills/` travels). Contract tarball picks up `templates/agent/skills/` automatically.
- **Agent Commands** — `/skills` handler registered in `commands/__init__.py`; `list_for_session` dynamic entries.
- **Realtime Events** — `WORKSPACE_FILES_CHANGED` (directory entry), `AGENT_UPDATED` on cache change.
- **Catalog route family** — `/catalog/skills` beside `/catalog/agents`.
- **Client regeneration** after every backend phase: `source ./backend/.venv/bin/activate && make gen-client` (new `SkillsService`, extended `AgentsService`, `SessionsService`, `CatalogService` types).
- **Naming** — "skills" already means A2A card skills (`a2a_config.skills`, `skills_generator.py`) and plugin `skills/`. Code and docs for this feature say **agent skills** / `AgentSkillsService`; never store skill data in `a2a_config`.

---

## 12. Future Enhancements (Out of Scope)

- `cinna skills publish|install` CLI verbs and desktop-kit integration beyond the layout/guide changes.
- Consumer-authored skills that survive a bundle apply-update (would need a `skills/` merge branch like `_seed_plugins_tree`).
- Access grants for `visibility=users` on skill packages (bundle-style allowlist).
- Auto-detect-and-suggest publishing from the web UI ("this agent has 3 unpublished skills").
- OpenCode hot-reload of plugin skills without a server relaunch.
- Skill usage analytics (invocation counts from the `skill` tool events).
- Skill-level `hooks` / `agents` parity for OpenCode.

---

## 13. Summary Checklist

**Backend — Phase 1**
- [ ] Add `skill_manifest.py` (pure): parser, validator, reserved names, `tree_hash`; vendor into env-core with a byte-identity test.
- [ ] Add env-core `skills_projection.py`; call `refresh()` from `sdk_manager.send_message_stream`; pass `skills_changed` to adapters.
- [ ] Claude Code adapter: add `Skill` to `pre_allowed_tools`.
- [ ] OpenCode adapter: dispose instance on `skills_changed`; plugin `skills/` → `config["skills"]`; server `stop()` invoked from `routes.install_plugins`; drop `skills` from `_OPENCODE_UNSUPPORTED_DIRS`.
- [ ] `environment_lifecycle`: `permission.skill = allow` in generated `opencode.json`.
- [ ] `workspace_classification`: `.claude` in `RUNTIME_NAME_DENYLIST`.
- [ ] `BaseSDKAdapter.supports_skills`; prompt-generator fallback block.
- [ ] `BUILDING_AGENT.md` skills section; feature docs + README registry row.

**Backend — Phase 2**
- [ ] env-core `GET /config/skills` + `SkillEntry`/`SkillsIndexResponse` models; `DockerAdapter.get_skills_index`.
- [ ] `AgentSkillsService` cache + event wiring; migration `add_agent_environment_skills_cache`.
- [ ] `SyncedFile("skills", "skills/", "pull_only")`; watcher directory-hash support; drift test passes.
- [ ] `/skills` command handler; `list_for_session` skill entries with `kind`.
- [ ] Routes `GET /agents/{id}/skills`, `POST …/skills/refresh`, `GET …/skills/{name}/content`.
- [ ] `skills_summary`: publish derivation, manifest in/out, revision column + migration, catalog entry field, git live manifest, publish hard-block on invalid/secret-bearing skills.
- [ ] Local kit: `layout.json` role, guide 08 rewrite, template README, contract version + changelog.

**Backend — Phase 3**
- [ ] Models `SkillPackage`, `SkillPackageRevision`, schemas, re-exports; `PluginSource.catalog`; `AgentPluginLink.skill_package_revision_id`; two migrations.
- [ ] `SkillCatalogService` (publish_from_agent, list, get/update/delist, install, upgrade, archive).
- [ ] `build_plugin_manifest` archive coordinates; env-core `_ensure_catalog_plugin` with sha256 verify + safe extract + synthesised `plugin.json`.
- [ ] Routes in `routes/skills.py` + agent-scoped publish/install; register router; `SKILL_STORAGE_DIR` setting + compose mount check.

**Frontend**
- [ ] Regenerate client after each backend phase.
- [ ] Agent skills list + SKILL.md viewer on the Config tab (per UI Specification).
- [ ] Popup: render `kind="skill"` entries with tooltip and label.
- [ ] Catalog card: skills line.
- [ ] Phase 3: `/catalog/skills` route, package detail, publish dialog, install-into-agent flow; Plugins tab rows rebuilt on `ListRow` (A10) with `catalog` source and upgrade.

**Agent-env**
- [ ] Verify `/root/.claude` is writable in every env template's compose file (general-env, python-env-advanced, platform-knowledge-env).
- [ ] Confirm the pinned OpenCode build exposes `POST /instance/dispose` and honours `skills` config paths; record the version in the tech doc.
- [ ] Rollout note: existing environments need a rebuild to pick up env-core changes.

**Testing & validation**
- [ ] Parser: valid/invalid frontmatter, name≠dir, reserved names, caps, secret predicate, byte-identity of the vendored copy.
- [ ] Projection: first run copies, unchanged tree is a no-op, removed skill is pruned, foreign dir under `~/.claude/skills` untouched, symlink refused, failure never raises.
- [ ] Claude Code: a skill added between two messages is listed in the next `init` message's `skills`; `Skill` invocation is not denied by `can_use_tool`.
- [ ] OpenCode: dispose called only when the hash changed; plugin `skills/` paths appear in the materialised config; unsupported report no longer lists `skills`.
- [ ] Cache service: refresh on activation / post-action / watcher signal; rate-limit bypass on explicit change; `AGENT_UPDATED` emitted on change only; env-not-running path.
- [ ] Commands: `/skills` table; popup lists skills with `kind`; `/<skill>` passes through to the LLM; reserved-name collision excluded.
- [ ] Bundles: publish captures `skills/`, `skills_summary` derived, publish blocked on invalid skill and on secrets; install seeds `skills/`; apply-update replaces and prunes; pre-feature revision (no key) restores without touching skills; git commit includes `skills/` and ignores `.claude/`.
- [ ] Kit: contract tarball includes `templates/agent/skills/`; `kit.py validate` accepts the new folder; cloud import carries `skills/`.
- [ ] Catalog: publish creates package + revision atomically, second publish increments revision, visibility rules on list/detail/content, install creates a `catalog` link and the container fetches/verifies/extracts, sha mismatch surfaces as failed, upgrade re-fetches, uninstall prunes, bundle publish of an agent with catalog skills snapshots them as bundle plugins, archive route rejects an env without a matching link.
- [ ] Regression scope: `tests/api/agents/core/agents_resilient_plugins_test.py` + `tests/api/agent_environments/test_plugin_sync_events.py` (plugins), `tests/api/agents/bundles/` + `bundles_install/`, `tests/api/agents/commands/` (`/skills`, autocomplete), `tests/api/agents/git/`, `tests/unit/` for the pure modules (parser, projection, vendored-copy identity).

---

## UI Specification

_Produced by cinna-core.ui.design on 2026-09-08. Composition decisions here override the plan's frontend section (§6); data and API decisions stay with the plan. SSOT: `docs/development/frontend/ui_ux_guidelines.md` — every placement below cites a §2 row, every pattern a §3 reference._

§6 lists 8 surfaces. Three of them serve two user stories each and are split per §1 (a surface serving two stories is two surfaces), so this specification carries **11**. Phase tags are the plan's: **P2** = plan Phase 2 (visibility), **P3F** = plan Phase 3 (catalog). Nothing here belongs to plan Phase 1.

### Surfaces

| # | Surface | Phase | Host | Story | Placement (§2 row) | Pattern | Budget | Verification |
|---|---------|-------|------|-------|--------------------|---------|--------|--------------|
| S1 | Skills card | P2 | agent page › Configuration grid | View | card, half-width — "A card" (one concern), "List inside a card" | P5 + P3 rows | 2 blocks, 5 rows + Show all (N), 1 inline action/row (P2) → 1 inline + `⋯` of ≤2 (P3F), ≤2 flags | checklist |
| S2 | SKILL.md viewer | P2 | opened from an S1 row | View | dialog `sm:max-w-2xl` — "Disclosure depth" (card → dialog) | P5 accepted variant (read-only detail dialog) | 3 blocks, 2 actions, no nesting | checklist |
| S3 | Skill entries in the slash-command popup | P2 | `components/Chat/SlashCommandPopup.tsx` (exists) | View | in place — no new surface | existing popup row | +1 `Badge h-5` per skill row, 0 new columns | checklist |
| S4 | `/skills` command document | P2 | chat message, `display="document"` | View | in place | existing document command | 1 lead line + 3-column table | checklist |
| S5 | Skills line on the bundle catalog card | P2 | `components/Catalog/CatalogCard.tsx` (exists) | View | in place — "Card height" (bounded) | existing catalog card | 1 muted line, ≤3 names + "+N" | checklist |
| S6 | Skills catalog route + catalog section nav | P3F | new `routes/_layout/catalog/skills.tsx`, sibling of `catalog/index.tsx` | Manage-list (browse) | route — "Complex configuration" (own list lifecycle) | P9 variant: the catalog browse grid (`routes/_layout/catalog/index.tsx`) | 4 filters, grid 1/2/3/4 cols, 6 elements per card | checklist + screenshots |
| S7 | Skill package detail | P3F | new `routes/_layout/catalog/skills/$packageId.tsx` | View | route — the entity has revisions and its own lifecycle | detail-route grid (`components/Install/InstallPage.tsx`) + P5 for revisions | 3 cards, revisions 5 + Show all (N) | checklist + screenshots |
| S8 | Edit package details dialog | P3F | S7 › `⋯` | Edit | dialog `sm:max-w-md` — "Edit" story, one section ≤ 8 fields | P2-style edit dialog | 4 fields, Save/Cancel, dirty tracking | checklist |
| S9 | Publish skill dialog | P3F | S1 row `⋯` | Create | dialog `sm:max-w-md` — ≤ 3 visible fields | P6 single-step + success panel (`Admin/InviteSuccessPanel.tsx`) | 3 visible fields + 1 "Advanced" disclosure | checklist |
| S10 | Add skill to agent dialog | P3F | S6 card footer and S7 | Create | dialog `sm:max-w-md` — ≤ 3 fields; picker is a Popover, not a Dialog | form dialog (`Agents/InstallPluginModal.tsx`) + `Common/SearchableSelect` | 3 fields, 2 actions | checklist |
| S11 | Installed Plugins card rebuild (A10) | P3F | agent page › Plugins tab | View | card, full tab width (the tab is a single-column stack, not a card grid) | P5 + P3 segmented-multi-toggle row (`Admin/AccessPolicy/CompanyAiCredentialRow.tsx`) | 2 blocks, 5 rows + Show all (N), 1 inline control/row, menu of ≤4 | checklist + screenshots |

---

### S1 — Skills card

- **Intent:** "I want to see which skills this agent has, whether the engine can actually see them, and check one before I rely on it."
- **Phase:** plan Phase 2. Phase 3 adds only the row `⋯` menu (see Actions).
- **Layout:** skeleton by reference `frontend/src/components/Agents/AgentSchedulesCard.tsx` lines 98–172 — `Card` → `CardHeader` with the title/action row (`div.flex.items-center.justify-between.gap-3`) → `CardDescription` full width → `CardContent` holding one `PreviewList`. What differs: the header action is **Refresh**, not Create (skills are authored in the workspace, not in the web UI), and an `Alert` may precede the list.
- **Siblings:** the Configuration grid holds Information (`Info`), Agent Prompts (`ScrollText`), Bundle installation, Schedules (`CalendarClock`), Handover to Agents (`Workflow`), Agent status, Improvement requests — all half-width bare `Card`s in the tab's `grid grid-cols-1 lg:grid-cols-2 gap-6 items-start`, all icon `h-5 w-5` + noun title + one-sentence description. This card **matches**: same skeleton, same width, no `lg:col-span-2`. Insert it as a bare `Card` directly after the Agent Prompts card in `AgentConfigTab.tsx` so it flows into the grid (the host comment forbids wrapping a card in its own grid).
- **Rows / blocks:** 2 blocks — (1) the optional environment `Alert`, (2) the list as a whole. Rows: `ListRow` inside `PreviewList` (`previewCount = 5`, `total = skills.length`), sorted invalid first, then warning, then name ascending — the row that needs attention is the row that must be visible before "Show all".
  - `status` (always rendered, so every row aligns): `on` = "Projected — the engine can see this skill"; `warning` = the entry's `warning` text (oversized body, shadowed name); `error` = the entry's `error` text (invalid frontmatter, name ≠ directory, reserved name, budget). **This refines §10 of the plan**, which asks for "ok (no colour)": a conditional dot indents every clean row differently from every flagged one, and green is the house tone for "live" on every other list (§2 "Row state"). The plan's intent — *do not shout about a healthy skill* — is honoured by keeping colour out of the flags and the text.
  - `title` = skill name (`text-sm font-medium truncate`). No `Badge`.
  - `meta` = the skill's `description`, one line, truncated. This is the one fact that tells two skills apart and it is literally what the model is shown, so it earns the second line (§2 "Row height").
  - `flags` = at most two: a source `RowFlag` only when `source !== "local"` (`Puzzle` "Comes from the plugin `<plugin_ref>`", `GraduationCap` "Installed from the skills catalog"), then `RowInfo` with `["skills/<name>", has_scripts && "Ships scripts the agent can run", user_invocable ? "Invocable from chat as /<name>" : "Model-invoked only", "<size> KB"]`. Local skills carry no source flag — the default needs no glyph.
- **Actions:** inline **1** — `Button variant="ghost" size="icon" className="h-7 w-7"` with `FileText h-3.5 w-3.5` and a `Tooltip` "View SKILL.md", opening S2. Phase 3 adds the `⋯` `RowActionsMenu` beside it (2 inline total, at budget): "Publish to catalog…" (opens S9; rendered only when `useRole().isDeveloper` — a foreign install is already forced to agent-user by `RoleOverrideContext`; `disabled` when the entry carries an `error` or a secret warning, the reason being what the status dot's tooltip already says) and, for `source !== "local"`, "Open in Plugins tab". No destructive item — skills are files and the product offers no delete verb for them. Card header: `Button variant="outline" size="sm"` with `RefreshCw h-4 w-4` + "Refresh", `Tooltip` carrying "Checked <relative time>"; disabled + spinner while the mutation runs.
- **Interaction model:** view + disclosure. No auto-saving control, no inline form.
- **States:**
  - loading — `PreviewList` skeleton, 3 rows `h-[48px] w-full rounded-md`.
  - error (request failed) — `PreviewList` `isError={isError && !data}` → `QueryErrorAlert` "Couldn't load skills" + Retry; a failed background refetch keeps the rows.
  - payload carries `error` **and** cached rows exist — rows render with an `Alert` above them: `adapter_error` → "Rebuild the environment to enable skills." (plan §10 copy, verbatim); `env_not_running` → "The environment is asleep. Refresh to wake it and re-read the skills."; `parse_error` → "Couldn't read the skills folder. Refresh to try again."
  - payload carries `error` and no rows — the same `Alert` **replaces** the empty state.
  - empty — one sentence: "No skills yet. Skills are folders under `skills/` the agent can invoke by name; the agent creates them while it builds, or you add one from your local kit." (plan §10 copy, shortened to one sentence per §2 "Empty state"; there is no in-app Create action to link, and the header's Refresh is the only button the card has).
  - pending — Refresh disabled with a spinner; the row's View button disabled while its content query is in flight.
- **Components:** `Card`, `PreviewList`, `ListRow`/`ListRowGroup`, `RowFlag`, `RowInfo`, `RowActionsMenu` (P3F), `Alert`, `QueryErrorAlert`, `Skeleton`, `Tooltip`, `Button`. New files: `components/Agents/AgentSkillsCard.tsx`, `SkillRow.tsx`, `AllSkillsSheet.tsx` (the shared row keeps card and Sheet from drifting, as `HandoverRow` does). Header icon: **`GraduationCap`** (unused elsewhere in the tree; `Puzzle` is plugins, `Sparkles` is already three other things). No new primitive.
- **Anti-patterns:** A9 — the header carries its icon from the first commit. A10 — the list is built on `ListRow`/`PreviewList`, never a hand-written row.
- **Show all:** `AllSkillsSheet`, `SheetContent side="right" className="w-full sm:max-w-lg"`, skeleton by reference `Agents/AllSchedulesSheet.tsx`. Sheet rather than a route per P5's route-vs-Sheet test: no search, sort or pagination (the backend caps at 50) and a skill has no lifecycle of its own. Rows are the same `SkillRow`.
- **Read-only / foreign installs:** the card renders for consumers too (knowing what an installed agent can do is a use-capability); only the Publish menu item is absent.
- **Screenshots:** none. A P5 card in a host that already holds two P5 cards, on `PreviewList` + `ListRow` — §9 "straight instance of P3/P5" and R4 is judged from code.

### S2 — SKILL.md viewer

- **Intent:** "read the skill exactly as the model reads it."
- **Phase:** plan Phase 2.
- **Layout:** `Dialog` → `DialogContent className="sm:max-w-2xl"`; skeleton by reference `Agents/ScheduleLogsDialog.tsx` lines 250–291 (`DialogTitle className="flex items-center gap-2 min-w-0"` with `GraduationCap h-5 w-5`, `DialogDescription`, then the scroll region `max-h-[60vh] overflow-y-auto`). What differs: the scroll region holds one `<pre className="font-mono text-xs whitespace-pre-wrap break-words">` of the **raw** file.
- **Blocks:** 3 — header (skill name + the skill's description as `DialogDescription`), the path row (`Common/CopyableValue` showing `skills/<name>/SKILL.md`), the body.
- **Decision — raw source, not rendered markdown:** the stated intent is "as the model sees it", and the model is given the file verbatim, frontmatter included. Rendering it would hide the frontmatter, reflow fenced blocks and add a markdown viewer this dialog does not need. `Chat/MarkdownRenderer` stays out of this surface.
- **Actions:** 2 — "Copy" (`Copy h-4 w-4`, copies the file body, toast on success) and "Close". No `⋯`, nothing destructive, nothing opened from here (S9 Publish is on the **row**, not in this dialog — see A2 below).
- **Interaction model:** read-only.
- **States:** loading — one `Skeleton h-[240px] w-full` in the scroll region; error — `QueryErrorAlert` "Couldn't load SKILL.md" + Retry, in place of the body; empty — n/a (a skill without SKILL.md is not in the index); pending — the Copy button disables for the moment the write takes.
- **Components:** `Dialog`, `Skeleton`, `QueryErrorAlert`, `Button`, `CopyableValue`, `Tooltip`. New file: `components/Agents/SkillContentDialog.tsx`.
- **Anti-patterns:** A2 — this dialog opens nothing. The Publish dialog (S9) is reached from the row's `⋯`, so publish is card → dialog, never dialog → dialog (§2 "Disclosure depth", R8).
- **Screenshots:** none (form/detail dialog, §9).

### S3 — Skill entries in the slash-command popup

- **Intent:** "invoke a skill by name from chat without leaving the composer."
- **Phase:** plan Phase 2.
- **Layout:** no new surface. `SlashCommandPopup.tsx` renders `commands` unchanged; skill entries arrive from `listSessionCommands` with `kind === "skill"` and are rendered by the same row, with one addition: a `Badge variant="outline" className="h-5 text-[10px] font-normal"` reading **skill**, in the name cell after the command text. `h-5` keeps a badged row exactly as tall as an unbadged one (§2 "Row height").
- **Accessibility (plan §10):** the badge is visible text inside the `role="option"` row, so it is part of the option's accessible name and is announced — no `sr-only` duplicate. The description column already carries the skill's description.
- **Ordering:** registered commands first, then skills sorted by name; the existing filter narrows both. The popup keeps its own `max-h-64 overflow-y-auto`, so 50 skills cannot grow it.
- **States:** unchanged. A skill whose environment is unreachable simply does not appear (the cache is the source); no "Unavailable" row is minted for skills.
- **Components:** `Badge`. No new component, no new column.
- **Anti-patterns:** none. The popup's `<table>` predates this work and is not a card, so §5's "no `Table` in a card" does not apply; leave it.
- **Screenshots:** none — a badge added to an existing row shape (§9 "a badge, copy changes").

### S4 — `/skills` command document

- **Intent:** "list what this agent can do without leaving the chat."
- **Phase:** plan Phase 2. Backend-authored markdown; this is a copy and column spec, not a component.
- **Layout:** one lead line, then one table. Columns: **Skill · Source · What it does** — three, not the four in §6. The fourth ("invoke") would repeat the name in every row; the lead line says it once instead: "Type `/<name>` to ask the agent to use a skill."
- **Rows:** all cached skills, invalid ones last with their error in the "What it does" cell prefixed "⚠ ". Source cell: `local`, `plugin: <ref>`, `catalog`.
- **States:** empty — "This agent has no skills yet. Skills are folders under `skills/` the agent can invoke by name."; environment unreachable — "Rebuild the environment to enable skills." (same copy as S1, so the two surfaces cannot drift).
- **Screenshots:** none.

### S5 — Skills line on the bundle catalog card

- **Intent:** "does this bundle come with skills?"
- **Phase:** plan Phase 2.
- **Layout:** `components/Catalog/CatalogCard.tsx`, inside `CardContent`, **between** the badge row and the `bundle_id` `<code>` line: one `<p className="flex items-center gap-1.5 text-xs text-muted-foreground truncate">` with `GraduationCap h-3 w-3` and the names joined by ` · `, capped at **3** with `+N more` appended. Rendered only when `entry.skills` is a non-empty array — a pre-feature revision sends no key and the card is unchanged.
- **Decision — a line, not badges:** the card already carries two to three `Badge`s and a code line; a badge per skill would make the tallest card in an `auto-rows-fr` grid set the height of every card in it (§2 "Card height").
- **Actions:** none. The line is not clickable; the card's own click target is unchanged.
- **States:** absent when empty or missing. No loading state of its own.
- **Components:** none new.
- **Screenshots:** none — a text line on an existing card, capped at 3 names, so no data length can change the layout.

### S6 — Skills catalog route + catalog section nav

- **Intent:** "browse skills other people published and add one to an agent I own."
- **Phase:** plan Phase 3.
- **Layout:** skeleton by reference `frontend/src/routes/_layout/catalog/index.tsx` — `usePageHeader` title ("Skills catalog" / "Reusable skills published on this instance"), then `div.p-6.md:p-8.overflow-y-auto.space-y-6` → `div.mx-auto.max-w-7xl.space-y-4` → section tabs → filters → grid. Card grid by reference `Catalog/CatalogGrid.tsx` (`grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 auto-rows-fr`).
- **Navigation (a gap in §6, decided here):** `/catalog` is reachable from one sidebar item and has no section switch, so a second catalog route would be unreachable. Add `components/Catalog/CatalogSectionTabs.tsx` — two `RouterLink`s styled as the existing `CatalogFilters` pills ("Agents" → `/catalog`, "Skills" → `/catalog/skills`), rendered at the top of **both** routes, and widen `CatalogMenu`'s `isActive` in `components/Sidebar/AppSidebar.tsx` from `=== "/catalog"` to `startsWith("/catalog")`. No new sidebar entry: one Catalog destination, two sections.
- **Filters:** a `SkillCatalogFilters` built to the `Catalog/CatalogFilters.tsx` skeleton, 4 pills — All · Public (`Globe`) · Mine (`User`) · Installed (`Download`). Client-side over the fetched list, as the bundle catalog does.
- **Card (`SkillCatalogCard`, 6 elements):** `GraduationCap h-5 w-5` tile + `display_name`; publisher line reusing `Common/PublisherEmailConfirmedIcon`; `description` `line-clamp-3`; a badge row of exactly two — visibility (`Globe`/`Lock`) and `v<version>` (or `rev <n>`); the `package_id` `<code>` line; a footer `Button`. Footer verb: "Add to agent" (opens S10), or "Add to another agent" with a muted "In N of your agents" when `installed_in_agent_ids` is non-empty. Card body click → S7.
- **Actions:** 1 per card (the footer button) + the card body as a navigation target. No `⋯` on catalog cards — publisher actions live on S7.
- **Interaction model:** browse + navigate; the only mutation is behind S10's dialog.
- **States:** loading — `PendingItems` as the sibling route does; error — the sibling's centred destructive message (kept identical so the two catalog sections read as one page); empty (nothing published) — centred block, `GraduationCap h-8 w-8` in a `rounded-full bg-muted p-4`, "No skills published yet" + "Publish a skill from an agent's Configuration tab to see it here."; empty (filter) — "No skills match this filter."
- **Components:** `Card`, `Badge`, `Button`, `PublisherEmailConfirmedIcon`, `PendingItems`. New files: `routes/_layout/catalog/skills.tsx`, `components/Catalog/SkillCatalogCard.tsx`, `SkillCatalogGrid.tsx`, `SkillCatalogFilters.tsx`, `CatalogSectionTabs.tsx`.
- **Anti-patterns:** A9 — n/a (the route's cards are catalog tiles, not concern cards, and follow the sibling's tile header). A10 — n/a (no rows).
- **Screenshots:** **required** (§9: a new route). `/catalog/skills` at **1440** and **1024**, light theme, populated. 2 captures, booked against the route.

### S7 — Skill package detail

- **Intent:** "read what this package actually contains, see its versions, and decide whether to add it."
- **Phase:** plan Phase 3. Story: **View** only — the publisher's editing is S8, split per §1.
- **Layout:** route `catalog/skills/$packageId.tsx`; page header via `usePageHeader` (package `display_name`, publisher as the subtitle). Body: `div.p-6.md:p-8.overflow-y-auto` → `div.mx-auto.max-w-6xl` → `div.grid.items-start.gap-6.lg:grid-cols-[minmax(280px,360px)_1fr]`, skeleton by reference `components/Install/InstallPage.tsx` line 24. This is a detail route, not a card grid, so the asymmetric two-column split is the host's shape and A8 does not apply.
- **Left column — "Package" card** (`GraduationCap`): 5 facts as label/value lines (publisher, latest version, revisions, installs, visibility), the `package_id` via `CopyableValue`, one primary `Button` "Add to agent" (opens S10), and — publisher only — a `⋯` `RowActionsMenu`-style `DropdownMenu` on the title row with "Edit details…" (S8), separator, "Delist" (superuser) / "Delete package" (publisher, destructive, `AlertDialog` naming the package). 4 blocks.
- **Right column — "SKILL.md" card** (`FileText`): the selected revision's raw source in `<pre className="font-mono text-xs whitespace-pre-wrap">` inside `max-h-[60vh] overflow-y-auto`, with a "Copy" ghost button on the title row. Same raw-source decision as S2, and the same component: extract nothing — S2's dialog and this card each render the shared `<pre>` block through a small `SkillSource` presentational component only if a second consumer appears (§5: extract at the second consumer — this **is** the second consumer, so `components/Catalog/SkillSource.tsx` is the one new primitive this specification authorises, imported by both).
- **Right column — "Revisions" card** (`History`): `PreviewList`, 5 rows + "Show all (N)" → `AllSkillRevisionsSheet`, rows by reference `Agents/BundleRevisionRow.tsx` — `status` `on` for the latest / `off` for superseded, `title` = `v<version>` or `rev <n>`, `meta` = "published <relative time> · <size>", `RowInfo` = release notes + content hash, **1** inline action ("View" — swaps the SKILL.md card to that revision, a read-only expand of an existing panel, not a new dialog). No `⋯` (revisions are immutable).
- **Actions summary:** 1 primary (Add to agent), 1 publisher `⋯` of ≤3, 1 inline per revision row.
- **Interaction model:** view + disclosure; the only auto-saving control on the route is none.
- **States:** loading — `Skeleton` blocks shaped like the three cards; error — `QueryErrorAlert` + Retry in place of the grid; not-found / no visibility — centred block "This package isn't available to you." with a link back to `/catalog/skills`; empty — n/a (a package always has ≥ 1 revision); pending — Add-to-agent disabled with a spinner while S10's mutation runs.
- **Components:** `Card`, `PreviewList`, `ListRow`, `RowInfo`, `DropdownMenu`, `AlertDialog`, `CopyableValue`, `QueryErrorAlert`, `Skeleton`. New files: the route, `components/Catalog/SkillPackageCard.tsx`, `SkillRevisionRow.tsx`, `AllSkillRevisionsSheet.tsx`, `SkillSource.tsx`.
- **Anti-patterns:** A5 — the publisher's Edit / Delist / Delete are in the `⋯`, never visible controls. A9 — all three cards carry header icons.
- **Screenshots:** **required** (§9: a new route). `/catalog/skills/<id>` at **1440** and **1024**. 2 captures. The dev database holds no skill packages: publish one from a dev agent **through the product UI** (S9, the flow under test), capture, then delete it with the product's own publisher delete — the reversible, verified-clean-beforehand, restored shape the Channels build established. If deletion is not available to the publisher at capture time, report "required but not capturable" and review from the JSX rather than leaving data behind.

### S8 — Edit package details dialog

- **Intent:** the publisher wants to rename a package, retitle its description, or make it public.
- **Phase:** plan Phase 3. Story: **Edit** (split from S7's View per §1).
- **Layout:** `Dialog` → `DialogContent className="sm:max-w-md"`, one section, 4 fields: `display_name` (Input), `description` (Textarea, 3 rows), `visibility` (`ToggleGroup type="single"`, 2 segments Private · Public, with the consequence as a `Tooltip`: "Public — anyone on this instance can install it"), `is_listed` (`Switch` with a label beside it — one auto-save-shaped control, but here it is part of the form and saves with it). Explicit `DialogFooter`: Cancel · Save changes, disabled until dirty.
- **Interaction model:** form. One model — nothing in this dialog persists on change.
- **States:** pending — footer buttons disabled, Escape and outside-click blocked mid-request; error — `Alert variant="destructive"` above the footer with the server message; success — close + toast "Package updated"; loading/empty — n/a (the dialog opens from loaded data).
- **Components:** `Dialog`, `Input`, `Textarea`, `ToggleGroup`, `Switch`, `Label`, `LoadingButton`, `Alert`.
- **Anti-patterns:** A5 — this is the destination of the menu items that must not sit on the page.
- **Screenshots:** none (form dialog, §9).

### S9 — Publish skill dialog

- **Intent:** "share this skill on the server catalog."
- **Phase:** plan Phase 3. Story: **Create**.
- **Layout:** `Dialog` → `DialogContent className="sm:max-w-md"`, title `Upload h-5 w-5` + "Publish <name>". **3 visible fields**: `version` (Input, placeholder `1.0.0`), `release_notes` (Textarea, 3 rows, optional), `visibility` (`ToggleGroup type="single"`, Private · Public, default Private). `package_id` is prefilled by the server and lives inside **one** `Advanced` disclosure (a `Button variant="ghost" size="sm"` toggling a block) — §1: required fields only, sensible defaults pre-filled, advanced behind one disclosure. Three visible fields keeps this one dialog rather than a wizard.
- **Sequencing (plan §10):** the row's menu item is `disabled` when the entry carries an `error` or a secret-scan warning, so the dialog cannot be opened for a skill that would be rejected; the reason is already in the status dot's tooltip. A republish of an existing package shows the current `package_id` and the next revision number as static text above the fields.
- **Success:** the dialog body is replaced by a success panel (skeleton by reference `components/Admin/InviteSuccessPanel.tsx`): "Published <name> v<version>", a link "Open in the skills catalog" (`/catalog/skills/<id>`), a "Copy package id" button, and Close. It **links** onward; it never opens another dialog (§2 disclosure depth, A2).
- **Interaction model:** form.
- **States:** pending — the whole form disabled, `LoadingButton` on Publish, dialog not dismissible mid-request; error — `Alert variant="destructive"` naming the failure, and for `skill_contains_secrets` listing the offending relative paths one per line; loading/empty — n/a.
- **Components:** `Dialog`, `Input`, `Textarea`, `ToggleGroup`, `Alert`, `LoadingButton`. New file: `components/Agents/PublishSkillDialog.tsx`.
- **Anti-patterns:** A2 — opened from the S1 row `⋯` only. It must never be reachable from S2's viewer dialog.
- **Screenshots:** none (form dialog, §9).

### S10 — Add skill to agent dialog

- **Intent:** "put this catalog skill into one of my agents."
- **Phase:** plan Phase 3. Story: **Create** (install).
- **Layout:** `Dialog` → `DialogContent className="sm:max-w-md"`, skeleton by reference `Agents/InstallPluginModal.tsx` lines 59–192. Fields: **Agent** — `Common/SearchableSelect` (a `Popover` + search list anchored to the field); **Enable for** — the two `Checkbox`es the reference uses (Conversation · Building), both checked by default; **Advanced** disclosure — revision `Select`, default "Latest (v<x>)".
- **Decision — never `AgentSelectorDialog` here:** it is a `Dialog`, and a picker dialog on top of a form dialog is exactly R8/A2. §2's value-picker rule requires a Popover anchored to the field, which `SearchableSelect` already is.
- **Copy (plan §10):** under the agent field, one muted line — "A suspended agent is woken to install the skill."
- **Actions:** 2 — Cancel · "Add to agent" (`LoadingButton`).
- **Interaction model:** form.
- **States:** loading — the agent list loads inside the popover with its own "Loading…"; empty — "You don't have an agent to add this to." with a link to `/agents`; pending — form disabled, dialog not dismissible; error — `Alert variant="destructive"` with the server message (archive hash mismatch and sync failures surface through the existing plugin-sync warning banner on the Plugins tab, not here); success — close, toast "Added <name> to <agent>", invalidate `["agent", agentId, "plugins"]` and `["skills-catalog"]`.
- **Components:** `Dialog`, `SearchableSelect`, `Checkbox`, `Select`, `Label`, `LoadingButton`, `Alert`. New file: `components/Catalog/AddSkillToAgentDialog.tsx`.
- **Screenshots:** none (form dialog, §9).

### S11 — Installed Plugins card rebuild (A10 fix-on-touch)

- **Intent:** "which plugins and catalog skills does this agent carry, are they on, and is there an update?"
- **Phase:** plan Phase 3 — the phase that adds `source=catalog` rows to this list is the phase that touches the file, so A10 is fixed here rather than deferred.
- **What is wrong today** (`components/Agents/AgentPluginsTab.tsx` lines 411–570): a `<Table>` of rows carrying **three** `Switch`es, up to **four** `Badge`s, a visible `Uninstall` button that fires `uninstallMutation.mutate` with **no confirmation** (R6), an uncapped `map` (R4), an icon-less header (A9), and raw palette classes on the switches (`data-[state=checked]:bg-green-500` / `bg-blue-500` / `bg-orange-500`, R14a).
- **Layout:** `Card` → shared card skeleton with `Puzzle h-5 w-5` + "Installed plugins" + the existing description → `CardContent` with one `PreviewList` (cap 5, "Show all (N)" → new `AllInstalledPluginsSheet`). The same commit adds `Store h-5 w-5` to the "Available plugins" card header in the same file (A9, fix-on-touch, one line).
- **Siblings / width:** the Plugins tab is a single-column `space-y-6` stack, not a card grid — the Available plugins card holds a search field and its own tile grid and genuinely needs the width. Both cards therefore stay full tab width; A8 concerns spanning two columns of a card grid and does not apply. Do **not** convert this tab to a two-column grid in this phase.
- **Row (`InstalledPluginRow`, skeleton by reference `Admin/AccessPolicy/CompanyAiCredentialRow.tsx`):**
  - `status` — `on` when not disabled, `off` when disabled, label "Enabled" / "Disabled". `muted={plugin.disabled}`.
  - `title` — plugin name. `badges` — **at most one**: `v<installed_version>` as `Badge variant="secondary" className="h-5"`; nothing else.
  - `meta` — omitted. The plugin description does not tell two rows apart at a glance and would cost a line on every row; it goes to `RowInfo` (§2 "Row height").
  - `flags` — the source `RowFlag` (`Store` "From a marketplace", `Package` "Delivered by the bundle — managed by its publisher", `GraduationCap` "Installed from the skills catalog"); a `warning`-toned `RowFlag` `ArrowUpCircle` "Update available — v<latest_version>" when `has_update`; then `RowInfo` with `[description, category, "Installed <relative time>"]`.
  - inline control — **1**: `ToggleGroup type="multiple"` with 2 segments, `Chat` and `Build`, auto-saving one write per click (this replaces the two mode `Switch`es and counts as one control, §2 "Toggles on rows"). Full labels on `aria-label` + `Tooltip` ("Enabled in conversation mode" / "Enabled in building mode"). Disabled while the row is disabled; per-row pending via `mutation.variables?.id === plugin.id`.
  - `⋯` `RowActionsMenu` — "Update to v<latest>" (only when `has_update`), "Disable"/"Enable", `DropdownMenuSeparator`, "Uninstall" (destructive, `AlertDialog` naming the plugin, owned by the row). Bundle-sourced rows show only Enable/Disable; their managed-ness is the `Package` flag, replacing today's "Managed by bundle" text cell.
- **Density:** 2 blocks (the sync-warning banner already above the card is not part of it), 5 rows, 1 inline control, ≤ 4 menu items, ≤ 3 flags, 1 badge.
- **Interaction model:** auto-save on rows (the segmented toggle), everything else in the menu or its confirm — the `CompanyAiCredentialRow` model, one model per card.
- **States:** loading — `PreviewList` skeletons (replacing the card-level skeleton block at lines 334–346); error — `QueryErrorAlert` + Retry (replacing the raw `<p className="text-destructive">` at lines 348–357); empty — one sentence + a link to the Available plugins card below ("No plugins installed yet. Browse the available plugins below."); pending — per-row, never list-wide.
- **Components:** `PreviewList`, `ListRow`, `RowFlag`, `RowInfo`, `RowActionsMenu`, `ToggleGroup`, `AlertDialog`, `QueryErrorAlert`, `Badge`, `Skeleton`. New files: `components/Agents/InstalledPluginRow.tsx`, `AllInstalledPluginsSheet.tsx`. No new primitive.
- **Anti-patterns:** **A10 fixed** (row anatomy, no `Table`, no per-row `Switch`, no state `Badge`, ≤ 1 badge, no per-row border). **A9 fixed** for both cards in the file. **R6 fixed** (uninstall now confirms). **R4 fixed** (cap + Sheet). **R14a fixed** (no `bg-green-500` / `bg-blue-500` / `bg-orange-500`; state colour comes from the status dot's tokens). The tab's other A10 rows outside these two cards (`PluginCard` tiles) are **deferred** — they are tiles in a browse grid, not entity rows, and the phase does not touch their shape.
- **Screenshots:** **required** (§9: P5 used in a host that has never held one, and a rebuilt dense list). Agent page › **Plugins** tab at **1440** and **1024**, booked against the tab (host booking), not per card. 2 captures.

---

### Frontend phase order

**Plan Phase 2 — visibility (build in this order):**

| # | Surface | Backend contract it needs |
|---|---------|---------------------------|
| S1 | Skills card + `SkillRow` + `AllSkillsSheet` | `GET /agents/{id}/skills` → `AgentsService.getAgentSkills`; `POST /agents/{id}/skills/refresh` → `AgentsService.refreshAgentSkills` |
| S2 | `SkillContentDialog` | `GET /agents/{id}/skills/{name}/content` → `AgentsService.getAgentSkillContent` |
| S3 | Popup skill entries | `SessionsService.listSessionCommands`, entry shape extended with `kind` |
| S4 | `/skills` document | none (backend-rendered markdown) |
| S5 | Catalog card skills line | `CatalogService.listCatalog`, entry extended with `skills: [{name, description}]` |

**Plan Phase 3 — catalog (build in this order):**

| # | Surface | Backend contract it needs |
|---|---------|---------------------------|
| S6 | `/catalog/skills` + section nav | `SkillsService.listSkillCatalog` |
| S7 | Package detail + revisions | `SkillsService.getSkillPackage`, `getSkillPackageRevisionContent` |
| S10 | Add-to-agent dialog | `SkillsService.installAgentSkill`; `AgentsService.readAgents` for the picker |
| S9 | Publish dialog (+ the S1 row `⋯`) | `SkillsService.publishAgentSkill` |
| S8 | Edit package dialog + delist/delete | `SkillsService.updateSkillPackage`, `delistSkillPackage` |
| S11 | Installed Plugins card rebuild | existing `LlmPluginsService` list/upgrade/uninstall/mode routes, with catalog links projected (see gap 4) |

Regenerate the client after each backend phase; no frontend surface may be started before its service exists in `src/client`.

### Open questions and contract gaps

Composition questions are decided above; these are gaps for the plan owner, all on the data contract.

1. **No publish capability flag.** Plan §9 requires the Publish verb to be hidden "for that role (capability reply, not a role check in the client)", but `AgentSkillsPublic` / `SkillEntryPublic` (§3.1, §5.2) carry no capability field, so the only thing the client can branch on is `useRole().isDeveloper` — the role check §9 forbids. Add `can_publish: bool` to `AgentSkillsPublic` (or per entry). Until it exists, S1 gates on `isDeveloper` and the gap stands.
2. **Secret-scan result has no shape.** §4 says `validate_tree` runs the secret predicate "so the agent-page card can warn before publish", but the index entry (§3.1) carries only free-text `error` / `warning`. S1's dot tooltip and S9's disabled menu item need a discriminable value — e.g. `warning: "secrets"` with `warning_paths: [str]`, or an enum for both fields. Free text cannot be branched on.
3. **`error` / `warning` are free strings.** Same problem more generally: §9's table names the conditions (`reserved_name`, `budget`, `projection_error`, `shadowed`, `oversized`) but §3.1 types both fields as `str | null`. The status tone mapping in S1 needs a stable code plus a human sentence — recommend `{code, message}`.
4. **Catalog plugin links are under-projected.** §6 asks the Plugins tab to show "source badge `catalog`, `has_update`, upgrade/uninstall" for `source=catalog`, but such a link has `plugin_id = NULL`, so today's marketplace-derived `plugin_name`, `plugin_description`, `plugin_category` and `latest_version` on `AgentPluginLinkWithUpdateInfo` have no source. §5.3 defines `has_update` only inside the service prose. Specify that the link projection falls back to `snapshot_plugin_name` / the package's `description` and takes `latest_version` from `package.latest_revision.version` — otherwise S11's catalog rows render nameless.
5. **The new route has no way in.** §6 calls `catalog/agents.tsx` a "sibling" page; it is a pass-through `<Outlet/>`, the actual page is `catalog/index.tsx`, and the sidebar links to `/catalog` exactly once with `isActive: pathname === "/catalog"`. S6 therefore also specifies `CatalogSectionTabs` and the `startsWith` fix. Confirm this is in scope for the phase — without it `/catalog/skills` is only reachable by typing the URL.
6. **Publish from a suspended environment is undefined.** §5.3 says `publish_from_agent` "resolves the active env" and pulls the skill through the adapter, but §9 does not say what happens when the environment is suspended. S9 needs either a documented wake (like `POST /skills/refresh`) or a specific 409 the dialog can spell out.
7. **"Installed in" gives ids, not names.** `list_catalog` returns `installed_in_agent_ids` (§5.3), so S6's card says "In N of your agents" rather than naming them. Fine as specified; if names are wanted on the card, the entry needs them — a second query per card is not acceptable in a grid.
8. **Revision projection fields.** S7's revision rows use version, published-at, `size_bytes`, `release_notes` and `content_hash`; §3.4 has them all on the table, but `SkillPackageRevisionPublic` is only named in §5.3. Confirm all five are projected.
