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
are preserved verbatim. Three of them are *interpreted* by the platform:
`user-invocable: false` hides the skill from the slash-command popup,
`disable-model-invocation: true` records that the model may not reach for it
unprompted, and **`version`** is read into `SkillEntry.version` — the only
optional key the platform also **writes** (see "Versions live in the header"
below).

| Field | Rule |
|-------|------|
| `version` | optional; free text on **one line**, at most 64 characters. Nothing validates its *shape* and nothing refuses a skill without one, but a newline or any other control character is refused — the catalog writes this key back into the block, where a newline would inject top-level keys. A header longer than 64 is truncated on read; a request body longer than 64 is a 422. An unquoted `1.0` arrives as a float and is stringified, so the header and the row say the same thing |

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

- **"Plugins and skills" card** — agent page › **Addons** tab. Rows show name,
  version and author badges and a status dot; clicking the row opens Details
  with the `SKILL.md` viewer. Capped at five rows with a
  "Show all (N)" sheet; sorted **errors first, then warnings, then
  alphabetically**, so a row that needs a decision is always in the preview. The
  card is rendered for consumers of a foreign install too — knowing what an
  installed agent can do is a *use* capability.
  **This replaced the Configuration tab's Skills card**, which showed skills
  alone and so listed a catalog install twice, once here and once on the
  Plugins tab. The unified list and its dedupe rule are
  [agent_addons](../agent_addons/agent_addons.md).
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

Clicking the card's row opens a dialog with the `SKILL.md` rendered as markdown
(frontmatter split off). A plugin that
ships several skills lists them as rows, each opening its own dialog. The path
comes from the cached index, never from the requested name, so a
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

From the Addons tab's row menu (**Share…**, or **Update published skill…** once
this agent has published it before) — or with `cinna skills publish <agent>
<name>` — a developer publishes one named skill. The service reads the skill from
the **publisher's workspace on disk**, validates it, snapshots it atomically into
immutable storage and appends a `SkillPackageRevision`.

- A **suspended or stopped** environment publishes exactly like a running one —
  the workspace is bind-mounted on the host, so nothing needs waking.
- **The workspace read is the *cloud* agent's environment workspace**, resolved
  through `active_environment_id`. There is no local-folder branch anywhere in
  the publish path, so a stale local copy publishes the **older cloud content** —
  as an immutable revision that can only be appended beside, never replaced.
- The first publish creates the `SkillPackage` (reverse-DNS `package_id`,
  visibility **`private` unless a visibility is supplied** — a bare publish
  succeeds, prints a catalog URL and shares the skill with nobody); later
  publishes append revisions.
- **Three refusals, and only three:** the skill's index entry carries an `error`
  (`skill_invalid`), the folder holds a file *named* like a credential
  (`skill_contains_secrets`), or the folder exceeds 16 MB (`skill_too_large`,
  strictly greater-than — exactly 16 MB passes). The 64 KB `SKILL.md` body limit
  is a non-blocking `oversized` **warning** the publish path never reads.
- **The secret gate is filename-only.** Nothing reads file contents; the only
  file the validator opens at all is `SKILL.md`, and only for its frontmatter. A
  token pasted inside `SKILL.md` publishes cleanly.
- The dialog's success panel links to the catalog entry; it never opens another
  dialog.

### 5b. Reading a package in the catalog

The package route shows what the skill *is* before anybody installs it: the
Package card (publisher, version, installs, visibility, package id) beside a
`SKILL.md` panel rendered as markdown.

`SKILL.md` is one file, and a skill is a folder. The card's **Content** fact is
the rest of that answer:

- it names how many files the shown revision ships (`4 files`), and opens a
  Sheet listing every one of them — path, size, and a flag on anything the
  archive keeps the executable bit for;
- beside it, a **download** takes the whole revision as the same deterministic
  `.tar.gz` an agent installs, so what a reader inspects is what an agent would
  run. The reader may take it whenever they may see the package — visibility is
  the only gate.

