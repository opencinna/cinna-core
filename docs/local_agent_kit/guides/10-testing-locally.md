# 10 — Testing locally

## Read this when

Before you tell the user anything is finished, and always before `11-go-cloud.md`.

Building an agent and being the agent are different jobs. The build session knows
too much: which script you just wrote, what the test data looked like, what the
argument was called. The agent, in a fresh session, knows only its prompts and its
files. Testing is how you find the gap.

Locally the builder and the agent run in the same runtime and, in Cinna Desktop, the
same chat and engine, so the default guard against that gap is a static review of the
prompt, not a conversation.

Sections 1–4 are required. Section 5, the conversation test, is optional.

Going cloud is different: the cloud runs the agent in another environment and model,
so the post-import `cinna chat` check in `11-go-cloud.md` is mandatory.

## 1. Prompt-completeness review (required)

Read `docs/WORKFLOW_PROMPT.md` cold: only that file, not the build log, not
`README.md`, not your memory of the last hour. Then check:

a. Every script in `scripts/` that the agent calls is named, with its invocation and
   arguments.
b. Each script's output format is stated, or the reply format is.
c. Each entry in `example_prompts` maps to a documented path: a skill, a script, or a
   section of the prompt. An entry that maps to nothing is either a missing path or a
   wrong example. Decide which, and fix that one.
d. Nothing relies on build-session knowledge.

Use this table as the checklist:

| Symptom | The real defect |
|---------|-----------------|
| The prompt does not name a script the agent needs | Workflow prompt is incomplete. |
| The prompt does not give a script's arguments | Script has no usable `--help`, or the prompt omits the invocation. |
| The prompt does not state a script's output format | The prompt must state the format. |
| The prompt does not explain something you know from building | Something is documented nowhere. |
| The prompt promises an answer shape the scripts cannot produce | Prompt and behaviour disagree. |

Fix the agent, never the review. Then read the prompt again from the top.

## 2. Script-level checks (required)

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
case and nothing else. This check is required.

## 3. Secret leak sweep (required)

Non-negotiable, every time, before anything leaves the machine:

```bash
git check-ignore -v credentials/.env             # must match an ignore rule
git status --short                               # .env must not appear
grep -rn "credentials/.env" scripts/             # only cinna_credentials.py may
grep -rniE "password|api[_-]?token|secret" app-data/storage/ docs/ skills/ knowledge/ config/
```

The last grep should return only field *names* in documentation, never a value. If a
secret ever reached a committed file, treat it as leaked: rotate it, then clean it.

## 4. Validate (required)

```bash
uv run ../../.cinna-kit/tools/kit.py validate .
```

Errors block. Warnings are advice — read each one and decide out loud, do not
silently ignore them.

`--json` gives you a machine-readable report; `--fix` regenerates
`workspace_requirements.txt` from `pyproject.toml`.

## 5. Conversation test (optional)

A conversation test sends example prompts to the agent and judges the answers with
the §1 table. **Offer it once, in one line, after the required checks. Run it only if
the user asks. Never insist.** For example:

> Want me to run an example prompt through the agent as a final check?

When the user says yes, run any subset of `example_prompts` they choose, one of three
ways:

- **The agent folder has `app-data/desktop.json`:** send the prompt to the real agent.
  Cinna Desktop runs it behind a local API, and `kit.py chat` streams the answer back:

  ```bash
  uv run ../../.cinna-kit/tools/kit.py chat . "<example prompt>"
  ```

  `kit.py list` has a DESKTOP column saying, per agent, whether that call will reach
  the network. When the marker is missing, or the desktop is not running with this
  agent connected, `chat` exits non-zero with one line saying so. It **never** falls
  back to role-play, on purpose: an answer nobody can attribute is worse than no
  answer. Treat a failed `chat` as a cue to choose another way deliberately.
- **Inside Cinna Desktop:** open a **fresh** conversation with the agent and send the
  prompt there.
- **Otherwise, role-play.** Say *"Switching to the Agent role."*, read only
  `docs/WORKFLOW_PROMPT.md`, take the example prompt verbatim, run the real scripts
  against real data, and answer in the format the prompt specifies.

If an example needs a value from the user (`"Summarize invoice <invoice number>"`),
supply a plausible value **from the user's real data**, not from your fixtures, and
check that the agent asks for it rather than inventing one when it is missing.

Fix the agent, never the answer.

## 6. Report to the user

Three short lines: what you checked, what it did, what is not covered. No padding.

## What not to build

Do not add a unit-test framework, fixtures or CI to a small agent unless the user
asked. The required checks above are the proportionate suite here. If the agent
grows real business logic worth pinning down, a plain `scripts/selftest.py` with a
`selftest` command is the natural next step — one script, no framework.

## Done when

- Every script's `--help`, happy path and missing-credential path were run, and every
  `script_trigger` command prints exactly `OK` in the quiet case.
- The secret sweep returned nothing.
- `kit.py validate` exits 0, and every warning was consciously decided.
- The prompt-completeness review found nothing undocumented.
- The conversation test was offered once, and run or declined.
- You told the user what is checked and what is not.
