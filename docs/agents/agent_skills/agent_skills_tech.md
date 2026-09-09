# Agent Skills — Technical Reference

Implementation reference for [agent_skills.md](agent_skills.md). Delivered in
three backend phases (convention + projection + engine enablement → visibility →
catalog) plus the Local Agent Kit contract bump.

---

## Architecture

| Component | Location | Responsibility |
|-----------|----------|----------------|
| Skill parser / validator | `backend/app/services/agents/skill_manifest.py` | Parse `SKILL.md` frontmatter, validate, secret predicate, budget caps, tree hash. Pure, stdlib-only |
| Vendored parser copy | `backend/app/env-templates/app_core_base/core/server/skill_manifest.py` | Byte-identical mirror env-core imports |
| Projection | `backend/app/env-templates/app_core_base/core/server/skills_projection.py` | Mirror workspace `skills/` into `/root/.claude/skills/`, prune, retry, latch |
| env-core index builder | `.../core/server/agent_env_service.py::build_skills_index` | Merge workspace + plugin skills, apply caps once, flag `shadowed` / `projection_error` |
| env-core endpoint | `.../core/server/routes.py::get_skills_index` (`GET /config/skills`) | Serve the index |
| env-core wire models | `.../core/server/models.py` | `SkillIssuePublic`, `SkillEntry`, `SkillsIndexResponse`, `PluginArchiveCoords` |
| Adapters | `.../core/server/adapters/{base,claude_code_sdk_adapter,opencode_sdk_adapter}.py` | `SUPPORTS_SKILLS`, `skills_changed` keyword, `Skill` pre-allow, `/instance/dispose`, `skills.paths`, `stop()` |
| Container installer | `.../core/server/agent_env_service.py::_ensure_catalog_plugin` | Download / verify / safe-extract a catalog archive, synthesise `plugin.json` |
| Backend cache service | `backend/app/services/agents/agent_skills_service.py` | Pull, normalise and cache the index on `AgentEnvironment` |
| Read routes | `backend/app/api/routes/agent_skills.py` | `GET/POST /agents/{id}/skills*`, `GET .../skills/{name}/content` |
| Catalog models | `backend/app/models/skills/{skill_package,skill_package_revision,schemas}.py` | Tables + wire shapes |
| Catalog service | `backend/app/services/skills/skill_catalog_service.py` | Publish, browse, manage, install, upgrade, archive |
| Coded failures | `backend/app/services/skills/exceptions.py` | `SkillCatalogError` + `STATUS_BY_CODE` + `http_error_for` |
| Catalog routes | `backend/app/api/routes/skills.py` | `/skills/...` and the agent-scoped publish/install verbs |
| Slash command | `backend/app/services/agents/commands/skills_command.py` | `/skills` document |
| Wake helper | `backend/app/services/agents/environment_resolver.py::wake_suspended_environment` | Shared by the status and skills refresh buttons |

### Data flow — one message

1. Backend `POST /chat/stream` → env-core `routes.chat_stream` →
   `sdk_manager.send_message_stream(mode, …)`.
2. `sdk_manager` runs `skills_projection.refresh(adapter.workspace_dir)` **off
   the event loop** (`asyncio.to_thread`) before dispatching to the adapter. The
   fast path is one stat walk; a real re-projection can copy up to the 16 MB
   budget, and this process also serves every other session's SSE stream.
3. `skills_changed` is computed **per mode** by comparing the returned
   `identity` against `SDKManager._skills_state_by_mode[mode]`, then latched
   *before* the adapter runs.
4. Claude Code ignores `skills_changed` (fresh CLI per message rescans).
   OpenCode calls `POST /instance/dispose?directory=/app/workspace` — but only
   when the flag is set **and** the server was not just started by this call.

---

## `skill_manifest.py`

### The two copies

env-core runs inside the container from `/app/core` and cannot import backend
modules, so the module is **vendored byte-identically** into
`backend/app/env-templates/app_core_base/core/server/skill_manifest.py` — the
same host ⇄ container mirror precedent as `OPENCODE_RUNTIME_DIR_TEMPLATE`.
`backend/tests/unit/test_skill_manifest.py::test_host_and_env_core_copies_are_byte_identical`
compares SHA-256 digests. Edit the backend file and copy it over; never edit one
side.

Consequences: **stdlib only** (no pydantic, no PyYAML, no project imports) and
**pure** (no I/O beyond the tree it is pointed at, no globals outliving a call).

### Frontmatter is parsed by a restricted-subset parser, not PyYAML

The plan's §4 said "YAML parsed with `safe_load`", which conflicted with §5.1's
stdlib-only rule: **the container image does not declare PyYAML**. Rather than
have two parsers with two behaviours, `parse_frontmatter(text) -> (mapping,
body)` implements the subset the Agent Skills standard actually uses.

Supported: top-level `key: value` scalars; flow sequences `[a, b, "c"]`; block
sequences (`- item`); block scalars (`|`, `>`, and the `-`/`+` variants); one
level of nested mapping; a closing fence of `---` or `...`. Scalar coercion
handles matched quotes, `true/yes`, `false/no`, `null/~`, integers and floats;
everything else stays a string. Unknown keys are preserved untouched.

**Behavioural divergences from real YAML** (the same list is carried in the
module docstring, so the parser explains both *why* it is not PyYAML and *how*
it differs; keep the two in step):

| Divergence | Effect |
|------------|--------|
| Inline comments are not stripped | `name: foo # note` yields the string `foo # note`. Only a line whose stripped form *starts* with `#` is a comment |
| Flow sequences split naively on `,` | `[ "a, b", c ]` becomes three items |
| Block-scalar chomping indicators are accepted but not honoured | `\|`, `\|-` and `\|+` behave identically, as do `>`, `>-` and `>+`; lines are `.strip()`ed, so indentation inside a block scalar is lost, and the folded (`>`) form joins blank lines with a single space, so paragraph breaks do not survive |
| Nesting is one level deep and depth is not tracked | Deeper structure is mangled, not dropped: a grandchild `key: value` is hoisted into the *same* mapping (where it can silently overwrite a sibling), and any `- item` under a nested key becomes a sequence that replaces the mapping outright |
| Unclassifiable lines are skipped, not raised on | A malformed line disappears rather than failing the parse |
| Scalar coercion uses a fixed, narrow token set | `yes`/`no` are booleans (YAML 1.1, not 1.2) but `on`/`off` are **not**; `null`/`~` are null; a number is `-?\d+` or `-?\d+\.\d+`, so `1e3`, `0x1f`, `+5` and `.5` stay strings — though `\d` is Unicode-aware, so a non-ASCII decimal digit does become an `int` |
| No anchors, aliases, tags, complex keys or multi-document | Not supported at all |
| Keys are restricted to ASCII `[A-Za-z0-9_.-]+` | Any other key shape, a Unicode key included, is skipped along with its value |
| Quote stripping does no escape processing | `"a\nb"` keeps the literal backslash-n |
| Duplicate keys | Last wins |
| `utf-8-sig` decoding | A BOM-prefixed `SKILL.md` still parses (a leading `﻿` would otherwise fail the `---` fence test) |

### Contract constants

```python
SKILL_NAME_RE          = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME_LENGTH        = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_BODY_BYTES         = 64 * 1024          # oversized WARNING, still projected
DEFAULT_MAX_SKILLS     = 50                 # per agent
DEFAULT_MAX_TOTAL_BYTES = 16 * 1024 * 1024  # per agent
RESERVED_SKILL_NAMES   = frozenset({...})   # mirror of the command registry
SKIP_DIR_NAMES         = {".git", ".venv", "__pycache__", ".mypy_cache",
                          ".ruff_cache", "node_modules"}
```