The listing is paths and sizes, never contents: deciding whether to install a
skill needs to know that it ships three scripts and two references, and a
per-file viewer would turn the catalog into a general reader over other
people's published trees. Both surfaces follow the revision the Version fact
has pinned, so reading `v1` lists `v1`'s files.

The Sheet caps at 500 listed files; past that the file count and total size
still describe the whole revision, and the Sheet says so ("showing the first
500") rather than silently dropping the rest. The download is unaffected —
it always carries every file — so a reader who needs what the list could not
show still gets it by taking the whole folder.

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

### Reading the index can fail three ways, and each has exactly one remedy

The issue vocabulary above describes a skill. This one describes the *read* — why
the card has no index to show — and it is a separate list because the remedies
are not interchangeable:

| `skills_error` | What happened | What the user must do |
|----------------|---------------|-----------------------|
| `env_not_running` | The environment is asleep (`suspended` / `stopped` / `error`) | Refresh, or send a message — the wake re-reads |
| `adapter_error` | The container is not asleep and did not answer: a timeout, a transport failure, or any non-404 status **from an endpoint that exists** | Restart it, then refresh |
| `adapter_unsupported` | The container answered **404**: its `/app/core` predates the feature and has no `/config/skills` route at all | **Rebuild** it |
| `parse_error` | The container answered with something the backend could not read | Refresh to try again |

`adapter_error` and `adapter_unsupported` were one code until they were split,
and the split is the point. Both mean "the call failed on a container that is not
asleep", but **a restart re-runs the same image and can never add a route that
was never built into it** — only a rebuild replaces `/app/core` from the
template. While the two shared a code, every surface had to pick one remedy and
was therefore wrong half the time: the card told an unreachable environment to
rebuild, and the CLI told a pre-feature container to restart.

Two consequences follow, both deliberate:

- **The 404 is classified before the sleeping-status check.** A pre-feature
  container that also happens to be suspended reports `adapter_unsupported`, not
  `env_not_running` — otherwise the copy would say "refresh to wake it" and send
  the user round a loop the wake never breaks.
- **Only a 404 earns the unsupported code.** Any other status came from *inside*
  an endpoint that exists, and must not borrow the rebuild copy.

The rebuild has a verb of its own now: `cinna agent rebuild-env <agent>`, beside
the `/rebuild-env` session command and the environment card's rebuild action (see
[account_cli_workspace](../../application/cinna_cli_integration/account_cli_workspace.md)).

### An installed skill that contributes no skill is not healthy

An addon row of `kind="skill"` wraps exactly one `SKILL.md` by definition, so an
empty skill list on such a row is a contradiction: the link is correct, the files
are not there, and the model cannot load it. Such a row used to read **`ok`** —
the row was reporting the *link*, and the link was genuinely fine.

The row now reports the files as well, and how loudly depends on whether absence
is evidence:

| The index was… | Row status | `status_code` |
|----------------|-----------|---------------|
| read, and the skill is not in it | **error** | `not_materialized` |
| not readable (the read failed) | **warning** | `unverified` |
| never read — no environment, or no read yet | silent, as before | — |

Claiming "missing" while the container could not be reached would be the same
overreach in the other direction, which is why the middle case is its own code
rather than folded into either neighbour. `kind="plugin"` rows are **exempt**: a
plugin legitimately ships only commands or agents and contributes nothing here in
perfect health. See [agent_addons](../agent_addons/agent_addons.md).

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

### Versions live in the header, and publishing is what writes them

A skill's version is a line in its own `SKILL.md`, not a field of a database row
a reader of the folder cannot see. The publisher is never asked to invent one:

