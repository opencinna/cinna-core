---
feature: artifacts
domain: application
one_liner: "Frozen, versioned copies of files or small sites published to the platform, viewable by the owner, granted users or anyone with a revocable link."
docs:
  tech: artifacts_tech.md
affects: [guest_sharing, local_agent_kit, desktop_auth]
---
# Artifacts

## Purpose

An **Artifact** is a frozen copy of a file, a set of files or a small static site, published *to the core* so that people can open it without access to the publisher's machine or agent environment. Typical sources: an agent that built a dashboard in its workspace, a person publishing a folder from Cinna Desktop, the `cinna` CLI, or a browser upload.

Not to be confused with the `artifacts` field of an input task, which is just a list of links. See the README glossary.

## Core Concepts

| Term | Meaning |
|------|---------|
| **Artifact** | The stable identity: title, description, kind, owner, visibility, source. Its URL never changes. |
| **Artifact Revision** | An immutable snapshot (numbered 1, 2, ...). Republishing adds a revision; the artifact points at the newest ("current"). |
| **Kind** | `site` (has an HTML entry point: given `entry`, else root `index.html`, else a single `.html` file) or `files`. |
| **Visibility** | `private` (owner plus explicit grants) or `instance` (any signed-in valid account). |
| **Grant** | View access for one named user. Roles are not supported (422). |
| **Artifact Link** | A public, revocable URL: optional 4-digit code, optional expiry, optional pin to one revision. |
| **Source** | Where it came from: `desktop`, `core_agent`, `cli`, `upload`, plus label and (for agents) a `source_key` so the same path republishes into the same artifact. |

## User Stories / Flows

### 1. Publish and republish
1. A publisher sends either one zip (`archive`) or a list of files (`files[]`, filename = relative path). A single non-zip file becomes a single-file artifact.
2. The core validates and extracts it (see Business Rules), stores the files, records the manifest and creates revision 1; the artifact starts `private`.
3. Publishing again to the same artifact adds revision N+1 and moves "current"; older revisions stay until deleted, and the URL is unchanged.

### 2. View
1. A signed-in user opens the artifact page; the core mints a short-lived **view capability** for the current (or chosen) revision.
2. Files are served from a separate content path (`/a/v/<cap>/<path>`) in a sandboxed iframe. A site gets its entry page; a `files` artifact gets a file list with per-type viewers.
3. Download: the whole revision as a zip, or any single file with `?dl=1`.

### 3. Share
- **Grants:** the owner replaces the full grant list of people or [user groups](../user_groups/user_groups.md) ("view" level; a role principal is rejected). Group members are resolved live: removing someone from the group, or revoking the group grant, makes the next request 404.
- **Visibility:** the owner flips `private` / `instance`.
- **Public link:** the owner creates a link; the token (and the code, if any) is shown **once**, at creation. Anyone with the URL (and code) opens the artifact without an account. The owner can revoke it at any time.

### 4. Open a public link
1. `info` tells the page the title, owner name, whether a code is needed, whether it is expired or blocked.
2. `open` (with the code if needed) returns a view capability for the pinned revision, or the current one if not pinned.
3. Three wrong codes block the link permanently; the owner must create a new one.

