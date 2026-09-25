# 13 — Design patterns: what to recommend before you build

## Read this when

The `design` rung fires: the agent reaches an external system; sends, creates,
signs or posts something; answers more than one kind of question; or will be used
by someone other than the person building it. Read §0 during the interview for
every new agent, before the first script.

These patterns come from agents running in production. Each exists because its
absence once produced a wrong number, an exposed field, a double send, or an
answer nobody could re-check. An agent built this way can be handed over, moved to
a smaller model and changed without fear.

The guide is written local-first; each section ends with what differs in the
cloud. In a cloud building session this file is
`/app/core/prompts/AGENT_DESIGN_PATTERNS.md`, and "kit guide NN" pointers map to
`COMPLEX_AGENT_DESIGN.md` and `REST_API_BUILDING.md` beside it.

## 0. The advisor table

The user is usually not an architect, so do not only ask ("CSV or SQLite?"). When a
row below fires, **recommend** the pattern: the reason in one sentence, the cost in
another, then ask for a yes. Recommend every row that fired in one message, before
you build. A row that has not fired is not recommended — an agent with one kind of
question and no external system needs none of this.

| When you notice… | Recommend… (pattern) |
|---|---|
| a credential whose reach exceeds what the agent needs | a producer agent-api in front of it, even if private (§3) |
| the model would add, subtract, compare or derive dates/numbers | move the figure into the script/producer payload (§4) |
| ≥ 2 distinct kinds of question, or wording that must change without redeploy | skills + routing table (§1) |
| a second audience, schedule or credential set | a second agent, not a bigger prompt (§2) |
| a send / create / sign / post | confirmation gate + `--dry-run` + audit log + idempotency key (§6) |
| "remember what was sent", retries, polling, a queue | SQLite under `app-data/storage/` (§5) |
| the agent runs unattended | `script_trigger` with the OK contract + `STATUS.md` (§7) |
| the agent will serve other people | `docs/test_scenarios/` before the first hand-over (§9) |
| a prompt/skill/model/provider change | re-run the scenario set (§9) |
| conversation latency or cost matters | keep §4/§1, then try the smaller conversation model (§10) |
| the user's answer contradicts the recommendation | say the trade-off once, then build what they chose |

What a recommendation sounds like:

> Before I build, three recommendations. (1) The HR login this needs can also read
> salaries, so I'd put it in a small API agent that returns only time-off fields;
> the assistant never holds the login. It costs one more agent to maintain.
> (2) "Days left" and "who is away" are different questions: one skill each keeps
> the prompt short and lets us reword one without touching the other. (3) Your
> team will use it, so I'll record a test scenario per skill and re-run them after
> any prompt or model change. OK to go this way?

Write each decision, including the declined ones and the reason, into
`docs/AGENT_DEVELOPMENT.md` (§11) so the next session does not re-argue it.

## 1. The prompt routes; the skill executes

**Rule.** `docs/WORKFLOW_PROMPT.md` holds the scope and a routing table. Each kind
of question is one skill, `skills/<name>/SKILL.md`. The prompt says: invoke the
matching skill before writing anything; decline what no skill covers.

The workflow prompt, in order:

1. **Scope.** What is in; what is out "however easy it would be to answer"; the
   refusal — one or two sentences naming two things the agent *can* do. An
   off-topic turn loads no skill and calls nothing, which also keeps a refusal
   cheap. A mixed request answers its in-scope half. Refusing an in-scope question
   asked in everyday words ("can I take Friday off") is a defect too.
2. **Routing table**, in the user's words:

   | Skill | When the user asks… |
   |---|---|
   | `my-balance` | about **their own** days — "how many days do I have", "am I losing any" |
   | `who-is-away` | whether **a named person or team** is in — "is sam around", "who's off friday" |
   | `team-balances` | how many days **somebody else** has left — "can my team still book august" |

3. **Tie-breaks** for questions between two rows, and "a request spanning two
   skills → invoke both".

A skill's `description` carries the trigger phrases **and** the boundary — *"…Use
for 'is sam around', 'who's off friday'. Not for days left (my-balance,
team-balances)."* It is the only text the model reads before choosing.

**Put a rule where the model reads it while acting.** A rule stated once in a Tone
section at the bottom of the prompt changed nothing in production; the same rule
repeated at the end of every `SKILL.md` worked. End each skill with a short
pre-answer reminder of the mistakes that happen at the moment of writing:

