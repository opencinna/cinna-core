# The file binding — tasks and handovers as Markdown files

Any project folder adopted in Cinna Desktop as a bare agent is also a task inbox. The same verbs the `tasks_*` tools offer exist as files, so a terminal session, a shell script, `git` or a person can take part without a tool server. Cinna Desktop watches the files and does the rest: records them as tasks, asks the person or runs them, and delivers results.

Both file contracts use the same small frontmatter subset: flat `key: value` lines (quoted or bare), one nested `origin:` map, and `artifacts:` / `blocked_by:` as YAML lists. The closing `---` is the first later line holding only `---`. Write files whole — a temp file, then a rename — so a half-written file is never read.

## Own queue: `.cinna/tasks/<slug>.md`

One file per task, directly under `.cinna/tasks/`. Create a task by writing a file; read your queue by listing the folder.

```markdown
---
cinna_task: 1
id: t-3f9a2b1c          # optional; the filename stem when absent
title: Add retry to the uploader
status: ready            # a status word, see below
priority: normal         # low | normal | high | urgent
parent: t-11aa22bb       # optional
blocked_by: [t-99887766] # optional; all must be done before this runs
origin: cinna
team: platform           # any other key is a custom field
---
The description, in Markdown.
```

**Status words.** `status:` takes the folder's own words, defined in `.cinna/tasks/_board.yaml` as `Word: category` lines, then a default table: Backlog `new refining backlog todo`, Ready `open ready next`, Active `in_progress doing wip`, Waiting `blocked waiting review`, Done `done completed closed`, Failed `error failed`, Cancelled `cancelled canceled wontfix`. Matching ignores case, spaces and hyphens. An unknown word is shown as written and nothing reacts to it.

**Reporting through the file.** Changing `status:` to a Done, Failed or Waiting word is a report. Cinna appends two sections it owns and never reads back: a comment log between `<!-- cinna:log -->` and `<!-- /cinna:log -->`, and the handoff note between `<!-- cinna:handoff -->` and `<!-- /cinna:handoff -->`. Write your note inside the handoff markers; do not edit the log. Everything else in the file is yours and survives Cinna's writes byte for byte.

A task file written outside Cinna needs one approval from the person before Cinna runs it; a file Cinna wrote itself does not.

## Delegation: `.cinna/handovers/<id>/`

To ask another adopted folder for work, create a handover folder **inside that project**. The `<id>` is yours: 3 to 64 characters, lower case, starting with a letter or digit, then dots, dashes and underscores; `YYYYMMDD-HHMM-<slug>` is the recommended shape. Unique per folder.

```
<project>/.cinna/handovers/<id>/
  brief.md          you write it once; immutable after status: ready
  report.md         the executor writes and rewrites it
  revisions/001.md  your follow-ups, each written once
```

**`brief.md`**

```markdown
---
cinna_handover: 1
title: Add retry to the uploader
status: ready            # draft | ready — only ready is picked up; no default
execution: ask           # optional: ask forces approval; absent follows the receiver's setting
priority: high           # optional
origin:
  agent: <your agent id>
  chat: <the chat id from the turn context>
  task: <the task id from the turn context, when there is one>
depth: 1                 # your own handover depth plus one; capped at 2
group: 20260917-batch    # optional fan-out group
---
What you need done, in Markdown. Everything the executor needs — it may not see your conversation.

## How to report

Write `report.md` in the same folder as this brief (not your working folder), with frontmatter `cinna_handover: 1`,
`status: in_progress | blocked | done | failed` and a one-line `summary`, closed by a line holding only `---`, with the details below it.
Write it with `status: in_progress` before you start, so nothing else picks this up.
`done` and `failed` are final; `blocked` means you need an answer — put it in `question`.
List any files you changed under `artifacts:` as a YAML list of paths relative to the
project, one per line, like `- src/upload/retry.ts`.
```

Keep the **How to report** footer verbatim at the end of every brief: the executor may be a terminal session Cinna never talks to, and the footer is all it gets. Any other top-level key becomes a custom field of the task. A `ready` brief is never edited; if you got it wrong, write a new one.

**`report.md`** — the executor's side, the only file it owns:

```markdown
---
cinna_handover: 1
status: done             # in_progress | blocked | done | failed
summary: Retry added with backoff; three tests cover it
question:                # with blocked
artifacts:
  - src/upload/retry.ts
---
The full report, in Markdown.
```

`in_progress` before starting claims the brief. `done` and `failed` are final. An executor with the `cinna` tool server may call `tasks_report` instead of writing this file.

**`revisions/NNN.md`** — your follow-ups and answers to a `blocked` report (`001.md`, then `002.md`, …; each written once):

```markdown
---
cinna_handover: 1
title: Answer — use the uploader module
---
Your message.
```

Where Cinna runs the executor, a revision arrives in its own conversation with everything it has already read. Give several briefs the same `group:` and you are woken once, when all of them are over.

Once a report is final and you have read it, you may delete your own handover folder; nothing else cleans it up. Never write in another project's handover folder except to create your own.

## What the files cannot do

A folder with no Cinna Desktop open is never scanned: the files wait. There is no file form of `tasks_targets`; a requester must already know the project path. Approval, depth and the 20-per-conversation cap are enforced by the desktop when it picks the file up, so a file that oversteps them is refused with a visible reason rather than run.
