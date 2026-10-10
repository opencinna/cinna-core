---
feature: team_library
domain: application
one_liner: "Hosts shared agents as git repositories on the Cinna server so a team can get, update and share changes to the same agent, with per-agent access grants."
docs:
  tech: team_library_tech.md
affects: [agent_git_versioning, local_agent_kit, realtime_events, user_roles]
---
# Team Library (Core Git Host)

## Purpose

The team library lets people on one Cinna instance share an agent folder with each other without
setting up an external git host. Cinna core stores each shared agent as a bare git repository on the
server and serves it over git smart HTTP. Clients (Cinna Desktop, the CLI) talk to it with ordinary
git; the user never sees git words. Core only hosts, checks and announces: it does not run the
agents in the library, and it has no web editor.

## The four-verb client story

Clients present four verbs. Only the first three exist today.

| Verb | What happens |
|------|--------------|
| Get | Bring a library agent onto the user's machine (or into their cloud account) |
| Update | Bring in what teammates have shared since |
| Share changes | Send the user's changes back to the library (needs `write`) |
| Suggest | Propose changes without write access. **Deferred** |

Also deferred: the editing lease, cloud twins (a cloud agent bound to a library repo with a service
principal) and mirrors of external marketplaces. Mirrors ship only as groundwork: the `mirror` kind,
its columns, an always-empty `mirrors` list in `GET /library`, and a 403 for any push to a mirror.

## Access model

Each library agent has a list of grants. A grant names a principal and an access level.

- **Levels**: `read` (get), `write` (also share changes), `maintain` (also edit grants and remove the agent).
- **Principals**: one user, every user of a role (`agent-user`, `agent-developer`, `admin`), or everyone.
  A role grant for `admin` also matches superusers.
- **Effective access** is the highest level among the grants that match the caller.
- **Private by default**: the creator gets `maintain` and only the people (or roles) named on
  create get anything else. Create without a `grants` list means only the creator can see it;
  nobody else can see the entry at any moment unless named. A maintainer widens it afterwards,
  including to everyone.
- **Maintainers are always people**: only a user grant can be `maintain` for the listing, and every
  grant list must keep at least one person at `maintain` (409 otherwise).
- **Superusers manage and can clone, but not push**: they see every agent in the admin listing,
  edit grants, remove an agent and clone any library repo over git. Pushing and their own
  `GET /library` still follow grants; an admin who wants to share changes grants themselves `write`.
- Slugs follow the kit pattern `^[a-z0-9][a-z0-9-]{1,62}$` and are unique per instance.

## Push policy

A push is accepted only when all of these hold, enforced by a `pre-receive` hook on the server:

- Only the `main` branch exists; no force pushes, no deletes.
- No path matching a kit `never_travels` entry (`credentials/`, `app-data/` and the single file
  `.claude/settings.local.json`, the agent's Claude permission settings) and no secret file
  (the kit's `secret_files` rule); path matching is case-insensitive. An entry ending in `/` is a
  folder, any other entry is one exact file. A version whose tree still contains `.claude/settings.local.json` is refused even if the change
  did not touch it (same as `credentials/` and `app-data/`); deleting it in a new version is accepted; repos that already track it are not migrated, the desktop's next share
  removes it. Reason: a teammate with write access could otherwise widen what runs without a
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
to the team library." If the policy cannot run, the server answers with "The team library can't
accept changes right now. Ask your admin to look at the Cinna server." (503). Exact sentences live in
`backend/app/services/library/push_policy.py`.

Without `write` a push gets 403 "You can't share changes to this agent."; without `read`,
"You don't have access to this agent."

## Events

Each listed agent carries `pushed_by`, the person whose push is the current version (clients show
"Last shared <date> by <name>"); it is empty before the first push.

After an accepted push, core emits `library.updated` (`repo_id`, `head_sha`, `pushed_by`) to every
reader's own user room. When a grant change gives people access they did not have, they get
`library.granted` (`repo_id`). Both are refresh hints; `GET /library` is the recovery path. See
[Event Bus](../realtime_events/event_bus_system.md).

## Removing an agent

A maintainer or superuser can remove an agent. It is refused (409) while another person has the agent
in Cinna, i.e. their git-versioned agent points at the library repo. The bare repo is moved to
`.deleted/` inside the git volume for the operator to purge.

## Admin page: Team Library

Superusers get **Admin > Team Library** (`/admin/team-library`): a table of library agents (name and
slug with who added it, maintainers, who can get it, last updated or "Not shared yet") with a row
menu: **Edit access** and **Remove from team library**. Edit access opens a dialog listing grants
(Can get, Can share changes, Maintainer; Maintainer only for people) with an add picker for
Everyone, roles and people, saved as one full replace. Removal is an alert dialog showing the 409
sentence when refused. There is no create button: agents join the library from Cinna Desktop or the
CLI. Mirrors are not shown.

## Plaintext on the server (by design)

Library trees are stored as plaintext git objects on the server and readable by the server operator.
The zero-knowledge guarantee of [App Sync](../app_sync/app_sync.md) does not apply to them. The
secret rules above are what keeps keys and credentials out.

## Integration points

- [Git-Backed Agent Versioning](../../agents/agent_git_versioning/agent_git_versioning.md): a library
  repo is a remote like any other; cloud twins will be an `AgentGitSource` pointing at it. The shared
  tree format is tree v3 (the kit folder as-is).
- [Local Agent Kit](../local_agent_kit/local_agent_kit.md): contract 1.8.0 `shared_remote`, 1.9.0 `.claude/settings.local.json` in `never_travels`, the policy
  data (`never_travels`, `secret_files`) and the manifest schema the hook validates against.
- [Event Bus](../realtime_events/event_bus_system.md): `library.updated`, `library.granted`.
- [User Roles](../user_roles/user_roles.md): role principals use `User.role` values.
- [Plugin Marketplaces](../plugin_marketplaces/plugin_marketplaces.md): mirrors of marketplaces are planned.
- Deployment: [Nginx Setup](../../infrastructure/nginx_setup.md) `/git/` block and
  [Data Volumes](../../infrastructure/data_volumes.md).

## Changelog

- Initial release: agent repos, grants, push policy, events, admin page. Suggest, lease, cloud twins
  and mirrors deferred.