> **Before you write anything:** (1) no text before the answer — whatever you write
> before running the command is glued to the front of the reply; (2) use the
> person's name or they/them, never a guessed he/she; (3) quote figures from the
> output, never compute one.

Examples inside a skill use names, never pronouns: a pronoun in an example is
copied into real answers. A skill reaches nothing outside its own folder.

*Locally:* `AGENTS.md` makes the assistant open the `SKILL.md` a prompt names (kit
guide 08). *In the cloud:* the engine indexes descriptions and loads a body only on
invocation, so ten skills cost ten lines until one is used.

## 2. Skill, second agent, agent API or delegation?

| The new capability… | Build it as |
|---|---|
| shares the agent's audience, credentials and tone | a **skill** in the same agent (§1) |
| answers to different people, runs on another schedule, or needs a credential the first agent must not see | a **second agent**, not a bigger prompt |
| is data that code needs with no model in the loop, or sits behind a credential that must be narrowed first | a **producer agent API** (§3) |
| needs another agent's judgement, not its data | a **handover** (agent-to-agent delegation) |
| should be a tool inside some other model's session | an **MCP connector** |

Do not split for tidiness: two agents are two prompt sets to keep true. When
requests keep landing between two agents, redraw the boundary; never copy a script.

*Locally:* a second agent is a sibling folder plus `handovers[]` (kit guide 09);
agent APIs and connectors exist only in the cloud, so keep that boundary as a
module (§3). *In the cloud:* `REST_API_BUILDING.md` builds the producer; consumers
connect with `cinna connect agent-api`.

## 3. The credential lives in one producer; consumers hold a connection

**Rule.** An external system's credential lives in exactly one producer agent that
exposes a narrow agent API. Conversational agents hold only the connection to it.

It fires whenever the credential can do more than the agent needs — nearly always:
the HR login that answers time-off questions can read salaries too. The wrapper is
the security boundary **even for a private agent nobody else will call**: a prompt
injection, a careless skill or a curious user reaches only what the producer
exposes.

Inside the producer, name the fields you read **and the ones you never read**, and
route every read of the sensitive model through one function:

```python
# The ONLY contract fields this API may ever read.
CONTRACT_FIELDS = ("employee_id", "date_start", "job_id")
# Named so a future reader sees the intent rather than inferring it from absence.
CONTRACT_FIELDS_NEVER_READ = ("wage", "salary", "notes")

def _read_contracts(client, domain):
    """The single choke point for contract reads. Widening it is a deliberate act."""
    leaking = [f for f in CONTRACT_FIELDS if f in CONTRACT_FIELDS_NEVER_READ]
    if leaking:
        raise error(500, f"Refusing to read protected field(s): {', '.join(leaking)}")
    return client.search_read("hr.contract", domain, fields=list(CONTRACT_FIELDS))
```

- Pin read-only twice: `read_only: true` in `agent_api/policy.yaml`, and an
  allowlist of read methods in the upstream client.
