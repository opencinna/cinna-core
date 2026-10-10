---
feature: catalog
domain: application
one_liner: "One catalog lists every agent (running in Cinna or on your computer) and skill shared on the instance, with one access model, install counts, a delete guard and realtime update hints."
docs:
  tech: catalog_tech.md
affects: [agent_bundles, agent_skills, desktop_agents, local_agent_kit, realtime_events, user_roles]
---
# Catalog

## Purpose

The catalog is the one place where people on an instance find what others have shared. It holds three
kinds of item, all through the same listing, the same access model and the same management actions:

| Kind | Words in the UI | Where it runs |
|------|-----------------|---------------|
| Cloud agent (bundle, `runs_on='cloud'`) | Runs in Cinna | Installed as the user's own agent in Cinna |
| Desktop agent (bundle, `runs_on='desktop'`) | Runs on your computer | Fetched and run by Cinna Desktop; see [Desktop Agents](desktop_agents.md) |
| Skill (package) | Skill | Added to cloud agents, or pinned into local agents by Cinna Desktop |

Agent bundles are described in [Agent Bundles](../../agents/agent_bundles/agent_bundles.md) and skill
packages in [Agent Skills](../../agents/agent_skills/agent_skills.md). The UI never shows git words
(commit, branch, clone URL).

## Access model

