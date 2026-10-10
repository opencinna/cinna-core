---
feature: desktop_agents
domain: application
one_liner: "Lets people share an agent that runs on their computer through the catalog: each is a bundle backed by a git repo on the Cinna server, with access grants and one version per accepted share."
docs:
  tech: desktop_agents_tech.md
affects: [agent_bundles, agent_git_versioning, local_agent_kit, realtime_events, user_roles]
---
# Desktop Agents in the Catalog

## Purpose

A desktop agent is a catalog bundle that **runs on your computer** (`runs_on='desktop'`) and whose
content store is a git repository hosted by Cinna core. People on one instance share an agent folder
from Cinna Desktop (or the CLI) without setting up an external git host; others get it, update it and
share changes back. Core stores each agent as a bare git repository and serves it over git smart HTTP
at `/git/<repo_id>.git`. Clients talk to it with ordinary git; the user never sees git words. Core
only hosts, checks and announces: it does not run these agents, and it has no web editor.

Every accepted share becomes a **version** (a bundle revision), so a desktop agent shows up in the
catalog next to agents that run in Cinna, with the same access model, listing and history. Cloud
install paths do not apply to it: they answer 409 `desktop_only` ("This agent runs on your computer in
Cinna Desktop."). See [Agent Bundles](../../agents/agent_bundles/agent_bundles.md).

## What the user sees

| Verb | What happens |
|------|--------------|
| Get | Bring a catalog agent onto the user's machine |
| Update | Bring in what teammates have shared since |
| Share changes | Send the user's changes back (needs `edit`); each accepted share is "Version N" |
| Suggest | Propose changes without edit access. **Deferred** |

Also deferred: the editing lease, cloud twins and mirrors of external marketplaces. Mirrors ship only
as groundwork: the `mirror` kind and its columns exist, and any push to a mirror is refused with 403.

## Creating a desktop agent

`POST /api/v1/catalog/desktop-agents` (any signed-in user) with the kit slug, display name,
description, the agent's manifest id, visibility and optional grants. Core:

- checks the slug (kit pattern `^[a-z0-9][a-z0-9-]{1,62}$`, unique among desktop agents; 409 "An agent
  called <slug> is already in the catalog.") and the manifest id (unique; 409 "This agent is already in
  the catalog. Share changes to it instead.");
- mints the `bundle_id` as `<reversed host>.<8 hex of the manifest id>`, with a `-2`, `-3` suffix on
  collision;
- creates the bundle (listed, visibility `private` unless given), a 1:1 git repo, the bare repository
  on disk and the grants, in one transaction. The creator becomes a maintainer.

The new agent has no version until the first share.

## Access model

Desktop agents use the catalog access model of bundles: visibility (`private`, `users`, `public`) plus
`is_listed`, and grants that name a user or every user of a role (`agent-user`, `agent-developer`,
`admin`) at a level.

- **Levels**: `use` (get), `edit` (also share changes), `maintain` (also edit grants and visibility,
  remove the agent).
- **Effective access** is the highest level among the grants that match the caller.
- **Private by default**: only the creator can see it until a maintainer widens it.
- **Maintainers are always people**, and an agent keeps at least one (409 otherwise).
- **Superusers** see every agent (`scope=all`), manage grants, remove an agent and clone any repo
  over git. Pushing still needs `edit` like everyone else.

Grants are managed through `GET/PUT /api/v1/catalog/items/{uuid}/grants`, the same routes every
catalog item uses.

## Push policy

A push is accepted only when all of these hold, enforced by a `pre-receive` hook on the server:

- Only the `main` branch exists; no force pushes, no deletes.
- No path matching a kit `never_travels` entry (`credentials/`, `app-data/` and the single file
  `.claude/settings.local.json`, the agent's Claude permission settings) and no secret file
  (the kit's `secret_files` rule); path matching is case-insensitive. An entry ending in `/` is a
  folder, any other entry is one exact file. A version whose tree still contains `.claude/settings.local.json` is refused even if the change
  did not touch it (same as `credentials/` and `app-data/`); deleting it in a new version is accepted; repos that already track it are not migrated, the desktop's next share
  removes it. Reason: a teammate with edit access could otherwise widen what runs without a
  permission prompt on every member's machine.
- No symlinks.
- No file over 10 MB and no tree over 200 MB; a request body over 256 MB is refused with 413.
- `cinna-agent.json` exists, validates against the kit schema, and its `contract_version` has the
  server's major version (a newer or older major is refused; a newer minor is accepted with a warning;
  a folder with no `contract_version` is accepted).

A refusal is one plain sentence that clients show verbatim, written in the user vocabulary (never
"branch", "commit", "push" or "SHA"). Examples: "This change includes credentials/, ... which never
leaves your computer.", "This change includes the agent's Claude permission settings
(.claude/settings.local.json), which never leave your computer.", "The file X is larger than 10 MB, too big to share with the team.", "This
agent has no cinna-agent.json, so it can't be shared.", "Only the agent's main version can be shared
to the catalog." If the policy cannot run, the server answers with "The catalog can't
accept changes right now. Ask your admin to look at the Cinna server." (503). Exact sentences live in
`backend/app/services/library/push_policy.py`.

A push needs `edit` on the agent; without it the transport answers 403 "You can't share changes to this agent.".
Without `use` (and not a superuser) a clone gets "You don't have access to this agent.".

### Identity is bound to the agent

Every desktop agent is bound to the `id` in its `cinna-agent.json` when it is created. A push whose
manifest `id` differs (compared case-insensitively) is refused with "This agent's identity changed;
share it as a new agent instead.". The hook stays free of database access: core passes the bound id
to `receive-pack` in `CINNA_BOUND_MANIFEST_ID`.

## Versions

Each accepted share adds one revision to the bundle (origin `share`), recorded after the push is
accepted:

- **Release notes** are the commit messages since the previous version.
- **Required credentials** come from the manifest's `credentials[]` as user-provided slots; a bad
  entry is skipped and logged.
- **Skills summary** lists the agent's own `skills/*/` (authored) and the catalog skills it pins by
  reference (`catalog_skills[]`).
- The description follows the manifest.

If the bookkeeping loses a race, that push gets no revision of its own and the next version's notes
cover the gap. Revisions list with "Version N" (no free-text label).

## Events

Each item carries its latest version and who shared it ("Last shared <date> by <name>").

After an accepted push, core emits `catalog.updated` (`kind`, `uuid`, `id`, `revision_number`,
`head_sha`) to every reader's own user room. When a grant change gives people access they did not have,
they get `catalog.granted` (`kind`, `uuid`). Both are refresh hints; `GET /api/v1/catalog/items` is the
recovery path. See [Event Bus](../realtime_events/event_bus_system.md).

## Listing, editing and removing

`GET /api/v1/catalog/items` lists every agent (both kinds of runtime) and skill the caller may see;
filter with `kind` and `runs_on`. A desktop item carries the clone URL, repo id, head, manifest id,
slug, engine and contract version. `PATCH /catalog/items/{uuid}` (maintainer) changes display name,
description, visibility and listing.

A maintainer or superuser can remove an agent (`DELETE /catalog/items/{uuid}`). It is refused (409
"<n> person/people use this. It can be removed once nobody does.") while other people have it. The bare
repository moves to `.deleted/` inside the git volume for the operator to purge. There is no separate
admin page: the catalog's admin view covers it.

## Plaintext on the server (by design)

Trees are stored as plaintext git objects on the server and readable by the server operator. The
zero-knowledge guarantee of [App Sync](../app_sync/app_sync.md) does not apply to them. The secret
rules above are what keeps keys and credentials out.

## Integration points

- [Agent Bundles](../../agents/agent_bundles/agent_bundles.md): a desktop agent is a bundle with
  `runs_on='desktop'`; access, listing, revisions and delete use the bundle model; cloud paths refuse it.
- [Git-Backed Agent Versioning](../../agents/agent_git_versioning/agent_git_versioning.md): the repo is
  a remote like any other; the shared tree format is tree v3 (the kit folder as-is).
- [Local Agent Kit](../local_agent_kit/local_agent_kit.md): contract `shared_remote`, the policy data
  (`never_travels`, `secret_files`) and the manifest schema the hook validates against.
- [Event Bus](../realtime_events/event_bus_system.md): `catalog.updated`, `catalog.granted`.
- [User Roles](../user_roles/user_roles.md): role principals use `User.role` values.
- [Plugin Marketplaces](../plugin_marketplaces/plugin_marketplaces.md): mirrors of marketplaces are planned.
- Deployment: [Nginx Setup](../../infrastructure/nginx_setup.md) `/git/` block and
  [Data Volumes](../../infrastructure/data_volumes.md).

## Changelog

- Desktop agents are catalog bundles; the separate team library, its `/library` API, `library.*`
  events and admin page were replaced. Existing shared agents were converted: grants map
  `read/write/maintain` to `use/edit/maintain`, an everyone grant became `public` visibility, and an
  agent that already had a head got Version 1. An agent whose stored manifest id was missing or
  duplicated got a fresh one and refuses pushes until shared again as a new agent.