- **Deriving it.** One sentence: *the header's version, unless it has already
  been published — then the next one after the newest release.* So a hand-written
  `2.0.0` is honoured exactly once and then continued from; a skill with no
  version anywhere starts at `1.0.0`. The successor is the last run of digits
  incremented (`1.0.0`→`1.0.1`, `1.2`→`1.3`, `v3`→`v4`), because the field is
  free text and a strict semver parser would refuse most of those. A release
  with no digits at all (`2.0.0-beta`) gets `.1` appended rather than restarting
  the series — restarting at `1.0.0` would publish a *next* revision numbered
  below its own predecessor and stamp that into the author's header. `1.0.0` is
  reached only when there is no release to continue from. A version explicitly
  named in the publish body always wins and is never de-duplicated — two
  revisions may legitimately carry one version — but it must be a single line:
  a newline is refused at the request boundary, because this value is written
  into a frontmatter block where it would inject top-level keys.
- **Writing it back.** The resolved version is written into
  `skills/<name>/SKILL.md` on the publisher's workspace **before** the snapshot
  is taken, so the published bytes carry their own version and the next publish
  reads it back. It is a surgical one-line edit — the block is never
  re-serialised from the parsed mapping, which would rewrite the author's
  quoting and drop their comments. A write that cannot happen (a read-only
  workspace, a file with no fence) is logged and **not** fatal: the revision
  still carries the version, and its *stored frontmatter* then deliberately
  omits it, so the row never claims a version the snapshot does not have.
- **Showing it.** The index the addon row reads is built inside the container,
  and `app/core/` is copied out of the template at environment *creation* — so
  an environment made before skills carried a version would report none forever.
  The host backfills it for local skills from the same workspace file the
  publish reads, and never overrides a version the container did report — but
  only on an index *fetch*, so on those environments the version appears after a
  Refresh, a start sweep or a publish, never on a first page load. A row with no
  version renders no badge at all, exactly like every other absent fact; the
  detail dialog says "No version in SKILL.md" in words, and the Share dialog
  fills one in before the publisher has to think about it.

### Catalog identity and visibility

- `package_id` is reverse-DNS, unique on the instance, and **immutable** once
  published: a re-publish naming a different id is refused (`package_id_immutable`),
  never silently ignored, because every install and every container manifest
  references it.
