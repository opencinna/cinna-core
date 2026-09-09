# Agent Addons

**Addon** is the user-facing umbrella over the two things an agent carries beyond
its prompt: **plugins** (`AgentPluginLink` rows — marketplace, bundle, or skills
catalog) and **agent skills** (`skills/<name>/SKILL.md` folders in its
workspace). Both existed before this feature. What did not exist is **one list
that agrees about them**.

The word is deliberately presentation-layer only. Backend tables, services and
these docs keep saying `plugin`, `skill` and `SkillPackage`; "addon" appears on
the tab title, in the Add-addon dialog, and on the projection endpoint. **A row
never says "addon"** — it says plugin or skill.

**Tech reference:** [agent_addons_tech.md](agent_addons_tech.md)

---

## Overview

### The problem this feature exists to solve

A skill installed from the catalog is *both* an `AgentPluginLink` and an entry in
the environment's skills index. Before this projection, the agent page showed it
**twice**: once on the Plugins tab, once on the Configuration tab's Skills card.
Two surfaces, two sort orders, two ideas of what was broken, and a user who had
to know which of the two to act on.

`GET /agents/{id}/addons` folds the two lists **on the server**, once, so the
agent page, the CLI (`cinna skills list`) and any later consumer see the same
list rather than each inventing its own fold.

### What replaced what

| Before | After |
|--------|-------|
| Agent page › **Plugins** tab | Agent page › **Addons** tab, one card: "Plugins and skills" |
| Configuration tab › **Skills** card | (removed — its content is in the Addons tab) |
| Admin › **Plugin Marketplaces** | Admin › **Addon Marketplaces** (same route, same feature id) |
| `visibility` `public` \| `private` on a skill package | `public` \| `private` \| **`users`** (per-user grants) |
| Marketplace format `claude` only | `claude` \| **`codex`** \| **`skills`** |

The Addons tab is visible to **consumers of a foreign install** too, read-only.
That is not a courtesy: the Skills card it replaces was the only place a consumer
could see what an installed agent can do, and the Plugins tab it also replaces
was developer-only. Removing both without adding the tab to the agent-user tab
set would have been a silent capability regression.

---

## The dedupe rule

Four rules, applied in order. Everything downstream — counts, sort, what the
row's verbs are — falls out of them.

1. **Every plugin link is one row.** `kind="skill"` for a catalog link — it
   wraps exactly one skill, and calling it a plugin would leak an implementation
   detail onto a row whose own caption reads "Installed from the skills
   catalog" — and equally for an installed **`skills`-format marketplace
   entry**, which is the same thing arriving by another road. `kind="plugin"`
   for everything else.

   **`kind` is the word, `source` is the route.** The noun follows the entry's
   *format*; install, toggle, upgrade and uninstall all key on `source` and on
   the link, and nothing routes on `kind`. Deriving the noun from the transport
   instead gave one entry two vocabularies — "plugin" in its aria-label, its
   actions menu and its uninstall sentence, "Skill" in the format fact right
   beside them.
2. **Every `source="local"` index entry is one row** — `kind="skill"`,
   `source="local"`. These are the folders the agent owns and can publish.
3. **Index entries belonging to a plugin are not rows.** An entry whose
   `plugin_ref` matches a link is attached to that link's row as one of its
   `skills[]`, carrying its own error / warning / `secret_paths` status. This is
   the rule that removes the double listing.
4. **An entry whose plugin has no link becomes an orphan row.** A plugin
   directory the engine still loads after its link was deleted is not hidden — it
   gets a synthetic, read-only row (`orphan=true`) so the user can see what the
   environment is actually loading. The next environment sync prunes the
   directory.

### Matching is on the *directory* identity, never a display name

A row's fold key is `<marketplace>/<plugin>` — the on-disk path segment under
`plugins/`, which is exactly what the index reports as `plugin_ref`. It is
deliberately not the display name: a catalog package's display name is
publisher-editable, so matching on it would make folding stop silently the first
time somebody renamed their package.

