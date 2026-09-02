# 01 — Your first agent

## Read this when

The user wants a new agent, or you are creating the first agent in this workshop.
Stop reading other guides until this one is done; it tells you which ones to open.

## 1. Interview (short, then build)

Ask at most four questions in one message. Do not build on guesses, and do not ask
things the user already told you.

1. **What job should it do?** Push until you have one sentence with a verb and an
   object: *"flag invoices that arrive without a purchase-order number"*.
2. **Where does the data come from?** A mailbox, an API, a folder, a database, a
   web page, or nothing at all (a prompt-only agent).
3. **What should come out?** A chat answer, a file, a table, an alert.
4. **When does it run?** On request only, or on a schedule?

Answer 2 decides whether the **Credentials** rung fires. Answer 4 decides whether
the **Schedules** rung fires. Do not add either yet — note them.

Then propose a `slug`: lowercase, hyphenated, `^[a-z0-9][a-z0-9-]{1,62}$`, and the
folder name. Confirm it with the user, because renaming later touches the manifest,
the docs and the cloud reference.

## 2. Scaffold

From the workshop root:

```bash
uv run .cinna-kit/tools/kit.py new invoice-watcher --name "Invoice Watcher"
```

This copies `templates/agent/`, fills `cinna-agent.json` and prints next steps. It
refuses if the folder exists. Never build the folder by hand.

Look at what you got before writing anything: `AGENTS.md`, `README.md`, `Makefile`,
`cinna-agent.json`, `docs/`, `scripts/`, `config/`, `credentials/`, `knowledge/`,
`files/`, `app-data/`. Each folder has a defined meaning — see
`03-scripts-and-data.md`. Do not invent new top-level folders.

## 3. The build loop

Work in small cycles. One cycle = one capability.

1. **Write the smallest script that does one step.** Print its result in a machine
   readable shape (JSON or CSV) and write large results to `app-data/storage/`.
   Read `03-scripts-and-data.md` the first time you write a script.
2. **Run it yourself.** `uv run scripts/<x>.py`. It must work before it is
   described anywhere.
3. **Catalog it** in `scripts/README.md` in the same change. Not later.
4. **Wire it into `docs/WORKFLOW_PROMPT.md`**: which script, how to read its output,
   how to present it.
5. **Ladder check** (`.cinna-kit/README.md`) and report one line.

Repeat until the job from the interview is done end to end.

If a credential is needed, stop and read `04-credentials.md` at that moment — not
before, not after the script is already reading files directly.

## 4. Prompts and description

Write the prompt set **last**, from what actually exists. Read
`02-prompts-and-description.md` and fill in:

- `docs/WORKFLOW_PROMPT.md` — the real execution instructions
- `description` — one accurate sentence
- `example_prompts` — at least one, written for a stranger
- `router_trigger_prompt` — one capability sentence

Also fill `docs/ENTRYPOINT_PROMPT.md` if the agent has (or will have) a schedule.

## 5. Test as the agent

Switch to the **Agent** role: forget the build context, read only
`docs/WORKFLOW_PROMPT.md`, and answer the user's first example prompt. If you needed
something that is not in the prompt or the scripts, that is the bug — fix the agent,
not the answer. Full procedure in `10-testing-locally.md`.

## 6. Validate

```bash
uv run .cinna-kit/tools/kit.py validate Local/invoice-watcher
```

Fix every error. Warnings are advice: read them, then decide.

## 7. Tell the user what exists

One short paragraph: what the agent does, how to run it, which example prompt to
try, and what is deliberately not built yet. No feature list padding.

## Common mistakes

| Mistake | Why it hurts |
|---------|--------------|
| Building the folder by hand | Layout drifts from the cloud contract; `validate` fails later. |
| Writing prompts before the scripts work | The prompt describes an agent that does not exist. |
| One large script that does everything | Cannot be re-run, re-used or scheduled in pieces. |
| Adding credentials / schedules / status "while we are here" | Unused scaffolding the user has to understand and maintain. |
| Leaving `scripts/README.md` stale | The agent tells users to run commands that do not exist. |

## Done when

- `Local/<slug>/` exists and was created by `kit.py new`.
- Every script in `scripts/` runs successfully and is described in `scripts/README.md`.
- `docs/WORKFLOW_PROMPT.md` names the real scripts and the real output format.
- `cinna-agent.json` has a true `description` and at least one `example_prompt`.
- `kit.py validate Local/<slug>` exits 0.
- You answered one example prompt in the Agent role without consulting the build log.
