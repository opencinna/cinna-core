# Authoring & Assigning Agent Prompts

End-to-end walkthrough for giving an agent its **prompts and description** from
the account workspace — in one bulk write that lands in the running environment
automatically.

## When to use this

Read this guide whenever you are **finishing** an agent: you have built its
functionality (scripts, credentials, an Agent REST API, MCP/agent-api
connections, team wiring) and now need to give it the prompts that make it
behave — and a description that accurately reflects what you built.

> **Author prompts LAST.** Create the agent with a one-line *provisional*
> description, build and verify all functionality first, then come back here to
> write the real prompt set from what actually exists. The final acceptance step
> of every build is rewriting the agent's **description** to match the finished
> agent.

## The six fields

An agent carries six prompt-ish fields. They look similar but each is consumed
by a **different system at a different moment** — do not conflate them.

| Field | What it is | When it fires | How it should look |
|-------|-----------|---------------|--------------------|
| `description` | Human-facing summary of what the agent does | Discovery (agent cards, A2A card); also feeds router-trigger / A2A skill generation | One clear sentence describing capability & purpose. *"Reconciles Stripe payouts against the ledger and flags mismatches."* |
| `workflow_prompt` | The **conversation-mode system prompt** — the agent's real execution instructions | Every conversation-mode session (the main prompt) | Operational: which scripts to run, how to parse their output (JSON/CSV), how to present results, decision logic. The agent is a *bridge* — it runs scripts, parses, and rephrases conversationally. |
| `entrypoint_prompt` | A short, human-like trigger message (1–2 sentences) | First user message for scheduled / automated runs | Conversational, **not** technical. ✅ *"What is my time-off balance?"* ❌ *"Query Odoo API and return JSON."* Must be **self-contained** — it is sent automatically with nobody there to fill anything in, so never leave a placeholder in it. |
| `refiner_prompt` | Instructions for turning a vague request into a structured task | During AI task refinement, before execution | Default-fill rules + mandatory fields. *"If no period is given, default to the current week. Always capture account id and currency."* |
| `router_trigger_prompt` | A single capability-verb sentence used to route incoming messages to this agent | Only by the routing classifier (`AgentClassifier.classify`) — never in any system prompt. Four consumers share it: Server Channels Pass 1 and Pass 2, App MCP Stage 1, and identity Stage 2 | *"Reconciles Stripe payouts and flags ledger mismatches."* Describes *when to route here*, not how to behave. |
| `example_prompts` | Ready-to-use task suggestions (`list[str]`) surfaced *for the agent* — **and a first-class routing input**: the classifier reads it alongside `router_trigger_prompt`, and an agent with neither is not routable | Shown in the A2A / external agent catalog and as MCP slash commands, **and rendered into the routing prompt** on every surface | Short imperative **templates a stranger can use**, never a replay of your build data. `["reconcile last week", "show failed payouts"]`. See [Writing `example_prompts`](#writing-example_prompts-templates-not-a-replay-of-your-build) — this is the field that goes wrong most often. |

### Don't confuse `example_prompts` with binding `prompt_examples`

- **`example_prompts`** (this guide) is an **agent-level** field — a list of
  ready-to-use task suggestions surfaced for the agent (e.g. in the A2A /
  external agent catalog). It is also one of the two fields the routing
  classifier reads, so writing it well makes the agent easier to reach, not
  just easier to start. Set it via the bulk write below.
- **`prompt_examples`** is a *different*, **binding-level** field. It survives
  on `IdentityAgentBinding` only (surfaced in MCP `prompts/list`); the App MCP
  routes that used to carry it are gone. It is not part of agent prompt
  authoring — leave it alone unless you are configuring an identity binding.
  (When you *do* configure one, the authoring rules below apply to it
  verbatim — same audience, same failure mode.)

## Writing `example_prompts`: templates, not a replay of your build

`example_prompts` are shown to **a different person, at a different time, with
different data** than the build you just finished. They are starting points the
user clicks and sends — so every one of them has to still make sense to someone
who was not in the room while you built the agent.

The single most common failure: the authoring agent copies concrete values out
of its own build session — the URL it tested against, the customer id in the
fixture, the report file it happened to open, today's date — and freezes them
into the examples. The result looks specific and useful and is actually dead on
arrival:

❌ `"Check this exact URL: https://internal.acme.example/reports/q3-2025"`
❌ `"Summarize invoice INV-88421"`
❌ `"Reconcile payouts for account acct_1M4kTest"`
❌ `"Analyze /app/workspace/data/sample_export.csv"`

A user who sends any of those gets an answer about *your* test data, not theirs.

### The two shapes an example may take

**1. Universal — needs no input from the user.** Write it out in full, ready to
send as-is:

✅ `"What is my status today?"`
✅ `"reconcile last week"`
✅ `"show failed payouts"`
✅ `"List everything waiting on my approval"`

**2. Input-required — the task is meaningless without a concrete value** (a URL,
an id, a file, an address, a date range). Write a **template that visibly stops
where the user takes over**, with a bracketed placeholder that can never be
mistaken for data:

✅ `"Investigate this URL — <paste the URL here>"`
✅ `"Summarize the invoice with number <invoice number>"`
✅ `"Reconcile payouts for account <account id>"`
✅ `"Analyze the uploaded file <file name>"`

Never substitute a realistic-looking fake (`https://example.com/report`,
`ACC-12345`, `john@example.com`) for the placeholder. A plausible fake is worse
than an obvious blank: the user sends it unchanged and the agent goes off and
does real work against a value nobody meant.

### Colons are structural — do not end a template with one

Each line is parsed as `slug: prompt text`, and **the first colon splits the
line**. A line with no usable colon is used verbatim as both the name and the
text, which is the normal case.

So `"Investigate this URL: <paste the URL here>"` does *not* render as one
sentence — it becomes a prompt named `Investigate this URL` whose entire body is
`<paste the URL here>`. Use an em dash, or fold the placeholder into the
sentence, instead of a trailing colon:

| Intent | ❌ Wrong | ✅ Right |
|--------|---------|---------|
| slugless template | `Investigate this URL: <url>` | `Investigate this URL — <url>` |
| explicit slug | *(none)* | `investigate_url: Investigate this URL — <url>` |

The explicit-slug form (`slug: prompt text`, one per line) is the format the
Config-tab editor documents and what MCP clients use for slash-command names.
Either form is accepted; just never let a *stray* colon appear in a slugless
line.

### Self-check before you write the list

- [ ] No URL, hostname, id, account, email address, file path, or date carried
      over from building or testing.
- [ ] No organization or person names from fixtures or sample data.
- [ ] Every entry is either fully universal, or an obviously unfinished template
      with a `<bracketed>` placeholder.
- [ ] Short and imperative — a handful of words before the placeholder.
- [ ] 3–6 entries covering *distinct* capabilities, not variations of one.
- [ ] Hand-test: would this example still be meaningful to a stranger, next
      month, with their own data? If it only works with the data you used while
      building, rewrite it.

Note the contrast with `entrypoint_prompt`: that one is fired **automatically**
by schedulers and triggers, with no human to complete it, so it must be fully
self-contained and rely on `refiner_prompt` defaults — never put a placeholder
there.

## The bulk workflow

Keep a single local artifact, `agents/<name>/prompts.json`, holding only the
prompt subset, and push it in **one atomic write**:

```jsonc
// agents/billing-agent/prompts.json
{
  "description": "Reconciles Stripe payouts against the ledger and flags mismatches.",
  "workflow_prompt": "You reconcile Stripe payouts. Run reconcile.py for the requested period, parse the JSON output, and present mismatches as a table. If everything matches, say so plainly. ...",
  "entrypoint_prompt": "Reconcile this week's payouts.",
  "refiner_prompt": "If no period is given, default to the current week. Always capture the account id and currency.",
  "router_trigger_prompt": "Reconciles Stripe payouts and flags ledger mismatches.",
  "example_prompts": [
    "reconcile last week",
    "show failed payouts",
    "Reconcile payouts for account <account id>"
  ]
}
```

Note the third entry: the task genuinely needs an account, so it ships as a
template the user finishes — not as the test account you reconciled during the
build. See [Writing `example_prompts`](#writing-example_prompts-templates-not-a-replay-of-your-build).

```bash
# 1. One bulk write → the agent's config (DB is authoritative)
cinna api PUT agents/<agent_id> --data @agents/billing-agent/prompts.json

# 2. Verify what actually landed
cinna agent show billing-agent --prompts
```

All fields are optional — keys you omit are left unchanged. You may also pass
the payload inline with `--json '{...}'` instead of `--data @file`, but a
persisted `prompts.json` is easier to iterate.

### How it reaches the environment (you don't have to push it)

The agent **config (database) is the source of truth.** Writing these fields is
enough: the three document-backed prompts (`workflow_prompt`,
`entrypoint_prompt`, `refiner_prompt`) are written into the container's
`docs/*.md` automatically on the next environment start/activation (the prompt
reconcile seeds them DB→env when the env files are still empty — the fresh-agent
case). `router_trigger_prompt`, `example_prompts`, and `description` are
config-only and take effect immediately.

If the environment is **already running** and you want the doc prompts pushed in
*right now* instead of on next start:

```bash
cinna api POST agents/<agent_id>/sync-prompts
```

(That call requires a running environment; if there isn't one, just rely on the
automatic seed on next start.)

### One-path rule

Author prompts **either** through this bulk write **or** by hand-editing the
synced `agents/<name>/workspace/docs/*.md` files — not both at once. Editing both
sides puts the three doc prompts into a three-way merge (last-writer-wins). For
the account orchestrator, the bulk write is the recommended single path.

## Optional: let the platform generate the router trigger

If you'd rather not hand-write `router_trigger_prompt`, the platform can derive
it from the agent's name + description:

```bash
cinna api POST agents/<agent_id>/generate-router-trigger-prompt
```

This derives the trigger from the agent's name **and description**, so it
requires a `description` to already be set on the agent (it errors out if none
is set) — call it *after* you've set the description, or rely on it during the
finalize bulk write where the description is included.

This only generates the routing sentence. The other prompts and the description
are yours to author — you have the full build context and are the better author.

## The finalize step (do this at the end of every build)

1. Confirm all functionality works (scripts run, connections resolve, the API
   spec harvests, etc.).
2. Author the full prompt set **from what you actually built**:
   - `workflow_prompt` describes the *real* scripts/flow you created;
   - `entrypoint_prompt` matches the *real* trigger;
   - `refiner_prompt` matches the *real* task fields;
   - `router_trigger_prompt` + `example_prompts` reflect *real* capabilities —
     and `example_prompts` are written as **user-ready templates**, scrubbed of
     every URL, id, path, and date you used while building (run the self-check
     in [Writing `example_prompts`](#writing-example_prompts-templates-not-a-replay-of-your-build));
   - **`description` is rewritten to accurately describe the finished agent.**
     Set it explicitly in the same payload — don't rely on auto-derivation.
3. `cinna api PUT agents/<id> --data @agents/<name>/prompts.json`
4. `cinna agent show <name> --prompts` to confirm.
5. If the env is already running and you want the docs live immediately,
   `cinna api POST agents/<id>/sync-prompts`.

An agent whose `description` and prompts match its actual behavior is the mark of
a finished build.
