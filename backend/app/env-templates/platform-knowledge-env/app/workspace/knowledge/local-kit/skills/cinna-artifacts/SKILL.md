---
name: cinna-artifacts
description: Publish a report, a set of files or a small self-contained dashboard from your workspace as a Cinna artifact, a private page your owner opens in Cinna. Covers when to publish, how to build a folder that works inside the artifact sandbox, and how to update it later. Use before the first artifacts_publish call and before building any HTML page meant to be published.
when_to_use: The user asks to publish, share, show or hand over a report, dashboard, page, chart or set of files; a tool named artifacts_publish is about to be called; you are building an HTML page, dashboard or static site that a person will open later.
user-invocable: false
version: "1"
compatibility: Cinna Desktop or a cinna-core agent environment that exposes the `cinna` tool server with the artifacts_publish tool.
metadata:
  cinna_protocol: artifacts/1
---

# Cinna artifacts

An artifact is something you made that a person opens in Cinna: a Markdown report, a CSV, a PDF, a folder of files, or a small website such as a dashboard. You publish it from your workspace with `artifacts_publish`; Cinna copies the files, and the person opens them on the artifact's page. Your copy in the workspace is not touched and later edits to it do not show until you publish again.

Everything you publish is **private to your owner**. You cannot share it, make it public or delete it; a person does that from the artifact's page. Do not tell anyone a link will work for them; give the `url` to your owner.

## Where to build it

Build anything meant to be published under **`artifacts/<name>/`**, one subfolder per artifact (`artifacts/q3-dashboard/index.html`); a single file directly inside, `artifacts/summary.md`, is fine too. This is the same place for an agent on your owner's computer and an agent in the cloud. Then publish it with `artifacts_publish {path: "artifacts/<name>"}`.

`artifacts/` is one of the two places you may write at runtime, beside `app-data/`. Keep scratch work, caches and durable state in `app-data/`, which is never published; `artifacts/` holds only what you build to publish. Like `app-data/`, it stays with this one person: it is not committed, not shared with teammates and not copied to the cloud with the agent. Keep the same `artifacts/<name>` for the same artifact so republishing updates it.

## Publishing

`artifacts_publish {path, title, description?, entry?, artifact_id?}`

- `path` is a file or a folder relative to the workspace root, normally `artifacts/<name>`.
- A single `.md`, `.csv`, `.pdf`, image or `.html` file is a fine artifact on its own.
- A folder with `index.html` at its root becomes a site; name another start page with `entry`. A folder without one is shown as a file browser.
- **Updating:** publish the same `path` again and the same artifact gets a new revision, with the same page URL. Pass `artifact_id` from an earlier result when the files moved to another path. Never publish a second copy to "update" one.
- **Never published**, and listed in the result's `excluded` when dropped: `.env` and `.env.*` files, `*.pem` and `*.key` files, `.DS_Store`, and anything under `credentials/`, `app-data/`, `node_modules/`, `.git/` or `.claude/`. A `path` that is itself one of these, or any hidden (dot-named) file or folder, is refused outright. Do not work around it; keep secrets out of the folder.
- **Hidden files are not published**: files and folders whose name starts with `.` are left out of a folder and listed in `excluded`. Give data files ordinary names.
- **Symlinks are not published.** Every symlink inside a folder is left out and listed in `excluded`, and a `path` that is a symlink or runs through one is refused. Copy the real files into the folder instead.
- **Limits:** 25 MB per file; 50 MB and 2,000 files per revision; your owner has a total storage quota across all artifacts. Over a limit the publish fails with the reason; nothing is published.
- **Republishing updates what people see.** If your owner has shared the artifact, a new revision is what everyone with access, including people holding a link, sees next — unless that link was pinned to an earlier revision. Say what changed when you republish.

Tell the person the title and the `url`, and what changed if it is a new revision.

## Building a site that works

Sites run in a locked sandbox, on their own origin, with no access to Cinna or the internet. Build for that from the start:

1. **One folder, `index.html` at its root, relative paths only.** `./app.js`, `data/summary.json`; never `/app.js` and never an absolute URL.
2. **Bundle every library into the folder.** No CDN `<script src="https://…">`, no web fonts from Google Fonts, no external images. The sandbox blocks every request that leaves the folder.
3. **No browser storage.** `localStorage`, `sessionStorage`, `IndexedDB` and cookies throw. Keep state in memory, or in the URL hash.
4. **No `new Worker('file.js')`.** Workers loaded from a file are blocked. Run the work on the main thread.
5. **SQLite in the browser:** use sql.js on the main thread and put `sql-wasm.wasm` in the folder beside the script; point `locateFile` at it with a relative path. Load the database with `fetch('./data.sqlite')`.
6. **Keep data small.** Aim for under ~20 MB of SQLite or CSV. Pre-aggregate: a summary table or a JSON file of the numbers the page shows beats a raw dump.
7. **No forms that post anywhere and no calls to an API.** The page is a snapshot of what you computed; publish again to refresh it.

Check it before publishing: open `index.html` from the folder with a local static server if you have one, and make sure nothing is fetched from outside the folder.

## Starter

`templates/artifact-sqlite-dashboard/` in the Local Agent Kit is a minimal dashboard: `index.html`, a script that reads `data.json`, and a note on adding sql.js. Copy it to `artifacts/<name>/`, replace the data, publish the folder.
