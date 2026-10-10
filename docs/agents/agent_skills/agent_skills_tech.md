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
| Read routes | `backend/app/api/routes/agent_skills.py` | `GET/POST /agents/{id}/skills*`, `GET .../skills/{name}/content`, `GET .../skills/{name}/files` |
| Catalog models | `backend/app/models/skills/{skill_package,skill_package_revision,schemas}.py` | Tables + wire shapes |
| Catalog service | `backend/app/services/skills/skill_catalog_service.py` | Publish, browse, manage, install, upgrade, archive |
| Coded failures | `backend/app/services/skills/exceptions.py` | `SkillCatalogError` + `STATUS_BY_CODE` + `http_error_for` |
| Catalog routes | `backend/app/api/routes/skills.py` | `/skills/...` and the agent-scoped publish/install verbs |
| Slash command | `backend/app/services/agents/commands/skills_command.py` | `/skills` document |
| Wake helper | `backend/app/services/agents/environment_resolver.py::wake_suspended_environment` | Shared by the status and skills refresh buttons and the `SKILL.md` viewer. Resolves its adapter through `EnvironmentService.get_lifecycle_manager()` — the shared singleton every other caller uses — rather than a fresh `EnvironmentLifecycleManager()` |

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
sequences (`- item`) **whose items may be flat mappings** (`- slot: x` plus
indented continuation lines — this is how `credentials:` declares one entry per
slot); block scalars (`|`, `>`, and the `-`/`+` variants); one level of nested
mapping; a closing fence of `---` or `...`. Scalar coercion
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
MAX_SKILL_CREDENTIALS  = 20                 # declared slots per skill
MAX_SLOT_LENGTH        = 255                # a slot is a Credential.service_uri
SKILL_CREDENTIAL_SLOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")
SKILL_CREDENTIAL_TYPES = frozenset({...})   # mirror of CredentialType, minus mcp_provider
RESERVED_SKILL_NAMES   = frozenset({...})   # mirror of the command registry
SKIP_DIR_NAMES         = {".git", ".venv", "__pycache__", ".mypy_cache",
                          ".ruff_cache", "node_modules"}
