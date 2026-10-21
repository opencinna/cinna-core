---
feature: user_groups
domain: application
one_liner: "Lets superusers define named groups of people that owners can pick as a sharing principal for catalog items, credentials, AI credentials, artifacts and knowledge sources."
docs:
  tech: user_groups_tech.md
affects: [catalog, agent_credentials, ai_credentials, artifacts, knowledge_sources, user_roles, desktop_credentials]
---
# User Groups

## Purpose

A **user group** is a named set of people that a superuser maintains. Anyone who can share a resource can pick a group in that resource's own share dialog instead of adding people one by one. Membership is **live**: access is resolved when it is checked, so adding a person to a group gives them everything the group holds, and removing them takes it away (unless another path remains).

## Core Concepts

- **Group** - name (unique, case-insensitive, max 100), optional description (max 500), a member list.
- **Member** - an active user. Only active users can be added; an inactive member keeps the row but gets no access.
- **Group share** - a resource shared with a group. No per-member rows are written; every access check asks "is the caller in a group that holds a share".
- **Effective access** - the maximum of the direct grant, every group grant and all existing paths (owner, role, visibility). Direct and group grants coexist; removing one never removes access the other still gives.
- **Via group** - the marker on credential and AI credential lists showing the key reaches the user through a group.

## What can be shared with a group

| Family | Where the owner shares | Level |
|---|---|---|
| Catalog items (agent bundles, desktop agents, skill packages) | People tab of the item (`principal.kind = group`) | `use` or `edit`, never `maintain` |
| Credential | Credential sharing section | read (use only) |
| AI credential | New group-only section on the AI credential | use |
| Artifact | Artifact share dialog | view |
| Knowledge source (admin) | Source access card, when the access level is `shared` | query |
| Agent REST API scopes (producer) | Producer's Access & Scopes card, or the bundle Permissions card | the scope names themselves (no level) |

- **API scopes are a live union.** A caller's effective scopes on a producer are their personal grant's scopes plus the scopes of every group they actively belong to (inactive users contribute nothing), de-duplicated. Nothing is materialised per member, so adding or removing a member, or editing a group's scopes, applies on the next call. An explicit empty personal grant plus a scoped group gives the group's scopes (accepted behaviour). Any personal or group grant makes the caller a known user. See [Agent REST API](../../agents/agent_api/agent_api.md).
Out of scope for v1: see Follow-ups below.

## User Stories / Flows

### Admin manages groups

1. A superuser opens **Admin -> User Groups** (right after Users) and creates a group with a name and optional description.
2. On the group page they add members (multi-select of active users) and remove them one at a time.
3. Deleting a group, removing a member or revoking one share first shows an **impact preview** (members, shares by type, users losing credential access, links removed, environments cleared / restarted) and then performs the change.
4. The group page lists everything shared with the group, by type, and lets the superuser revoke any single row (overriding the owner). New shares are never created here; they are made in each resource's own dialog.

### Owner shares with a group

1. In a share dialog the picker offers a groups mode backed by `GET /user-groups/search`, open to every authenticated user (no role gate). The result carries name and member count only, never who the members are.
2. The owner picks the group; the share takes effect for every current and future member.

### Member loses access

Catalog, artifact and knowledge access are resolved per request, so the next request after removal returns 404 / excludes the source. Credentials and AI credentials hold **materialised** state and are cleaned up explicitly, see below.

## Business Rules

- **Only superusers** create, rename, delete groups and manage members. Group search for pickers is open to any authenticated user.
- **member_count counts active members** everywhere (picker, lists, admin). Inactive members are still released on removal.
- **Group catalog grants are capped at use / edit**, like roles. "A person maintains" stays people-only; a group `maintain` grant is rejected.
- **Credential automatic paths stay per-person.** The provisioner, provision-local, revocation records and the `source` provenance of `credential_shares` are unchanged. A group share is always the owner's explicit, direct-like share. Revoking a group share, removing a member and deleting a group write **no** `credential_share_revocation` rows.
- **Installs see group shares.** Bundle install `use_existing` and the publisher-credential check accept a credential the installer reaches through a group, not only one they own or hold a direct share on. A normal publisher install still writes a durable direct `bundle_install` share that survives the user leaving the group. If the owner had revoked the installer's direct share but a group share remains, the server install links through the group and writes **no** direct share, so the revocation is not undone; when the group path goes away the link is dropped and the slot becomes a placeholder on the next provision. Without a group share a revocation still blocks the link. Desktop `provision-local` in the revoked-plus-group case answers `already_shared` and writes no row; a group-only user with no revocation gets a durable `skill_local_install` share.
- **Bearer secrets outlive removal.** An `agent_api` token reached through a group share stays valid inside a member's existing environment after removal (it is a bearer secret). Removal stops future visibility and linking; rotate the key to cut access. Issued (identity-bound) agent_api keys cannot be shared at all: `allow_sharing` is rejected with 400.
- **Loss of access is computed, not assumed.** A user keeps a credential link while any path remains: owner, direct share or any group share. The same rule fixes a pre-existing gap where revoking a direct share dropped the link even if a group still covered the user.
- **Credential loss:** for users who lost every path, agent links are deleted and running environments re-synced. Desktop copies are not recalled (same as every revoke); a group share counts as a direct share for local copy and still needs `allow_local_use`.
- **AI credential loss:** the lost user's environments that pin the credential get `conversation_ai_credential_id` / `building_ai_credential_id` set to NULL and the **running** ones are restarted so `.env` is regenerated without the key. Admin-managed AI credentials cannot be group-shared.
- **Deleting a group** snapshots which (credential, user) and (AI credential, user) pairs the group covered, deletes the group (members and share rows cascade), then releases only those pairs for users with no other path.
- **Superuser revoke is per row** and ignores ownership; it releases like an owner revoke.
- **Fail closed.** Every family except the catalog keeps groups in a separate `*_group_share` table, so a call site that is unaware of groups gives members nothing rather than too much.
- **Membership rows carry no `added_by`**; the audit event records the actor (avoids a double-cascade in one user delete).
- **Audit:** every mutation writes a security event (`USER_GROUP_CREATED/UPDATED/DELETED`, `USER_GROUP_MEMBERS_ADDED`, `USER_GROUP_MEMBER_REMOVED`, `USER_GROUP_SHARE_REVOKED`).