One rule for every kind: access is visibility plus grants (user or role x use/edit/maintain). The full
rules, including who may manage an item, are in
[Agent Bundles](../../agents/agent_bundles/agent_bundles.md#access-visibility-plus-grants-one-access-model).
In short: `edit` or `maintain` count at any visibility; `use` counts when the item is limited to listed
people and listed in the catalog, or when it is public; a superuser can manage anything. An item with no
version yet (a desktop agent before its first share) is listed only to people with `edit` or `maintain`.

## The listing

`GET /api/v1/catalog/items` returns every item the caller may see, optionally narrowed by kind
(`agent`/`skill`) or where it runs. Each entry carries the caller's access level, the latest version,
the publisher, the credentials it needs, the install count and the caller's own installs
(`my_installs`). A superuser can ask for every item (`scope=all`) for oversight.

- **install_count** is the number of distinct people using the item, across cloud installs and local
  installs, with the publisher excluded.
- **my_installs** lists the caller's cloud installs and the caller's local installs reported by
  Cinna Desktop (device, host agent for skills, installed version).

## Local installs

Cinna Desktop reports what it got, so the catalog can count it, show "on N computers" and tell the
person when a newer version exists.

- **Report an install**: `POST /api/v1/catalog/items/{uuid}/local-installs` with a device id (opaque,
  up to 128 characters), the version number and, for a skill, the id of the local agent it is pinned
  into. A repeat from the same device moves the version number. The reply is the item as the caller
  sees it.
- **Report a removal**: `DELETE /api/v1/catalog/items/{uuid}/local-installs/{device_id}`, optionally
  with `host_manifest_id`; without it every row of that item on that device goes. It is idempotent and
  only touches the caller's own rows.
- **Who may report**: someone who can use the item (404 otherwise). One exception: a skill pinned by
  the latest version of a desktop agent the caller can use; that caller gets the full item back, which
  is accepted.
- **Refusals**: a cloud agent answers 409 `cloud_only` ("This agent runs in Cinna."); a skill needs the
  host agent id and an agent refuses one; an unknown version is refused.

## Skills from a desktop

A skill that exists only on a computer can be published straight into the catalog by Cinna Desktop (preview,
then a folder upload). Any signed-in user may publish a new skill; a new version needs `edit`, and visibility or
grants need `maintain`. Grants sent with a publish are additive. Every credential slot the skill declares is filled
by each installer unless the publisher names one of their own credentials for it (publisher-provided or template);
a desktop install then asks core to share the publisher-provided ones so the computer can copy them. The chosen version is returned so the desktop writes it back locally, and `catalog.updated`
and `catalog.granted` fire as for any publish. Details and refusals:
[Agent Skills](../../agents/agent_skills/agent_skills.md#5c-publishing-a-skill-from-a-desktop).

**Download through an agent.** A person who can use a desktop agent may download, list the files of and read the
`SKILL.md` of a skill revision that the agent's latest version pins exactly, even without seeing the skill in the
catalog. The skill's own detail stays hidden that way.

**Credentials for a local install.** `POST /api/v1/skills/packages/{uuid}/revisions/{n}/provision-local` shares a
revision's publisher-provided credentials with a person installing it on a computer. It needs `use` on the skill or on
a desktop agent that pins that revision. Per slot it answers `user_provides`, `not_shareable`, `not_allowed_locally`,
`already_shared` or `shared`; the owner's sharing and local-use switches are re-checked each time. Details:
[Agent Skills](../../agents/agent_skills/agent_skills.md#5c-publishing-a-skill-from-a-desktop).

## Deleting an item

A maintainer or superuser can delete an item only while nobody else uses it. If other people use it the
answer is 409 with "1 person uses this. It can be removed once nobody does." or "{n} people use this.
It can be removed once nobody does." Other people's local installs count. The caller's own local rows
are removed with the item. The caller's own cloud links to a skill block the delete ("Remove it from
your agents first."); links are never removed silently.

## Realtime hints

After every publish of any kind (a cloud agent version, a skill version, a desktop agent share) core
emits `catalog.updated` with the kind, uuid, bundle or package id, the version number and, for desktop
agents, the head sha. It goes to people reached through the grants (role grants expanded), to cloud
installers, and to local installers who still have access. A grant change emits `catalog.granted` to
people who just gained access. Public items have no fan-out to everyone: a person who can use a public
item only through "everyone" gets no event and relies on polling. Events are refresh hints; the listing
is always the source of truth. See [Event Bus](../realtime_events/event_bus_system.md).

## Web surfaces

### Catalog page (`/catalog`)

Browse everything the user can get. A kind control (All, Agents in Cinna, Agents on your computer,
Skills) and a filter menu (All, Public, Shared with me, Installed, Mine) are kept in the URL as `kind` and `filter`. Each item
is a tile showing its kind, description, version and date, the credentials it needs, the skills of an
agent, and whether the user has it installed or has an update available. One footer action per kind:
install or update or open for a cloud agent, "Open in Cinna Desktop" for a desktop agent, "Add to
agent" for a skill. `/catalog/skills` redirects to `/catalog?kind=skill`.

### Desktop agent detail

Reached from a desktop agent tile (`/catalog/items/{uuid}`). Shows who shares it, where it runs, the version history, the
engine, the skills it pins, the credentials it needs, install counts and the catalog id. There is no
install button on the web; the page only links to Cinna Desktop. Release notes of the chosen version
are shown beside the facts.

### Maintainer controls

People who hold `maintain` (and superusers) get a menu on the desktop agent card, the skill package card and the install page's agent header card: edit who can use or change
it (a People dialog, which is also where a skill's visibility is changed;  with general access, user and role rows, and the levels Can use, Can share changes
and Maintainer; at least one person must be a maintainer), and delete (showing the sentence above when
refused). Superusers can also delist or relist an item.

### Admin catalog

A superuser page (`/admin/catalog`, "Catalog" in the admin menu) listing every item with its kind, maintainers, general access (delisted items are
marked), version, install count and last update, with the same People, delist and delete actions per
row. It replaces the former admin team library page.

## Known gaps

- A maintainer can re-list a skill that a superuser delisted; there is no record of who delisted it.
- Local installs are best effort reports from Cinna Desktop; the desktop's own state stays the truth
  for itself.

- Deleting a credential does not yet count the shares made for local skill installs in its impact tiers.

## Integration points

- [Agent Bundles](../../agents/agent_bundles/agent_bundles.md), [Agent Skills](../../agents/agent_skills/agent_skills.md), [Desktop Agents](desktop_agents.md): the three kinds and their publish paths.
- [Local Agent Kit](../local_agent_kit/local_agent_kit.md): the desktop-side contract that reports local installs and pins catalog skills.
- [Event Bus](../realtime_events/event_bus_system.md): `catalog.updated`, `catalog.granted`.
- [User Roles](../user_roles/user_roles.md): role principals in grants.

## Changelog

- One unified listing replaced the separate agent and skill catalog pages; local installs, the local-aware delete guard and `catalog.updated` for cloud and skill publishes were added.
- Skills can be published from a desktop, and a skill revision pinned by a usable desktop agent can be downloaded through that agent.
- Desktop skill publish can name publisher credentials per slot, and local installs can be provisioned with them.