```

`RESERVED_SKILL_NAMES` is a vendored mirror of
`backend/app/services/agents/commands/__init__.py` (env-core cannot import the
registry). `test_skill_manifest.py::test_every_registered_platform_command_is_reserved`
guards the drift.

`PLATFORM_SKILL_NAMES = frozenset({"cinna-tasks"})` and `PLATFORM_SKILL_SOURCE =
"platform"` (both copies) reserve the platform skills' names: a skill parsed
with any other `source` that takes one fails with `reserved_name` ("This name is
reserved by a platform skill."). Only a skill scanned with
`source="platform"` may carry it. Extra frontmatter keys such as `when_to_use`,
`compatibility` and a `metadata` map are kept verbatim, like every unknown key.

`SKILL_CREDENTIAL_TYPES` mirrors `app.models.credentials.credential.CredentialType`
for the same reason, minus `mcp_provider` — an MCP connector never reaches
`credentials.json`, so no script could consume that slot. A unit test guards
that mirror too.

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
- `invalid_credentials` is an **error** code: a malformed `credentials:` block
  excludes the skill from the projection and refuses publish, the same
  discipline as a malformed `name`. Its message carries the first problem the
  parser found, naming the offending slot.

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
    version: str | None = None      # frontmatter `version`, via coerce_version()
    credentials: list[dict] = []    # ALWAYS present; [] = none declared
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
| `parse_credential_declarations(raw)` → `(declarations, problem)` | Validates and normalises the `credentials:` block into `{slot, type, description}` entries. `problem` is a short phrase naming the **first** problem (no trailing period) and then the list is empty, which the caller turns into `invalid_credentials`. An absent key and a bare `credentials:` both declare nothing and are valid. Unknown item keys are ignored, so a skill written for this parser survives a later optional key |
| `coerce_version(raw)` | Frontmatter `version` → `str \| None`. Public because the publish path reads the same key and must agree with the index on what counts as a version. `_coerce_scalar` has already turned `version: 2` into an `int` and `version: 1.0` into a `float`, so a bare number is stringified rather than dropped; a list or mapping is `None`. Capped at `MAX_VERSION_LENGTH` (64), the same bound as `SkillPublishRequest.version` |
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

**Platform skills.** `refresh(..., platform_skills_dir=DEFAULT_PLATFORM_SKILLS_DIR)`
(`/app/core/skills`, installed per environment by
`backend/app/services/environments/platform_protocol_files.py`) adds the valid
entries whose name is in `PLATFORM_SKILL_NAMES` (`_platform_entries`) to the
projected set and copies them from that root (`_source_root`). The state hash is
`_combined_hash`: the workspace `tree_hash` folded with the platform tree's when
the platform root exists, and the workspace hash unchanged otherwise, so an image
without platform skills keeps the state it already latched. OpenCode gets no
`skills.paths` entry for `/app/core/skills` — it reads the projected copy, and a
second path would index the skill twice.

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
`skills/<name>/SKILL.md` before using a skill". The block also lists the
platform skills found under `/app/core/skills` (`- **cinna-tasks**
(`/app/core/skills/cinna-tasks/SKILL.md`) — <description>`); a scan failure there
is logged and never breaks prompt generation. For both shipped engines it
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

## Credential slots — `backend/app/services/skills/skill_credential_requirements.py`

A skill's scripts are useless without the credential they call. This module is
both halves of tying the two together: **what publish freezes**, and **how an
installed agent's slots stand afterwards**.

A skill declares its credentials in `SKILL.md` frontmatter
(`credentials: [{slot, type, description}]`, validated by
`skill_manifest.parse_credential_declarations`). **A slot is a
`Credential.service_uri` value** — that is the whole binding, and
`spec_slot(parsed)` (`credential_provisioner.py`) reads it as
`parsed.service_uri or parsed.name`.

Use a slot consistently for one service and credential type. Provisioning and
status match `(type, slot)`, while the container's general `by_slot` and
`require_slot` helpers look up the slot alone; `agent_api_session` rejects a
resolved credential of another type.

### Publish — `SkillCredentialRequirements`

| Member | Notes |
|--------|-------|
| `resolve_for_publish(session, *, agent, publisher, declarations)` → `list[SkillCredentialResolution]` | Read-only. Resolves each declaration against the credentials **linked to the publishing agent** — one query loads them all; candidates for a slot are the linked credentials of the declared type carrying it |
| `build_specs(...)` | Freezes the resolutions onto `SkillPackageRevision.required_credential_specs`, using **the same spec schema as a bundle revision** — written by `credential_spec.build_spec`, read back by `credential_spec.parse_credential_spec` |
| `to_publish_preview(...)` / `specs_to_public(...)` / `provisions_to_public(...)` | Read projections for the publish preview, the catalog detail payload and the install response |
| `parse_specs(raw_specs)` | Tolerant read of the frozen JSON |
| `publisher_usages_of_credential(...)` | Which published skills a given credential backs — feeds credential deletion impact. Returns the usages plus **every** revision id of the packages they name, not only the providing revisions (see `credential_sharing_tech.md`) |
| `SkillCredentialResolution` | `slot`, `type`, `description`, `provided_by`, `credential`, `reason`, `producer_agent_id`, `producer_agent_name`. `credential` names the publisher's **own** matched row even for a refused resolution, so the preview can say *which* credential was rejected rather than just that something was |

`SkillCredentialProvisionPublic.needs_setup` reports usability independently of
the provisioning outcome. In particular, `linked_existing` and `already_linked`
can reuse an unfilled placeholder. `provisions_to_public` computes the flag from
the same bounded credential/share reads used for name visibility; a previewed
`linked_publisher` is ready if the forthcoming share will make it usable. Both
install dialogs use this flag for their warning rows, setup panel and success-close
decision. Clients reading older responses fall back to the outcome's default.
Before sharing, the preview hides the publisher credential's id as well as its
name. Its internal `SlotProvision.is_placeholder` carries just configuration
state through that redaction, so an intentionally hidden id does not become a
false setup warning. The install response still reads the actual linked row.

**Resolution uses only the credential's own consent flags, and requires the
publisher to *own* it for `publisher` / `template`.** A share the publisher
merely *received* cannot be re-shared, and install later only shares a
credential whose owner is the package publisher. Bundle-only
`publish_settings.credential_overrides` never apply here. When a slot cannot
resolve to `publisher` or `template` it falls back to `provided_by="user"` and
records why: `no_linked_credential`, `not_shareable`, `not_owned`, or
`template_would_leak_secret`.

**The secret-leak guard is the load-bearing one.** A `template` spec is frozen
into an immutable revision **every catalog viewer can read**, and copied into
every installer's credential — so `_template_leaking_secret_fields` decides,
**on field names alone with no decryption**, whether a template of this
credential would still carry a secret, by mirroring exactly what
`PublishService._template_payload_for` strips. It unions the env-shaped
`CredentialsService.SENSITIVE_FIELDS[type]` with the stored `credential_data`
keys `_STORED_SECRET_FIELDS_BY_TYPE[type]` — `SENSITIVE_FIELDS` alone let an
`api_token` credential marked private only on `http_header_value` freeze its
raw `api_token`. It **fails closed**: a type that is neither force-private nor
classified by either map is treated as leaking.

`_DERIVED_SECRET_SOURCES` is the other half of that rule: an env-computed secret
(`api_token`'s `http_header_value`, derived from the stored `api_token`) is safe
when its source is private. The API accepts arbitrary credential-data keys,
however, so callers can also store a field with that derived name. Skill
`build_specs` strips all derived secret fields from `template_data` before
freezing it, even if one was explicitly stored. This keeps the ordinary
token-private-only workflow valid without leaking a separately stored header.
A unit test requires every derived field to name a source that is itself
classified as a stored secret of that type.

Bundle `_template_payload_for` has the same env-shaped/stored gap and is
deliberately left unchanged.

### After install — `SkillSlotIndex`

The one read model of how an agent's skill slots stand. For catalog skills it is
built from the frozen specs of the revisions the agent's `source=catalog` links
pin — **never from the environment's skill index**, because a pre-feature
container reports rows without credentials at all — plus the agent's linked
credentials. A skill with no pinned revision (a local skill, a marketplace or
bundle plugin's skill) has no record of its declarations other than its
`SKILL.md`, so the Addons projection hands its index entries' `(slot, type)`
pairs to `issues_for_declarations`. A pre-feature container reports none, and
those rows stay silent.

| Member | Notes |
|--------|-------|
| `build_for_agent(session, agent, *, with_declarations=False)` | Loads with a constant number of queries: the agent's catalog links, their revisions' specs, the agent's linked credentials, the share rows of the foreign ones, and (only for an agent with an installed bundle revision) that revision. The last three are skipped when no link declares a slot, unless `with_declarations` is set — the Addons projection sets it when a non-catalog index entry declares credentials |
| `issues_for_link(link_id)` → `list[CredentialIssue]` | Drives the Addons row status of a catalog row |
| `issues_for_declarations(pairs)` → `list[CredentialIssue]` | The same reasons for `(slot, type)` pairs read from the skill index, in order; unknown types are skipped. Drives the status of local and plugin rows |
| `slot_credential_ids()` | Every linked credential a catalog spec on the agent points at, with no bundle subtraction — "does anything installed still declare this slot". Read by `InstallService.list_setup_credentials` to stop a kept placeholder from claiming a skill needs it |
| `skill_provisioned_credential_ids()` | The readiness gate's exclusion: `slot_credential_ids()` **minus** the credentials the agent's bundle revision claims |
| `specs_for_link` / `specs_except_link` | Uninstall's released/retained split, handed to `CredentialProvisioner.release_skill_slots` |

**A slot is satisfied** when one of its candidates is filled in and either
owned by the agent owner, **or** foreign, still `allow_sharing`, **and** shared
with the agent owner. Candidates must match the frozen type and slot: a
publisher credential id alone is insufficient if its live `service_uri` or
type changed. Install preview/provisioning likewise treats such a publisher
credential as unavailable and tries the usual fallback paths.
*A share row alone is not enough* — it is a defence against
stale or manually-corrupted state. Both sharing-disable paths (`PUT
/credentials/{id}` and `PATCH …/sharing`) delete shares **and** recipient links,
so their normal result is `not_linked`. Otherwise `CredentialIssueReason` is
`not_linked` (no candidate), `not_configured` (a placeholder) or
`access_revoked`. `SkillSlotIndex._usable` also treats a linked issued `agent_api` key whose key status is anything but `active` as not usable (so its slot reads `access_revoked`); key states load in one batched `key_states_by_credential` call in `build_for_agent`, and connections (no external key) are unaffected.

**Why the readiness gate subtracts bundle-claimed ids.** An unfilled *skill*
slot is a warning, not a block — a bundle's specs are the agent's contract, a
skill is one capability among many. But claiming errs **wide**: a credential of
a bundle spec's type that no recorded pick answers counts as the bundle's,
because under-claiming would unblock an agent its bundle says is not ready.

Consumers: `addons_service` (row status), `InstallReadinessGate._drop_skill_provisioned`
(lazy import — the skills domain sits above bundles in the import graph),
`llm_plugin_service` (uninstall), `skill_catalog_service` (publish and install),
`routes/skills.py` (install response). The rows themselves are written by
`CredentialProvisioner` under `SKILL_INSTALL_POLICY` — see
[Agent Bundles](../agent_bundles/agent_bundles_tech.md).

## Backend: `AgentSkillsService`

### Cache columns on `AgentEnvironment`

| Column | Type | Notes |
|--------|------|-------|
| `skills_parsed` | JSON, nullable | list of index-entry dicts |
| `skills_hash` | `String(64)`, nullable | workspace tree hash as reported |
| `skills_fetched_at` | `DateTime(timezone=True)`, nullable | |
| `skills_error` | `String(256)`, nullable | `env_not_running` / `adapter_error` / `adapter_unsupported` / `parse_error` |

Constants live in `agent_skills_service.py`: `ERROR_ENV_NOT_RUNNING`,
`ERROR_ADAPTER_ERROR`, `ERROR_ADAPTER_UNSUPPORTED`, `ERROR_PARSE_ERROR`.

| Code | Raised from | Remedy the surfaces name |
|------|-------------|--------------------------|
| `env_not_running` | any adapter failure while `environment.status in SLEEPING_STATUSES` | refresh / send a message |
| `adapter_error` | any other adapter failure — timeout, transport, non-404 status | `cinna agent restart-env` |
| `adapter_unsupported` | `EndpointUnsupportedError` — a **404** from a reachable container | `cinna agent rebuild-env` / `/rebuild-env` |
| `parse_error` | the payload could not be read | refresh |

`adapter_error` and `adapter_unsupported` were one code. They are split because a
restart re-runs the same image and can never add a route the image never had;
only a rebuild replaces `/app/core` from the template. Every surface that had to
choose one remedy for the shared code was wrong half the time.

### Behaviour

- **Always attempts the adapter call, classifying only on failure.** The status
  column is not a reliable pre-check: the env-start sweep runs inside
  `_sync_dynamic_data`, *before* the row is stamped `running`, so gating on it up
  front would make the sweep always fail on a container that is demonstrably up.
  `SLEEPING_STATUSES = {"suspended", "stopped", "error"}` deliberately excludes
  transitional statuses — telling a user to "refresh to wake it" instead of
  "rebuild it" sends them to the wrong fix.
- **`EndpointUnsupportedError` is caught in its own `except` clause, ahead of the
  generic one — and therefore ahead of the sleeping-status branch.** Ordering is
  load-bearing: a pre-feature container that also happens to be suspended would
  otherwise be classified `env_not_running`, and "refresh to wake it" would send
  the user round a loop the wake never breaks. Its `/app/core` is the problem, and
  waking it changes nothing about that.
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
| `read_skill_content(environment, name, agent=None)` | `(path, text, truncated)`. Path comes from the **cached index**, never from `name` — that is what keeps this from being a workspace file-read endpoint wearing a skill's name. Wakes a suspended env via `wake_suspended_environment` **after** the cache lookup, so an unknown name never starts a container. Cap `MAX_CONTENT_BYTES = 256 KB`, decoded with `errors="replace"` |
| `list_skill_files(environment, name)` | `(path, files, count, total_bytes)`, read host-side at `<ENV_INSTANCES_DIR>/<env id>/app/workspace/<entry.path>` (plugin folders included). Folder from the **cached index**, which the container writes, so `_is_skill_folder_path` admits only `skills/<name>` and `plugins/<mkt>/<plugin>/skills/<name>` (no `.`/`..`/empty parts — `"."` would otherwise list the whole workspace). `_walk_skill_folder` opens each component with `O_NOFOLLOW` and walks with `os.fwalk(follow_symlinks=False)`, so a symlink swapped in after the check is refused at any depth; skips `SKIP_DIR_NAMES` and non-regular files, like the index walk that measured `size_bytes`. `None` → 404; missing workspace raises `SkillsIndexUnavailableError("workspace_unavailable")` → 503 |
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
`EnvironmentAdapter`) GETs `/config/skills` with a 15 s timeout.

A pre-feature container answers **404**, and that one status is raised as
`EndpointUnsupportedError` (`backend/app/services/environments/adapters/base.py`,
re-exported from `adapters/__init__.py`) rather than a flat `Exception`:

```
httpx 404  →  EndpointUnsupportedError("/config/skills")  →  adapter_unsupported
anything else → Exception(...)                            →  adapter_error
```

The distinction cannot be recovered downstream once the transport error is
flattened to a string — "no such route" and "no such container" read identically
— so it is raised as a type. `EndpointUnsupportedError` carries the `endpoint`
that answered 404 and is deliberately narrow: **only** a 404 from a container
that answered. Any other status came from *inside* an endpoint that exists, and a
rebuild is not the fix for that.

`DockerEnvironmentAdapter.set_plugins()` raises the same error, on the same 404
rule, for `/config/plugins` — see [agent_plugins](../agent_plugins/agent_plugins_tech.md).

---

## Read routes — `backend/app/api/routes/agent_skills.py`

Registered on the `agents` tag (so they land on `AgentsService` in the generated
client), before `agents.router`.

| Method + path | Behaviour |
|---------------|-----------|
| `GET /agents/{agent_id}/skills` | Cache-only — safe to poll, never wakes a container |
| `POST /agents/{agent_id}/skills/refresh` | Wakes a suspended env, re-reads, returns the same shape. **Never fails** on an unreachable environment: the body carries the cached rows plus an `error` code. Bypasses the 30 s limit |
| `GET /agents/{agent_id}/skills/{name}/content` | `SkillContentPublic`. Wakes a suspended env first; 503 when the env is still unreachable — "waking up — try again" while it is `activating`/`starting` (a parallel wake), otherwise "unavailable — start it" (stopped, errored, or the wake failed). Two distinct 404s: "Skill not found" versus "SKILL.md not found — refresh the skills list" when the index still lists it but the file is gone |
| `GET /agents/{agent_id}/skills/{name}/files` | `SkillFilesPublic` (rows are `SkillRevisionFilePublic`, capped at `MAX_LISTED_FILES`; `count` / `total_size_bytes` cover the whole folder). Host-side, never wakes a container. Same two 404s as `/content` ("Skill folder not found — refresh…"); 503 when the workspace is not on disk |

Access is `AgentService.user_can_access` (superusers bypass), so the read gate
and the capability reply cannot disagree about who the agent belongs to.

### Response models — `backend/app/models/agents/agent_skills.py`

`SkillIssuePublic`, `SkillEntryPublic`, `AgentSkillsPublic`,
`SkillContentPublic`, all re-exported from `app.models`.

`AgentSkillsPublic.can_publish` = `AgentService.can_build(session, user, agent)`
— the developer role **and** not a foreign install.
`SkillEntryPublic.can_publish` = that **and** `source == "local"` **and**
`entry.is_publishable`.

`SkillEntryPublic.credentials` (`list[SkillCredentialDeclarationPublic]`,
`{slot, type, description}`) carries the declared slots through to the agent
page. Always present: `[]` both for a skill that declares none and for one
reported by a container built before skills could declare credentials.
`AgentSkillsService._normalise_credential_declarations` coerces the reported
payload **tolerantly rather than validating** — the container's own parser
already refused an invalid block (the skill carries `invalid_credentials`) — but
still bounds it with the parser's own limits, because the payload comes from a
container that agent code controls: at most `MAX_SKILL_CREDENTIALS` items, an
over-long slot or unknown type dropped, a long description truncated.

**The catalog's own schemas live in `backend/app/models/skills/schemas.py`:**
`SkillCredentialRequirementPublic` (a revision's frozen slots, on
`SkillPackageRevisionPublic.required_credentials` and the package detail),
`SkillPublishCredentialPreview` (`SkillPublishPreview.credentials` — adds the
matched `credential_id` / `credential_name` and the `reason` a slot fell back to
`user`), `SkillCredentialProvisionPublic` + `SkillSlotOutcome` (the install
report and `SkillInstallPreview`). None of them can carry `template_data` or
`template_private_fields`; `publisher_credential_id` is an id, not a secret.
`provisions_to_public` fills `credential_name` **only** for a credential the
agent owner owns or already holds a share on, so a publisher's credential is
never named to an installer before its share exists.

`SkillEntryPublic.version` (`str | None`) carries the frontmatter's optional
`version`, so `GET /agents/{agent_id}/skills` reports it alongside the addons
projection's `AddonPublic.version`. `None` for a skill nobody has versioned and
for one reported by a container built before skills carried a version — a
client renders its absence, never a bare `v`. Currently read only through the
addons projection; nothing in `frontend/src` reads `skill.version` directly, so
a plugin-shipped addon's per-skill versions are not surfaced anywhere yet.

---

## `/skills` command and popup entries

`SkillsCommandHandler` (`streams=False`, `include_in_llm_context=True`,
`requires_running_environment=False`) renders the cached index as a markdown
table `Skill | Source | What it does | Invoke`. Flagged rows sort last — the
table is read top-down for "what can this agent do", and a broken skill is not an
answer to that. Values pass through `_cell()`, which escapes `|` and newlines: a
publisher-authored description with a pipe would otherwise shift every later
column. Empty index + `skills_error == adapter_unsupported` produces the rebuild
copy (an error); an ordinary empty index is **not** an error. The client copy
map (`frontend/src/utils/skills.ts::INDEX_ERROR_COPY`) is keyed on the same
codes, and the rebuild sentence moved with the code:

| Code | Sentence |
|------|----------|
| `adapter_unsupported` | "Rebuild the environment to enable skills." |
| `adapter_error` | "The environment isn't answering. Restart it, then refresh the skills." |
| `env_not_running` | "The environment is asleep. Refresh to wake it and re-read the skills." |
| `parse_error` | "Couldn't read the skills folder. Refresh to try again." |

The CLI keys the same four codes to remedies in `_index_error_remedy`
(`cinna-cli`, `src/cinna/account.py`), which an unknown code falls through to a
deliberately unhelpful default — naming a remedy this build cannot know is how a
caller ends up in a loop that cannot close.

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
| `required_credential_specs` | JSON, not null, server default `'[]'` | one frozen spec per declared slot, **the same entry schema as `agent_bundle_revision.required_credential_specs`** — added by migration `562ac5a89f04` |
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
4. `SkillCredentialRequirements.resolve_for_publish` then `build_specs` —
   read-only, and still **ahead of anything that commits**, so a template
   credential whose stored data cannot be decrypted is a refusal
   (422 `credential_template_unreadable`) rather than a half-written package.
5. Under a per-`(publisher, skill name)` `asyncio.Lock` (locking on the package
   uuid would leave the create path — the one that races into a unique-constraint
   violation — unguarded): resolve or create the package, allocate
   `revision_number`, write the snapshot off the event loop, insert the revision
   **with its frozen `required_credential_specs`**, point the package at it.

`_write_snapshot_to_disk` fills `<rev>.tmp/skills/<name>/` via `safe_copytree`
(symlinks refused at every depth) and moves it into place, then builds and caches
the archive **in the same thread** so its digest can be stored on the row.

`_apply_publish_to_package` carries the frontmatter description over **only while
the stored value still equals what the previous revision declared** — §5.3's
literal "updates description" would silently overwrite a publisher's edited
catalog blurb on their next publish.

The derived id is `base_package_id(name)` = `<reversed host>.skill.<name>`,
disambiguated by `derive_package_id` when it is already taken — the derivation
table below is the single description of that rule. Validated against
`BUNDLE_ID_REGEX`. (An earlier `default_package_id(user_id, name)` =
`<reversed host>.<8-hex owner slug>.<name>` is gone: the slug became the
collision fallback rather than an always-present segment.)

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

**Install also provisions the revision's credential slots**, in the *same*
transaction as the link (`CredentialProvisioner.provision` under
`SKILL_INSTALL_POLICY`, with `PublisherBoundary(package.publisher_user_id)`):
either both rows exist or neither does. A per-slot problem only marks the report
`degraded`; it never fails the install. The route then pushes credentials to the
environments **before** the plugin sync, because plugin sync does not carry
credentials — and only when `report.changed`. The provisioner is idempotent
when slots already link; the catalog install endpoint itself rejects an
already-installed package with `already_installed`. `install_preview` runs the
same decision tree read-only.

**Upgrade** — `upgrade_link` re-pins to `package.latest_revision_id`; idempotent.
It provisions only `_credential_specs_added(previous, latest)` — specs whose
`(type, slot)` the previous revision did not declare — in the re-pin's own
transaction. A slot the user unlinked after the install must stay unlinked, so
re-provisioning everything would undo a deliberate act.
**Uninstall** is deliberately not a verb here: it is
`DELETE /llm-plugins/agents/{agent_id}/plugins/{link_id}` like every other
plugin — which now routes through `LLMPluginService.uninstall_plugin_link`,
releasing the link's skill placeholders through
`CredentialProvisioner.release_skill_slots` before deleting the link row, in one
commit. **That is the only path that releases slots**: any future code that
deletes an `AgentPluginLink` another way would leak placeholders.

**File listing** — `list_revision_files(revision) -> (files, total_count,
total_bytes)` walks the snapshot through `snapshot_files(skill_dir)`, the one
definition of "what this revision ships" that both this route and the archive
builder call — a second copy of the symlink-refusing predicate is a second
place to get it wrong, and would let the file count and the tarball describe
different trees. `files` is capped at `MAX_LISTED_FILES` (500); `total_count`
and `total_bytes` always describe the whole snapshot, so a truncated response
can say "first 500 of 900" rather than print a wrong count. Same
`snapshot_missing` (410) as the content preview when the immutable files are
gone from disk.

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
| `not_accessible`, `package_not_found`, `revision_not_found`, `skill_not_found` | 404 | `skill_invalid`, `skill_contains_secrets`, `skill_too_large`, `credential_template_unreadable`, `package_id_invalid`, `invalid_visibility`, `invalid_display_name` | 422 |
| `not_developer`, `foreign_install`, `not_publisher`, `not_superuser` | 403 | `package_id_taken`, `package_id_immutable`, `already_installed`, `name_conflict`, `no_revision` | 409 |
| `no_environment`, `workspace_unavailable` | 409 | `snapshot_missing` | **410** |
| `not_a_catalog_link` | 400 | `archive_unavailable` | 503 |

`snapshot_missing` is 410 rather than 503 on purpose: a revision is immutable, so
files that are not there will not reappear on a retry, and "try again later"
would send the caller down the wrong path.

### Access model

`skill_package_access_grant` gained `principal_kind`, `principal_id`, `role` and `access` (unique key on item + kind + principal id) like the bundle table (see [Catalog access model](../agent_bundles/agent_bundles_tech.md#catalog-access-model)). `SkillCatalogService.user_can_manage` = holds `maintain` (no superuser bypass on the per-kind routes; an admin's lever there is `delist`); `not_publisher` (403) is raised for a visible package without `edit` on publish, and "Only a maintainer can change this package." on `update_package`. Skill-specific error codes live in `backend/app/services/skills/exceptions.py`. The per-user `GET`/`POST`/`DELETE .../grants` routes below now delegate to the shared access service.

### Routes — `backend/app/api/routes/skills.py`

Two routers, one `skills` tag → one `SkillsService` in the generated client.

| Method + path | Deps | Purpose |
|---------------|------|---------|
| `GET /skills/catalog` | `CurrentUser` | `SkillPackagesPublic` — unfiltered |
| `GET /skills/packages/{package_id}` | `CurrentUser` | `SkillPackageDetailPublic` (+ full revision history) |
| `PATCH /skills/packages/{package_id}` | publisher | `display_name` / `description` / `visibility` / `is_listed` |
| `POST /skills/packages/{package_id}/delist` | superuser | hide, never delete |
| `GET /skills/packages/{package_id}/revisions/{n}/content` | `CurrentUser` + visibility | `SKILL.md` preview |
| `GET /skills/packages/{package_id}/revisions/{n}/files` | `CurrentUser` + visibility | `SkillRevisionFilesPublic` — every file of the snapshot as `{path, size_bytes, is_executable}`, plus `count` / `total_size_bytes` / `truncated`. Paths and sizes only: the catalog is not a reader over other people's published trees. A directory walk of an immutable snapshot, capped at `MAX_LISTED_FILES` (500), with the totals still describing the whole tree |
| `GET /skills/packages/{package_id}/revisions/{n}/download` | `CurrentUser` + visibility | the same deterministic tarball the container gets, for a **person** in a browser. A second route rather than a second auth branch on `/archive`: that one is authorised by the calling environment's install and must stay so (a publisher going private must not break existing installs), this one by ordinary catalog visibility. `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff` |
| `GET /skills/packages/{package_id}/revisions/{n}/archive` | `AgentEnvContextDep` | tarball; `X-Content-SHA256` header. 403 (not 404) when the env authenticated fine but holds no install of that revision |
| `GET /agents/{agent_id}/skills/{name}/publish-preview` | owner + developer gate | `SkillPublishPreview` — the version and `package_id` a publish would take, from the same code that will take them. Runs the authorization gate and the workspace lookup but **not** the three content checks: a preview that refused would leave the Share dialog with nothing to show for a skill whose row already carries the warning |
| `POST /agents/{agent_id}/skills/{name}/publish` | owner + developer gate | `SkillPackageRevisionPublic` |
| `GET /agents/{agent_id}/skills/install-preview?package_id=&revision_number=` | owner | `SkillInstallPreview` — what the install would do per credential slot. **Declared before the `/{agent_id}/skills/{name}` patterns**, or the literal segment would be read as a skill name. Same gates as the install: another user's agent and an invisible package both answer 404 |
| `POST /agents/{agent_id}/skills/install` | owner | creates the link **and provisions its slots**, syncs credentials when anything changed, then `LLMPluginService.sync_plugins_to_agent_environments(message_prefix="Skill added.")` → `PluginSyncResponse` with `credential_provisioning` filled in |
| `POST /agents/{agent_id}/plugins/{link_id}/upgrade` | existing route | now maps `SkillCatalogError` through `http_error_for` |
| `GET`/`POST` `/skills/packages/{package_id}/grants`, `DELETE .../grants/{user_id}` | publisher (`_get_managed_package`; **no superuser bypass**) | `users`-visibility allowlist — see [agent_addons_tech](../agent_addons/agent_addons_tech.md) |

`POST /skills/packages/{package_id}/delist` was hardened when the grants landed:
it now checks `is_superuser` **before** loading the package and then loads without
a visibility check. It previously loaded through the visibility-gated path, which
was both an existence oracle and the reason a `users` package could not be
delisted.

#### Desktop publish

Both routes sit on `router`, declared before the `/packages/{package_id}` routes. Logic: `backend/app/services/skills/skill_upload.py` (`SkillUploadService`).

| Method + path | Deps | Purpose |
|---------------|------|---------|
| `POST /skills/packages/publish-preview` | `CurrentUser` | body `SkillDesktopPublishPreviewIn {name, frontmatter, package_uuid?}` -> `SkillPublishPreview` (now with `package_uuid`). With `package_uuid` the caller needs `edit` (404 unseen, 403 otherwise). Credentials parsed from `frontmatter.credentials`, all `provided_by='user'` |
| `POST /skills/packages/publish` | `CurrentUser` | multipart `archive` (tar.gz of `skills/<name>/`, one top-level dir) + `meta` JSON string `SkillUploadMeta {package_uuid?, version?, release_notes?, visibility?, grants?: CatalogGrantIn[] (max 50)}` -> `SkillPackageRevisionPublic`. A malformed `meta` is a 422 request validation error |

Order, with nothing written before step 4: `read_capped` (compressed bytes over `MAX_PACKAGE_BYTES`, 16 MiB -> 413 `skill_too_large`); `safe_extract` into a temp dir (rejects absolute/`..` paths, links, devices, several top-level dirs, more than `MAX_ARCHIVE_MEMBERS` = 2000 members, unpacked total over the cap, all `skill_archive_invalid` 400); `parse_skill_dir` + `validate_entry` (same codes as the cloud path); package resolution (`package_uuid` -> `edit` plus name match else 409 `skill_name_mismatch`; none and caller already publishes that name -> 409 `package_exists` with `package_uuid` via `SkillCatalogError.extra`); `_check_sharing` (visibility/grants on an existing package need `maintain`, `use` grants need `users` visibility, 409 `grants_require_users_visibility`); `build_specs` with `provided_by='user'`; `_resolve_package(source_agent_id=None)` for a new package; `_publish_tree` under the per-package publish lock (version write-back lands in the temp copy so the snapshot carries it; `source_agent_id` stays unchanged when `None`). Then `CatalogEvents.revision_published` and, for newly granted users, `CatalogEvents.granted`.

`SkillCatalogError` gained `status` (per-raise override, used for the 413) and `extra` (merged into the detail body); `STATUS_BY_CODE` gained `package_exists`, `skill_name_mismatch` (409), `skill_archive_invalid` (400), `invalid_grants` (422).

The `content`, `files` and `download` routes call `SkillCatalogService.get_readable_revision`: `user_can_see`, else `LocalInstallService.pinned_by_usable_desktop_agent`, else 404. `GET /catalog/items/{uuid}` is not opened by the pin rule.

#### Version and package-id derivation — `SkillCatalogService`

| Member | Notes |
|--------|-------|
| `resolve_version(*, requested, frontmatter_version, published_versions, latest_version)` | Pure. An explicit request wins verbatim and is never de-duplicated — but it passes through `coerce_version` first, so a control character disqualifies it rather than reaching the file. Else the header's version if it is not already published; else the successor of the seed (`latest_version`, else the header), walking on while the candidate is taken; a seed with no successor gets `.1` appended so the series stays **above** its predecessor. `FIRST_VERSION` (`1.0.0`) only when there is no seed at all |
| `_bump_version(v)` | Increments the **last run of digits** (`^(.*?)(\d+)$`) — `1.0.0`→`1.0.1`, `1.2`→`1.3`, `v3`→`v4`, `2026-09-09`→`2026-09-10`. `None` for a version with no digits, which starts a fresh series |
| `_write_version_to_skill_md(skill_md, version)` | Surgical single-line edit of the frontmatter — replace a top-level `version:`, else insert after `name:`, else after the opening fence. Preserves the BOM, the dominant line ending and every other byte (mixed endings normalise to the dominant one, via `splitlines()`); atomic (dot-prefixed temp + `os.replace`). Writes into the publisher's **host-side workspace** — `<ENV_INSTANCES_DIR>/<env id>/app/workspace/skills/<name>/SKILL.md`, the same bind-mounted tree the publish already reads from, so it needs no mount the publish did not already need. Returns `True` when the file carries the version afterwards, **including when it already did**, so the caller only warns on a genuine failure. Runs in a thread, off the event loop |
| `_version_history(session, package_uuid)` | One read → `(next revision number, every version used, newest version)`. Shared by the publish and the preview so the number shown is the number written |
| `base_package_id(name)` | `<reversed host>.skill.<name>`. `PACKAGE_ID_SKILL_SEGMENT` is the fixed segment that tells a package id from a bundle id |
| `derive_package_id(session, *, publisher_user_id, skill_name)` | `(id, disambiguated)`. Base → base + the publisher's 8-hex slug → `-2`, `-3`, and after 98 collisions a random `uuid4().hex[:8]` tail rather than raising. Read-then-write, so two simultaneous first publishes of one name can still pick the same free id; `uq_skill_package_package_id` is what actually decides, and `_resolve_package` catches that `IntegrityError`, rolls back and re-raises it as `package_id_taken`. Without the catch the loser got a raw **500** on an aborted session — this line documented the 409 before the code produced one |
| `publish_preview(session, *, agent, user, skill_name)` | The read behind `GET .../publish-preview` |

Order inside `publish_from_agent`, all within the per-`(publisher, skill)` lock:
`_resolve_package` → `_version_history` → `resolve_version` →
**`_write_version_to_skill_md`** → `_write_snapshot_to_disk`. The write-back is
before the snapshot on purpose — a revision is immutable, so there is no second
chance to put the version in the published bytes. The stored
`SkillPackageRevision.frontmatter` is `{**entry.frontmatter, "version":
resolved}` for the same reason: a revision whose stored frontmatter disagreed
with its own snapshot would be a second answer to "what version is this" — and
the merge is therefore **conditional on the write-back having succeeded**, since
on the degraded path the snapshot carries no `version:` at all and merging one
in would manufacture the very disagreement it prevents.

After the commit, `AgentSkillsService.refresh_after_action(env, force=True)` —
the write-back changed a file the index is a cache of — but **only when the
environment is not in `SLEEPING_STATUSES`**. `refresh_after_action` does not
wake a container (that is `force_refresh`), so on a suspended env `fetch_index`
fails and `_persist_error` *commits* `skills_error="env_not_running"`: a
successful publish would then paint "the environment is asleep" over the Addons
card until a manual Refresh. Publishing from a suspended environment is
supported and pinned, so the refresh is skipped there instead — the host-side
backfill reads the same header on the next read either way.

#### The host-side version backfill — `AgentSkillsService._backfill_local_versions`

The index is built **inside** the container, and `app/core/` is copied out of
the template at environment *creation* (`_copy_template`, one call site), not at
start. So an environment created before skills carried a version reports rows
with no `version` key however many times its author edits `SKILL.md` and presses
Refresh — while the publish path, which reads the same file from the host, would
happily publish one. `fetch_index` therefore backfills: for every `source=local`
row missing a version whose name matches `SKILL_NAME_RE`, it reads
`<ENV_INSTANCES_DIR>/<env id>/app/workspace/skills/<name>/SKILL.md` and parses
its frontmatter. A **backfill, not an override** — a container that reports a
version is believed. Best-effort throughout, off the event loop in a thread
(it is filesystem I/O in an `async def` that runs after every stream
completion, and a skill with no `version:` at all stays in the missing set
permanently), and it guards a symlinked skill *directory* as well as a
symlinked `SKILL.md`. It runs *before* `_entries_differ`, so the newly-filled
version is what makes the cache change and the card re-render. A rebuilt
container stops needing it.

**It is reached only through `fetch_index`**, whose callers are
`refresh_after_action` (rate-limited unless forced) and `force_refresh`. The
Addons tab's ordinary read is cache-only, so on exactly the environments this
exists for the version appears after a Refresh press, a start sweep, a watcher
signal or a publish — **never on a first page load**. Users on a pre-feature
container are told to press Refresh once.

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
`SkillPackageRevisionPublic`, `SkillPackageUpdate`, `SkillPublishRequest`
(whose `version` a `field_validator` refuses when it carries a newline or any
other control character — the value is written into a frontmatter block, where
a newline injects top-level keys into the author's header),
`SkillPublishPreview`,
`SkillInstallRequest`, `SkillRevisionContentPublic`, `SkillRevisionFilePublic`
(`path`, `size_bytes`, `is_executable`) and `SkillRevisionFilesPublic`
(`data`, `count`, `total_size_bytes`, `truncated` — the response of the
`.../files` route below). All re-exported from `app.models`.

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
| `562ac5a89f04` `add_skill_package_revision_required_credential_specs` | `d7b41e0c9a35` | `skill_package_revision.required_credential_specs` JSON NOT NULL, server default `'[]'`. No data step: every existing revision predates slots, so "declares nothing" is its true value. **Downgrade loses the frozen provisioning** of every revision published since — it cannot be re-derived, and installs would silently treat every slot as absent |

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
| ~~`Agents/SkillRow.tsx`~~ → `Agents/Addons/AddonRow.tsx` | `ListRow` with version + author badges; the row itself opens its dialog, mounted only while open. `AddonRow` deliberately does **not** reuse `SkillRow`: no `meta` line, no info tooltip, a different status precedence, a different action budget |
| ~~`Agents/AllSkillsSheet.tsx`~~ → `Agents/Addons/AllAddonsSheet.tsx` | "Show all (N)" |
| ~~`Agents/SkillContentDialog.tsx`~~ → `Agents/SkillContentBody.tsx` | `SKILL.md` viewer body, rendered in the **SKILL.md** tab of `Agents/Addons/SkillDocumentTabs.tsx` — inside `AddonDetailDialog` for a single-skill row and `Agents/Addons/SkillDetailDialog.tsx` for one skill of a many-skill plugin. Mounted only while that tab is active. Markdown-rendered through the shared `Catalog/SkillSource.tsx`, frontmatter split off |
| `Agents/SkillContentFact.tsx` | The **Content** fact beside Path and Size in both skill dialogs: "N files" from `GET .../skills/{name}/files`, opening the catalog's `Catalog/SkillRevisionFilesSheet.tsx` with no revision label. Query key `["agent", agentId, "skills", name, "files"]`, under the prefix every re-read invalidates. A failed read says "Unavailable"; on a shadowed entry (the route resolves local-first) the value reads "N files in <other path>" in warning tone, matching `SkillContentBody`'s notice; no download action — these are the agent's own files |
| `frontend/src/utils/skills.ts` | `skillsIndexErrorCopy`, `skillRowStatus`, `formatSkillSize`, `sortSkills`, `skillKey` — shared so the card, the row and the sheet cannot drift on sort order or on what a dot means. Still live: `utils/addons.ts` builds on it rather than duplicating it |
| `frontend/src/components/Agents/AgentConfigTab.tsx` | Hosted the card until the Addons tab took it. Was deliberately **not** gated on `showOperationalSettings` or `readOnly`, which is why `"addons"` had to join the `agentUserTabs` set in the same change |
| `frontend/src/components/Chat/SlashCommandPopup.tsx` | `kind === "skill"` renders a `h-5` outline `Badge`, visible text inside the `role="option"` row (so it is part of the accessible name and needs no `sr-only` twin) |
| `frontend/src/components/Catalog/CatalogItemCard.tsx` | One muted `GraduationCap` line, ≤3 names + `+N more`. `skills` is typed `Array<unknown> \| null` (there is no `SkillSummaryPublic` to import), narrowed defensively |

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
route (S7) gained access management through the shared People dialog (`Catalog/People/*`, opened from the package card's `CatalogItemMenu`; the former access card and its grant row / dialog / sheet were deleted, and `EditSkillPackageDialog` no longer carries Visibility), and later a **Content** fact on the Package card
(a file count, e.g. "4 files") opening `Catalog/SkillRevisionFilesSheet.tsx` —
the `.../files` listing rendered as rows — beside a **download** action that
fetches `.../download`:

| # | Surface | New files (per the spec) |
|---|---------|--------------------------|
| S6 | `/catalog/skills` route + `CatalogSectionTabs` (Agents · Skills segmented control on both catalog routes, beside the `Filters` menu in one toolbar row; `AppSidebar` `isActive` widened to `startsWith("/catalog")`) | `routes/_layout/catalog/skills.tsx`, `Catalog/SkillCatalogCard.tsx`, `SkillCatalogGrid.tsx`, `SkillCatalogFilters.tsx`, `CatalogFilterMenu.tsx`, `CatalogSectionTabs.tsx` |
| S7 | Package detail route — Package card (its Version fact opens the revisions Sheet) + `SKILL.md` card | `routes/_layout/catalog/skills/$packageId.tsx`, `Catalog/SkillPackageCard.tsx`, `SkillRevisionRow.tsx`, `AllSkillRevisionsSheet.tsx`, `SkillSource.tsx` |
| S8 | Edit package details dialog | (in the detail route) |
| S9 | Publish skill dialog, opened from the S1 row `⋯` only | `Agents/PublishSkillDialog.tsx` |
| S10 | Add-skill-to-agent dialog (`Common/AgentSelectorList` in `mode="single"` — a select trigger over a `Popover`, never a nested picker dialog) | `Catalog/AddSkillToAgentDialog.tsx` |
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

The credential requirements contract has focused regression coverage in addition
to the skill discovery and catalog tests below:

| File | Covers |
|------|--------|
| `backend/tests/unit/test_skill_manifest.py` | Credential block parsing, type/slot/description validation, limits, duplicates, and host/environment parser identity |
| `backend/tests/unit/test_skill_credential_secret_fields_coverage.py` | Complete stored/derived secret-field classification and template fail-closed rules |
| `backend/tests/unit/test_skill_credential_provision_readiness.py` | Placeholder reuse, privacy-redacted publisher previews, stale shares, and bounded projection queries |
| `backend/tests/unit/test_cinna_api_credentials_slots.py` | Fresh slot reads, missing/unfilled remedies, caller identity, and request/redirect origin protection |
| `backend/tests/api/agents/skill_credentials/agents_skill_credentials_publish_test.py` | Consent-derived publisher/template/user specs, immutable revisions, safe public fields, and producer identity |
| `backend/tests/api/agents/skill_credentials/agents_skill_credentials_install_test.py` | Preview/provision parity, sharing, slot reuse, templates/placeholders, upgrades, uninstall, and revocation |
| `backend/tests/api/agents/skill_credentials/agents_skill_credentials_gate_test.py` | Addons warnings, skill-only non-blocking readiness, bundle claims, and retained-placeholder copy |
| `backend/tests/api/credentials/test_credential_service_uri_env_sync.py` | Type-independent slot and placeholder fields in environment payloads |
| `backend/tests/api/credentials/test_credential_deletion_impact_skills.py` | Skill Tier 2 impact, including installs upgraded past the providing revision |
| `backend/tests/api/credentials/test_credential_share_revocation.py` | Individual-share and both sharing-disable paths remove recipient links |
| `frontend/tests/skillCredentials.test.mjs` | Install readiness/outcome copy and producer facts; run with `npm run test:skill-credentials` from `frontend/` |
| `frontend/tests/skillInstallDialog.test.mjs` | Real catalog install dialog (S2): failed/unsupported/partial sync warnings, reused-placeholder setup and focus, Done/Escape/Credentials close paths, and clean success; run with `npm run test:skill-credentials:ui` from `frontend/` |

The browser tests use `frontend/tests/fixtures/skillInstallHarness.tsx` to mount
the production dialog with its providers in a loopback Vite server and run it
in Playwright Chromium. All API calls are mocked; no running backend, login or
persistent test data is required. They exercise component lifecycle and
navigation separately from the helper tests, rather than claiming a live
publish/install end-to-end run.

The bundle and bundle-install suites remain the regression guard for the shared
`CredentialProvisioner`. Run backend tests in Docker as documented in
`backend/tests/README.md`; current audit results belong in the implementation
plan, rather than being frozen as test counts here.

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
| `backend/tests/api/agents/core/agents_skills_routes_test.py` | The four read routes: cached list, refresh, content and its two 404s, watcher-triggered refresh, no-environment fallback, opening a skill waking a suspended env (a stopped one → 503, one already `activating` → "waking up", an unknown name never starting the container), and `/files` listing folders host-side without waking the env (symlinked file/directory, `..` escape, `.`, top-level and gone folders all 404 without a listing) |
| `backend/tests/api/agents/commands/agents_skills_command_test.py` | `/skills` |
| `backend/tests/api/agents/bundles/agents_bundles_skills_test.py` | Publish gate end to end |

---

## Rollout and Unverified Items

- **Deployment concurrency:** the supported compose deployment runs one backend
  worker (`docker-compose.yml`, `--workers 1`), also required by its in-memory MCP
  sessions. Provisioning idempotency assumes that deployment model; there is no
  owner/type/slot lock to serialize concurrent multiworker provisioning. Harden
  that path before enabling multiple backend workers.
- **Existing environments need a rebuild** — `/rebuild-env` in a session,
  `cinna agent rebuild-env <agent>` from the CLI, or the admin bulk rebuild.
  env-core ships in the per-environment `/app/core` copy, so a pre-feature
  container has no projection and no `/config/skills`, and its cache records
  `adapter_unsupported`. A **restart is not a substitute**: it re-runs the same
  image, and the missing route is in the image.
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
