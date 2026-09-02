# 05 — Schedules

## Read this when

The user says *daily*, *weekly*, *every*, *at 7am*, *overnight*, *when I am not
watching* — or the agent should otherwise run unattended.

Locally a schedule is a **declaration plus a way to dry-run it**. Nothing on this
machine runs the agent while nobody is there; that is what the cloud step buys. Say
that plainly to the user instead of implying a local cron exists.

## Declare it in the manifest

```json
{
  "name": "Weekday morning check",
  "cron_string": "0 6 * * 1-5",
  "timezone": "Europe/Berlin",
  "schedule_type": "static_prompt",
  "prompt": "Check the invoices that arrived since yesterday.",
  "command": null,
  "enabled": true
}
```

| Field | Rule |
|-------|------|
| `name` | Unique within the agent. Import is idempotent by name. |
| `cron_string` | Standard five fields: minute hour day-of-month month day-of-week. |
| `timezone` | IANA name. Local-only metadata; used to render the cron description at import. |
| `schedule_type` | `static_prompt` or `script_trigger`. Immutable after creation on the platform. |
| `prompt` | Required for `static_prompt`. |
| `command` | Required for `script_trigger`. |
| `enabled` | Ship it `true` only if the user asked for it to be live immediately. |

## The two types

**`static_prompt`** — always starts a session. The `prompt` is sent as the first
message, the agent works, the user gets a result. Use it when the agent should
*report* something on a cadence.

**`script_trigger`** — runs a shell command on the cadence and starts a session
**only when the output is not `OK`**. Use it when the agent should *watch* something
and stay silent while everything is fine. This is the cheap pattern: a check that
runs every 30 minutes and costs nothing when there is nothing to say.

```json
{
  "name": "Queue depth check",
  "cron_string": "*/30 * * * *",
  "timezone": "UTC",
  "schedule_type": "script_trigger",
  "prompt": null,
  "command": "python scripts/check_queue.py",
  "enabled": true
}
```

The command is **cloud-first**: relative path, `python` not `uv run`, cwd is the
workspace root. Its script must print exactly `OK` when there is nothing to report,
and a short description of the problem otherwise. That output becomes the context
the session starts with.

**Cadence floor:** on the platform, `static_prompt` schedules may not run more often
than once every 10 minutes. `script_trigger` has no floor. Design accordingly — a
frequent check is a `script_trigger`.

## `ENTRYPOINT_PROMPT.md` becomes mandatory

A scheduled run has no human in the room. `docs/ENTRYPOINT_PROMPT.md` is the message
that starts it, so it must be entirely self-contained: no placeholders, no brackets,
nothing the agent is expected to ask about.

- Right: *"Run today's invoice check and report anything missing a PO number."*
- Wrong: *"Check invoices for <account>."* — nobody will fill that in.
- Wrong: *"Query the API and return JSON."* — technical, not a user's message.

Any defaults the run depends on ("if no period is given, use yesterday") belong in
`docs/REFINER_PROMPT.md`.

## Dry-run it locally

Every schedule gets a Makefile target so the user (and you) can run it on demand:

```makefile
run-morning-check: ## Dry-run the "Weekday morning check" schedule
	uv run scripts/check_invoices.py --since yesterday
```

For a `script_trigger`, run the command and check the contract:

```bash
uv run scripts/check_queue.py; echo "exit=$?"
```

It must print `OK` and nothing else in the quiet case. A script that prints a
progress line before `OK` will trigger a session every single run.

For a `static_prompt`, switch to the **Agent** role and answer
`docs/ENTRYPOINT_PROMPT.md` cold. If you needed anything the prompt did not give
you, the schedule will fail unattended.

## Real unattended runs, locally

There is no scheduler in the kit and you should not build one silently. If the user
insists on running unattended before going cloud, offer the one-liner and let them
install it themselves:

```
# crontab -e   (macOS / Linux)
0 6 * * 1-5 cd ~/Documents/MyAgents/Local/<slug> && /usr/bin/make run-morning-check >> app-data/storage/cron.log 2>&1
```

Say clearly what this does not give them: no retries, no logs in the UI, no
notification, nothing while the laptop is asleep. That is the honest argument for
the cloud step, and it is `11-go-cloud.md`.

## Ladder consequences

A schedule usually pulls in the **Status reporting** rung: an unattended agent that
cannot say whether it is healthy is a black box. Read `06-status-reporting.md` next
unless the user explicitly does not want it.

## Done when

- Every schedule the user asked for is in `cinna-agent.json` `schedules[]`, with a
  valid five-field cron, a timezone, and a `prompt` or `command` matching its type.
- No `static_prompt` schedule runs more often than every 10 minutes.
- Each `script_trigger` command uses a relative path and prints exactly `OK` when
  there is nothing to report.
- `docs/ENTRYPOINT_PROMPT.md` is non-empty, self-contained and placeholder-free.
- Each schedule has a `run-<name>` Makefile target that you have actually run.
- The user has been told that unattended execution starts in the cloud.