- Authorize every call from the system of record ("is the caller this person's
  manager?"), never from an id the caller sends.
- Keep `agent_api/CONSUMERS.md` beside the code — the contract no consumer's test
  will catch: a table of consumer agent → endpoints → fields relied on and why,
  then the change rules: (1) additive is safe; (2) renaming, removing or retyping a
  field is breaking — grep the consumers first; (3) changing a classification is
  breaking even when the shape holds; (4) after a change, refresh the spec and
  smoke-test each consumer from its own environment; (5) update this file in the
  same change.

In each consumer, every call carries **two** things: the connection's bearer token
("this agent may call") and the caller-identity header ("and I am this person").
In the cloud, `credentials.agent_api_session("<slot>")` builds a session with both
when the platform provided an identity. Reading `credentials.json` by hand, match
the connection by `type == "agent_api"` **and** `producer_agent_id` — never by label
or "the first one" — and read the identity header's name from its entry. A missing
connection, a missing identity, or a 401 from a producer that answers per person is
a **setup problem**: print the fix (`cinna connect agent-api --producer … --consumer
…`) and exit 3. Never turn it into a question for the user, and never let a call go
out anonymous — an anonymous answer carries the wrong permissions and looks right.

*Locally:* there is no agent API. Keep the boundary as one module:
`scripts/<system>_client.py` is the only file that reads the credential, holds the
field constants and exposes a few functions, so it lifts into a producer when the
agent goes to the cloud. *In the cloud:* `REST_API_BUILDING.md` — and never import
one `agent_api/*.py` from another; shared code lives under `scripts/`.

## 4. One rich payload per question; the model computes nothing

**Rule.** One call answers the whole user question. The payload carries every
figure, verdict and label the reply needs, and the model quotes it. If the question
needs a number the payload lacks, add it to the script or producer — never let the
model assemble it. It fires the moment the model would add, subtract, compare,
count, convert units, or work out a date or a weekday.

A good payload carries the context of the answer (who, which period, which scope),
counts, rows, pre-computed verdicts (`urgency`, `enough: true`), spelled-out dates
(`{"date": "2026-09-15", "weekday": "Tuesday"}`), local-time siblings
(`sent_at_local`), `warnings`, `data_fetched_at`, and an honest `truncated: true`
whenever a cap bit. A list gets a `view=summary` projection so 25 rows fit one tool
output.

What its absence looked like in a production team-planning agent:

| Question | Reply | Cause | Fix |
|---|---|---|---|
| "how much overtime does the team have this week" | "3 h" — really 18.5 h, and attributed to the wrong person | the script printed daily rows from a window the model had narrowed; the model summed them | the script prints per-person and team totals for the requested window |
| "who tracked how long yesterday" | "7 h" — the person clocked 9 h | the only hours column was in-schedule time | `hours_tracked` and `hours_lunch` on every row, quoted, never derived |

Every one was a number the model had to assemble.

- **Authorization is re-derived server-side on every call.** Rows the caller may
  see only partly have the sensitive fields *removed*, and the prompt says a
  limited row is a complete answer: never fill the gap with a guess.
- **Errors are answers.** 409 lists the candidates with enough detail to choose;
  404 lists the valid values; 403 is relayed in one sentence together with what the
  caller *can* have; a closed `hint` vocabulary maps to phrasing in the prompt.
- **The prompt states the rule in so many words:** *"Never do date or number
  arithmetic. If you are about to write a number that is not in the output, stop
  and make another call."*

"One step per script" still holds for the steps. Add one composition script or
endpoint that runs them and prints the answer-shaped payload, so conversation mode
makes one call. This is what lets a small conversation model answer correctly
(§10).

## 5. Durable state is SQLite under `app-data/storage/`

**Rule.** Queues, ledgers, "already sent" logs and mute lists live in
`app-data/storage/<area>/<name>.db`, which survives updates and reinstalls and is
never shipped with the agent. Disposable copies of upstream data go to
`app-data/cache/<area>/`.

```python
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")  # readers never block the processor

def claim_next(conn):
    """Move the oldest due job to `processing`; None when nothing is due."""
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM jobs WHERE status = 'queued' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT 1",
        (now(),)).fetchone()
    if row:
        conn.execute("UPDATE jobs SET status = 'processing', attempts = attempts + 1 "
                     "WHERE id = ? AND status = 'queued'", (row["id"],))
        conn.execute("INSERT INTO job_events (job_id, status, created_at) "
                     "VALUES (?, 'processing', ?)", (row["id"], now()))
    conn.commit()
    return row
```

- The status-guarded `UPDATE` stops two overlapping ticks — or an inline path and
  a schedule — from sending the same thing twice.
- Retries back off across ticks (`next_attempt_at`), with a cap; permanent errors
  (4xx, validation) fail at once.
- One append-only event row per transition gives "why is this stuck" its history.
- Schema changes are additive: `ALTER TABLE … ADD COLUMN`, guarded by
  `PRAGMA table_info`. Watermarks only move forward (`MAX(new, existing)`).

A cache is rebuildable: a TTL, a `--fresh` flag the prompt passes when the user
says "check again", and `from_cache` / `data_fetched_at` in every payload. Deleting
`app-data/cache/` changes only latency.

*Locally and in the cloud:* the same paths; `app-data/` is git-ignored and never
imported. Runtime state never goes into `files/`, `docs/` or `scripts/` — those are
replaced on every update.

## 6. External side effects: gate, dry-run, idempotency key, audit log

It fires when a script or endpoint sends, creates, signs, posts, books or deletes
anything outside the agent.

1. **Gate.** Conversation mode shows a short summary of exactly what will happen
   and waits for an explicit yes. A script that writes says so in its docstring:
   *"This WRITES to the live ticket. Only call it after the user confirms."*
2. **`--dry-run`** validates, prints the normalized payload, and touches nothing.
   Multi-field input goes through a job file scaffolded by `--init-job`, never
   inline JSON on the command line — the shell mangles quotes.
3. **A deterministic key** derived from what the action is about
   (`daily-summary-2026-09-14`, the document name), never from the clock — checked
   against the audit table before anything goes upstream.
4. **Audit every attempt** — success, repeat, failure — in SQLite (§5): `action_key`
   (unique together with the target), `outcome` (`done` · `already_done` ·
   `failed`), `target`, `upstream_id`, `error` (never a secret), `caller_user_id`,
   `caller_email`, `created_at` (UTC).
5. **Explicit override only.** A repeat returns the stored result with
   `already_done: true`; only `force=true` performs the action again.
6. **Escalate once.** A condition ("document overdue") is reported the first time
   and flagged in the database, not on every tick.

Scripts emit **facts** — the rows the user checks, shown as receipts above a drafted
message — and the **wording** lives in the skill. When the model drafts something
containing facts (counts, totals), the finalizing script recomputes them from the
data and ignores the draft's values.

*Locally:* test through `--dry-run`, and send for real only to yourself. *In the
cloud:* the same rules apply to endpoints; `COMPLEX_AGENT_DESIGN.md` has the shapes.

## 7. Unattended runs

A scheduled check is a `script_trigger` that prints exactly `OK` on the quiet path
(informational lines go to stderr) and short, agent-addressed context otherwise.
Every run updates `STATUS.md`, on success **and** on failure, and a condition
escalates once (§6). Plain `uv run` without `--quiet` breaks the `OK` contract: uv
prints resolver output on the first run after a rebuild. In cloud schedules and
`/run:` commands write `uv run --quiet python /app/workspace/scripts/<x>.py`; a kit
manifest keeps its cloud-first `python scripts/<x>.py`, which does not go through
uv. Details: kit guides 05 and 06; `COMPLEX_AGENT_DESIGN.md` in the cloud.

## 8. Where the prompts live

The three document-backed prompts exist twice: `docs/WORKFLOW_PROMPT.md`,
`docs/ENTRYPOINT_PROMPT.md` and `docs/REFINER_PROMPT.md` in the workspace, and the
matching fields of the agent's configuration. The platform reconciles the two in
both directions: a change on one side flows to the other, and when both changed
since the last sync the later write wins.

- **One path per change** — edit the doc in the workspace *or* write the
  configuration, never both in one session.
- `description`, `router_trigger_prompt` and `example_prompts` exist only in the
  configuration; no doc edit changes them.
- **Verify what the runtime uses** with `cinna agent show <slug> --prompts`, not by
  re-reading the file you edited.
- Skill bodies are files, never prompt fields.
- `example_prompts` are templates a stranger sends: no real people, ids or values
  from your build.

*Locally:* `docs/*.md` and `cinna-agent.json` are the source, and the import writes
both sides once. *In the cloud:* either path works; mixing them is the race.

## 9. Test scenarios — the regression set

It fires when the agent will serve anyone other than its builder — and the set is
re-run after every change to a prompt, a skill, the model, the provider, or a
producer's response shape.

```
docs/test_scenarios/
├── README.md               # fixtures, how to run, the bisect rule
├── scope_and_pushback.md   # what it refuses, and what it must not over-refuse
└── <kind_of_question>.md   # one per skill
```

```markdown
# Who is away — protects the `who-is-away` skill

## What must be true
1. Dates only — never the reason for an absence.
2. Someone with nothing booked is "working", not "unknown".

## Fixtures (verified 2026-09-14)
| Record | What it proves |
|---|---|
| "Sam", employee 497 | has booked leave next week |
| service account 506 | not a person; must never appear |

## Say | Expect
| Say | Expect |
|---|---|
| `is sam around tmrw` | "out of office until <date>", no reason, one skill call |
| `whats our q3 revenue` | one-line redirect naming two things it does; no tool call |
| `can i take friday off` | routed, **not** refused |

## Traps
- The profile card has a `today` block. It is not evidence for next week.
```

- **Say** is phrased like a rushed human — lowercase, missing context, pronouns. A
  precisely worded test passes on an agent that fails in production.
- Run each case at three levels: the conversation (read the tool calls too — an
  out-of-scope turn has none); the skill's script as that user (its JSON
  verbatim); the connection check.
