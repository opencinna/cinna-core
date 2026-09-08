# Agent Skills

Agents carry a set of **agent skills** — folders shaped like the open Agent Skills
standard (`skills/<name>/SKILL.md` with `name`/`description` frontmatter, plus
optional `scripts/`, `references/` and `assets/`). The workflow prompt stays the
orchestration narrative and names skills; the engine injects only each skill's
name and description and loads the body when the skill is actually invoked. That
progressive disclosure is the whole point: a ten-skill agent pays roughly ten
lines of context per turn instead of ten procedures.

Skills travel with the agent through every existing seam (bundles, git
versioning, env migration, the Local Agent Kit), are visible on the agent page
and in the slash-command popup, and can be published to — and installed from — a
server-wide **skills catalog**.

> **Naming.** "Skills" already means two other things in this codebase: the A2A
> agent-card skills in `a2a_config.skills`, and the `skills/` directory a plugin
> may ship. This feature is always **agent skills** / `AgentSkillsService` /
> `SkillCatalogService`. Agent-skill data is never stored in `a2a_config`.

**Tech reference:** [agent_skills_tech.md](agent_skills_tech.md)

---

## Overview

### What an agent skill is

A folder in the agent workspace:

```
/app/workspace/skills/<name>/
├── SKILL.md            # required — YAML-ish frontmatter + markdown body
├── scripts/            # optional — helpers only this skill uses
├── references/         # optional — read on demand
└── assets/             # optional
```

Two frontmatter fields are validated and everything else is passed through
untouched:

| Field | Rule |
|-------|------|
| `name` | required; `^[a-z0-9]+(-[a-z0-9]+)*$`, 1–64 chars, **must equal the folder name**, must not collide with a platform slash command |
| `description` | required; 1–1024 chars after trim. The only text the model sees before it decides to open the skill |

Optional standard keys (`allowed-tools`, `argument-hint`, `model`, `license`, …)
are preserved verbatim. Exactly two of them are *interpreted* by the platform:
`user-invocable: false` hides the skill from the slash-command popup, and
`disable-model-invocation: true` records that the model may not reach for it
unprompted.

### Three sources, one index

| Source | Where the files live | How they arrive |
|--------|----------------------|-----------------|
| `local` | `skills/<name>/` in the agent workspace | authored by the building agent, by the owner locally through the Local Agent Kit, or seeded from a bundle revision |
| `plugin` | `plugins/<marketplace>/<plugin>/skills/<name>/` | installed with a marketplace or bundle plugin |
| `catalog` | `plugins/cinna-skills/<package name>/skills/<name>/` | installed from the instance skills catalog (a plugin link with `source=catalog`) |

The agent page, the `/skills` command and the popup all read one merged index
covering all three.

### How the engines see them

Neither Claude Code nor OpenCode looks at `skills/` in the workspace. Before
**every message**, env-core *projects* the workspace's valid skills into
`/root/.claude/skills/` — the writable `claude_sessions/` bind mount, which is a
user-scope skill source both engines already read (`~/.claude/skills/*/SKILL.md`
is a documented OpenCode global discovery path, which is why one projection
serves both engines).

- **Claude Code** spawns a fresh CLI subprocess per message, so it rescans on
  its own. The `Skill` tool is pre-allowed.
- **OpenCode** memoizes its skill list per workspace instance, so env-core calls
  `POST /instance/dispose` when — and only when — the projection actually moved.
  Generated `opencode.json` carries `permission.skill = "allow"`.
- **Plugin** skills are not projected. Claude Code discovers a plugin's `skills/`
  itself; OpenCode gets each active plugin's `skills/` directory registered as an
  entry in its `skills.paths` config.

An agent with no `skills/` directory behaves exactly as it did before this
feature existed — no directory is created, no engine is asked to rebuild, and
nothing is added to any prompt.

---

## User Flows

### 1. Authoring a skill