`RESERVED_SKILL_NAMES` is a vendored mirror of
`backend/app/services/agents/commands/__init__.py` (env-core cannot import the
registry). `test_skill_manifest.py::test_every_registered_platform_command_is_reserved`
guards the drift.

### Issue vocabulary

`ISSUE_MESSAGES: dict[str, str]` holds every code **and** the sentence that
explains it in one table, so the container, the cache and the UI cannot disagree
about what "budget" means.

```python
@dataclass
class SkillIssue:
    code: str
    message: str = ""            # filled from ISSUE_MESSAGES when omitted
    paths: list[str] = []        # populated only for code="secrets"
```

- `issue_from_dict(value)` rebuilds one from JSON and tolerates a bare code
  string — a cache row written before the `{code, message}` shape existed must
  read cleanly until the next refresh replaces it (the cache is refreshed from
  the container, never migrated).
- `pick_warning(*candidates)` returns the most severe per
  `WARNING_PRECEDENCE = ("secrets", "shadowed", "oversized")`.

### `SkillEntry`

```python
@dataclass
class SkillEntry:
    name: str
    description: str = ""
    source: str = "local"           # "local" | "plugin" | "catalog"
    plugin_ref: str | None = None   # "<marketplace>/<plugin>"
    path: str = ""                  # workspace-relative folder
    has_scripts: bool = False
    user_invocable: bool = True
    model_invocable: bool = True
    size_bytes: int = 0             # whole folder
    error: SkillIssue | None = None
    warning: SkillIssue | None = None
    secret_paths: list[str] = []    # ALWAYS present; empty = clean
    frontmatter: dict = {}          # opt-in on to_dict()

    is_valid       = error is None
    is_publishable = error is None and not secret_paths
```

`to_dict(include_frontmatter=False)` — frontmatter is opt-in because only the
catalog publish path needs it; the UI index never does.

### Functions

| Function | Notes |
|----------|-------|
| `parse_skill_dir(path, *, source, plugin_ref, rel_path)` | Never raises; a malformed skill comes back carrying an `error`. `rel_path` **must** be passed for a plugin's skill folder, or `path` would point at the agent's own `skills/<name>` and read the wrong file |
| `scan_skills_root(root, *, source, plugin_ref, rel_root, max_skills, max_total_bytes)` | Missing root → `[]`. Skips dotfiles **and non-directories silently** (see below). Sorts by name, then applies the caps |
| `apply_budget(entries, *, max_skills, max_total_bytes)` | Mutates in place. Separate from the scan because the caps are per **agent** while a scan sees one root. Entries already carrying an error are skipped and charge nothing. Re-application is a fixed point — the merged pass sees the union of the per-root passes, so a second pass can only add exclusions |
| `tree_hash(root)` | SHA-256 over `(relative path, size, mtime_ns)` — never file contents, because it runs before every message. A missing root hashes the empty string |
| `validate_tree(root)` / `is_secret_filename(name)` | The secret predicate. Rules are content-identical to `docs/local_agent_kit/layout.json` → `secret_files.rules`, mirrored here because the module runs inside the container where the kit contract is absent. Unknown clause vocabulary fails **toward** treating the file as secret in `match` and **away** in `unless` |
| `_measure_tree(root)` | One walk, two answers (total bytes + secret hits) — walking a skill tree twice per index build costs on a 50-skill agent |
| `_walk_files(root)` | Skips symlinks at both directory and file level: the projector refuses to follow them, so hashing them would describe a tree that is not the tree that gets copied |

**A non-directory at the skills root is skipped silently, not reported.** The
Local Agent Kit ships `skills/README.md` as scaffolding, so a plain file there is
the normal case. `not_a_directory` is reachable only through a direct
`parse_skill_dir` call. This guard is load-bearing; removing it would put a red
row on every kit-scaffolded agent's card.

---

## `skills_projection.py` (env-core)

Target: `/root/.claude/skills/` — the writable `claude_sessions/` bind mount.
All three env templates
(`backend/app/env-templates/general-env/docker-compose.template.yml`,
`python-env-advanced`, `platform-knowledge-env`) mount
`${HOST_INSTANCE_DIR}/claude_sessions:/root/.claude` read-write.

`~/.claude/skills/*/SKILL.md` is a documented OpenCode global discovery path as
well as Claude Code's user-scope source, which is why **one** projection serves
both engines.

### `ProjectionResult` — `identity`, not `changed`

```python
@dataclass
class ProjectionResult:
    changed: bool = False    # this run's own view — logging only
    projected: int = 0       # marker-bearing dirs counted ON DISK after the run
    hash: str = ""           # workspace skills/ tree hash
    identity: str = ""       # sha256 over json.dumps([tree_hash, sorted(names)])
    errors: list[str] = []
```

The plan specified a single `changed: bool`. The implementation carries
`identity` and consumers latch **that**, because:

- it moves when a skill's content changes (the hash) **and** when a previously
  failed skill finally lands (the names) — which the hash alone cannot see;
- it holds still while a durable failure keeps retrying, so a broken skill cannot
  make OpenCode dispose its instance on every single turn.

`json.dumps` rather than a separator join keeps the encoding injective: a
directory name containing the separator cannot masquerade as two names.

`SDKManager._skills_state_by_mode: dict[str, str]` latches it **per mode**, not
globally: building and conversation are separate engine processes with separate
memoized skill lists, so a skill projected during a building message must still
register as a change for the conversation adapter — otherwise the feature's
headline flow (build a skill in building mode, use it in conversation) is exactly
what breaks. An empty `identity` (the projection could determine nothing) asks
for no rebuild.

### State file and marker

`~/.claude/.cinna_skills_hash` holds
`{"hash": ..., "projected": [names], "failed": [names]}`.

- `projected` records what is **actually on disk** at the end of a run, not what
  the run intended to write — a directory that resisted removal must be in the
  recorded state, or the fast-path comparison would mismatch on every later
  message and re-project the whole tree forever.
- `failed` is what keeps a durable copy failure from re-copying everything each
  turn: the hash latches, and only the failed names are retried.
- An absent, corrupt or truncated state file is not an error — it means "nothing
  known", costs one re-projection and self-heals.

Each projected directory carries a `.cinna_projected` marker file. `_prune_stale`
removes **only** marker-bearing directories, so a skill a user placed under
`~/.claude/skills` by hand is never eaten.

### Guards

- `_copy_skill` is **rm-then-copy**, not a merge: a skill folder is
  publisher-owned as a whole, so a file the publisher deleted must disappear.
- `copytree(symlinks=False, ignore=_ignore_symlinks)` drops every symlink.
  Following one would let an agent-controlled workspace pull host-visible content
  into the projection, bypass the size budget (the manifest walk skips links, so
  a linked payload is never counted) and, on a link loop, raise `RecursionError`
  — which is not an `OSError` and would escape the per-skill guard.
- `_project_one` catches `shutil.Error` explicitly (copytree's per-file
  aggregate is not an `OSError`) and `rmtree`s the destination on failure:
  a half-written directory, or a complete one whose marker write failed, is one
  `_prune_stale` refuses to touch while both engines happily index its
  `SKILL.md`.
- `refresh()` never raises — a bare `except Exception` returns
  `changed=False`. A broken projection degrades to "the model does not see the
  new skill", never to a failed turn.

### Fast path and the "wiped projection" case

