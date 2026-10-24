---
feature: addons_directory
domain: application
one_liner: "One endpoint lists every skill, plugin and MCP server a user may add (catalog, marketplaces, MCP directory), ranked, ETag-cached, with a platform policy for Cinna Desktop."
docs:
  tech: addons_directory_tech.md
affects: [plugin_marketplaces, catalog, local_agent_kit, mcp_integration]
---
# Addons Directory

## Purpose

Cinna Desktop's Add picker needs one searchable list of everything a user can add to an agent: skills, plugins and MCP servers. Core holds those in three different places, each with its own visibility rule. The addons directory is the single normalised view over all three, built per caller, so the desktop makes one call instead of merging sources itself.

## Sources and who sees what

| Source (`ref.source`) | Kind | Visible to |
|---|---|---|
| `core-catalog` | `skill` | Whoever the [catalog](../catalog/catalog.md) access model lets see the package. Delisted packages appear only for someone who can manage them. |
| `core-marketplace` | `skill` (skills-format marketplace) or `plugin` | The marketplace owner, or anyone when the marketplace is public for discovery. No superuser bypass. See [plugin marketplaces](../plugin_marketplaces/plugin_marketplaces.md). |
| `core-mcp-directory` | `mcp` | Everyone, except entries an admin has banned. |

A private marketplace or a catalog package the caller cannot see never appears, for any query.

## Entry shape

Every entry carries a source-scoped `ref`, `kind`, `name` (slug), `title`, `summary`, `description`, `category`, `tags`, `publisher` (`name`, `verified`), `origin` (label, kind `catalog`/`marketplace`/`directory`, links), `version`, `digest`, plugin `components`, `requires.credentials`, `runs_code`, `status`, `unsupported_reason` and `install`. The shape is a frozen contract with Cinna Desktop. Compatibility with a computer's engines is not in the entry; the desktop computes it from `components`.

- **Status.** For marketplace rows the snapshot state decides: ready snapshot is `ready`, failed is `unsupported` (reason from the snapshot, else the plugin's own reason), anything else is `pending`. Catalog and MCP entries are always `ready`.
- **Publisher.** Catalog: the publisher name or email, `verified` when the email is confirmed. Marketplace: the entry author (falling back to the marketplace owner); `verified` only when the author matches the marketplace owner by name, or by email ignoring case. No author means no claim. MCP: the entry's publisher, `verified` as stored.
- **Install descriptor.** Catalog skills and ready marketplace entries carry an archive path to download. A pending or unsupported marketplace entry has no `install`. MCP entries carry a connect descriptor (transport, URL or package, auth kind, env variable names only, never values).
- **runs_code.** Catalog: the latest revision ships `scripts/`. Marketplace: from the snapshot analysis. MCP: only stdio servers.

## Query, ranking and paging

`GET /api/v1/addons/directory` (any signed-in user) takes `search`, `kind`, `category` (matches the category or a tag), `source`, `runs_on=desktop`, `skip`, `limit` (default everything, max 5000).

- `runs_on=desktop` drops entries that cannot run on a computer: a catalog skill needing a credential type that is not locally compatible, and marketplace entries that are app-connector only.
- With `search`, entries are ranked by tier: exact name, name prefix, title contains, tag or category, description. Non-matching entries are dropped. Inside a tier, usable (`ready`, `pending`) entries come before `unsupported`, then by title.
- Without `search`: ordered by kind (skill, plugin, mcp), then title.

## ETag and caching

The response carries `ETag` (a hash of the response body, so it differs per caller and per page) and `Cache-Control: private, no-cache`. Sending it back as `If-None-Match` returns 304. The built index is cached per user for 60 seconds, keyed also by a global version counter (`server_config.addons_directory_version`). Writers bump the counter in their own transaction, so a change shows at once; a missed bump costs at most the 60 s, never a wrong 304.

The counter is bumped by: marketplace create/update/delete and sync, snapshot worker batches that changed a row, catalog publish, package update, delist, grant and revoke, and every MCP directory write.

## Policy

The response includes `policy`, four booleans the desktop must honour: `external_directories`, `custom_mcp`, `plugin_hooks`, `stdio_mcp`. All are `true` for now; they are not editable yet (an admin control is planned, and will let operators tighten them without a desktop release).

## MCP directory

An admin-curated list of MCP servers, served through the directory as `kind=mcp`.

- **Entries** are either `manual` (added by an admin) or `opencinna` (imported). HTTP and SSE entries need an HTTPS URL; stdio entries need a package (npm, pypi or oci, with version and args). Env variables are names only, matching `^[A-Z_][A-Z0-9_]*$`. A name (the slug) is unique, otherwise 409 `name_taken`.
- **Ban / unban.** A banned entry (optional reason) disappears from the directory but stays in the admin list; unban restores it. A ban survives re-imports.
- **Import from the opencinna index.** Superuser action that fetches the "Cinna Official" registry (URL from `MCP_DIRECTORY_INDEX_URL`). Rules: HTTPS only, no redirects, 1 MB cap, 20 s overall deadline. Only index version 1 is accepted (422 otherwise). Entries map to HTTP or SSE servers with the first HTTPS remote; auth is `oauth` when the index says authentication is required, else `none`; publisher "Cinna", verified. The import is idempotent: it creates missing entries, overwrites only the index-owned fields of entries it created earlier (so admin edits of those fields are lost; bans and access stay), and reports the rest. It never deletes. Result: `created`, `updated`, `unchanged`, `skipped` (with reasons such as `no_remote`, `name_taken`, `invalid_entry`, `duplicate`) and `stale` (imported entries the index no longer lists, left in place). An index id that equals a manual entry's name is skipped as `name_taken`, never overwritten.
- **Admin page.** Superusers manage the directory on an Admin page (MCP Directory): a list of entries with their transport, auth, source and banned state, an add wizard, edit, ban/unban, delete, and an import dialog showing the result counts.

## Integration points

- [Catalog](../catalog/catalog.md): source of skills; publish, delist and grants bump the version.
- [Plugin marketplaces](../plugin_marketplaces/plugin_marketplaces.md): source of plugin and skill entries and their snapshot status and components.
- [Local agent kit](../local_agent_kit/local_agent_kit.md) and Cinna Desktop: the consumer; kit manifests may name directory entries.
- [MCP integration](../mcp_integration/agent_mcp_connector.md): the directory lists servers a desktop can connect; it does not itself connect anything.

## Changelog

- Directory endpoint, policy and MCP directory with opencinna import added.
