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
| `entrypoint_prompt` | A short, human-like trigger message (1–2 sentences) | First user message for scheduled / automated runs | Conversational, **not** technical. ✅ *"What is my time-off balance?"* ❌ *"Query Odoo API and return JSON."* |
| `refiner_prompt` | Instructions for turning a vague request into a structured task | During AI task refinement, before execution | Default-fill rules + mandatory fields. *"If no period is given, default to the current week. Always capture account id and currency."* |
| `router_trigger_prompt` | A single capability-verb sentence used to route incoming messages to this agent | Only by the App MCP router classifier — never in any system prompt | *"Reconciles Stripe payouts and flags ledger mismatches."* Describes *when to route here*, not how to behave. |
| `example_prompts` | Ready-to-use task suggestions (`list[str]`) surfaced *for the agent* | Shown in the A2A / external agent catalog as suggested tasks for this agent (not the same as route-level `prompt_examples`) | Short imperative tasks. `["reconcile last week", "show failed payouts"]` |

### Don't confuse `example_prompts` with route `prompt_examples`

- **`example_prompts`** (this guide) is an **agent-level** field — a list of
  ready-to-use task suggestions surfaced for the agent (e.g. in the A2A /
  external agent catalog). Set it via the bulk write below.
- **`prompt_examples`** is a *different*, **route/binding-level** field on App
  MCP routes and Identity bindings (surfaced in MCP `prompts/list`). It is not
  part of agent prompt authoring. Leave it alone unless you are configuring a
  route.

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
  "example_prompts": ["reconcile last week", "show failed payouts"]
}
```

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
   - `router_trigger_prompt` + `example_prompts` reflect *real* capabilities;
   - **`description` is rewritten to accurately describe the finished agent.**
     Set it explicitly in the same payload — don't rely on auto-derivation.
3. `cinna api PUT agents/<id> --data @agents/<name>/prompts.json`
4. `cinna agent show <name> --prompts` to confirm.
5. If the env is already running and you want the docs live immediately,
   `cinna api POST agents/<id>/sync-prompts`.

An agent whose `description` and prompts match its actual behavior is the mark of
a finished build.