A marketplace link resolves both halves from its live plugin row and falls back
to the install-time snapshot columns; every other source reads the snapshot
columns only. Both halves are now written on every install path (see
[Where a name comes from](#where-a-name-comes-from-and-why-it-survives)).

### An orphan does not always know a marketplace

An index entry can carry **no `plugin_ref` at all**. Such an entry cannot be
attributed to any plugin, so it gets a degenerate orphan row of its own rather
than being lumped with the others under one empty key. That row has
`marketplace_name = null`.

**Consequence for anyone writing copy:** an orphan's sentence must read as *"the
engine loads this, nothing installed it"* and must never name a marketplace,
because it cannot always know one.

---

## Business Rules

### Status is a three-state dot, and `has_update` is not one of them

Precedence, resolved once on the server and once more by a single client helper
so no call site re-derives it:

1. `orphan` → **error** (`status_code="orphan"`)
2. link-level `source_unavailable` → **error** — a catalog link whose package or
   revision is gone, a marketplace link whose plugin row is gone, or a
   marketplace entry the last sync marked unsupported
3. the first skill carrying an `error` → **error**, with that skill's issue code
4. the first skill carrying a `warning` → **warning**, with that code
5. otherwise **ok**

`has_update` is deliberately **not** a status. It is a flag; colouring it would
make routine maintenance look like a fault. Disabled is not a status either — it
is the fourth dot state on the client, below error and warning, so a broken
plugin that also happens to be disabled still reads as broken.

### One list, sorted by attention — never grouped by kind

Rows sort by `(status, display_name)`: errors, then warnings, then alphabetical.
**Kind is a filter, not a sort key, and never a grouping.** A broken plugin and a
broken skill are equally urgent, and splitting the list in two would push one of
them off a capped preview. The umbrella lives at the tab; the distinction lives
on the row.

### The counts are row counts, and two of them overlap

- `plugins` — every `kind="plugin"` row, **including orphans**. It can therefore
  exceed the number of installed links, and that surplus is the point: it is what
  needs attention.
- `skills` — every `kind="skill"` row: catalog installs, installed
  `skills`-format marketplace entries, *and* the agent's own folders, because to
  a user those are one kind of thing with several origins. An installed
  `skills`-format entry therefore counts here and **not** under `plugins` — one
  row moves between the two buckets; its verbs are unchanged.
- `local_skills` — a **subset** of `skills`, not a third bucket beside it.
  `skills − local_skills` is the catalog half.

Skills folded into a plugin row are counted **nowhere** — they are not rows,
which is exactly the double listing this projection removes. And none of these is
the "Show all (N)" total: that is the length of the list.

### Capabilities are server replies, never client role checks

Three booleans, all computed on the server (memory:
`feedback_role_gating_in_capability_replies`):

- **`can_add`** — may this caller add an addon at all. Developer role, on an
  agent that is not a consumer install.
- **`can_manage`** (per row) — may this caller toggle, upgrade or uninstall this
  row. False on a foreign install, for a non-developer, on orphans, and **on
  every local skill** — a workspace folder has no link to toggle; its verb is
  `can_share`. A **bundle** row *is* manageable: "no uninstall for a bundle
  source" is a client rule read off `source == "bundle"`, not part of this flag,
  because the publisher owns what a bundle delivers.
- **`can_share`** (per row) — local skills only, and both halves are server-side:
  the caller may publish *and* the skill itself is clean.

### The skill half can fail without taking the plugin half with it

If the environment is asleep, its adapter errors, or it predates the feature, the
projection still returns **every plugin link row** with `skills=[]` and puts the
banner sentence in `skills_error`. A tab that blanks because an environment is
asleep is a worse answer than a tab that says so. When the list is *also* empty,
the alert replaces the empty state, so the card never asserts "nothing here" over
a failed read.

### Where a name comes from, and why it survives

A link's directory identity is snapshotted at install time on **every** source —
marketplace included, as of this feature — and backfilled for existing rows.
Before that, a marketplace link carried no snapshot at all and read its names off
the live plugin row.

That worked right up to the moment the live row disappeared. `plugin_id` is
`ON DELETE SET NULL`, so an admin deleting a marketplace — or an upstream repo
dropping an entry on the next sync — **orphans** the link rather than silently
uninstalling somebody's plugin. Without the snapshot such a link had no name
left: it rendered as an unknown plugin, and the skills the environment still
loaded from its directory landed on a *second*, synthetic row. One directory, two
rows, neither of them nameable.

Two more mechanisms keep the names true as the world moves: a marketplace rename
follows through to its live links, and an orphaned link **re-attaches** when its
entry reappears upstream.

**Re-attaching requires the repository, not just the name.** A name is unique at
any one instant but freely transferable — the unique index frees a marketplace
name exactly when its holder is deleted, which is the very event that orphans the
links, and every sync reassigns a marketplace's name from the repository's own
`marketplace.json`. So a third-party repo can legitimately take a deleted
marketplace's freed name. The age guard alone does not stop that: it only rejects
a marketplace row *younger* than the link, and a rename goes the other way — an
**older** marketplace can acquire the freed name, adopt the orphans, and from the
next environment sync deliver code from a repository the agent's owner never
chose.

So an install now snapshots **the git URL it was actually made from**, and
adoption requires all three of: the directory identity (both name snapshots), the
same repository, and the original age guard. Consequences worth stating plainly:

- **A link orphaned before this shipped never re-attaches.** It has no recorded
  URL, and a missing URL fails closed — "unknown" must not read as "matches". The
  backfill could only read a URL from a marketplace row that was still alive, and
  an already-orphaned link has none. Those rows stay named, keep reporting
  `source_unavailable`, and their remedy is uninstall and re-install.
- **URL matching is narrow on purpose.** Only SSH→HTTPS for the three public
  hosts (GitHub, GitLab, Bitbucket) and a trailing `/` or `.git` are normalised.
  Host casing, `http` vs `https`, a `user@` prefix and SSH on any other host all
  mean "no match" — failing toward a manual re-install, which costs an
  administrator one action, where a false match would let somebody else's code
  into an agent's container.
- **All three conditions are required, so "same repository" is necessary and not
  sufficient.** What is adopted: an entry that disappeared upstream and came back
  on a marketplace row that was never deleted, and an older row legitimately
  renamed onto a freed name **whose repository matches the install's**. What is
  not: a *different* repository under a reused name (the security case), a link
  with no recorded URL, and — because the age guard still applies — an
  administrator who **deletes** a marketplace and **re-adds the same
  repository**, since the new row postdates the orphaned link. That last one
  re-installs by hand, like a pre-migration orphan.
- **Bundle and catalog links are untouched.** They have no upstream repository of
  their own and cannot be orphaned this way.

---

## User Flows

### 1. Seeing everything an agent carries

Agent page › **Addons** tab › the "Plugins and skills" card. One list, capped at
five rows with a "Show all (N)" sheet, sorted errors → warnings → alphabetically.
Each row is name · version · the mode icons it is enabled in (conversation,
building) · **author badge** (the marketplace manifest's
author, else the marketplace's owner — the official marketplace names itself
once at the top and leaves most entries blank — or the catalog package's
publisher; the same badge the Add addon list
shows, so an entry reads the same before and after install), a status dot with
the server's own sentence, an update flag when one is available, and the `⋯`
menu.

A **local** row — one of the agent's own `skills/<name>/` folders — says two
things no other source can. **Local** is a badge, taking the slot `author`
fills on every other row: a list merging four origins has no unmarked default,
and a local skill has neither a marketplace nor an author to put there.
**Published** is a `RowFlag` beside the update flag once the skill has a package
behind it (`published_package_id`) — a glyph rather than a third badge, because
the title line is capped at the two §2 grants by exception and "it is in the
catalog" is a passive per-row fact, which is exactly what a flag is for. The two
are not alternatives: a published skill is still a local one, and both are
unconditional once true. An **absent** version renders nothing, like every other
absent fact — the publisher is not left guessing, because the Share dialog fills
one in and says so (see
[agent_skills § Versions live in the header](../agent_skills/agent_skills.md#versions-live-in-the-header-and-publishing-is-what-writes-them)).
Details repeats the `Local` badge and states the published fact and the missing
version in words. **The row itself opens Details** — click, or Enter / Space — a read-only
dialog with the facts (source, format, marketplace, a link to the original
repository when the marketplace data names one, install time, modes with their
icons, the commit hash as a click-to-copy value), and
what it ships: a single skill's `SKILL.md`, **rendered as markdown**, inline; a
plugin's several skills as rows, each opening its own skill dialog ("Part of the
plugin X") with that skill's facts and rendered `SKILL.md`. The source glyph,
the published glyph and the info tooltip are gone from the row; those facts
live in Details. The row carries **no per-mode toggle**: the modes are chosen at
install time and read in Details. **Refresh is offered to every viewer**,
including a consumer of a foreign install — the read gates on access, not on
role, and a stale index nobody can re-read is a dead end.

### 2. Adding one

The card's **Add addon** action opens a two-step dialog that searches plugin
discovery and the skills catalog **together**, then picks the modes to enable.
Both halves end in the same place — an `AgentPluginLink` plus the ordinary plugin
sync — so disable-without-delete, the amber failure banner and
prune-on-uninstall all work with no addon-specific transport.

Each result row carries the version, an **author badge** (the plugin manifest's
author, or the package's publisher) and a **Details** button opening a
read-only dialog — for a catalog package with its latest revision's `SKILL.md`,
rendered. What the agent **already carries is left out** of the results: it is
a row on the card behind the dialog already.

The modes step is titled by the chosen entry — its name linked to its
repository (marketplace) or catalog page, its version badge, its description —
and carries one note: a plugin meant only for developing the agent, not for
running it, belongs in **building mode alone** — keeping it out of conversation
mode improves that mode's performance and the quality of its answers.

An entry the last marketplace sync marked **unsupported** is shown but not
installable, with the reason sentence. Hiding it would make an admin's
half-broken marketplace look empty.

### 3. Managing one

Toggle, upgrade and uninstall are the **existing plugin mutations**, unchanged —
the Addons tab calls exactly what the Plugins tab called. There are no addon
verbs to keep in sync with them. Per-mode enable is still on the link and on
its update route, but the row offers no control for it: modes are chosen when
the addon is added.

### 4. Sharing a local skill

A local skill's row offers **Share…**, or **Update published skill…** once it has
been published from this agent before. The distinction carries business meaning:
a re-publish appends a revision to an immutable package rather than editing it.

The dialog chooses a visibility — **Everyone**, **Only me**, or **People** — and,
for People, picks users. See [Sharing with named people](#6-sharing-with-named-people).

**The publisher is not asked to invent a version or an id.** On open, the dialog
reads `GET /agents/{id}/skills/{name}/publish-preview` — the same derivations the
publish will perform — and shows both: the version field arrives filled in
(overtypable, and an emptied field simply lets the server derive it again), and a
line above the form names the package id and the revision number this publish
would become. When the derived id collided with another publisher's, the line
says so rather than letting a suffixed id appear unexplained. Release notes stay
optional, and the **Advanced** disclosure still holds the one immutable field —
now placeholdered with the id that will actually be used, so the field reads as
an override rather than a requirement. Sharing then writes the version into the
skill's own `SKILL.md`.

### 5. Publishing — what actually gets published

**Publishing reads the cloud agent's environment workspace, never a local
folder.** The service resolves the agent's `active_environment_id` and packages
the `skills/<name>/` it finds in *that* workspace. The environment does not need
to be running — the workspace is bind-mounted on the host — but it must exist and
have been materialised.

The consequence is quiet and worth stating plainly: **if a local edit has not
travelled to the cloud yet, publish packages the older cloud content**, and a
published revision is immutable — it cannot be replaced, only appended beside.
Push first, then publish.

Publish refuses on **four** things, all before anything is written — three about
the content, one about the request contradicting itself:

| Refusal | Code | Status |
|---------|------|--------|
| the skill's index entry carries an `error` | `skill_invalid` | 422 |
| the folder contains a file **named** like a credential | `skill_contains_secrets` (with paths) | 422 |
| the folder exceeds 16 MB | `skill_too_large` | 422 |
| the call names people to share with while the publish would leave the package's visibility something other than `users` | `grants_require_users_visibility` | 409 |

The last one is [described in full below](#6-sharing-with-named-people); "before
anything is written" is load-bearing for all four, because a published revision is
immutable and cannot be taken back.

The size test is strictly greater-than, so a folder of exactly 16 MB publishes.
The 64 KB `SKILL.md` body limit is a **different** thing: it produces a
non-blocking `oversized` warning that the publish path never reads. A long body
publishes.

**The secret gate is filename-only.** It matches exact names (`credentials.json`,
`id_rsa` and friends), globs (`*.pem`, `*.key`, …) and the dotenv rule — all on
the basename. **Nothing reads file contents.** A token pasted inside `SKILL.md`,
a customer name in a fixture, an internal hostname in a script: all publish
cleanly. The only file the validator opens at all is `SKILL.md`, and only to
parse its frontmatter. Read the folder yourself before calling it ready — it is
copied verbatim into someone else's agent.

### 6. Sharing with named people

`SkillPackageVisibility` gained **`users`**, mirroring the bundle access-grant
pattern.

- **A package is `private` by default** and the publish call only overwrites
  visibility when one is supplied. A bare publish therefore succeeds, prints a
  catalog URL, and shares the skill with **nobody**. Naming an audience is a
  separate, deliberate act.
- A grant confers **catalog visibility only**. Archive download is authorised by
  "this environment holds an install of this revision", so **revoking a grant
  never breaks a running install** — identical to bundles. The card's copy has to
  say so: removing a person hides the skill from their catalog, it does not
  uninstall it.
- A grant respects `is_listed` exactly like a public package does. Delisting
  beats a grant; the publisher always sees their own.
- Grants are **additive on re-publish** — publishing never revokes. Revoke is its
  own verb.
- Changing visibility away from `users` **keeps** the grant rows, inert and
  reversible, same as bundles.
- Granting to an unknown address is a 404 before anything is written, so a bad
  email fails the whole publish rather than half-creating a package. Address
  matching is case-insensitive on both sides (memory:
  `project_email_case_sensitivity_bug`).

**A publish that names people must also leave the package on `users`.** A grant
row is consulted under `users` visibility and nowhere else, so writing one under
any other visibility does not share the skill — it leaves a latent row that a
later flip to `users` turns into access nobody asked for. The publish call now
refuses that combination outright, with **409 `grants_require_users_visibility`**:

> Sharing a skill with named people only takes effect when its visibility is
> 'users', and this publish would leave it '<effective>'. Set the visibility to
> 'users', or publish without the email addresses.

Three details decide what is refused and what is not:

- **It keys on the *effective* visibility** — the one supplied, else the existing
  package's current value, else `private` for a package that does not exist yet.
  `visibility=None` is the documented "leave it alone" re-publish shape, so
  re-publishing an already-`users` package with more addresses and no visibility
  field is **allowed**; adding addresses to a `public` package without restating
  the visibility is refused.
- **It keys on the *resolved* people**, not on the raw address list. Naming only
  the publisher's own address resolves to nobody and stays the documented no-op it
  has always been, rather than becoming a refusal.
- **The standalone grant routes stay permissive.** Granting three colleagues on a
  still-`private` package and *then* flipping it to `users` is ordinary
  preparation, and the whole request there is "let this person see it", stated per
  person. The asymmetry is deliberate: publish refuses a request whose stated
  visibility contradicts the addresses it carries as a *side effect*; the grant
  route answers a request that states nothing else.

Web clients never reach the refusal — `ShareSkillDialog` drops the address list
unless the chosen visibility is People — but a second caller (the CLI, a script)
is now told, and told before a revision exists.

### 7. Publishing from the CLI

`cinna skills list <agent>` renders this same projection; `cinna skills publish
<agent> <name>` calls the same publish route. Both travel through the account
**API proxy**, not as direct platform calls — an account token's `sub` is a
`CLIToken` row id, not a user id, so a direct call could never satisfy
`CurrentUser`. The proxy forwards to the same handlers, so ownership and the
developer gate still apply.

---

## Marketplace formats

An addon marketplace is registered in one of three formats, chosen at create time
and validated as a closed set — an unknown value is a 422, never a silent
fallback to the Claude parser.

| Format | Catalog file | One entry is | Notes |
|--------|--------------|--------------|-------|
| `claude` | `.claude-plugin/marketplace.json` | a plugin | unchanged |
| `codex` | `.agents/plugins/marketplace.json` | a plugin | `.codex-plugin/plugin.json` read for metadata; `policy` stored verbatim, **not enforced**; `.app.json` connectors ignored |
| `skills` | none — a directory walk | **one skill** | `skills/<name>/SKILL.md`, or `<name>/SKILL.md` for a repo that is nothing but skills |

Codex is a **marketplace format only**. Containers keep running Claude Code and
OpenCode; a Codex plugin is normalised container-side into a synthesised
`.claude-plugin/plugin.json`. There is no Codex engine.

**An entry this platform cannot install is flagged at sync, not at install.** The
sync writes `supported=false` plus a stable `unsupported_reason` code, discovery
returns the row so the user can see *why*, and an install attempt is refused with
`409 plugin_unsupported`. Five reasons exist: published to npm, app-connectors
only, no valid `SKILL.md`, an unfetchable source kind, and a path pointing
outside its repository. The last is per-entry — one unsafe path flags that entry
rather than failing the whole sync.

See [plugin_marketplaces](../../application/plugin_marketplaces/plugin_marketplaces.md)
for the admin surface and the sync mechanics.

---

## Error Handling & Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| Environment asleep / adapter error / pre-feature container | Plugin rows still return; `skills_error` carries the banner; local rows come from the cache if there is one |
| Catalog link whose package or revision was deleted | `status=error`, `status_code=source_unavailable`; uninstall offered, upgrade hidden |
| Marketplace link whose plugin row is gone | Same `source_unavailable`; upgrading answers **409 `source_unavailable`** with a sentence telling the user to uninstall |
| Plugin directory present, link gone | Orphan row, read-only, "the next environment sync removes it" |
| Index entry with no `plugin_ref` at all | Its own degenerate orphan row, no marketplace name |
| A local skill and a plugin skill share a name | The index flags both `shadowed`; no dedupe across sources — they are different files |
| Installing an entry flagged unsupported | `409 plugin_unsupported` with the reason sentence; the dialog marks it uninstallable before the click |
| Marketplace catalog unreadable at sync | **422 `marketplace_catalog_unreadable`**, marketplace `status=error`, and **nothing is deleted** — stale-row deletion lives past the parse, so a temporarily broken upstream repo cannot empty an admin's entry list |
| A `skills` repo with zero valid `SKILL.md` folders | Treated as an unreadable catalog — nothing to install and nothing to list |
| Publishing with `users` and no people | Allowed; the package is effectively private until someone is added |
| Publishing with people named while the visibility would end up `public` or `private` | **409 `grants_require_users_visibility`**, before anything is written. Keyed on the *effective* visibility, so a re-publish of an already-`users` package that omits the field is allowed |
| Publishing with only the publisher's own address named | Unchanged no-op — it resolves to nobody, so nothing is written and nothing is refused |
| An orphaned link whose marketplace name is taken by a different repository | Not adopted: re-attach requires the snapshot repository URL to match. A link with no recorded URL (orphaned before that column existed) is never adopted at all — uninstall and re-install |
| A granted user uninstalls, is revoked, reinstalls | Refused — the catalog no longer shows it. Existing installs elsewhere are untouched |
| Consumer of a foreign install opens the tab | `can_add`, `can_manage`, `can_share` all false; rows render read-only, with details and Refresh |
| Bundle apply-update drops `skills/` | Local rows disappear on the next index refresh; catalog and marketplace rows survive. Unchanged behaviour, now visible in one place |
| Container cannot write a synthesised `plugin.json` | Reported `manifest_write_failed` and kept out of `settings.json` — **unless the directory still holds a loadable manifest**, in which case the previous working revision keeps running rather than being punished for a transient disk error |

---

## Integration Points

| Feature | How addons touch it |
|---------|---------------------|
| [agent_plugins](../agent_plugins/agent_plugins.md) | Every link row, and every mutation. The tab calls the existing install / uninstall / upgrade / toggle routes unchanged. The `plugin_id` cascade fix and the install-time name snapshots landed here |
| [agent_skills](../agent_skills/agent_skills.md) | The cached skills index is the projection's second input; `visibility=users` and the grant table extend the skills catalog; the Skills card moved into this tab |
| [plugin_marketplaces](../../application/plugin_marketplaces/plugin_marketplaces.md) | The `codex` and `skills` formats, the `supported` / `unsupported_reason` verdict, and the admin surface's rename to **Addon Marketplaces** |
| [agent_bundles](../agent_bundles/agent_bundles.md) | `BundleAccessGrant` is the pattern `SkillPackageAccessGrant` mirrors, down to "revoking hides, it never uninstalls". Bundle-sourced rows appear in the list, manageable but not uninstallable |
| [agent_environment_core](../agent_environment_core/agent_environment_core.md) | The container-side manifest normaliser that makes a Codex plugin or a bare-skill directory loadable by both engines |
| [agent_prompts](../agent_prompts/agent_prompts.md) | `BUILDING_AGENT.md` gained "Designing around skills" and "Publishing a skill" — the building agent **prepares** a skill and tells the user to share it; it never publishes and has no tool that could |
| [local_agent_kit](../../application/local_agent_kit/local_agent_kit.md) | Guide 08 and the agent template carry the same two subsections for local builders; the `knowledge` rung's trigger now fires on a finished skill worth publishing |
| [cinna_cli_integration](../../application/cinna_cli_integration/cinna_cli_integration.md) | `cinna skills list` / `cinna skills publish`, through the account API proxy |
| [user_roles](../../application/user_roles/user_roles.md) | `can_add` is the developer role plus "not a consumer install"; every capability is a server reply |

---

## Rollout Notes

- **Existing environments need a rebuild.** env-core ships in the per-environment
  `/app/core` copy, so the manifest normaliser and the `skills`-format copy step
  reach an environment only after `/rebuild-env` or the admin bulk rebuild.
- **Release note — a `skills`-format install onto a stale environment looks
  healthy and does nothing.** The link is created, the manifest is delivered, the
  install reports success and the row renders `ok`; but a pre-feature container
  copies the tree to the wrong place and synthesises no manifest, so nothing is
  indexed. Same class of failure as the stale env-template MCP 404 — new backend,
  old container, and no surface says so. **Rebuild before installing a
  `skills`-format entry, and before publishing a bundle from an agent that holds
  one.** The install case is recoverable — rebuild and the next sync lays the
  files down correctly. **Publishing is not:** a bundle revision snapshots the
  workspace as it stands, bundle plugin entries are hard-coded
  `plugin_type: "claude"` and are never re-shaped by the container, and a revision
  is immutable. A bundle published from a stale container therefore ships the
  broken layout to every consumer permanently, and no consumer-side rebuild fixes
  it. Gating the install on a container-reported manifest-version marker, so the
  backend can refuse rather than half-deliver, is a known follow-up.
- **One schema migration and two data migrations.** The first adds the grant table
  and the two `supported` / `unsupported_reason` columns; the second backfills
  marketplace links' name snapshots; the third adds
  `agent_plugin_link.snapshot_repository_url` and backfills it from the live
  marketplace rows. See [agent_addons_tech.md](agent_addons_tech.md).
- **No new environment variables and no new compose mounts.** The skills
  catalog's `SKILL_STORAGE_DIR` mount was introduced by
  [agent_skills](../agent_skills/agent_skills.md) and is unchanged.

---

## Known Gaps

- **Latent grant rows written before the visibility guard existed are not cleaned
  up.** The write path is closed — a publish naming people under a non-`users`
  effective visibility is now refused with 409 (see
  [Sharing with named people](#6-sharing-with-named-people)). What remains is
  history: rows created by pre-fix publishes are still in the table, still inert,
  and would become real access the moment such a package is flipped to `users`.
  No migration removes them, so a database that ran an earlier build can hold
  grants nobody remembers asking for. Revoke is a per-row verb and the package's
  grants card lists them, which is the only remedy there is.
- **A refused entry can be truncated out of the Add-addon list.** Discovery sorts
  installable entries first and *then* cuts the page (default 30), so an entry
  flagged `supported=false` can fall off the server-side page before the dialog
  ever sees it — which quietly contradicts the "never hide an unsupported entry"
  rule the dialog itself honours. Narrowing the search brings it back. Recorded,
  not fixed.
- **A marketplace rename transiently splits one directory into two rows.** Between
  the rename and the next environment sync, the index still reports the old
  `<marketplace>/<plugin>` while the link reports the new one, so the same
  directory renders as a real row plus an orphan. The next sync heals it; nothing
  is lost and no action is offered on the orphan.
- **Cross-repo follow-up in `cinna-cli`.** Its `--grant` gate is now marginally
  *stricter* than the server: it demands an explicit `--visibility users`, where
  the server would accept `--grant` on a package that is already `users`. The
  refusal is still correct advice, just narrower than the rule; its inline comment
  claiming "the server stores a grant unconditionally" is now false. That
  repository is versioned separately and is not changed here.
- **A publish whose final commit fails leaves an orphan revision.** The revision
  row commits before the package row; the window is documented in code and
  unclosed.
- **Nothing verifies that `docs/local_agent_kit/` and its `platform-knowledge-env`
  snapshot agree** — no test and no CI step. The kit's own test suite resolves the
  kit directory to `docs/local_agent_kit/` first and only falls back to the
  snapshot, so a drifted snapshot passes every test while `/agent-start` serves
  stale bytes. Keeping the two in step depends on somebody running
  `make sync-platform-knowledge`.
- **Two Phase 2 deviations from the plan's UI specification**, both deliberate:
  Refresh is rendered for every viewer rather than developers only (the read
  gates on access, not role), and `SkillContentDialog.tsx` was deleted rather
  than kept once its body moved into the shared `SkillContentBody`.
- **Three Phase 3 UI items deferred**, accepted in review rather than fixed: the
  marketplace entries list is a hand-rolled `Table` instead of the shared
  `DataTable` the specification named; `Common/StatusDot` still is not extracted
  although the tone-dot now has three consumers; and the admin plugin-detail page
  has no query-error branch.

---

## Out of Scope

- Sharing via roles, groups or workspaces (a project-wide primitive that would
  serve bundles too).
- A model-invocable publish tool — publishing stays a human act, and a tool would
  need agent-token scoping first.
- Codex `policy` enforcement and Codex `.app.json` connectors.
- Marketplace auto-sync on a schedule.
- `cinna skills install`, and installing catalog skills into a local kit
  workspace.
- Consumer-authored local skills that survive a bundle apply-update.
- Skill usage analytics.