**In the cloud.** The building agent writes `skills/<name>/SKILL.md` in its own
workspace. `BUILDING_AGENT.md` carries the authoring guidance: when a procedure
deserves a skill (occasional, long, self-contained) versus staying in
`docs/WORKFLOW_PROMPT.md` (identity, a few lines, applied unprompted), the <!-- nocheck -->
`SKILL.md` contract, and an example of a workflow prompt that has shrunk to a
skills index. The skill becomes visible to the engine on the *next* message —
the projection runs before the adapter dispatches, so a skill written during a
building turn is usable in the conversation turn that follows.

**Locally.** The Local Agent Kit (contract `1.1.0`) scaffolds `skills/` with a
`README.md` and declares it in `layout.json` with role `skills` and
`survives_update: true`. Guide `08-knowledge-and-local-skills.md` teaches the
folder convention.

> **Locally, progressive disclosure is instruction, not machinery.** Nothing on
> the user's own machine registers `skills/` with their coding assistant. The
> folder earns the same behaviour because `templates/agent/AGENTS.md` carries an
> explicit rule: when the workflow prompt names a skill, open
> `skills/<name>/SKILL.md` and follow it. Same layout, same files, same result —
> but one is the engine's doing and the other is a rule the assistant follows.

The older `docs/skill_<name>.md` form is **legacy, not removed**: those docs <!-- nocheck -->
still ship, still travel to the cloud, and are still read when the workflow
prompt points at them. What they do not get is the engine's own progressive
disclosure. Guide 08 carries a five-step migration to be applied when the author
next touches that capability — never as a sweep, because a capability living in
both places is two sources of truth.

### 2. Seeing what an agent carries

- **Skills card** — agent page › Configuration tab. Rows show name, description,
  a status dot, source flags, and a "View SKILL.md" action. Capped at five rows
  with a "Show all (N)" sheet; sorted **errors first, then warnings, then
  alphabetically**, so a row that needs a decision is always in the preview. The
  card is rendered for consumers of a foreign install too — knowing what an
  installed agent can do is a *use* capability.
- **`/skills`** — a chat slash command rendering the same index as a markdown
  table (`Skill | Source | What it does | Invoke`). Reads the cache, so it
  answers instantly and works on a sleeping agent. Its output goes into the next
  LLM turn's context.
- **Slash-command popup** — every valid, user-invocable skill appears as
  `/<name>` with a `skill` badge. These are **not** registered handlers: the text
  passes through to the model, where Claude Code reads a leading `/<skill>` as an
  explicit invocation and OpenCode reads it as an ordinary request. The
  reserved-name rule is what stops a skill from shadowing a real command.
- **Bundle catalog card** — a muted line naming up to three skills the bundle
  ships, with a `+N more` tail.

### 3. Reading a SKILL.md

The card's row action opens a dialog with the raw `SKILL.md` — the text the model
reads. The path comes from the cached index, never from the requested name, so a
plugin's skill resolves inside the plugin folder rather than the agent's own, and
the endpoint can never be used as a general workspace file reader.

### 4. Shipping skills in a bundle

`skills/` is ordinary bundle-owned workspace content: it is captured by the
existing denylist walk with no special case, replaced wholesale on apply-update
like `scripts/`, and carried by env migration and git push/pull.

