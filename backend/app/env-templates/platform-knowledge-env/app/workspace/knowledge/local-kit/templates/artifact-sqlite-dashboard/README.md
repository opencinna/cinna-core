# Artifact starter: dashboard

A minimal site that works inside the Cinna artifact sandbox. Copy this folder
into the workspace, replace `data.json` (or switch to SQLite, see the comment at
the top of `app.js`), then publish the folder with
`artifacts_publish {path: "<folder>", title: "..."}`.

Rules it already follows: `index.html` at the root, relative paths only, every
script in the folder, no browser storage, no workers. Delete this README before
publishing if you do not want it in the file list.