- **Bisect:** the data is right and the reply is wrong ⇒ fix the skill or prompt.
  The data is wrong ⇒ fix the script or producer.
- When behaviour was wrong, keep a before/after table in the scenario file.
- `example_prompts` are the smoke subset; the scenario set is the full run.
- `docs/` travels with the agent. Name fixtures by role and id, never with personal
  details.

*Locally:* talk to the agent with `kit.py chat` when Cinna Desktop runs it,
otherwise in the Agent role (kit guide 10). *In the cloud:* `cinna chat --agent
<slug> "…"` and read its `events`; `cinna exec` runs a skill script.

## 10. Model per mode: build strong, converse small

Building needs the strongest model available; conversation usually does not. §4
(the model quotes, never computes) and §1 (skills load on demand, refusals make no
call) are what make a small, fast tier sufficient for conversation — say so when you
recommend it. The gate is §9: switch the conversation model, re-run the scenario
set, and keep the switch only if it passes.

Stay provider-neutral: the strong tier of the user's provider for building, its
small tier for conversation — with OpenAI, for example, a flagship model building
and a mini-class model conversing.

- *In the cloud:* the agent's page → **Environments** → edit the conversation
  mode's model → rebuild. From an account workspace the equivalent is
  `cinna api POST environments/<env_id>/reconfigure` (the environment id is the
  agent's `active_environment_id`). Send the full settings of **both** modes —
  read them first with `cinna api GET environments/<env_id>`, because a field you
  leave out is reset — plus `"rebuild": false`, then apply it with
  `cinna agent rebuild-env <agent>`: the API escape hatch gives up after 30
  seconds, and a rebuild takes minutes. The per-mode
  `conversation_ai_credential_id` / `building_ai_credential_id` (with
  `use_default_ai_credentials: false`) pin the user's own provider keys the same
  way.
- *Locally:* the model is whatever your assistant or Cinna Desktop runs. A host that
  offers a choice reads `runtime` in `cinna-agent.json`: `engine` is a preference
  (`opencode`, `claude` or `codex`; a host that does not support it runs OpenCode),
  and `complexity` (`simple` | `medium` | `complex`) is the portable way to ask for
  a tier — prefer it to a concrete `model`, never write both. Both `model` and
  `complexity` stay local; the cloud import does not carry them. Record the
  intended conversation tier in `docs/AGENT_DEVELOPMENT.md` so it is set once the
  agent is in the cloud.

## 11. A development map with a defects log

Ship `docs/AGENT_DEVELOPMENT.md` for the next person who builds on the agent. The
workflow prompt never references it, so it costs no runtime context.

- **Topology** — this agent, the agents it calls or serves (with ids), what each holds.
- **File map** — what lives where; which files are generated, and from what.
- **Design decisions** — the §0 recommendations, accepted or declined, and why;
  the chosen conversation model and the scenario run that justified it.
- **Invariants** — what a change must not break ("never submit before the user
  confirms", "connections by producer id").
- **How to extend** — adding a skill, a field, an endpoint, a schedule.
- **Testing** — the commands from §9.
- **Defects this agent already had — do not reintroduce them**, dated, each with
  symptom, cause, fix and where the rule now lives:

  > **Plan narration glued to the answer** (found in chat testing). Every text
  > block of a turn is shown as one message, so "I'll look that up…" prefixed the
  > reply. A "no preamble" line in the prompt's Tone section did nothing; the fix
  > was explaining the mechanism near the top of the prompt **and** repeating it in
  > every skill's pre-answer reminder.

When the agent carries business rules someone could argue with, add
`docs/SPEC.md`: a table of assumptions (A1…An — the assumption, why it matters,
the confirmed decision) mirrored one-to-one into constants in a single `rules.py`,
and a "where the build deviates" log instead of silent edits.

## Done when

- Every §0 row that fired was recommended with its reason, and the decision is in
  `docs/AGENT_DEVELOPMENT.md`.
- The workflow prompt is scope plus routing; every skill `description` names its
  triggers and what it is not for; every `SKILL.md` ends with a pre-answer reminder.
- No conversational agent holds a credential with more reach than it needs.
- No number, date or weekday in a reply is computed by the model.
- Every external side effect has a gate, `--dry-run`, a deterministic key and an
  audit row.
- Durable state lives under `app-data/storage/`, disposable data under
  `app-data/cache/`.
- `docs/test_scenarios/` exists before other people use the agent, and it was
  re-run after the last prompt, skill, model or provider change.
