# Claude Code

Notes for working in this workshop with Claude Code. Everything in `START.md`, the
kit `README.md` and the guides applies unchanged; this file only covers what is
specific to this assistant.

## `CLAUDE.md` and `AGENTS.md`

The kit writes both at the workshop root and in every agent folder:

- `AGENTS.md` holds the actual instructions — it is the assistant-neutral file.
- `CLAUDE.md` is one line, `@AGENTS.md`, plus a pointer to this document.

Never duplicate content into `CLAUDE.md`. The import directive is enough, and two
copies drift.

Files are picked up by directory. At the root you get the orchestrator role; the
moment you work inside `Local/<slug>`, that agent's pair wins. When you change
folder, say which role you switched into.

If the user already had a `CLAUDE.md` in the chosen root, do not overwrite it. Append
the delimited pointer block from `START.md` instead — it is idempotent.

## Permissions

The agent scaffold ships `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": [
      "Bash(uv run:*)",
      "Bash(make:*)"
    ]
  }
}
```

That covers the normal build loop without a prompt per command. Add entries only for
things the user actually runs repeatedly, and never add a blanket `Bash(*)`. Network
commands (`curl`, `git push`, `cinna`) stay unapproved on purpose — they should be a
visible decision.

## Asking questions

Use `AskUserQuestion` when you genuinely need input you cannot infer:

- the agent's job is ambiguous, or several readings are valid;
- a required credential does not exist yet;
- a design choice materially changes the result (CSV or SQLite, one agent or two);
- an assumption would silently decide something for the user (timezone, date format,
  which mailbox).

Ask specific questions with options, grouped into one message, and say why you need
the answer. Do not ask what the user already told you, and do not ask about things
the guides already decide — folder layout, file names, conventions.

## Habits that fit this kit

- **Read on demand.** Read a guide when its ladder trigger fires, not up front.
- **Small edits, verified.** Run the script you just wrote before describing it.
- **Same-change doc updates.** A new script and its `scripts/README.md` entry are one
  change, never a follow-up.
- **Announce the role switch** in one line: Orchestrator, Builder or Agent.
- **Use the tool, not bespoke shell.** `kit.py new` / `validate` / `list` exist so
  every assistant produces the same layout.

## Secrets

Never `cat`, `Read`, `grep` or otherwise open `credentials/.env` or
`credentials/credentials.json`. Not to "check the format" — `.env.example` is there
for that. If a tool result ever contains a secret, do not repeat it in your reply,
and tell the user which file leaked it.

## Downloading the kit

You can normally run the `curl … | tar xz` from `START.md` yourself. If the user has
network commands unapproved, prefix it for them to approve, or ask them to paste the
three lines into their own terminal and tell you when they are done.

## Plan mode and long builds

For anything past a couple of scripts, sketch the ladder rungs you expect to need,
confirm the plan with the user, then build one rung at a time and report after each.
The ladder is the plan; do not invent a parallel one.