The fast path requires **both** `state["hash"] == current_hash` **and** the
marker-bearing names on disk equal to `state["projected"]`. A state file that
outlived its directory (a wiped `claude_sessions/`, a half-failed prune) would
otherwise latch "up to date" forever while the engine sees nothing.

The retry path calls `parse_skill_dir`, not `scan_skills_root`: the caps were
already applied when the name was put on the failed list, and the unchanged tree
hash pins the tree they were applied to.

`projection_failures(home)` reads only the latched state (no walk, no copy) and
is what the index builder turns into `projection_error` rows.

**Known limitation**, inherited from `tree_hash`: content that changes while
keeping both size and `mtime_ns` (an mtime-preserving restore) does not move the
identity, so the projection is not refreshed. The mirror case — an mtime-only
touch on byte-identical content — costs one needless re-copy.

---

## Engine enablement

### `BaseSDKAdapter`

```python
SUPPORTS_SKILLS: bool = True          # engine has a native Agent Skills index
supports_skills -> bool               # property

async def send_message_stream(..., skills_changed: bool = False)
```

Default `False` on the keyword so other callers are unaffected.

### Claude Code

`"Skill"` is appended to `pre_allowed_tools`. A skill's body is content the owner
authored or installed through the plugin pipeline; the tools that body then asks
for are still gated by `can_use_tool` and `allowed_tools`. `skills_changed` is
accepted and deliberately unused — a fresh CLI subprocess per message rescans
`~/.claude/skills` on start.

### OpenCode

| Piece | Detail |
|-------|--------|
| `_dispose_instance()` | `POST {base_url}/instance/dispose?directory=<workspace>`, 10 s timeout. A 404 sets `_dispose_unsupported` and logs **once per server**; skills then appear at the next server start, and `/rebuild-env` is the user-facing fix. Never breaks the message path |
| Dispose gating | `if skills_changed and not server_just_started` — `_ensure_server_running()` now returns whether *this* call launched the server, and a fresh process has already read the current projection |
| `stop()` | Terminates the per-mode `opencode serve` (`SIGTERM`, then `SIGKILL` after `OPENCODE_STOP_TIMEOUT = 10 s`), clears `_current_session_id`. Returns whether a live process was stopped |
| `permission.skill = "allow"` | Emitted by `environment_lifecycle._generate_opencode_config_files`. Without it OpenCode asks, and headless the ask surfaces as the tools-approval flow instead of the skill running |
| Plugin skills | `config["skills"]` is an **object** — `{"paths": [...], "urls": [...]}` with `additionalProperties: false` at both levels — **not** the array the plan specified. `OPENCODE_CONFIG` pins the file, so a wrong shape is not quietly ignored: it can stop the server from starting. Existing `paths` from the base template are merged, never replaced |
| The agent's own skills | Deliberately **not** listed in `config["skills"]` — they reach OpenCode through the `/root/.claude/skills` projection, and listing them twice would double every skill in the index |
| `_OPENCODE_UNSUPPORTED_DIRS` | `("agents", "hooks")` — `skills` left the list |

### `install_plugins` stops the OpenCode servers — but only sometimes

`routes.install_plugins` calls `sdk_manager.stop_opencode_servers()` **gated**,
not unconditionally as the plan had it:

```python
plugins_before = _active_plugin_signature()
results = agent_env_service.install_plugins(...)
...
if installed or failed or _active_plugin_signature() != plugins_before:
    await sdk_manager.stop_opencode_servers()
```

The endpoint is not only the plugin-mutation path: **env start and every tool
approval** reach it through `adapter.set_plugins`, and the container routine is
idempotent — so the result statuses alone would report "nothing happened" for an
uninstall or a per-mode toggle and "something happened" for neither. A restart
costs ~30 s **and** kills any stream in flight, so it fires only when files were
fetched or the active set itself moved.

`_active_plugin_signature()` deliberately excludes `allowed_tools` (which shares
`settings.json` but has no bearing on the OpenCode config), and its whole body is
guarded — `settings.json` lives in the agent-writable workspace, and a hand-edited
entry must not 500 a plugin install through a helper whose only job is deciding
whether to restart a server.

### Prompt-generator fallback

`PromptGenerator(workspace_dir, supports_skills=True)`. When an adapter sets
`SUPPORTS_SKILLS = False`, `_get_agent_skills_section()` appends a
`## Agent Skills` block listing `name — description` plus "read
`skills/<name>/SKILL.md` before using a skill". For both shipped engines it
returns `None` and costs exactly zero tokens.

---

## env-core: `GET /config/skills`

```
SkillsIndexResponse { hash: str, skills: [SkillEntry], errors: [str] }
```

Built by `AgentEnvService.build_skills_index()`:

1. `scan_skills_root(workspace/skills)` → `source="local"`.
2. For every **active plugin in any mode** (deduped by `<marketplace>/<plugin>` —
   `settings.json` carries one row per mode-enabled plugin),
   `scan_skills_root(plugin_dir/skills, source="plugin", plugin_ref=…,
   rel_root=<workspace-relative>)`. Mode is not filtered: the index is what a
   person reads on the agent page, and the engines resolve per-mode enablement
   themselves.
3. `projection_failures()` → stamp `projection_error` on **local** entries that
   are valid on disk but did not land. Plugin skills never travel through the
   projection.
4. Sort by `(name, source, plugin_ref)`, then `apply_budget()` **once over the
   merged list** — the caps are per agent.
5. Flag `shadowed` on every entry whose name appears more than once (on **both**
   rows: which copy an engine loads differs between the two, so the honest report
   is "there are two").

`hash` is the **workspace** `skills/` tree hash and nothing else. Folding plugin
state into it would make every plugin toggle read as a workspace edit; the
backend cache short-circuits on content instead (below).

`errors` carries labels only (`"workspace"`, `"plugins"`, or a `plugin_ref`) —
never exception text.

---

## Backend: `AgentSkillsService`

### Cache columns on `AgentEnvironment`

| Column | Type | Notes |
|--------|------|-------|
| `skills_parsed` | JSON, nullable | list of index-entry dicts |
| `skills_hash` | `String(64)`, nullable | workspace tree hash as reported |
| `skills_fetched_at` | `DateTime(timezone=True)`, nullable | |
| `skills_error` | `String(256)`, nullable | `env_not_running` / `adapter_error` / `parse_error` |

### Behaviour

- **Always attempts the adapter call, classifying only on failure.** The status
  column is not a reliable pre-check: the env-start sweep runs inside
  `_sync_dynamic_data`, *before* the row is stamped `running`, so gating on it up
  front would make the sweep always fail on a container that is demonstrably up.
  `SLEEPING_STATUSES = {"suspended", "stopped", "error"}` deliberately excludes
  transitional statuses — telling a user to "refresh to wake it" instead of
  "rebuild it" sends them to the wrong fix.
- **Write short-circuit on index CONTENT, not on the reported hash.** The hash
  covers only the workspace folder, while the index also carries plugin skills;
  trusting the hash would throw away a freshly installed plugin's skills and, for
  an agent with no local skills (a constant hash), would never show them. An
  unchanged index costs one HTTP round trip and no writes — the common case after
  every stream and every cron run.
- **Own rate-limit bucket.** Module-level `dict[UUID, datetime]`,
  `FORCE_REFRESH_TTL_SECONDS = 30`, independent of the CLI-commands and status
  buckets so a busy agent's status pull cannot starve its skills pull.
- **`AGENT_UPDATED`** (`changed_fields: ["skills"]`) is emitted only when the
  **list** moved — a tree hash that shifted for a touched README inside a skill
  is not worth waking every open browser tab for. Fire-and-forget; a missed
  notification costs one manual refresh.