- The **derived** id is `<reversed FRONTEND_HOST>.skill.<skill name>` — the same
  reversed-host prefix a bundle id gets, with a fixed `skill` segment as the
  thing that says which family it belongs to. It carries **no publisher slug**,
  because an id gets pasted into READMEs and the common case is the only person
  on the instance with a skill by that name. When it *is* taken, the publisher's
  own 8-hex slug is appended (then a counter) rather than the publish being
  refused with `package_id_taken` — a refusal whose only fix would be "invent a
  reverse-DNS name", asked of somebody who did nothing wrong. `GET
  /agents/{id}/skills/{name}/publish-preview` returns the id that will be used
  and whether it was disambiguated, so the Share dialog shows it before the
  press rather than after.
- A skill `name` is unique **per publisher**, not globally. Two people may both
  publish `pdf-report`; `package_id` disambiguates them. But one agent has a
  single `plugins/cinna-skills/<name>/` directory, so two publishers' same-named
  packages cannot coexist in one agent — that is a distinct `name_conflict`
  refusal, not "already installed".
- `private` means the publisher only, honoured literally — an administrator has
  no product reason to read an unshared skill body. The one bypass is keyed on
  *visibility*, not listing: an admin may still see a package that was public and
  has been delisted, so delisting is not a trapdoor that hides its own output.
- **`users` is the third visibility**, added by
  [agent_addons](../agent_addons/agent_addons.md): a per-user allowlist mirroring
  `BundleAccessGrant`. A grant confers **catalog visibility only** — archive
  download is authorised by "this environment holds an install of this revision",
  so revoking never breaks a running install. It respects `is_listed` exactly like
  a public package, and there is **no superuser bypass**: delisting beats a grant.
  A publish that names people while it would leave the package on any visibility
  other than `users` is **refused** (409) rather than writing rows that share
  nothing; granting through the package's own grant routes stays allowed at any
  visibility, since that is how a publisher prepares an audience before flipping.
  Grants are additive on re-publish, and survive a visibility change away from
  `users` (inert and reversible).
- The package **description** follows the skill's frontmatter until a publisher
  edits it in the catalog; after that a re-publish leaves the edited blurb alone.

### Install counts are computed, per person, excluding the publisher

There is no `install_count` column. An install is an `AgentPluginLink` row that
vanishes with its agent through `ON DELETE CASCADE`, which a stored counter
cannot observe — it would drift upward forever. The count is derived at
projection time, and it counts two things away:

- **The publisher's own agents**, entirely, so dogfooding does not inflate it
  (the same rule the bundle catalog uses).
- **Repeats by one person.** It is one per *user*, however many of their agents
  carry the skill. The question the catalog number answers is "how many other
  people adopted this", and one enthusiast with six agents is one adopter, not
  six — counting agents let a single consumer outvote a dozen real ones.

Which makes `Catalog installs 0` the correct answer for a publisher who has just
put their own skill into three of their own agents — and a baffling one on its
own. The same payload already carries `installed_in_agent_ids` ("which of *your*
agents have it", viewer-scoped and unaffected by the per-person rule, because it
answers a different question), so both catalog surfaces show it: the grid card
says "Used in N of my agents", and the package card carries **Catalog installs**
and **Used in my agents** as two facts, plus — for the publisher only, and only
when the publisher has installs of their own — the sentence that reconciles
them. A number
whose definition the reader cannot see is not a fact, it is a contradiction.

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
| Pre-feature container (old `/app/core`) | No `/config/skills` route → **404** → cache error **`adapter_unsupported`**; the card says "Rebuild the environment to enable skills." Neither refreshing nor restarting will ever fix it. Classified **before** the sleeping-status branch, so a pre-feature container that is also suspended still reports `adapter_unsupported` rather than `env_not_running` |
| Running container that does not answer (timeout, transport failure, non-404 status) | Cache error **`adapter_error`**; the card says "The environment isn't answering. Restart it, then refresh the skills." A rebuild is minutes of downtime that cannot help — the route is already there |
| An installed `kind="skill"` row that contributes no skill | Never `ok`. Index read and the skill absent → **error** / `not_materialized`; index unreadable → **warning** / `unverified`; index never read → silent. `kind="plugin"` rows are exempt, since a plugin may legitimately ship no skills |
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
| [agent_addons](../agent_addons/agent_addons.md) | The index is one of the two inputs to the addons projection; the Skills card moved into the Addons tab; `visibility=users` + `SkillPackageAccessGrant` extend the catalog; `cinna skills list|publish` |
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

- **Existing environments need a rebuild — not a restart.** env-core ships in the
  per-environment `/app/core` copy, so an environment created before this feature
  has no projection and no `/config/skills`. Until a rebuild runs
  (`/rebuild-env` in a session, `cinna agent rebuild-env <agent>` from the CLI,
  or the admin bulk rebuild), its Addons tab shows `adapter_unsupported` with
  "Rebuild the environment to enable skills." Refreshing does not help, and
  neither does restarting: a restart re-runs the same image, and only a rebuild
  replaces `/app/core` from the template.
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

- `cinna skills install` (the CLI installing a catalog skill into a cloud agent)
  and local install of catalog skills into a kit workspace. `cinna skills list`
  and `cinna skills publish` **do** exist — see
  [agent_addons](../agent_addons/agent_addons.md).
- Consumer-authored skills that survive a bundle apply-update (would need a
  `skills/` merge branch like the plugins tree).
- OpenCode hot-reload of plugin skills without a server relaunch.
- Skill usage analytics from `skill` tool events.
- Skill-level `hooks` / `agents` parity for OpenCode.
