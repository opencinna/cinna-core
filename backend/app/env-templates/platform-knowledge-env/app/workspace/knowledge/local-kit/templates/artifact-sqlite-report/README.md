# Artifact starter: report

A minimal site that works inside the Cinna artifact sandbox: a snapshot of numbers
to read or hand to people. For a live view the owner keeps and refreshes, build a
dashboard component instead (the `cinna-dashboards` skill). Copy this folder
into the workspace, replace `data.json` (or switch to SQLite, see the comment at
the top of `app.js`), then publish the folder with
`artifacts_publish {path: "<folder>", title: "..."}`.

Rules it already follows: `index.html` at the root, relative paths only, every
script in the folder, no browser storage, no workers. Delete this README before
publishing if you do not want it in the file list.