Publish additionally derives a `skills_summary` (`[{name, description,
has_scripts}]`) into the revision manifest — **derived, never authored**, the
same discipline as `content_hash` — and blocks on two conditions (see
[Business Rules](#business-rules)).

### 5. Publishing a skill to the catalog

From the agent's Skills card, a developer publishes one named skill. The service
reads the skill from the **publisher's workspace on disk**, validates it,
snapshots it atomically into immutable storage and appends a
`SkillPackageRevision`.

- A **suspended or stopped** environment publishes exactly like a running one —
  the workspace is bind-mounted on the host, so nothing needs waking.
- The first publish creates the `SkillPackage` (reverse-DNS `package_id`,
  visibility `private` unless asked otherwise); later publishes append revisions.
- The dialog's success panel links to the catalog entry; it never opens another
  dialog.

### 6. Installing a catalog skill

From the skills catalog, a user picks one of their own agents. This creates an
`AgentPluginLink(source=catalog)` under the synthetic marketplace
`cinna-skills` and then runs the ordinary plugin sync — so per-mode toggles,
disable-without-delete, the amber failure banner and the prune-on-uninstall path
all work with no catalog-specific transport. A suspended target agent is woken by
the plugin sync, and the dialog's copy says so.

The container fetches the pinned revision as a signed archive, verifies its
sha256, safe-extracts it into `plugins/cinna-skills/<package name>/` and
synthesises the `.claude-plugin/plugin.json` locally.

### 7. Upgrading, uninstalling, managing

- **Upgrade** re-pins the link to the package's latest revision, through the same
  `POST /agents/{id}/plugins/{link_id}/upgrade` route every plugin uses.
- **Uninstall** is the ordinary plugin delete — there is deliberately no
  catalog-specific uninstall verb.
- **The publisher** may rename, re-describe, change visibility and list/unlist
  their package. **A superuser** may *delist* (hide from the catalog) but never
  delete or edit somebody else's package; existing installs keep working, which
  is exactly why delist exists instead of a delete button.

### 8. Consumers of a bundle that carries catalog skills

A publisher who installs a catalog skill and then publishes a bundle ships that
skill to consumers **as an ordinary bundle plugin**. Consumers get it, and it
updates with the bundle — they have no independent upgrade path for it. This is
documented behaviour, not a special case.

---

## Business Rules

### Validation and the issue vocabulary

Every flagged condition is a `{code, message, paths}` structure. Clients pick
their status tone from the stable `code` and print the sentence the server
wrote; they never match on prose.

**Errors** (the skill is excluded from the projection):
`not_a_directory`, `missing_skill_md`, `unreadable`, `invalid_frontmatter`,
`missing_name`, `invalid_name`, `name_mismatch`, `reserved_name`,
`missing_description`, `description_too_long`, `budget`, `projection_error`.

**Warnings** (the skill still works): `secrets`, `shadowed`, `oversized`.

A skill shows **one** warning. Precedence is `secrets > shadowed > oversized` —
secrets outranks the rest because it is the only warning that blocks publishing;
shadowed outranks oversized because a shadowed skill may not be the one that
runs.

### Caps are per agent, applied once over the merged list

50 skills and 16 MB per **agent**, not per root. The index builder merges the
workspace's skills with every active plugin's, sorts, and then applies the caps
once — a plugin-heavy agent does not get several times the budget. Overflow
entries stay in the list carrying `error: budget` so the UI can explain the
exclusion instead of silently losing a skill. Entries that already carry an error
are skipped and charge nothing: a broken skill is not projected, so charging it
would exclude a working one for nothing.

### Publishability

`secret_paths` is **always present** on an index entry (empty means the scan ran
and found nothing), so a consumer can test the result without first working out
whether the scan happened. A skill is publishable when
`error is None and not secret_paths`.

### `can_publish` is a capability reply, never a client role check

The server answers on two levels:

- **Response level** (`AgentSkillsPublic.can_publish`) — the agent-developer role
  **and** an agent that is not a foreign install. A client that asked
  `useRole()` instead would offer the verb on a consumer install, where every
  role is use-only, and only the server can see that second half.
- **Entry level** (`SkillEntryPublic.can_publish`) — the above **and**
  `source == "local"` **and** the skill is clean. A plugin's skill belongs to the
  plugin's publisher, not to this agent.

### Bundle publish blocks — and what it does not block

Bundle publish hard-blocks (HTTP **400**, naming the offender) on:

1. any skill carrying an `error`, and
2. any file inside `skills/` that the secret predicate flags.

Two deliberate deviations from the plan:

- **`budget` is not a blocker.** It is an index-presentation exclusion, not
  malformed content. An agent with 51 skills would otherwise be unable to publish
  at all with no route to a fix; the overflow skills still travel in the
  snapshot, they are simply absent from the derived summary.
- **400, not a coded 422.** The coded 422 belongs to the Phase-3 per-skill
  publish verb, whose dialog needs to branch. Bundle publish's sibling
  pre-flight (unresolvable plugins) already answers 400 with a sentence, and one
  publish form reporting two failure classes two different ways would be worse
  than either.

The `skills_summary` is derived from the **live publisher workspace before any
disk write** — the same tree the snapshot is about to copy — so the hard block
and the summary provably describe the same bytes.

### Catalog identity and visibility

- `package_id` is reverse-DNS, unique on the instance, and **immutable** once
  published: a re-publish naming a different id is refused (`package_id_immutable`),
  never silently ignored, because every install and every container manifest
  references it.
- A skill `name` is unique **per publisher**, not globally. Two people may both
  publish `pdf-report`; `package_id` disambiguates them. But one agent has a
  single `plugins/cinna-skills/<name>/` directory, so two publishers' same-named
  packages cannot coexist in one agent — that is a distinct `name_conflict`
  refusal, not "already installed".
- `private` means the publisher only, honoured literally — an administrator has
  no product reason to read an unshared skill body. The one bypass is keyed on
  *visibility*, not listing: an admin may still see a package that was public and
  has been delisted, so delisting is not a trapdoor that hides its own output.
- The package **description** follows the skill's frontmatter until a publisher
  edits it in the catalog; after that a re-publish leaves the edited blurb alone.

### Install counts are computed, excluding the publisher

There is no `install_count` column. An install is an `AgentPluginLink` row that
vanishes with its agent through `ON DELETE CASCADE`, which a stored counter
cannot observe — it would drift upward forever. The count is derived at
projection time and excludes the publisher's own agents, so dogfooding does not
inflate it (the same rule the bundle catalog uses).

### Trust boundary

A skill's body is prompt text and its `scripts/` run with the agent's
credentials, so **the trust boundary of a skill is the trust boundary of its
scripts.** Agent-local skills are authored by the owner or the building agent
inside their own container — no new boundary. Catalog and marketplace skills are
third-party content and go through the plugin pipeline on purpose, inheriting its
posture: explicit install, per-mode toggles, disable without delete, visibility
rules, and the tools-approval flow. A skill's `allowed-tools` never widens what
`can_use_tool` permits.

Secrets never travel: the same predicate warns on the agent page and refuses at
publish, so the refusal can never surprise a publisher who read their own card.
Projection writes are confined to `/root/.claude/skills/`, refuse symlinks on
both sides, and only ever copy directories whose name already passed the skill
regex.

---

## Error Handling & Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| `SKILL.md` missing, unreadable, no frontmatter fence, bad name, name ≠ folder | Excluded from projection; listed with the matching `error` code; the card shows the sentence; bundle publish blocks |
| Skill name collides with a platform command | `error: reserved_name` — excluded from projection and from the popup |
| **A non-directory at the skills root** | **Skipped silently — this is the normal case, not a mistake.** The Local Agent Kit ships `skills/README.md` as scaffolding, so the `not child.is_dir()` guard is load-bearing rather than defensive. `not_a_directory` stays reachable only through a direct `parse_skill_dir` call. Dotfiles at the root are skipped the same way |
| More than 50 skills or over 16 MB | Overflow entries stay in the index with `error: budget`; not projected; not in `skills_summary`; **not** a publish blocker |
| `SKILL.md` body over 64 KB | `warning: oversized` — still projected. The content viewer's own cap is 256 KB, comfortably above it, so an over-long skill stays readable |
| A file inside a skill looks like key material | `warning: secrets` with the offending relative paths; the skill still works, but it is not publishable and bundle publish refuses |
| Same name from two sources (local + plugin, or two plugins) | Both rows exist; **both** are flagged `shadowed`. Which copy an engine loads differs (Claude Code namespaces plugin skills, OpenCode keeps the first loaded), so the honest report is "there are two", not a guess. The popup and the content route resolve to the **local** one |
| Projection write fails (disk full, permissions) | Logged; the message proceeds. The failing skill is reported as `projection_error` — a valid skill the model cannot see would otherwise render as healthy. Only that skill is retried on later messages; the tree hash latches so a durable failure does not re-copy everything every turn |
| Projected directory that is not ours | Left alone. Only directories carrying the `.cinna_projected` marker are ever pruned |
| A skill folder is a symlink, or contains one | Refused / not followed, at both the parse and the copy step |
| OpenCode build without `POST /instance/dispose` | 404, logged once per server. Skills become visible at the next server start; `/rebuild-env` is the user-facing fix |
| Pre-feature container (old `/app/core`) | No `/config/skills` route → cache error `adapter_error`; the card says "Rebuild the environment to enable skills." Refreshing will never fix it |
| `WORKSPACE_FILES_CHANGED` for `skills/` while the env is suspended | The refresh exits early and records `env_not_running`; picked up by the next activation sweep |
| Environment asleep when the card loads | The cached rows are still returned, with an `error` banner over them. Blanking the card on a sleeping agent would be a worse answer than showing what is known |
| Deleting the whole `skills/` folder | Still fires a resync: the watcher always records a directory entry (an absent root hashes to the empty digest), unlike a missing *file*, which is omitted |
| Bundle apply-update drops `skills/` | The directory is pruned wholesale (publisher-owned, like `scripts/`). Consumer-installed catalog skills under `plugins/cinna-skills/` survive — plugins merge, they are not delete-swept |
| Archive sha256 mismatch on install | Nothing is extracted; the plugin reports `failed`, surfacing through the existing amber banner and `PLUGIN_SYNC_FAILED` notification |
| Package or revision deleted while a container re-ensures | The manifest still emits the entry with `archive: null`; the container reports `catalog_revision_missing` as a per-plugin failure rather than silently losing the skill. The link survives (`SET NULL`) and renders as "source unavailable" |
| Snapshot files gone from disk | `snapshot_missing` → **410**, not 503: a revision is immutable, so what is not there will not reappear on a retry |
| Publish from an agent with no environment | 409 `no_environment` — "create one first" |
| Publish from an agent whose workspace was never materialised | 409 `workspace_unavailable` — "start the environment once" |
| Publish naming a skill that is not in the workspace | 404 `skill_not_found` — "refresh the skills list" |
| Non-developer, or a foreign install, tries to publish | 403 (`not_developer` / `foreign_install`). The card never offers the verb, because `can_publish` already said so |
| A skill of already-compressed assets at the size boundary | Publishable **and** installable: the publish cap (16 MiB) measures the uncompressed tree while the container's download cap (24 MiB) measures the gzipped archive. The two are deliberately unequal so a skill cannot be publishable-but-uninstallable — a failure the consumer would discover, at every install, unfixable without a re-publish |

---

## Integration Points

| Feature | How agent skills touch it |
|---------|---------------------------|
| [agent_environment_core](../agent_environment_core/agent_environment_core.md) | New `skills_projection` module and vendored parser; `sdk_manager` projects before every message; both adapters take a `skills_changed` signal; new `GET /config/skills` |
| [agent_prompts](../agent_prompts/agent_prompts.md) | `BUILDING_AGENT.md` gains the authoring section. No change to the three synced prompt docs or their reconcile. The prompt generator's `## Agent Skills` fallback block is a **no-op for both shipped engines** — it only fires for an adapter that sets `SUPPORTS_SKILLS = False` |
| [agent_plugins](../agent_plugins/agent_plugins.md) | `skills` left the OpenCode "unsupported" list; each active plugin's `skills/` is registered as an OpenCode `skills.paths` entry; new `PluginSource.catalog` with archive coordinates; the per-mode OpenCode server is stopped after a real manifest change |
| [agent_bundles](../agent_bundles/agent_bundles.md) | `skills/` is captured by the existing denylist walk (no change); derived `skills_summary` in manifest, revision and catalog entry; publish hard-blocks on invalid or secret-bearing skills |
| [agent_environment_data_management](../agent_environment_data_management/agent_environment_data_management.md) | `.claude` joined `RUNTIME_NAME_DENYLIST` — see the consequence below |
| [agent_git_versioning](../agent_git_versioning/agent_git_versioning.md) | Automatic. `workspace/.claude` now appears in the generated `.gitignore`, and the git live manifest derives the same `skills_summary` so `cinna.agent.json` and `manifest.json` stay one schema |
| [agent_commands](../agent_commands/agent_commands.md) | `/skills` handler; dynamic `/<skill>` popup entries with `kind="skill"` |
| [cli_commands](../cli_commands/cli_commands.md) | The skills cache is the third pull-only entry in the Synced Workspace File Registry, and the first **directory** entry |
| [local_agent_kit](../../application/local_agent_kit/local_agent_kit.md) | Contract `1.0.0 → 1.1.0`; new `skills` role in `layout.json`; guide 08 rewritten; new `templates/agent/skills/README.md`; the capability ladder's "Knowledge & local skills" rung is now satisfied by `knowledge/` **or** `skills/` |
| Realtime events | `WORKSPACE_FILES_CHANGED` naming `skills/` forces a refresh (bypassing the rate limit); `AGENT_UPDATED` is emitted only when the cached **list** changed |

### Consequence of `.claude` joining `RUNTIME_NAME_DENYLIST`

A workspace-level `.claude/` directory is now **silently dropped** from bundle
publish, install seed, apply-update and env migration, and it is listed in the
generated `.gitignore` as `workspace/.claude`. It is engine runtime state; the
canonical home for skills is the top-level `skills/` folder. An existing
`.claude/` in a live workspace is *not* deleted — the apply-update stale-prune
sweep skips denylisted names — it simply never travels again.

> This is unrelated to the repository's own `.gitignore` negations for the Local
> Agent Kit scaffold's `.claude/`, which are repo-hygiene rules about kit
> **content**, not workspace classification.

---

## Rollout Notes

- **Existing environments need a rebuild.** env-core ships in the per-environment
  `/app/core` copy, so an environment created before this feature has no
  projection and no `/config/skills`. Until `/rebuild-env` (or the admin bulk
  rebuild) runs, its Skills card shows `adapter_error` with "Rebuild the
  environment to enable skills." Refreshing does not help.
- **Operators upgrading need a new compose mount.** Phase 3 adds
  `SKILL_STORAGE_DIR` (`/app/data/skills`) and the compose volume
  `${HOST_SKILL_STORAGE_DIR:-./backend/data/skills}:/app/data/skills`.
  `/app/data` was **previously unmounted**, so without this mount every published
  skill snapshot and archive vanishes on the next backend container recreate —
  and with it the content behind every catalog install.
- **Four migrations** land across Phases 2 and 3; see
  [agent_skills_tech.md](agent_skills_tech.md).
- **All three env templates** (`general-env`, `python-env-advanced`,
  `platform-knowledge-env`) already mount `claude_sessions:/root/.claude`
  read-write, so the projection target is writable everywhere.

---

## Out of Scope

- `cinna skills publish|install` CLI verbs and desktop-kit integration beyond the
  layout/guide changes.
- Consumer-authored skills that survive a bundle apply-update (would need a
  `skills/` merge branch like the plugins tree).
- `visibility=users` allowlist grants on skill packages.
- OpenCode hot-reload of plugin skills without a server relaunch.
- Skill usage analytics from `skill` tool events.
- Skill-level `hooks` / `agents` parity for OpenCode.