### 5. Publishing from an agent or workspace
- **Agent verb:** `artifacts_publish` (building and conversation mode) publishes a workspace file/folder as a *private* artifact. Agents build what they publish under `artifacts/<name>/` in the workspace (a single file directly in `artifacts/` also works) and publish `{path: "artifacts/<name>"}`; the same path again republishes the same artifact (`source_key` = `<agent_id>:artifacts/<name>`). `artifacts/` is per-user runtime output: never shipped with the agent (not in bundle publishes, install seeds, git sources or team-library pushes) and not deleted on bundle update. `app-data/` stays never-published. An agent cannot change visibility, grants, links or delete (A8).
- **Owner UI:** "Publish as artifact" on a workspace file/folder (agent owner only), same rules.
- **Tool:** `artifacts_publish(path, title, description?, entry?, artifact_id?)` returns `{artifact_id, revision, url, kind, file_count, size_bytes, excluded}`. Defined in the kit (contract 1.12.0, `artifacts/` folder role since 1.13.0): [protocol](../../local_agent_kit/protocols/artifacts.schema.json), [skill `cinna-artifacts`](../../local_agent_kit/skills/cinna-artifacts/SKILL.md), and a SQLite-dashboard template under `docs/local_agent_kit/templates/artifact-sqlite-dashboard/`. See [Cinna Task Tools](../../agents/agent_environment_core/cinna_tasks_tools.md#artifacts_publish).
- **Always private.** Artifacts from agents have `source_kind=core_agent`. An agent may republish only artifacts made by the same agent for the same owner; extra fields are a 422.
- **Republish by path.** The key is `<agent_id>:<path>` (per agent, survives environment recreation), so the same path adds a revision. A foreign `artifact_id` is a 404.
- **Safety (A10).** The requested path is refused (400) if it, or any segment, is excluded or dot-named, or any component is a symlink. The pull skips dotfiles, `__pycache__` and all symlinks; skipped names are merged into `excluded`. Needs an environment rebuild; older environments still work but report no skipped names.
- **Errors:** 400 bad/excluded path or symlink, 404 missing path/environment, 409 environment not running or a concurrent first publish, 413 over a limit.
- **Owner UI:** "Publish as artifact" on a workspace file uses `POST /artifacts/from-environment` (agent owner only; non-owners get 404) with the same rules. The entry point is a hover action, "Publish as artifact", on files and folders in the environment workspace file tree (agent owner only). The dialog says "Updates <title> - revision N next" when the same `<agent_id>:<path>` was published before.
- Accepted residual: the single-file branch re-opens the file after the check (TOCTOU); low risk since the agent can read that file anyway.

### 6. Frontend surfaces
- **Sidebar:** an "Artifacts" entry, kept highlighted on the artifact page.
- **List page:** Mine / Shared with me tabs, filtered by workspace. "Upload" opens a dialog for a zip or several files (source `upload`); the result panel lists what was excluded.
- **Artifact page:** header, a full-height viewer, and a details panel (a side column on wide screens, a sheet below). Revisions show five at a time with "Show all" and "Published by <name>"; the current and pinned revisions cannot be deleted. The owner gets a link to the source session. Edit, Delete and Share dialogs.
- **Viewers:** a site opens in a sandboxed iframe (never with the app's own origin). A files artifact shows a file tree with a viewer per kind: markdown (raw HTML off, relative links kept inside the artifact), CSV (virtualised, 10k rows), text and JSON (2 MB cap), code files syntax-highlighted by extension (Python, JS/TS, shell, YAML, TOML/INI, SQL, XML, CSS/SCSS, Go, Rust, Java, C/C++, Ruby; plain text and files over 200 KB stay plain; shown plain first, then coloured, so nothing shifts), images including SVG, PDF in-page; an HTML file offers "Open as site"; anything else downloads. A single-file artifact opens straight into its viewer.
- **Share dialog:** People (Only me / Everyone on the instance, plus a user picker; roles are not offered) and Public links (label, expiry, optional code; the link URL is copied at once on creation; the owner can copy any live link's URL again from its row, because the token is stored encrypted next to its lookup hash; links created before that change have no URL to copy; the 4-digit code is a row badge; revoke).
- **Public link page** (`/s/<token>`, no login): title, shared-by, "made by an agent" when applicable, description, Download; a code prompt ("Incorrect code. N attempts left."); plain messages for expired or revoked links.

## Business Rules

**Access (single rule in `ArtifactAccess`).**
- View: owner; or `instance` visibility and a valid account; or a grant. A valid account means active and allowed by the access policy.
- Manage (republish, edit, share, delete revisions, delete): owner only.
- A public link additionally dies when the owner's account is deactivated.
- Non-viewers get 404, not 403 (no existence leak).

**Ingest (all publishers).**
- Names: backslashes, absolute paths, `..`/`.`/empty segments, control characters, duplicates (case-insensitive after NFC normalisation) are rejected (400). Symlink, device, encrypted or unsupported-compression zip entries are rejected (400), naming the entry.
- Excluded silently and reported in `excluded`: anything under `.git`, `.claude`, `credentials/`, `app-data/`, `node_modules/`, any `.env*`, `*.pem`, `*.key`, `.DS_Store`. Agent/workspace publishes never list dotfiles because the environment zipper already skips them.
- Limits (settings, 413 when exceeded): 50 MiB per revision, 2000 files, 25 MiB per file, compression ratio 100 per entry (zip bombs), and a per-owner quota of 2 GiB summed over all revisions of all the owner's artifacts. Declared sizes are checked first, then the real decompressed bytes.

**Revisions.** The current revision cannot be deleted (409). A revision pinned by a live link cannot be deleted (409); dead (revoked or expired) pins are cleared first.

**Deletion.** Deleting an artifact removes rows and files at once; every link and view capability stops working on the next request. Deleting a user removes all of their artifacts and files.

**Capabilities (view caps).** A cap is a signed token (`aud=artifact_view`, 8 h) naming one artifact, one revision and the principal it was issued to. It is **not** a login token. It is re-checked on every content request: revoking the link, deleting the artifact or revision, losing a grant, flipping visibility to private, or deactivating the owner makes the next request 404, even though the token has not expired.

**Public link codes.** 4 digits, stored encrypted; any wrong attempt counts (including malformed input); the 3rd wrong attempt blocks the link. Response messages include attempts left.

## Accepted Limits

- After a revoke, a viewer may keep **already-loaded** files from the browser cache for up to 1 hour (served files use `Cache-Control: private, max-age=3600`). New requests fail immediately.
- Error responses on `/a/` carry a stricter CSP than served files (nothing may load) and `no-store`.
- Storage folders are keyed by revision id, not number; the revision zip is built once per revision and cached next to it.
- Agent publishes copy from the container through the environment adapter; dotfiles are skipped there and symlinks are followed, but the backend ingest still enforces all rules above on what arrives.
- Roles as grant principals, edit-level grants, and cookie-based content auth are out of scope.

## Integration Points

- [Guest Sharing](../../agents/guest_sharing/guest_sharing.md): same link-token and code pattern (hash storage, attempt counting).
- [Local Agent Kit](../local_agent_kit/local_agent_kit.md): the `artifacts_publish` verb, protocol and skill (placeholder until P6 lands).
- [Desktop App Authentication](../desktop_auth/desktop_auth.md): `/.well-known/cinna-desktop` advertises `artifacts: 1` so Cinna Desktop knows the instance supports artifacts.
- [Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md): CLI publish/download/link routes.
- [Auth Token Taxonomy](../../flows/auth_token_taxonomy.md): the view cap and link token.
- [Nginx Setup](../../infrastructure/nginx_setup.md) and [Data Volumes](../../infrastructure/data_volumes.md): `/a/` routing, upload body limit, storage mount.

## Changelog

- Grants accept user groups; list counts and `shared_with_me` include group grants.

- Initial backend release (models, ingest, content serving, API, links, CLI account routes).
- Contract 1.13.0: `artifacts/` folder role as the place agents build what they publish; excluded from bundles/library pushes, carried on env switch, survives in-place rebuild; env prompt carries an "Artifacts location" line (running envs need a rebuild to pick it up).
- Artifact file viewer highlights code files by extension (frontend only; no contract or API change).
