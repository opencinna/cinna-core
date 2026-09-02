# 09 — Several agents

## Read this when

A second agent appears in `Local/`, or one agent should hand work to another.

## First: should it be a second agent at all?

Split into a new agent when **any** of these is true:

- the two jobs answer to different people or different schedules;
- they need different credentials, and one should not see the other's;
- one is useful on its own without the other;
- combining them would make a single workflow prompt unreadable.

Keep it as one agent (probably a new **local skill**, see
`08-knowledge-and-local-skills.md`) when the jobs share data, share credentials and
are always used together. Two agents cost two prompt sets, two manifests and two
things to keep in sync. Do not split for tidiness.

## Naming and boundaries

Each agent gets a slug that says what it does, not where it sits:
`invoice-watcher`, `payout-reconciler`, `vendor-portal-sync`.

Write the `description` of both agents next to each other and read them aloud. If
you cannot tell from the two sentences which one should handle a given request, the
boundary is wrong — fix it before writing any code. This is exactly the judgement
the platform's router makes later from `router_trigger_prompt` and `example_prompts`.

## Shared conventions live at the root

Anything two agents both need goes in the root `AGENTS.md`, not copied into each
agent:

- how the user is addressed, tone, output conventions;
- a shared vocabulary or set of ids;
- which agent owns which domain.

Anything specific to one agent stays in that agent's folder. Never copy a script
between agents: if two agents need the same helper, either they are one agent, or
the helper is small enough to duplicate honestly, or the work belongs to a third
agent that both hand off to.

## Handovers

When one agent should delegate to another, declare it in the delegating agent's
`cinna-agent.json`:

```json
"handovers": [
  {
    "target_slug": "payout-reconciler",
    "description": "Send anything about payouts, settlements or ledger mismatches here."
  }
]
```

`target_slug` must be a real sibling under `Local/`. `kit.py validate` checks that.

Then say so in the delegating agent's `docs/WORKFLOW_PROMPT.md`, in the agent's own
voice:

```markdown
## Handover
If the request is about payouts or ledger reconciliation, say that this belongs to
the payout reconciler and stop. Do not attempt it.
```

**Locally, a handover is a routing instruction, not a transfer.** You, the assistant,
are the one who switches folders. Tell the user which agent you are moving to and
why, then read that agent's `AGENTS.md` and `docs/WORKFLOW_PROMPT.md` before
answering. In the cloud the same declaration becomes a real handover between running
agents.

## The orchestrator role

At the root you are not any single agent. From there:

- `uv run .cinna-kit/tools/kit.py list` — what exists, which rungs each has, which
  are already in the cloud.
- Route a request: read the descriptions, pick one agent, announce the choice.
- Keep boundaries clean: when a request keeps landing between two agents, that is a
  signal to redraw the boundary, not to duplicate a script.

Never answer a domain question from the root without entering an agent folder. If no
agent covers it, say so and offer to build one.

## Ordering the work

Build agents one at a time, to a working state, before starting the next. Two
half-built agents are much harder to reason about than one finished plus one empty.

When several agents share a credential slot, declare it in each manifest with the
same `name` and `type`. Locally each keeps its own `.env`; at import the platform
shares one credential with both instead of creating duplicates.

## Done when

- Each agent's `description` makes it obvious which requests belong to it, and the
  descriptions do not overlap.
- No script, prompt or knowledge file is duplicated across agents.
- Shared conventions are in the root `AGENTS.md`, not copied into each agent.
- Every `handovers[].target_slug` names a real folder under `Local/`.
- Every declared handover is also stated in the delegating agent's workflow prompt.
- `kit.py validate` passes for every agent, not just the one you just touched.
- `kit.py list` shows the set you expect, with no half-scaffolded leftovers.
