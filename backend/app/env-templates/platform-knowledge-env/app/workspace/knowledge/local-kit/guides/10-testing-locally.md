# 10 — Testing locally

## Read this when

Before you tell the user anything is finished, and always before `11-go-cloud.md`.

Building an agent and being the agent are different jobs. The build session knows
too much: which script you just wrote, what the test data looked like, what the
argument was called. The agent, in a fresh session, knows only its prompts and its
files. Testing is how you find the gap.

## 1. The role-switch test

The core test, and the one that finds most defects.

1. Deliberately drop the build context. Say so out loud: *"Switching to the Agent
   role."*
2. Read **only** `docs/WORKFLOW_PROMPT.md`. Not the build log, not `README.md`, not
   your memory of the last hour.
3. Take the agent's first `example_prompt` verbatim as the user's message.
4. Do the work. Run the real scripts against real data.
5. Answer in the format the workflow prompt specifies.

Then judge honestly:

| Symptom | The real defect |
|---------|-----------------|
| You needed a script name that was not in the prompt | Workflow prompt is incomplete. |
| You guessed an argument | Script has no usable `--help`, or the prompt omits the invocation. |
| You had to open a script to know its output format | The prompt must state the format. |
| You reached for build-session knowledge | Something is documented nowhere. |
| The answer's shape differs from what the prompt promised | Prompt and behaviour disagree. |

Fix the agent, never the answer. Then run the test again from a clean read.

## 2. Example prompts are the acceptance suite

Every entry in `example_prompts` must be answerable, in the Agent role, with only
the prompt and the files. Run through all of them.

If an example needs a value from the user (`"Summarize invoice <invoice number>"`),
supply a plausible value **from the user's real data**, not from your fixtures — and
verify the agent asks for it rather than inventing one when it is missing.

An example you cannot answer is either a broken agent or a wrong example. Decide
which, and fix that one.

## 3. Script-level checks

For every script:

```bash
uv run scripts/<x>.py --help            # exits 0, arguments are self-describing
uv run scripts/<x>.py <normal args>     # exits 0, output matches what README claims
```

Then the failure paths, which is where unattended agents actually break:

- a missing credential → a clear message naming the slot, **and no value printed**;
- an unreachable source → non-zero exit and a readable message, not a traceback dump;
- empty input → a defined result, not a crash;
- re-running twice → the same result (idempotent), or a documented reason why not.

For a `script_trigger` schedule command, confirm it prints exactly `OK` in the quiet
case and nothing else.

## 4. Secret leak sweep

Non-negotiable, every time, before anything leaves the machine:

```bash
git check-ignore -v credentials/.env             # must match an ignore rule
git status --short                               # .env must not appear
grep -rn "credentials/.env" scripts/             # only cinna_credentials.py may
grep -rniE "password|api[_-]?token|secret" app-data/storage/ docs/ knowledge/ config/
```

The last grep should return only field *names* in documentation, never a value. If a
secret ever reached a committed file, treat it as leaked: rotate it, then clean it.

## 5. Validate

```bash
uv run ../../.cinna-kit/tools/kit.py validate .
```

Errors block. Warnings are advice — read each one and decide out loud, do not
silently ignore them.

`--json` gives you a machine-readable report; `--fix` regenerates
`workspace_requirements.txt` from `pyproject.toml`.

## 6. Report to the user

Three short lines: what you tested, what it did, what is not covered. No padding.

## What not to build

Do not add a unit-test framework, fixtures or CI to a small agent unless the user
asked. The role-switch test plus the example prompts is the proportionate suite
here. If the agent grows real business logic worth pinning down, a plain
`scripts/selftest.py` with a `selftest` command is the natural next step — one
script, no framework.

## Done when

- The role-switch test was run from a clean read of `docs/WORKFLOW_PROMPT.md` and
  produced a correct answer with no build-session knowledge.
- Every `example_prompt` was executed and answered.
- Every script's `--help`, happy path and missing-credential path were run.
- The secret sweep returned nothing.
- `kit.py validate` exits 0, and every warning was consciously accepted.
- You told the user what is tested and what is not.