- **Errors keep the cached rows.** `_persist_error` writes only `skills_error`; a
  suspended environment still has the skills it had.
- **Defensive normalisation.** `_normalise_entries` validates every field (the
  payload comes from a container the agent's own code runs in), drops entries
  without a name, and caps at `MAX_CACHED_SKILLS = 200` — env-core already
  applies the 50-skill budget, so this is the guard against a compromised or
  wildly out-of-date container filling a JSON column.

### Public surface

| Method | Purpose |
|--------|---------|
| `fetch_index(environment, db_session=None)` | Pull + cache. Raises `SkillsIndexUnavailableError` |
| `get_cached(environment)` | Cached dicts, no adapter call |
| `get_cached_entries(environment)` | Cached rows as `SkillEntry` dataclasses (`is_valid` / `is_publishable`) |
| `refresh_after_action(environment, db_session, force=False)` | Best-effort, rate-limited unless `force`. Never raises |
| `force_refresh(environment, agent, db_session)` | Wakes a suspended env via `wake_suspended_environment`, then pulls. Falls back to cached rows |
| `find_cached_skill(environment, name)` | Local wins over plugin when a name is shadowed (the index sorts `local` first) |
| `read_skill_content(environment, name)` | `(path, text, truncated)`. Path comes from the **cached index**, never from `name` — that is what keeps this from being a workspace file-read endpoint wearing a skill's name. Cap `MAX_CONTENT_BYTES = 256 KB`, decoded with `errors="replace"` |
| `handle_post_action_event(event_data)` | Registered event handler |

### Event registration

`app/main.py` maps registry key `"skills"` →
`AgentSkillsService.handle_post_action_event` in `_PULL_ONLY_HANDLERS`, and the
loop over `pull_only_files()` subscribes it to the same event set every pull-only
cache uses (`ENVIRONMENT_ACTIVATED`, `STREAM_COMPLETED` / `STREAM_ERROR`, the
`CRON_*` family, `WORKSPACE_FILES_CHANGED`).

A `WORKSPACE_FILES_CHANGED` whose `changed_files` names `skills/` sets
`force=True` — direct evidence, not a guess. Every other trigger is speculative
and respects the rate limit.

`EnvironmentLifecycleManager` calls `refresh_after_action(force=True)` in the
env-start sweep, beside the STATUS.md and CLI_COMMANDS.yaml pulls.

### Synced Workspace File Registry — the first directory entry

```python
SyncedFile("skills", "skills/", "pull_only")
```

A **trailing slash** marks a directory entry. env-core's `_WATCHED_FILES` gains
the same string and `_read_mtimes` watches `skill_manifest.tree_hash(path)`
instead of one file's mtime.

Unlike a missing *file* (which is omitted from the snapshot), a missing
*directory* is still recorded — `tree_hash` of an absent root is the empty
digest, the same value an empty directory hashes to. That is what makes
**deleting the whole `skills/` folder** a change the backend hears about;
omitting it would leave the cache advertising skills that no longer exist.
Creating an empty folder still fires nothing, because nothing about the index
moved.

`DockerEnvironmentAdapter.get_skills_index()` (declared abstract on
`EnvironmentAdapter`) GETs `/config/skills` with a 15 s timeout. A pre-feature
container answers 404 → exception → `adapter_error`.

---

## Read routes — `backend/app/api/routes/agent_skills.py`

Registered on the `agents` tag (so they land on `AgentsService` in the generated
client), before `agents.router`.

| Method + path | Behaviour |
|---------------|-----------|
| `GET /agents/{agent_id}/skills` | Cache-only — safe to poll, never wakes a container |
| `POST /agents/{agent_id}/skills/refresh` | Wakes a suspended env, re-reads, returns the same shape. **Never fails** on an unreachable environment: the body carries the cached rows plus an `error` code. Bypasses the 30 s limit |
| `GET /agents/{agent_id}/skills/{name}/content` | `SkillContentPublic`. 503 when the env is unreachable. Two distinct 404s: "Skill not found" versus "SKILL.md not found — refresh the skills list" when the index still lists it but the file is gone |

Access is `AgentService.user_can_access` (superusers bypass), so the read gate
and the capability reply cannot disagree about who the agent belongs to.

### Response models — `backend/app/models/agents/agent_skills.py`

`SkillIssuePublic`, `SkillEntryPublic`, `AgentSkillsPublic`,
`SkillContentPublic`, all re-exported from `app.models`.

`AgentSkillsPublic.can_publish` = `AgentService.can_build(session, user, agent)`
— the developer role **and** not a foreign install.
`SkillEntryPublic.can_publish` = that **and** `source == "local"` **and**
`entry.is_publishable`.

---

## `/skills` command and popup entries

`SkillsCommandHandler` (`streams=False`, `include_in_llm_context=True`,
`requires_running_environment=False`) renders the cached index as a markdown
table `Skill | Source | What it does | Invoke`. Flagged rows sort last — the
table is read top-down for "what can this agent do", and a broken skill is not an
answer to that. Values pass through `_cell()`, which escapes `|` and newlines: a
publisher-authored description with a pipe would otherwise shift every later
column. Empty index + `skills_error == adapter_error` produces the rebuild copy
(an error); an ordinary empty index is **not** an error.

`SessionCommandPublic` gains `kind: str = "command"`.
`CommandService.list_for_session` appends `/<skill name>` entries with
`kind="skill"` for cached local + plugin skills that are `is_valid` **and**
`user_invocable`, deduped by name (typing a name can only mean one thing to the
user). These are **not** registered handlers: `is_command()` does not match them,
so the text goes to the model. The reserved-name check in `skill_manifest` is
what keeps a skill from shadowing a real command here.

---

## Bundles

### `skills_summary`

| Where | Shape |
|-------|-------|
| `AgentBundleRevision.skills_summary` | JSON, **nullable**. `NULL` = the revision predates the feature; `[]` = published from an agent with none |
| `AgentBundleRevisionPublic.skills_summary` | `list \| None` |
| `CatalogEntryPublic.skills` | `list \| None`, from the latest revision |
| Manifest key `skills_summary` | Written by `RevisionFormat.build_manifest`; read back with `manifest.get("skills_summary")` and **no `or []`** — collapsing absent into empty would tell a consumer that an old bundle definitely ships no skills, which is not known |

`PublishService._collect_skills_summary(env_workspace_root)` returns
`[{name, description, has_scripts}]` for valid entries, derived from the
publisher's **live workspace** — the same tree the snapshot is about to copy.
`GitSourceService._build_live_manifest` calls the same helper so
`cinna.agent.json` and `manifest.json` stay one schema.

### Publish pre-flight

`PublishService._ensure_publisher_skills_publishable(env_workspace_root)` runs
**before anything is written** (a revision is immutable) and raises `ValueError`
→ **400** naming the offender, on either:

- a skill carrying an `error` whose code is **not** `budget`, or
- any `secret_paths` hit inside `skills/`.

See [agent_skills.md § Bundle publish blocks](agent_skills.md#bundle-publish-blocks--and-what-it-does-not-block)
for why `budget` is excepted and why this is a 400 rather than the plan's coded
422.

### `plugin_sync._resolve_link_identity`

The branch test flipped from `link.source == PluginSource.bundle` to
`link.source != PluginSource.marketplace`. A `catalog` link also carries
`plugin_id = NULL`, so an is-bundle test would send it down the marketplace
branch, resolve to `(None, None, None)` and make
`_ensure_publisher_plugin_files` hard-block bundle publish for **every** agent
that has a catalog skill installed.

### `workspace_classification`

`.claude` joined `RUNTIME_NAME_DENYLIST`. Consequences, all automatic:

- `is_bundle_owned_toplevel(".claude")` and `is_env_migration_toplevel(".claude")`
  are now `False` — dropped from bundle publish, install seed, apply-update and
  env migration.
- `RevisionFormat.generate_gitignore()` emits `workspace/.claude`.
- `workspace_copy`'s apply-update stale-prune sweep **skips** denylisted names,
  so an existing workspace-level `.claude/` is left alone; it simply never
  travels again.

---

## Skills catalog

### Tables

**`skill_package`** — `backend/app/models/skills/skill_package.py`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `package_id` | `String(255)` | reverse-DNS, unique (`uq_skill_package_package_id`) |
| `name` | `String(64)` | the skill folder name; unique with publisher (`uq_skill_package_publisher_name`) |
| `display_name` | `String(255)` | |
| `description` | Text, nullable | from frontmatter at publish, then publisher-editable |
| `publisher_user_id` | UUID FK `user.id` | `ON DELETE SET NULL` — ownerless, not deleted |
| `source_agent_id` | UUID FK `agent.id` | `ON DELETE SET NULL`, provenance only |
| `latest_revision_id` | UUID FK `skill_package_revision.id` | `ON DELETE SET NULL` |
| `visibility` | `String(16)` | `private` \| `public` \| `users`; default `private`; indexed. `users` was added by [agent_addons](../agent_addons/agent_addons_tech.md) — no DDL, the level set is enforced by `_normalise_visibility` (422 `invalid_visibility`) |
| `is_listed` | bool | default `true` |
| `created_at`, `updated_at` | `DateTime` (naive) | |

`CATALOG_MARKETPLACE_NAME = "cinna-skills"` lives here — a constant rather than a
literal because the dedupe rule, the manifest builder and the container installer
must all agree on it.

**There is deliberately no `install_count` column.** The plan sketched one
"maintained by the install service", but an install is an `AgentPluginLink` row
that disappears with its agent through `ON DELETE CASCADE`, which a stored
counter cannot observe — it would drift upward forever. The count is derived in
`SkillCatalogService`, the same way `CatalogService._bundle_to_entry` counts
bundle installs.

**`skill_package_revision`** — append-only, immutable.

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `package_id` | UUID FK `skill_package.id` | `ON DELETE CASCADE`, indexed |
| `revision_number` | int | unique with `package_id`; allocated under the publish lock, never reused |
| `version` | `String(64)`, nullable | publisher label, independent of `revision_number` |
| `frontmatter` | JSON, not null, default `'{}'` | parsed `SKILL.md` frontmatter as published |
| `snapshot_path` | `String(1024)` | absolute path of the snapshot's `skills/<name>/` |
| `content_hash` | `String(64)` | `PublishService.hash_workspace_tree` over the snapshot root |
| `archive_sha256` | `String(64)`, nullable | digest of the **gzipped tarball** — different bytes, hence a second column |
| `size_bytes` | int | |
| `release_notes` | Text, nullable | |
| `published_by_user_id` | UUID FK `user.id` | `ON DELETE SET NULL` |
| `published_at` | `DateTime` (naive) | |

**Timestamps are naive `DateTime`**, matching the sibling `agent_bundle` /
`agent_bundle_revision` tables rather than the plan's TIMESTAMPTZ.

`archive_sha256` is stored rather than computed on demand because the plugin
manifest carries it, and that manifest is built on the asyncio event loop from
environment activation and every allowed-tools sync — reading or gzipping a
snapshot there would stall session streaming. It is nullable only as a guard: no
live path reaches NULL, and `archive_sha256()` logs at ERROR when it does, so a
future backfill or restore degrades to `catalog_revision_missing` rather than
raising mid-manifest-build.

**`agent_plugin_link`** gains `skill_package_revision_id` (UUID FK,
`ON DELETE SET NULL`, indexed) and `PluginSource` gains `catalog`.
`has_update` = `package.latest_revision_id != link.skill_package_revision_id`.

### Storage layout

```
<SKILL_STORAGE_DIR>/<package uuid>/<revision_number>/skills/<name>/…
<SKILL_STORAGE_DIR>/<package uuid>/<revision_number>.tar.gz     # archive cache
```

`settings.SKILL_STORAGE_DIR` defaults to `/app/data/skills`.
`.env.example` documents `HOST_SKILL_STORAGE_DIR=./backend/data/skills`, and
`docker-compose.yml` mounts
`${HOST_SKILL_STORAGE_DIR:-./backend/data/skills}:/app/data/skills` on the
backend.

> **Operator action on upgrade.** `/app/data` was previously **unmounted** on the
> backend container (only `/app/data/agents`, `/app/data/app-data` and
> `/app/data/bundles` were). Without this new mount every published snapshot and
> archive is lost on the next container recreate, and with it the content behind
> every catalog install. `backend/data/skills/` is gitignored. <!-- nocheck -->

### `SkillCatalogService`

**Publish** — `publish_from_agent(session, *, agent, user, skill_name, version,
release_notes, visibility, package_id)`:

1. `AgentService.assert_can_build` → `CanBuildError` mapped to
   `not_developer` / `foreign_install`.
2. `_resolve_skill_dir` — reads the **host-side** workspace at
   `<ENV_INSTANCES_DIR>/<env id>/<WORKSPACE_ROOT_REL>/skills/<name>`. **A
   suspended or stopped environment publishes normally**; waking it would cost
   30–120 s and change nothing about the bytes. Three distinguishable 4xx:
   `no_environment` (409), `workspace_unavailable` (409), `skill_not_found`
   (404). `SKILL_NAME_RE` guards the path join as much as it validates the name.
3. One `parse_skill_dir`, three content refusals: `skill_invalid` /
   `skill_contains_secrets` (with paths) / `skill_too_large` — all 422. A fourth,
   **409 `grants_require_users_visibility`**, fires inside the publish lock when
   the call carries resolved `grant_emails` while the *effective* visibility
   (requested, else the package's current, else `private`) is not `users` — see
   [agent_addons_tech](../agent_addons/agent_addons_tech.md#visibility-and-grants).
   All four run **before** anything is written.
4. Under a per-`(publisher, skill name)` `asyncio.Lock` (locking on the package
   uuid would leave the create path — the one that races into a unique-constraint
   violation — unguarded): resolve or create the package, allocate
   `revision_number`, write the snapshot off the event loop, insert the revision,
   point the package at it.

`_write_snapshot_to_disk` fills `<rev>.tmp/skills/<name>/` via `safe_copytree`
(symlinks refused at every depth) and moves it into place, then builds and caches
the archive **in the same thread** so its digest can be stored on the row.

`_apply_publish_to_package` carries the frontmatter description over **only while
the stored value still equals what the previous revision declared** — §5.3's
literal "updates description" would silently overwrite a publisher's edited
catalog blurb on their next publish.

`default_package_id(user_id, name)` = `<reversed host>.<8-hex owner slug>.<name>`,
mirroring `BundleIdService.generate_bundle_id` so the two id families read as one
namespace. Validated against `BUNDLE_ID_REGEX`.

**Read** — `list_catalog` returns public+listed packages plus the caller's own,
excluding packages with no revision yet (a publish that failed between creating
the row and committing its first revision), newest-updated first. Filtering
(all / public / mine / installed) is client-side, as the bundle catalog does.
`_entry_context` resolves revisions, publishers, install counts and
"installed in my agents" in **four grouped queries** rather than 4N+2, and
`package_to_entry` refuses a context built for a different viewer —
`installed_in_agent_ids` is per viewer.

`user_can_see`: publisher always; public+listed to anyone; public+delisted to a
superuser. That bypass is keyed on **visibility, not listing**, so `delist` is
not a trapdoor whose own output 404s for the admin who produced it.
`user_can_manage`: publisher only. `delist`: superuser only, never a delete.

**Install** — `install_into_agent` verifies the revision belongs to the package,
then dedupes service-side on `(agent_id, "cinna-skills", package.name)` (the
table's unique index only covers `plugin_id`, and Postgres treats NULLs as
distinct). Two distinct refusals: `already_installed` when it is the same
package, `name_conflict` when a **different** publisher's package already
occupies that skill name in this agent — one agent has a single
`plugins/cinna-skills/<name>/` directory, so the two genuinely cannot coexist.

**Upgrade** — `upgrade_link` re-pins to `package.latest_revision_id`; idempotent.
**Uninstall** is deliberately not a verb here: it is
`DELETE /llm-plugins/agents/{agent_id}/plugins/{link_id}` like every other plugin.

**Archive** — `build_archive(revision) -> (bytes, sha256)`. The tarball holds the
snapshot verbatim under `skills/<name>/` and **not** the
`.claude-plugin/plugin.json` §8 mentioned: that file is package metadata a
publisher can edit without cutting a new revision, so baking it into an immutable
artifact would freeze stale text. The container synthesises it instead (§5.3's
own instruction — §8 contradicted it).

The archive is **deterministic**: sorted members, `mtime = 0`, `uid/gid = 0`,
blank `uname/gname`, `REGTYPE` only, gzip `mtime=0`, and mode collapsed to
`0o755`/`0o644` by the source's executable bit. Preserving that bit matters (a
skill's `scripts/run.sh` is meant to run); collapsing to two values keeps the
tarball reproducible and drops setuid/setgid/sticky. So a cache rebuilt after an
eviction still matches the stored `archive_sha256`.

`env_may_download(session, agent_id, revision)` is the archive route's whole
authorisation: does the calling environment's agent hold a catalog link for
**this** revision? Package visibility does not enter into it — flipping a package
private must not break agents that already installed it.

### Coded failures

`SkillCatalogError(code, message, paths=[])` +
`STATUS_BY_CODE` + `http_error_for(exc)`. The mapper lives beside the map rather
than in a router because **two** routers raise these (the skills routes and the
shared plugin-upgrade route), and one refusal must not answer two different ways.
The detail body is `{code, message, paths?}` so a dialog can branch without
parsing prose.

| Code | Status | Code | Status |
|------|--------|------|--------|
| `not_accessible`, `package_not_found`, `revision_not_found`, `skill_not_found` | 404 | `skill_invalid`, `skill_contains_secrets`, `skill_too_large`, `package_id_invalid`, `invalid_visibility`, `invalid_display_name` | 422 |
| `not_developer`, `foreign_install`, `not_publisher`, `not_superuser` | 403 | `package_id_taken`, `package_id_immutable`, `already_installed`, `name_conflict`, `no_revision` | 409 |
| `no_environment`, `workspace_unavailable` | 409 | `snapshot_missing` | **410** |
| `not_a_catalog_link` | 400 | `archive_unavailable` | 503 |

`snapshot_missing` is 410 rather than 503 on purpose: a revision is immutable, so
files that are not there will not reappear on a retry, and "try again later"
would send the caller down the wrong path.

### Routes — `backend/app/api/routes/skills.py`

Two routers, one `skills` tag → one `SkillsService` in the generated client.

| Method + path | Deps | Purpose |
|---------------|------|---------|
| `GET /skills/catalog` | `CurrentUser` | `SkillPackagesPublic` — unfiltered |
| `GET /skills/packages/{package_id}` | `CurrentUser` | `SkillPackageDetailPublic` (+ full revision history) |
| `PATCH /skills/packages/{package_id}` | publisher | `display_name` / `description` / `visibility` / `is_listed` |
| `POST /skills/packages/{package_id}/delist` | superuser | hide, never delete |
| `GET /skills/packages/{package_id}/revisions/{n}/content` | `CurrentUser` + visibility | `SKILL.md` preview |
| `GET /skills/packages/{package_id}/revisions/{n}/archive` | `AgentEnvContextDep` | tarball; `X-Content-SHA256` header. 403 (not 404) when the env authenticated fine but holds no install of that revision |
| `POST /agents/{agent_id}/skills/{name}/publish` | owner + developer gate | `SkillPackageRevisionPublic` |
| `POST /agents/{agent_id}/skills/install` | owner | creates the link, then `LLMPluginService.sync_plugins_to_agent_environments(message_prefix="Skill added.")` → `PluginSyncResponse` |
| `POST /agents/{agent_id}/plugins/{link_id}/upgrade` | existing route | now maps `SkillCatalogError` through `http_error_for` |
| `GET`/`POST` `/skills/packages/{package_id}/grants`, `DELETE .../grants/{user_id}` | publisher (`_get_managed_package`; **no superuser bypass**) | `users`-visibility allowlist — see [agent_addons_tech](../agent_addons/agent_addons_tech.md) |

`POST /skills/packages/{package_id}/delist` was hardened when the grants landed:
it now checks `is_superuser` **before** loading the package and then loads without
a visibility check. It previously loaded through the visibility-gated path, which
was both an existence oracle and the reason a `users` package could not be
delisted.

`SkillPublishRequest` gained `grant_emails: list[str]` (max 50) and
`SkillPackageEntry` gained `is_granted`. A publish carrying grant targets is
refused (**409 `grants_require_users_visibility`**) unless it leaves the package
on `users`; the standalone grant routes above stay deliberately permissive, so
granting ahead of a visibility flip still works.

The three agent-scoped rows resolve the agent through
`LLMPluginService.verify_agent_access`, shared with the `llm-plugins` router.
A caller who is not the owner (or a superuser) gets **404 `Agent not found`** —
the same answer as an id that does not exist, so a guessed id cannot be used to
discover that somebody else's agent is real. That is `assert_can_build`'s
`not_accessible` rule applied at the ownership check itself, which runs first
and would otherwise have made the later 404 unreachable.

Wire schemas: `backend/app/models/skills/schemas.py` — `SkillPackagePublic`,
`SkillPackageEntry` (adds `install_count`, `installed_in_agent_ids`,
`can_manage`), `SkillPackagesPublic`, `SkillPackageDetailPublic`,
`SkillPackageRevisionPublic`, `SkillPackageUpdate`, `SkillPublishRequest`,
`SkillInstallRequest`, `SkillRevisionContentPublic`. All re-exported from
`app.models`.

### Plugin manifest and the container installer

`LLMPluginService._build_catalog_entry` emits:

```json
{
  "marketplace_name": "cinna-skills",
  "plugin_name": "<package name>",
  "source": "catalog",
  "git": null,
  "archive": {
    "url": "<AGENT_ENV_BACKEND_URL>/api/v1/skills/packages/<uuid>/revisions/<n>/archive",
    "sha256": "…",
    "ref": "<revision id>",
    "description": "…"
  },
  "conversation_mode": true, "building_mode": true,
  "disabled": false, "version": "…", "commit_hash": null
}
```

`AGENT_ENV_BACKEND_URL` because the consumer is the container, not a browser. An
orphaned link still emits an entry with `archive: null`, so the container reports
`catalog_revision_missing` as a per-plugin failure rather than the skill
silently vanishing.

`AgentEnvService._ensure_catalog_plugin(plugin_dir, entry)`:

1. Marker short-circuit — `.cinna_plugin_ref == ref` → **`skipped`**, but the
   `.claude-plugin/plugin.json` is **rewritten anyway**. It is synthesised from
   editable package metadata, and a publisher renaming their package cuts no new
   revision, so the marker would never move and the rename would never reach
   installed containers. Cheap to rewrite; skip the download, not the metadata.
2. Streamed download with this env's own token
   (`Authorization: Bearer AGENT_AUTH_TOKEN` + `X-Agent-Env-Id`),
   `follow_redirects=False`, 120 s. Streamed so the cap bounds **memory**, not
   just extraction.
3. sha256 verify — a mismatch never extracts.
4. `_safe_extract_tar` into a staging dir: rejects absolute paths, `..`,
   symlinks and hard links, devices/FIFOs, anything not a regular file, and a
   total expanded size over the cap. A rejected member fails the **whole**
   archive. Modes are forced to `0o755`/`0o644` by the member's executable bit.
5. Write `plugin.json`, move staging into place, write the marker.

Failures are returned, never raised — one unfetchable skill must not stop the
other plugins in the manifest.

**The two caps are deliberately unequal:**

| Cap | Value | Measures |
|-----|-------|----------|
| `MAX_PACKAGE_BYTES` (publish) | 16 MiB (`DEFAULT_MAX_TOTAL_BYTES`) | the **uncompressed** tree |
| `_CATALOG_ARCHIVE_MAX_BYTES` (download) | 24 MiB | the **gzipped archive** |
| `_CATALOG_EXTRACT_MAX_BYTES` | 64 MiB | the expanded tree |

A skill of already-compressed assets (PDFs, PNGs) gzips to roughly its own size
plus tar headers, so equal numbers would make a 16 MiB skill publishable and then
uninstallable — a failure discovered by the consumer, at every install, unfixable
without a re-publish. The headroom keeps the refusal in front of the publisher.

*(Note: the download-cap failure message currently says "larger than the 16 MB
limit" while the constant is 24 MiB — cosmetic, and the refusal itself is
correct.)*

### `AgentPluginLinkWithUpdateInfo` projection

The display fields are projections, not columns, and each source must fill all of
them or the row renders nameless:

- `marketplace` — live plugin + marketplace rows; `has_update` compares commit
  hashes.
- `bundle` — frozen `snapshot_*`; never has an update of its own.
- `catalog` — the `SkillPackage` behind the pinned revision: `display_name` /
  `name`, its `description`, category `"skill"`, `latest_version` from the
  package's latest revision, `has_update` from the revision id, plus a new
  `skill_package_id` so the Plugins tab can link a row to its catalog page
  without a second lookup.

`LLMPluginService.upgrade_agent_plugin` delegates `source=catalog` to
`SkillCatalogService.upgrade_link` and lets `SkillCatalogError` propagate —
collapsing it to `None` would answer "Plugin link not found" for a link that
plainly exists.

---

## Database Migrations

| Revision | Down-revision | Changes |
|----------|---------------|---------|
| `08e5661b36ed` `add_agent_environment_skills_cache` | `45938a69aee7` | `agent_environment`: `skills_parsed` JSON, `skills_hash` VARCHAR(64), `skills_fetched_at`, `skills_error` VARCHAR(256) — all nullable |
| `2affe6c57bf7` `add_bundle_revision_skills_summary` | `08e5661b36ed` | `agent_bundle_revision.skills_summary` JSON nullable, **no backfill** (NULL = predates the feature) |
| `c24bd7ff8728` `add_skill_package_tables` | `2affe6c57bf7` | `skill_package` (+ `ix_skill_package_publisher`, `ix_skill_package_visibility`) and `skill_package_revision` (+ `ix_skill_package_revision_package`); `fk_skill_package_latest_revision` added **after** both tables exist |
| `b71f4a9c2d30` `add_agent_plugin_link_catalog_source` | `c24bd7ff8728` | `agent_plugin_link.skill_package_revision_id` + index + FK `ON DELETE SET NULL`. `source` stays VARCHAR (the `catalog` enum value is app-level). Downgrade requires rewriting `source='catalog'` rows first — noted in the migration docstring |

---

## Frontend

### Phase 2 (implemented)

> **Superseded by [agent_addons](../agent_addons/agent_addons_tech.md).** The four
> skills-only components below were **deleted** when the Addons tab replaced the
> Configuration tab's Skills card: `AgentSkillsCard.tsx`, `SkillRow.tsx`,
> `AllSkillsSheet.tsx` and `SkillContentDialog.tsx`. Their behaviour lives on in
> `components/Agents/Addons/` (`AddonsCard`, `AddonRow`, `AllAddonsSheet`,
> `AddonDetailDialog`) and in the shared `Agents/SkillContentBody.tsx`. The rows
> are kept here because the *rules* they encode still hold and are the reason the
> replacements are shaped the way they are. `utils/skills.ts` survives unchanged.

| File | Role |
|------|------|
| ~~`Agents/AgentSkillsCard.tsx`~~ → `Agents/Addons/AddonsCard.tsx` | Query key was `["agent", agentId, "skills"]`, now `["agent", agentId, "addons"]`; Refresh mutation. A 200 with `result.error` toasts a **failure** — the route never fails on an unreachable env, so "Skills refreshed" over an error banner would say the opposite of the truth |
| ~~`Agents/SkillRow.tsx`~~ → `Agents/Addons/AddonRow.tsx` | `ListRow` + `RowFlag` + `RowInfo`; owns its dialog, mounted only while open. `AddonRow` deliberately does **not** reuse `SkillRow`: no `meta` line, a different status precedence, a different action budget |
| ~~`Agents/AllSkillsSheet.tsx`~~ → `Agents/Addons/AllAddonsSheet.tsx` | "Show all (N)" |
| ~~`Agents/SkillContentDialog.tsx`~~ → `Agents/SkillContentBody.tsx` | `SKILL.md` viewer, now a body rendered inside `AddonDetailDialog` rather than a second dialog |
| `frontend/src/utils/skills.ts` | `skillsIndexErrorCopy`, `skillRowStatus`, `formatSkillSize`, `sortSkills`, `skillKey` — shared so the card, the row and the sheet cannot drift on sort order or on what a dot means. Still live: `utils/addons.ts` builds on it rather than duplicating it |
| `frontend/src/components/Agents/AgentConfigTab.tsx` | Hosted the card until the Addons tab took it. Was deliberately **not** gated on `showOperationalSettings` or `readOnly`, which is why `"addons"` had to join the `agentUserTabs` set in the same change |
| `frontend/src/components/Chat/SlashCommandPopup.tsx` | `kind === "skill"` renders a `h-5` outline `Badge`, visible text inside the `role="option"` row (so it is part of the accessible name and needs no `sr-only` twin) |
| `frontend/src/components/Catalog/CatalogCard.tsx` | One muted `GraduationCap` line, ≤3 names + `+N more`. `skills` is typed `Array<unknown> \| null` (there is no `SkillSummaryPublic` to import), narrowed defensively |

`skillKey` is `${source}:${plugin_ref}:${name}` — the name alone is not unique
when a local and a plugin skill share one, and two rows keyed the same would make
React reuse one row's dialog state for the other.

### Phase 3 (specification — code still landing)

The Phase-3 surfaces shipped. Two of the rows below were then **superseded by
[agent_addons](../agent_addons/agent_addons_tech.md)**: S9's
`Agents/PublishSkillDialog.tsx` became `Agents/Addons/ShareSkillDialog.tsx` (plus
`ShareSkillSuccessPanel`, and the `users` visibility with its people picker), and
S11's `Agents/InstalledPluginRow.tsx` / `AllInstalledPluginsSheet.tsx` were
deleted into `Addons/AddonRow.tsx` / `AllAddonsSheet.tsx`. The package detail
route (S7) additionally gained `Catalog/SkillPackageAccessCard.tsx` and its
grant row / dialog / sheet:

| # | Surface | New files (per the spec) |
|---|---------|--------------------------|
| S6 | `/catalog/skills` route + `CatalogSectionTabs` (Agents · Skills pills on both catalog routes, `AppSidebar` `isActive` widened to `startsWith("/catalog")`) | `routes/_layout/catalog/skills.tsx`, `Catalog/SkillCatalogCard.tsx`, `SkillCatalogGrid.tsx`, `SkillCatalogFilters.tsx`, `CatalogSectionTabs.tsx` |
| S7 | Package detail route — Package card, `SKILL.md` card, Revisions `PreviewList` | `routes/_layout/catalog/skills/$packageId.tsx`, `Catalog/SkillPackageCard.tsx`, `SkillRevisionRow.tsx`, `AllSkillRevisionsSheet.tsx`, `SkillSource.tsx` |
| S8 | Edit package details dialog | (in the detail route) |
| S9 | Publish skill dialog, opened from the S1 row `⋯` only | `Agents/PublishSkillDialog.tsx` |
| S10 | Add-skill-to-agent dialog (`SearchableSelect`, never a nested picker dialog) | `Catalog/AddSkillToAgentDialog.tsx` |
| S11 | Installed Plugins card rebuilt on `ListRow` / `PreviewList` / `RowActionsMenu` (A10 fix-on-touch) | `Agents/InstalledPluginRow.tsx`, `AllInstalledPluginsSheet.tsx` |

Query keys: `["skills-catalog", filter]`, `["skill-package", packageId]`;
install/upgrade also invalidate `["agent", agentId, "plugins"]`.

**All eight of the spec's "open questions and contract gaps" were closed by the
backend as built** — `can_publish` (gap 1), structured `secret_paths` (gap 2),
`{code, message}` issues (gap 3), the catalog link projection (gap 4), suspended
publish answering coded 409s (gap 6), and the five revision fields (gap 8). Gap 5
(the route's navigation) and gap 7 (ids, not names) are frontend concerns the
spec itself decided.

Regenerate the client after each backend phase:
`source ./backend/.venv/bin/activate && make gen-client`.

---

## Local Agent Kit — contract 1.1.0

| File | Change |
|------|--------|
| `docs/local_agent_kit/CONTRACT_VERSION`, `kit.json`, `layout.json` | `1.0.0` → `1.1.0` (all three must agree, or both contract representations 503) |
| `docs/local_agent_kit/layout.json` | New `agent` member `{path: "skills", kind: "directory", role: "skills", survives_update: true}`; the `docs` role text drops "one doc per local skill". `cloud_import_excludes` unchanged — `skills/` travels |
| `docs/local_agent_kit/templates/agent/skills/README.md` | New. Layout, the two validated fields, reserved names, caps, "what goes where" |
| `docs/local_agent_kit/templates/agent/AGENTS.md` | New numbered rule 3: when the workflow prompt names a skill, read `skills/<name>/SKILL.md` and follow it. `skills/` added to the never-write-as-the-agent list |
| `docs/local_agent_kit/guides/08-knowledge-and-local-skills.md` | Rewritten to the folder convention; states plainly that local progressive disclosure is instruction, not machinery; documents `docs/skill_*.md` as legacy-but-still-imported with a five-step per-capability migration |
| `docs/local_agent_kit/CHANGELOG.md` | `1.1.0 — skills are folders` (additive; majors match, so a 1.0.0 tool still operates a 1.1.0 folder) |
| `docs/local_agent_kit/tools/kit.py` | `_rungs_present`: the "Knowledge & local skills" rung is now satisfied by authored files under `knowledge/` **or** `skills/`, excluding each folder's scaffolded `README.md` |

The contract tarball picks up `templates/agent/skills/` automatically. The kit
tree is mirrored into
`backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit/`
by `make sync-platform-knowledge`.

---

## Testing

| File | Covers |
|------|--------|
| `backend/tests/unit/test_skill_manifest.py` | Byte-identity of the two parser copies; every validation rule; warning precedence; `is_publishable`; issue round-trip and the bare-code legacy shape; caps (overflow, invalid entries charging nothing, re-application as a fixed point, merged == first-N); `tree_hash` stability; the secret predicate; every registered command being reserved; **a plain file at the skills root being skipped** |
| `backend/tests/unit/test_skills_projection_guards.py` | Symlinked skill refused, symlink inside a skill not followed, foreign directory untouched, prune, corrupt state self-heal, wiped projection rebuilt despite a matching hash, failure never raising |
| `backend/tests/unit/test_skills_engine_enablement.py` | `Skill` in `pre_allowed_tools`; per-mode `skills_changed` latching (told once per mode, durable failure reported once, a recovered skill reported even though the tree did not change, an empty identity asking for no rebuild); OpenCode `skills.paths`; the agent's own skills not double-listed |
| `backend/tests/unit/test_skills_index.py` | The merged index: plugin dedupe, `shadowed` on both rows, per-agent caps, `projection_error` |
| `backend/tests/unit/test_agent_skills_service.py` | Cache short-circuit, error classification, rate limit, `AGENT_UPDATED` on list change only, content read |
| `backend/tests/unit/test_publish_skills_gate.py` | Bundle publish hard-blocks; `budget` not a blocker |
| `backend/tests/unit/test_skills_bundle_workspace.py`, `test_revision_marshaller.py`, `test_revision_format.py` | `skills_summary` in/out, `None` vs `[]` |
| `backend/tests/unit/test_skill_catalog_archive.py` | Deterministic archive, digest stability, safe extract |
| `backend/tests/unit/test_workspace_classification.py` | `.claude` denylisted |
| `backend/tests/api/agents/core/agents_skills_routes_test.py` | The three read routes |
| `backend/tests/api/agents/commands/agents_skills_command_test.py` | `/skills` |
| `backend/tests/api/agents/bundles/agents_bundles_skills_test.py` | Publish gate end to end |

---

## Rollout and Unverified Items

- **Existing environments need `/rebuild-env`** (or the admin bulk rebuild):
  env-core ships in the per-environment `/app/core` copy, so a pre-feature
  container has no projection and no `/config/skills`, and its cache records
  `adapter_error`.
- **Operators need the new `/app/data/skills` compose mount** before Phase 3
  publishes anything (see [Storage layout](#storage-layout)).
- **The OpenCode build is not pinned.** All three env Dockerfiles install it with
  `RUN curl -fsSL https://opencode.ai/install | bash` at image-build time, so
  there is no version to record for plan §13's checklist item. Support for
  `POST /instance/dispose` and for the `skills` config object is therefore
  **assumed and degradation-handled**, not verified against a pinned build: a
  build without the endpoint answers 404 (logged once, skills appear at the next
  server start), and the `skills` object shape is written to match OpenCode's
  published closed schema. Anyone who needs certainty should pin the installer
  and record the version here.
- **`${CLAUDE_SKILL_DIR}` is taught in `BUILDING_AGENT.md` but is a Claude Code
  variable.** Whether OpenCode exports an equivalent is **unconfirmed**. A skill
  authored with it may not resolve its own `scripts/` path under OpenCode; a
  workspace-relative path (`skills/<name>/scripts/…`) is the portable form, and
  is what the Local Agent Kit's own templates use.
