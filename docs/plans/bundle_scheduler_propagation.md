# Plan: Bundle-propagated Agent Schedulers

## Goal

Let a publisher ship their **agent schedules** as part of a bundle. On install, the
consumer receives the publisher's schedules **as published** (pre-populated, with the
published enabled/disabled state). The consumer **cannot edit/create/delete** schedules
(definition is publisher-authored) but **can enable/disable**, **Run now**, and **view
logs**. On bundle updates, schedules are merged so the user's enable/disable survives
when a schedule is behaviorally unchanged, and changed/added/removed schedules are
synced from the new revision.

This reverses the current explicit exclusion (schedules were previously NOT bundled).

## Decisions (confirmed)

- **Match rule** for "same scheduler" across revisions: **behavioral signature** =
  `(schedule_type, cron_string, command, prompt)`. `name`/`description` are cosmetic —
  excluded from identity. A rename/description tweak keeps the user's toggle; a
  cron/command/prompt/type change is treated as a different scheduler (reinstalled).
- **Consumer capabilities** on a bundled (read-only) schedule: **enable/disable + Run
  now + Logs**. New/Edit/Delete are blocked in the UI and enforced server-side.
- A consumer install's schedules are **entirely bundle-owned** (consumers can't create
  their own), which simplifies the merge — the full schedule set on a foreign install is
  bundle-managed.

## Merge algorithm (apply-update)

```
new_defs   = revision.schedules                       # list of dicts
existing   = install's AgentSchedule rows
new_by_sig = group new_defs by sig()                  # sig = (type, cron, command, prompt)

for row in existing:
    if sig(row) in new_by_sig:
        # behaviorally unchanged → keep row (preserve enabled, next/last_execution, logs)
        # refresh cosmetic fields only: name, description
        consume new_by_sig[sig(row)]
    else:
        delete row                                    # changed or removed by publisher

for remaining def in new_by_sig:                      # added or changed
    create AgentSchedule(enabled=def.enabled,
                         next_execution=calc(def.cron_string), ...)
```

- Install-time materialization is the same as the "create" branch for every snapshotted
  schedule.
- `next_execution` is recomputed from the UTC cron via
  `AgentSchedulerService.calculate_next_execution` (revision cron is already UTC).
- Edge: duplicate behavioral signatures within one revision are assumed unique; if a
  collision occurs the later definition wins (documented limitation).

## Backend changes

1. **Migration** — add `schedules JSON NOT NULL DEFAULT '[]'` to `agent_bundle_revision`.
   Existing revisions backfill to `[]` (no schedules), fully backward compatible.
2. **Model** `app/models/bundles/agent_bundle_revision.py` — add
   `schedules: list = Field(default_factory=list, sa_column=Column(JSON))` and surface it
   on `AgentBundleRevisionPublic`.
3. **PublishService** (`_publish_locked`) — snapshot the publisher install's
   `AgentSchedule` rows into `revision.schedules` and `manifest["schedules"]` as
   `{name, cron_string, description, prompt, schedule_type, command, enabled}`. Because
   the manifest feeds `content_hash`, a schedule-only change yields a new hash → installs
   see a pending update.
4. **Schedule sync helper** — new `app/services/bundles/schedule_sync.py` (or a method on
   `InstallService`) implementing `sig()`, `materialise(install, revision)`, and
   `merge(install, revision)`.
5. **InstallService._install_from_revision** — add step 7: materialise schedules from the
   revision (best-effort, like the MCP-route step; failure logs + marks degraded, doesn't
   abort install).
6. **InstallService.apply_update** — after prompt sync, run the schedule merge.
7. **Route guards** (`app/api/routes/agents.py`) — for a foreign install
   (`agent.bundle_uuid is not None and not agent.is_publisher_install`):
   - `POST /{id}/schedules` → 403
   - `DELETE /{id}/schedules/{sid}` → 403
   - `PUT /{id}/schedules/{sid}` → allow only when `exclude_unset` fields ⊆ `{enabled}`;
     otherwise 403
   - `POST /{id}/schedules/{sid}/run` and `GET .../logs` → allowed
   Publisher installs (`is_publisher_install=True`) and standalone agents are unaffected.

   The background scheduler needs **no change** — install schedules are ordinary
   `AgentSchedule` rows and get polled/executed in the install's own env/sessions.

## Frontend changes

8. **AgentSchedulesCard** — add `readOnly?: boolean`. When set: hide New/Edit/Delete;
   keep Power toggle, Run now, Logs. Toggle still calls `PUT .../schedules/{id}` with only
   `{enabled}`.
9. **AgentConfigTab** — render the Schedules card when `showOperationalSettings || readOnly`
   (so foreign installs show it read-only), passing `readOnly`. Handovers stay gated on
   `showOperationalSettings` only. Adjust the grid when only the schedules card shows.
10. **Client regen** — `make gen-client` for the new revision field.

## Docs

11. Update `docs/agents/agent_bundles/agent_bundles.md` (+ `_tech`) — schedules are now
    snapshotted, materialised on install, and merged on update; document the behavioral
    match rule and consumer read-only-except-toggle.
12. Update `docs/agents/agent_schedulers/agent_schedulers.md` — replace the "schedules are
    not included in bundle revision snapshots / not synced" integration note with the new
    behavior; add the read-only-on-installs rule and consumer capabilities.
13. `docs/README.md` — touch the `agent_bundles` / `agent_schedulers` registry blurbs if
    needed.

## Tests (backend, API-only per tests/README.md)

- Publish snapshots publisher schedules into the revision.
- Install materialises schedules with published enabled state + computed next_execution.
- Apply-update: unchanged schedule keeps a user-disabled toggle (and refreshes name);
  cron change reinstalls (enabled per publisher); added schedule appears; removed schedule
  is deleted.
- Route guards: consumer create/delete → 403; consumer PUT with non-`enabled` field → 403;
  consumer PUT `{enabled}` → 200; publisher install retains full CRUD.

## Phasing

- **P1** migration + model + publish snapshot + install materialisation (+ tests)
- **P2** apply-update merge (+ tests)
- **P3** route guards (+ tests)
- **P4** frontend (card readOnly + config tab) + client regen
- **P5** docs