## Edge Cases

- Adding an existing member is idempotent; unknown or inactive user ids are rejected (422); a concurrent add is conflict-safe.
- A duplicate name differing only in case returns 409.
- An empty PATCH is a no-op.
- Deleting a user cascades their memberships; a group outlives its deleted creator (`created_by` becomes null).

## Frontend

- **Admin -> User Groups** (`/admin/user-groups`) lists groups; the detail page (`/admin/user-group/$groupId`) has two tabs, "Members (N active)" and "Shared with this group". Delete, remove-member and revoke-share all use one impact dialog that fetches the server preview before confirming. In the Shared tab, catalog and knowledge source names are links; credential, AI credential and artifact names are plain text.
- **Share pickers** gain a groups mode (see [User Selector Pattern](../../development/frontend/user_selector_pattern.md)); glyphs: person User, group Users, role ShieldCheck.
- **Agent Bundle tab:** the "Permissions management" card has a "Groups" section after "People": one row per group with "N members" (active only) and its catalog level (no level; being listed means the members can use and install the bundle) plus the group's per-producer scope chips (click to edit), Edit and Remove actions, capped at 5 with Show all. The Add modal picks a person or a group (groups mode, already-granted groups excluded); with a group picked the producer-scope sections show as for a person and adding always grants `use`. Existing `edit` group grants still list and are never changed by the card. Remove revokes both the bundle access and the group's scopes on the manageable producers (one confirm, partial failures reported in one toast). The card lists groups only while the bundle visibility is `users`; a group grant added at another visibility is revocable only from the catalog People dialog.
- **Agent REST API:** the producer's Access & Scopes card has a Groups sub-list after People (name, N members, scope chips, Edit scopes, Remove scopes; capped at 5 with Show all). The Shared tab of a group lists "API scopes" rows (scope names, or "no scopes") that a superuser can revoke (confirm "Revoke X's API scopes on Y?", button "Revoke scopes"; type defined in `utils/userGroupShareTypes.ts`).
- **Credentials:** group pills in the sharing section; unsharing a group that would cut links asks for confirmation. The owned-credential badge counts people and groups.
- **AI credentials:** each owned, non-managed row has "Share with groups" and a "Shared with N groups" flag; environment configuration lists keys under "Shared with you".
- **Knowledge sources:** the access card shows group shares.
- "Via group" markers appear in tooltips and secondary lines.

## Follow-ups

- Groups as principals for server channel grants.
- Person-to-person AI credential sharing; managed AI credential membership by group.
- cinna-cli and Desktop group principal parsing (needs a kit contract bump).
- `admin_environment_service.py:460` uses an untracked `create_task`.
- A shared AI key in environment config gets model suggestions (its `default_model` plus the static list for its type), but the list-models button (`ListModelsButton`) gets no key for shared keys.
- Environment config opens a second dialog on top of the Add Environment dialog (pre-existing).
- The add-members dialog needs a server-side exclude of existing members on user search.
- `ShareSkillDialog` publish-time grants are people-only (contract is `grant_emails`, no group field).
- HashTabs selected-tab colour.
- `EnvironmentCard` still uses `window.confirm`.
- Realtime notifications on member add; a "groups I'm in" view.
- No test yet asserts that a group's API scopes reach the producer in the `X-Cinna-Caller-Scopes` proxy header; coverage stops at `resolve_scopes_for_caller`.
- The bundle card's group-grant POST ignores an `access` field instead of rejecting it, so an old client asking for `edit` silently gets `use`.

## Integration Points

- [Catalog](../catalog/catalog.md) and [Agent Bundles](../../agents/agent_bundles/agent_bundles.md) - group principal on catalog grants.
- [Credential Sharing](../../agents/agent_credentials/credential_sharing.md) and [Credential Materialization](../../flows/credential_materialization.md) - group shares, release on loss.
- [AI Credentials](../ai_credentials/ai_credentials.md) - group-only sharing, shared-with-me, pin clearing and restart.
- [Artifacts](../artifacts/artifacts.md), [Knowledge Sources](../knowledge_sources/knowledge_sources.md) - group grants / shares.
- [Desktop Credentials](../desktop_credentials/desktop_credentials.md) - group share counts as a direct share.
- [User Roles](../user_roles/user_roles.md) - groups are managed by superusers only.

Technical details: [user_groups_tech.md](user_groups_tech.md).
