# 02 — Prompts and description

## Read this when

Always, before the agent is tested for the first time — and again whenever what the
agent *does* changes. This rung is never skipped.

Write the prompt set **last**, from what you actually built. A prompt authored from
the plan instead of from the finished scripts describes an agent that does not exist.

## The six fields

They look similar. Each is consumed by a different system at a different moment. Do
not conflate them.

| Field | Where it lives | When it fires | Shape |
|-------|----------------|---------------|-------|
| `description` | `cinna-agent.json` | Discovery: agent cards, catalogs; also feeds router-trigger generation | One clear sentence with a verb and an object. *"Reconciles Stripe payouts against the ledger and flags mismatches."* |
| `workflow_prompt` | `docs/WORKFLOW_PROMPT.md` | Every conversation-mode session — the agent's main system prompt | Operational: which scripts to run, how to parse their output, how to present results, what decisions to make. |
| `entrypoint_prompt` | `docs/ENTRYPOINT_PROMPT.md` | The first message of a **scheduled / automated** run | One or two conversational sentences. Fully self-contained — nobody is there to fill anything in. |
| `refiner_prompt` | `docs/REFINER_PROMPT.md` | Turning a vague request into a structured task, before execution | Default-fill rules and mandatory fields. |
| `router_trigger_prompt` | `cinna-agent.json` | Only by the routing classifier, when several agents could answer | One capability-verb sentence. Says *when to route here*, never how to behave. |
| `example_prompts` | `cinna-agent.json` | Shown as task suggestions **and** read by the routing classifier | Short imperative templates a stranger can send as-is. |

An agent with neither `router_trigger_prompt` nor `example_prompts` cannot be routed
to at all once it reaches the cloud.

## Writing `workflow_prompt`

The agent is a *bridge*: it runs scripts, parses their output, and rephrases the
result conversationally. Write it that way.

Cover, in this order:

1. **Identity and job** — one or two sentences.
2. **The procedure** — numbered. Name the exact script, its arguments, and the exact
   output format it prints (JSON / CSV / lines).
3. **Decision logic** — what counts as a problem, what to flag, what to ignore, and
   what to say when there is nothing to report.
4. **Presentation** — table, summary line, bullet list. Be specific.
5. **Boundaries** — what to refuse, and what to do when a credential or input is
   missing (say which one and stop; never guess).

Do not restate the folder layout, do not restate these conventions, and do not
duplicate anything from `AGENTS.md`. `docs/WORKFLOW_PROMPT.md` is the single source
for behaviour; the wrapper only points at it.

## Writing `entrypoint_prompt`

It is fired automatically by a schedule, with no human present.

- Correct: *"Check the invoices that arrived since yesterday."*
- Wrong: *"Query the API and return JSON."* (technical, not a user's message)
- Wrong: *"Check invoices for <account>."* (a placeholder nobody will fill)

Mandatory once the agent has a schedule. Defaults it relies on belong in
`refiner_prompt`.

## Writing `example_prompts` — templates, not a replay of your build

This is the field that goes wrong most often. Examples are shown to a **different
person, at a different time, with different data**. The classic failure is copying
concrete values out of the build session and freezing them in:

- Wrong: `"Summarize invoice INV-88421"`
- Wrong: `"Reconcile payouts for account acct_1M4kTest"`
- Wrong: `"Analyze data/sample_export.csv"`

A user who sends any of those gets an answer about *your* test data.

Two legitimate shapes:

**1. Universal** — needs nothing from the user; ready to send as-is.

- `"What is my status today?"`
- `"reconcile last week"`
- `"List everything waiting on my approval"`

**2. Input-required** — the task is meaningless without a concrete value. Write a
template that visibly stops where the user takes over, with a bracketed placeholder
that can never be mistaken for data.

- `"Summarize the invoice with number <invoice number>"`
- `"Investigate this URL — <paste the URL here>"`

Never substitute a realistic-looking fake for the placeholder. A plausible fake is
worse than an obvious blank: the user sends it unchanged and the agent does real
work against a value nobody meant.

**Colons are structural.** Each entry is parsed as `slug: prompt text` and the first
colon splits the line. `"Investigate this URL: <paste the URL here>"` becomes a
prompt *named* "Investigate this URL" whose whole body is the placeholder. Use an em
dash, or fold the placeholder into the sentence. The explicit form
`investigate_url: Investigate this URL — <url>` is also valid; just never leave a
stray colon in a slugless line.

### Self-check before you write the list

- [ ] No URL, hostname, id, account, email address, file path or date carried over
      from building or testing.
- [ ] No organisation or person names from fixtures.
- [ ] Every entry is either fully universal or an obviously unfinished template.
- [ ] Short and imperative — a handful of words before the placeholder.
- [ ] 3–6 entries covering *distinct* capabilities, not variations of one.
- [ ] Would this still be meaningful to a stranger, next month, with their own data?

## Writing `router_trigger_prompt`

One sentence describing the capability, so a classifier can decide whether an
incoming message belongs to this agent: *"Reconciles Stripe payouts and flags ledger
mismatches."* Not instructions, not a personality, not a paragraph.

If you cannot write it, the agent's job is probably not narrow enough yet.

## Keeping them in sync

Whenever the agent's behaviour changes:

1. Update `docs/WORKFLOW_PROMPT.md` (the procedure that changed).
2. Rewrite `description` so it still matches the finished agent.
3. Re-check `example_prompts` against the self-check list.
4. Re-check `router_trigger_prompt` if a capability was added or removed.

An agent whose description and prompts match its actual behaviour is the mark of a
finished build.

## Done when

- `docs/WORKFLOW_PROMPT.md` names every script that exists and no script that does not.
- `cinna-agent.json` `description` is one sentence describing the finished agent.
- `example_prompts` has at least one entry (aim for 3–6) and passes the self-check.
- `router_trigger_prompt` is a single capability sentence.
- `docs/ENTRYPOINT_PROMPT.md` is self-contained, or the agent has no schedule.
- You read `docs/WORKFLOW_PROMPT.md` cold and could execute the job from it alone.
