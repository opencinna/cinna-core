# UI/UX Guidelines — the house pattern library

This is the single source of truth for **how a surface is structured**: which user story it serves, where it lives (card / dialog / sheet / page), how dense it may be, which controls are visible, and which house pattern it reuses. It is read by:

- `cinna-core-ui-designer` — produces the **UI Specification** for a plan (`cinna-core.ui.design`) and reviews built surfaces against it (`cinna-core.ui.review`).
- `cinna-core-ui-developer` — implements frontend phases from the specification.
- `cinna-core-developer` / `cinna-core-code-reviewer` — whenever they touch or review a frontend file.

Implementation conventions (React Query, routing, toasts, the generated client) live in [frontend_development_llm.md](frontend_development_llm.md). This document is about **composition**, not wiring.

The rules carry numbers on purpose. A rule with a number can be checked without taste; a rule without one gets argued. Thresholds are policy until the [lessons log](#10-lessons-log) changes them.

---

## 1. The four user stories

Every surface is classified **before** it is designed. A surface that serves two stories at once is two surfaces.

| Story | The user's intent | What the surface looks like |
|---|---|---|
| **Create** | "Set it up fast, with as few actions as possible." | ≤ 3 fields → one dialog. More → a wizard of ≤ 4 steps with a stepper. If the form's shape depends on a *type*, choosing the type **is step 1** (tile/pill picker, [pattern 4](#p4-type-picker--detail-route)) — never a `<Select>` at the top of a long form. Required fields only; everything with a sensible default is pre-filled; advanced options collapsed behind one "Advanced" disclosure. |
| **View** | "What do I have here?" A glance, not a control panel. | Compact rows or summary cards. **Passive** state (badge, star, "Default", "Managed", expiry) is visible. **Mutating** controls (delete, set-default, edit) are not: they live in an overflow menu or appear on hover. Delete is never a default-visible control. |
| **Edit** | "Show me the whole configuration, structured." | The full form, grouped into titled sections, in a Dialog (one section, ≤ 8 fields), a Sheet (2–3 sections) or a dedicated route (more, or with sub-lists). Explicit Save / Cancel with dirty tracking. Never inline in a list row. |
| **Manage list** | "Find, sort, filter, act on many." | Dedicated route with `DataTable` (search, pagination, per-row overflow menu). A card elsewhere shows only a **preview** of this list ([pattern 5](#p5-preview-list--show-all)). |

Create and Edit share one form component when the fields are the same and the create story needs no type step. When they differ, do not force the create story through the edit form.

---

## 2. Placement decision table

| Situation | Rule |
|---|---|
| **A card** | One concern, named by the title. **≤ 7 logical blocks** (a block = one field group, one button row, one summary row, or **one list as a whole** — rows are counted by the list-cap rule, not here). If the spec has more, split the card or promote the concern to a tab / route. |
| **Card width** | Half-width in the tab's `grid grid-cols-1 lg:grid-cols-2 gap-6 items-start` grid. Full-width **only** for a table that needs its columns (≥ 3 columns — a `DataTable` or a role/permission matrix). "This card reads badly at half width" means the card is too big, not that the grid is wrong. |
| **Card height** | Bounded and predictable. Nothing inside a card may grow the card without a user action that says so (a "Show all" link, a tab switch). Expand-in-place is allowed only for read-only detail (a log line, a description). A state toggle (Enable, Default) must not change the card's height — if enabling reveals a form, that form is a dialog. |
| **List inside a card** | Show the **5** most relevant items (recent, enabled, favourites first) and a "Show all (N)" link to a dedicated route or a full-height Sheet. Never render an unbounded list in a card. This applies to entity lists; a selection checklist inside a wizard step or dialog is not a list in a card — it gets `max-h-[40vh] overflow-y-auto` when its length is data-driven. |
| **Row actions** | **1 primary** action inline (the reason the user comes to the row: Run, Open, Copy). **≤ 2** inline in total. Everything else — and **always** Edit, Delete, Disable, Set-default — in a `⋯` overflow `DropdownMenu` ([pattern 3](#p3-compact-row--overflow-menu)). Destructive items last in the menu, `text-destructive`, confirmed with `AlertDialog` (never `window.confirm`). A row whose **only** action is its purpose (Disconnect on a session, Copy on a link) may show that one action inline; when it is destructive, hover-reveal it. |
| **Toggles on rows** | Enabled/disabled is shown as a `Badge` (or muted row styling). A `Switch` on the row only when toggling is the row's *primary purpose* (e.g. a feature-flag list, a channel list) — and then it **is** the row's primary action: `Switch` + `⋯` menu uses the whole inline budget. A control on a row acts on that row's entity only; a control that acts on a different entity (a person inside a channel row) belongs on that entity's own surface. |
| **Inline forms** | At most **one** inline form open at a time per card, and only for a single-field quick edit (rename, one textarea). Anything with ≥ 2 fields → dialog. N rows must never render N forms. |
| **Disclosure depth** | **2 levels**: card/list → dialog/sheet/route. A dialog never opens another dialog (an `AlertDialog` confirm is the one exception). A success screen *links* onward (to the created entity's row or page) instead of nesting the next step. A **value picker** inside a form dialog (choose an agent, a user, a model) is a `Popover`/command list anchored to its field, not a second `Dialog`; an existing picker dialog is tolerated only if it returns one value and closes itself. Shared controls meant to live inside dialogs (`ListModelsButton`, `UserAllowlistPicker`) must therefore never open a `Dialog` of their own. **Chained** is not nested: a dialog that closes and then opens an informational dialog ("these environments are affected") is allowed. |
| **Interaction model** | One per card: **either** auto-saving controls (Switch / Select that persist on change, with a toast) **or** edit-through-a-form (a Save / Cancel form, or P2 summary rows that open a dialog). Never both in the same card — an auto-saving `Select` beside two summary→dialog rows is a violation even when each piece is fine alone. (An auto-save card *may* hold one P2 summary row whose dialog edits a value too long for an inline control, such as a pattern list — the rule is about the card body's controls, not about where a long value is typed.) |
| **Complex configuration** | Its own tab (`HashTabs`, as on the agent page) or its own route. Not a taller card. |
| **Empty state** | Every list has one: **≤ 1 sentence** + a button or link to the primary Create action (the card header's Add button satisfies this when the card has one; a sentence that names another page must link to it). Not just "No items." |
| **Sections inside one card** | Allowed when they are facets of **one** concern (2FA: passkeys / authenticator / recovery codes). Separated with `Separator`, each with a small heading. Three sections about three different concerns is three cards. |
| **Icons** | Every icon-only button has a `Tooltip`. Icon size `h-3.5 w-3.5` inside `h-7 w-7` ghost buttons on rows; `h-4 w-4` elsewhere. |
| **Confirmation** | Destructive and irreversible actions confirm with `AlertDialog` naming the entity ("Delete schedule *Nightly sync*?"). A plain `Dialog` is acceptable only when the confirmation must show fetched impact data (what else breaks if this credential goes) — it still names the entity and ends in one destructive button. Reversible actions (disable, unfavourite) do not confirm; they toast with the result. |

---

## 3. House patterns

Each pattern names a reference implementation in this codebase. Reuse the reference's structure and class names; do not reinvent the skeleton.

### P1. Concern card → modal
**Use for:** a configuration concern whose editor is large (prompts, descriptions, long text).
**Reference:** `frontend/src/components/Agents/AgentConfigTab.tsx` — Information and Agent Prompts cards.
**Skeleton:** `Card` → `CardHeader` (icon + title, one-line description) → `CardContent` with a `flex gap-2 flex-wrap` row of `Button variant="outline"` items, each opening one modal. The modals are rendered once at the bottom of the tab component, controlled by boolean state. The card itself shows **no** form fields. The buttons name the thing they open ("Description", "Workflow Prompt") — the one place where a noun label is right.
**States:** none needed beyond the modal's own loading/saving.

### P2. Summary row → edit dialog
**Use for:** a small set (1–4) of named settings, each with a few fields. The inline `Pencil` is the "only action" case of §2 Row actions: the moment a row also gets Delete / Disable / Set-default it becomes a P3 row with a `⋯` menu.
**Reference:** `frontend/src/components/UserSettings/AICredentials.tsx` — "Default SDK Preferences" rows + `SDKModeEditDialog`.
**Skeleton:** per setting, `div.flex.items-start.justify-between.gap-3.rounded-md.border.px-3.py-2.5` with an icon tile, a label (`text-xs text-muted-foreground`), the current value (`text-sm font-medium`) and secondary lines; a single `Button variant="ghost" size="icon" className="h-7 w-7"` with `Pencil` opening the dialog. The dialog holds the whole form for that setting.
**States:** skeleton rows while loading; the value line says "Not configured" when empty.

### P3. Compact row + overflow menu
**Use for:** any list of entities with more than one action (schedules, handovers, credentials, connectors, shares, sessions).
**Reference:** `frontend/src/components/Admin/UserActionsMenu.tsx` (the menu) inside the `/admin/users` table; the row layout from `frontend_development_llm.md` § "Compact List Row Pattern".
**Skeleton:** header = `CardHeader` with title and the one primary `Button size="sm"` on the same row, `CardDescription` full-width below them (a description squeezed beside a button wraps to 5 lines at 1024). Row = `flex items-center justify-between px-3 py-2 border rounded-lg`. Left (`min-w-0 flex-1`): primary text `text-sm font-medium truncate`, ≤ 2 `Badge`s, one metadata line `text-xs text-muted-foreground` holding ≤ 2 facts joined by ` · ` (cadence · next run); further facts go to a badge tooltip or the edit dialog. Right (`flex items-center gap-0.5 shrink-0`): the primary action as a ghost icon button with tooltip (optional), then a `DropdownMenu` triggered by `EllipsisVertical`. Menu order: secondary actions, `DropdownMenuSeparator`, destructive item(s). Edit opens a dialog ([P2](#p2-summary-row--edit-dialog) style form). Inactive rows: `opacity-60`, plus an "Off" badge — the state badge is **off-only**; the enabled majority carries no badge (both P3 instances, Handovers and Schedules, do this). A type or kind is an icon with a tooltip and `sr-only` label, not a third text badge, so the name keeps its width at 1024.
**Hover-reveal variant:** when the list is dense (≥ 10 rows) or a View row carries a visible mutating/destructive control (a menu-only row stays visible — hiding the one control costs discoverability with no density to pay for it), the right cluster gets `opacity-0 group-hover:opacity-100 focus-within:opacity-100` on a `group` row. Passive indicators (star, "Default") stay visible; their toggle affordance moves to the menu.
**States:** loading skeleton rows; empty state with the Create action; per-row pending state disables the menu trigger.

### P4. Type picker → detail route
**Use for:** creation where the form depends on a chosen type.
**Reference:** `frontend/src/components/Credentials/AddCredential.tsx` → `frontend/src/routes/_layout/credential/$credentialId.tsx`.
**Skeleton:** a dialog with a search input and grouped pill/tile buttons (`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-medium`), grouped under `text-xs font-semibold uppercase tracking-wide text-muted-foreground` headings. Choosing a type creates the entity with defaults (or carries the type in state) and lands on the type-specific form: a dedicated route for large entities, step 2 of a wizard for small ones. **Allowed variant:** a type that has no meaningful empty draft (an OAuth connection, a key that must be bound at creation) routes to its own create dialog instead of the shared route — the reference does this for "Connect MCP Provider" and `agent_api`. When the landing is a `?new=1` detail route, secondary cards (sharing, danger zone) stay hidden until the first save so the Create story is not shown Edit-only concerns.
**States:** "No types match …" for the search; pending ring on the chosen tile.

### P5. Preview list + Show all
**Use for:** a card that summarises a list the user manages elsewhere (recent sessions, recent runs, last invitations).
**Reference:** `frontend/src/components/Agents/AgentHandovers.tsx` + `HandoverRow.tsx` + `AllHandoversSheet.tsx` (cap 5, Show all → Sheet, shared row component so card and Sheet cannot drift). Second instance: `UserSettings/DesktopSessionsCard.tsx` (hover-revealed single destructive action, Sheet). Both render through `Common/PreviewList.tsx` (cap, four states, Show-all link) — use it for every further P5 card.
**Skeleton:** card shows the first **5** rows in [P3](#p3-compact-row--overflow-menu) style (often with **no** row actions at all), a count in the title or description ("3 of 12"), and a `Button variant="link"` "Show all (N)" that navigates to the manage-list route (or opens a full-height `Sheet`). **Route vs Sheet:** a Sheet when the full list needs no search, sort or pagination and the entity has no lifecycle of its own (handovers); a route ([P9](#p9-manage-list-route)) the moment any of those appears. The Sheet is a second disclosure level, so its rows carry the same `⋯` menu and nothing opens a further dialog except the row's own edit/confirm. The empty state and the "Show all" target may live on another route (the card previews a list managed elsewhere) — then they link there. Sorting: most recent / most relevant first.
**States:** empty state in the card; the "Show all" link hidden when N ≤ 5.
**Accepted variant:** a read-only per-row detail list (execution logs) may open in a `Dialog` with `max-h-[60vh] overflow-y-auto` and a stated cap ("last 50") instead of a route or Sheet.

### P6. Wizard
**Use for:** Create stories with > 3 fields or with a type decision.
**Reference:** `frontend/src/components/Admin/InviteUserDialog.tsx` for the two-step flow and per-step validation. As of 2026-09-07 the reference has the stepper header and its success panel (`InviteSuccessPanel.tsx`) links onward instead of nesting a dialog; the AI-credential hand-off lands on `/admin/ai-credentials` via a search param that opens the controlled dialog there.
**Skeleton:** `Dialog` with a stepper header ("1 Who · 2 Provisioning"); either one `<form>` per step or one form with hidden step panes (the reference does the latter — it keeps typed values and per-step Enter semantics; both are conformant), Back / Next / Create in `DialogFooter`, validation per step, ≤ 4 steps. Final step may be a review summary. The success state is a panel with links ("Open user", "Copy invitation link"), never another dialog.
**States:** per-step errors inline; the whole wizard disabled while submitting.

### P7. Section-divided single card
**Use for:** one concern with 2–3 facets that share a lifecycle.
**Reference:** `frontend/src/components/UserSettings/Security/SecurityTab.tsx` (2FA: passkeys / authenticator / recovery codes).
**Skeleton:** one `Card`; sections as `<section>` blocks with an `h3.text-sm.font-medium` and a one-line description, separated by `Separator`. Each section has at most one control row. If any section needs a form, that form is a dialog.
**Not for:** three different concerns that happen to be adjacent settings (anti-pattern A1).

### P8. Tabs for domains
**Use for:** an entity with several configuration domains (agent: Configuration / Integrations / Credentials / …).
**Reference:** `frontend/src/routes/_layout/agent/$agentId.tsx`, `frontend/src/routes/_layout/settings.tsx`, `HashTabs` in `frontend/src/components/Common/HashTabs.tsx`.
**Skeleton:** `HashTabs` with each tab's content being the two-column card grid. A tab is the right home for a concern that would exceed the card budget; a new top-level route is right when the concern has its own list/detail lifecycle.

### P9. Manage-list route
**Use for:** the Manage-list story.
**Reference:** `frontend/src/routes/_layout/admin/users.tsx` + `frontend/src/components/Common/DataTable.tsx` + `frontend/src/components/Admin/columns.tsx`.
**Skeleton:** page header (title, one primary Create button, optional secondary), `DataTable` with column definitions, per-row `UserActionsMenu`-style overflow menu, server- or client-side pagination. Filters as a toolbar above the table, never as a side card.

---

## 4. Anti-patterns (found in this codebase — fix on touch)

| # | Anti-pattern | Instance | Rule violated | Fix |
|---|---|---|---|---|
| A1 | **Several concerns in one card** | `Admin/AccessPolicyCard.tsx` (523 lines): who can join + how they sign in + new-user defaults + an embedded 328-line credential matrix; switches auto-save while the textarea has Save/Cancel | §2 one concern, ≤ 7 blocks, one interaction model | **Fixed 2026-09-07** → `Admin/AccessPolicy/` (Registration, Sign-in methods, New user defaults, Company AI credentials full-width matrix capped at 5) |
| A2 | **Dialog in dialog** | `Admin/InviteUserDialog.tsx` (926 lines): success panel opens `ManagedCredentialDialog` | §2 disclosure depth | **Fixed 2026-09-07** → `InviteSuccessPanel.tsx` links onward; hand-off via search param on `/admin/ai-credentials` |
| A3 | **Expand-in-place editing** | `UserSettings/UserChannelsCard.tsx` (1056 lines): every row expands its full edit form, card grows page-wide | §2 card height, inline forms | **Fixed 2026-09-07** → `UserSettings/Channels/` (half-width card, `Switch` + `⋯` rows capped at 5, configure in a two-section Sheet that auto-saves because null means inherit-from-admin, People who shared with you as its own card). `IdentityServerCard` on the same tab still carries toggle + pencil + trash per row |
| A4 | **Unbounded list in a card** | `UserSettings/DesktopSessionsCard.tsx` renders all sessions | §2 list inside a card | **Fixed 2026-09-07** → `DesktopSessionsCard.tsx` + `AppSessionRow.tsx` + `AllAppSessionsSheet.tsx` on `Common/PreviewList.tsx` (the second consumer, so the primitive was extracted here). Same shape still live in `LocalDevelopmentCard.tsx` (uncapped token list, lying empty state, native `title`s) |
| A5 | **Every action visible on every row** | `Agents/AgentSchedulesCard.tsx` 5 icon buttons per row; `UserSettings/AICredentials.tsx` star + pencil + trash per row | §2 row actions | Schedules **fixed 2026-09-07** → `AgentSchedulesCard.tsx` + `ScheduleRow.tsx` + create/edit/logs dialogs + `AllSchedulesSheet.tsx` (Run + Logs inline, rest in menu, off-only badge, type as an icon with tooltip). AI Credentials: in progress |
| A6 | **N inline forms** | `Agents/AgentHandovers.tsx`: one textarea + buttons per enabled handover; hand-rolled toggle; `window.confirm` | §2 inline forms, confirmation, component vocabulary | **Fixed 2026-09-07** → `AgentHandovers.tsx` + `HandoverRow.tsx` + `EditHandoverPromptModal.tsx` + `AllHandoversSheet.tsx` (now the P3/P5 reference) |
| A7 | **Type select inside the form** | (historic) a `<Select type>` at the top of a create form whose fields change with the type | §1 Create | P4 or P6 step 1 |

When a phase touches a file listed here, the spec must either fix the anti-pattern or state why it is out of scope.

---

## 5. Component vocabulary — when, not how

| Component | Use when |
|---|---|
| `Card` | One concern on a tab grid. |
| `Dialog` | Create (≤ 3 fields), Edit (one section), Wizard, type picker. `sm:max-w-md` for forms, `sm:max-w-2xl` for editors. |
| `Sheet` | Edit with 2–3 sections, or a "Show all" list for entities without a route. |
| `AlertDialog` | Confirm destructive actions. Only dialog allowed on top of a dialog. |
| `DropdownMenu` | Row overflow actions (`EllipsisVertical` trigger), header "more" actions. |
| `Tabs` / `HashTabs` | Domains of one entity; URL-addressable sections. |
| `Table` / `DataTable` | Manage-list routes. Not inside half-width cards. |
| `Badge` | Passive state on rows (Enabled, Default, Managed, expiry, type). ≤ 2 per row. In a `DataTable` cell a tone-dot + label line is an accepted third form of passive state next to a `Badge` column (two chips flatten two unequal facts); it is not a hand-rolled badge. |
| `Switch` | Auto-saving boolean in an auto-save card; a row toggle only when toggling is the row's purpose. |
| `Tooltip` | Every icon-only button. |
| `Skeleton` | Loading state of rows and summary values. |
| `Pagination` | Manage-list routes; never in a card. |
| `Separator` | Facets inside a P7 card; menu groups. |
| `Alert` | Inline warning that changes what the user can do on this surface (locked control, missing prerequisite). Not for success feedback — toasts do that. |

Primitives that do not exist yet are extracted at the **second** consumer, from two real shapes, not designed speculatively at the first: `EmptyState`, `RowActionsMenu`, `PreviewList`. Added 2026-09-07: `Common/QueryErrorAlert.tsx` (error state with Retry; use it for R10's error branch).

---

## 6. States and copy

- **Loading:** `Skeleton` rows shaped like the real rows. No spinners in cards.
- **Empty:** one sentence saying what will appear here + the primary action. In cards: text + button; on routes: centred block.
- **Error:** an `Alert variant="destructive"` in place of the content with a Retry; never an empty state pretending nothing exists (`isError` must be handled separately from `!data`).
- **Pending:** disable the triggering control, keep the layout stable (no layout shift on save). When the triggering control unmounts on click (a menu item), the pending state moves to the thing it opened (the confirm's buttons) or to the row; Escape / outside-click must not close a dialog mid-request.
- **Result feedback that stays:** never. Success is a toast; only a state that still needs the user's action ("created but not their default key") stays, as an `Alert` with the action, and clears itself when the state resolves.
- **Feedback:** success and failure via `useCustomToast`; never inline success text that stays.
- **Copy:** titles are nouns ("Schedules"), descriptions are one sentence, buttons are verbs ("Add schedule") except P1 buttons, which name what they open; destructive buttons name the effect ("Delete schedule"). No "Manage" / "Settings" as button labels inside a settings page.

---

## 7. Design checklist (used by `cinna-core.ui.design`)

For every surface the plan adds or changes, the UI Specification answers, in this order:

1. **Surface** — name, route, host (which tab / grid / dialog).
2. **Story** — Create / View / Edit / Manage-list. If two, split.
3. **Placement** — card / dialog / sheet / route / tab, with the §2 row that justifies it.
4. **Pattern** — P1–P9 reused, or "new composition" (which triggers screenshots, §9).
5. **Density budget** — number of blocks (≤ 7), rows shown (≤ 5), visible actions per row (≤ 2) and the menu contents.
6. **Interaction model** — auto-save or form.
7. **States** — loading / empty / error / pending copy.
8. **Components** — from §5, plus any new primitive to add.
9. **Anti-patterns touched** — from §4, fixed or deferred with a reason.
10. **Verification** — "checklist review" or "checklist + screenshots" per §9, with the URLs and click paths to capture.

---

## 8. Review checklist and score (used by `cinna-core.ui.review`)

Answer each line **yes / no / n/a** with a file:line or screenshot reference:

| # | Check |
|---|---|
| R1 | The surface serves exactly one story. |
| R2 | The card names one concern and has ≤ 7 blocks. |
| R3 | Card is half-width in the grid, or is a table that needs full width. |
| R4 | No list in a card renders more than 5 rows without "Show all". |
| R5 | No row shows more than 2 inline actions; Edit / Delete / Disable / Set-default are in a `⋯` menu or hover-revealed (single-action rows). A row `Switch` counts as an inline action. |
| R6 | Destructive actions confirm with `AlertDialog` (or a `Dialog` that carries fetched impact data); never `window.confirm`, never unconfirmed. |
| R7 | No multi-field form is inline in a list; never more than one inline form at a time. |
| R8 | Disclosure depth ≤ 2; no dialog opens a dialog (AlertDialog and chained-after-close excepted); no shared control inside a dialog opens a `Dialog`. |
| R9 | One interaction model per card. |
| R10 | All four states exist and are distinct: loading (`Skeleton`), error handled separately from `!data` with a Retry, empty with an action, pending on the triggering control. Any one missing is a *no*. |
| R11 | Shared primitives used (`Switch`, `AlertDialog`, `Badge`, `Tooltip` on icon buttons); no hand-rolled equivalents. |
| R12 | Matches the referenced house pattern's skeleton (structure and class names). |
| R13 | Matches the UI Specification (or the deviation is justified in the report). |
| R14a | Status colours use semantic tokens (`bg-primary`, `text-destructive`) or the shared badge variants; a raw palette class (`bg-emerald-500`) on a status element is a finding unless it is the established idiom the whole codebase shares and a token sweep is logged as follow-up. |
| R14 | *(screenshots only)* Card height comparable to grid neighbours; no truncation, overflow or misalignment at 1440 and 1024; readable in dark mode. |

**Score 0–10 per surface** = 10 − (number of *no* among R1–R10). An *n/a* counts as satisfied: a form dialog has no rows, so R4–R6 are n/a and do not cost it points. R11–R14 are gates, not points: a *no* on any of them is reported as a finding and blocks PASS until fixed, but does not change the score.

**Verdict:** **PASS** = score ≥ 8 **and** none of **R4, R5, R7, R8** is *no* (these are the overload rules — an unbounded list, a row with visible Edit/Delete, N inline forms, a dialog in a dialog — and any one of them is ITERATE regardless of score). **FAIL** = score ≤ 5. Everything else is ITERATE. When ≥ 7 checks are n/a (a small dialog), say so and judge on the applicable ones; do not pad the score.

---

## 9. When screenshots are required — the complexity gate

Playwright screenshots (`node frontend/scripts/ui-shot.mjs`) cost time and see whatever data the dev database holds. They are **not** part of reviewing a simple surface, and they are never a substitute for the checklist. Use them only when the composition cannot be judged from the JSX:

Screenshots apply to surfaces **added or changed in the phase under review**. Unchanged neighbours and pre-existing dialogs are reviewed from code even if they would qualify below. At most **2 captures per surface** (one composition view, one interaction state); more is a sign the surface should be judged from code. When several cards move together (a tab switched to the grid), the thing inspected is the **host**: book the captures once against the tab, not per card.

Screenshots **required** when any of these holds for a changed surface:
- It is a **new composition** — no P1–P9 pattern covers it, or a pattern is used in a new host (e.g. first P5 instance).
- A **new route or tab** is added, or a card is moved between grids / tabs.
- It has **expand/collapse, drag, resizable panes, or a dense list (≥ 10 rows visible)**, or a stepper with > 2 steps.
- Its layout **depends on data length and the dev database actually holds enough rows to show it** (> 5). An uncapped `map` over 1 row proves nothing a screenshot can add; R4 is judged from code.
- The review found an **ITERATE** on a composition rule (R2, R3, R14) that cannot be settled from the JSX — card height against neighbours, wrapping, alignment.
- The user asked for a visual check.

Screenshots **not required** (checklist review of the JSX suffices):
- A form dialog, a summary row, a confirm dialog, a new menu item, a badge, copy changes — anything that is a straight instance of P1, P2, P3, P6, P7 with the reference's skeleton.
- Backend-driven changes where the frontend only rebinds fields.

Rules when screenshots *are* taken:
- The docker frontend on `:5173` is nginx serving the **image's** build, not the working tree; a capture there shows yesterday's code and proves nothing. `ui-shot.mjs` therefore never uses it: it reuses a Vite dev server on `:5199` or starts one for the run.
- Capture only the surfaces named in the spec's Verification line, at 1440 and 1024 (390 only for surfaces that claim mobile support). Dialogs and sheets are captured with `--element "[role=dialog]"`; pages with the default full-page mode (the tool expands the app's inner scroll region); themes with `--theme light|dark`.
- If a state cannot be reached with the tool (a wizard step that needs typed input, a list the dev database cannot populate), report **"required but not capturable"** and review that state from the JSX. Do not fake it.
- Do not create or modify data to make a screenshot look better; if the dev database lacks data for the state under review, say so and review the empty state instead.
- Findings from screenshots are about **composition** (height, alignment, overflow, density). Content seen in a screenshot (names, emails, keys) is test data and is never quoted in a report.

---

## 10. Lessons log

Append one line per lesson learned from a review or calibration run: date, surface, what the rule missed or over-flagged, and the rule change made. This is how the thresholds above earn their numbers.

- 2026-09-07 — initial rules derived from the agent configuration page (good) and the surfaces in §4 (bad).
- 2026-09-07 — calibration round 1 (9 surfaces, opus + sonnet, blind). Recall on the labelled defects: complete on both models. Misses/over-flags and the rule changes made:
  - §8 counted only *yes*, so a clean P1 card (4 n/a) scored 6 and a clean form dialog 5 → score is now 10 − *no*, n/a satisfied.
  - App Sessions PASSed at 8 with an unbounded list because R4 was not a gate → R4 added to the gates.
  - R10 was answered *yes* for surfaces whose error branch rendered as empty → R10 split into four named states.
  - §9 fired on every uncapped `map` regardless of data, and on pre-existing dialogs next to the surface under review; opus took 5 captures for one card → gate scoped to changed surfaces, data-length trigger needs > 5 real rows, max 2 captures per surface.
  - P3 "one metadata line" could not hold a schedule's cadence + next run → ≤ 2 facts joined by ` · `.
  - "Buttons are verbs" flagged the P1 reference's noun buttons → P1 exempt.
  - P5 named `DesktopSessionsCard` as its reference while §4 listed it as A4 → reworded: designated first instance, currently A4.
  - P4 and P6 references drift from their text (extra "Connect MCP Provider" row; no stepper header, nested success dialog) → the variants that are legitimate are now named in the pattern; the rest is "fix on touch".
  - An Enable toggle that grows a card (Handovers) was only catchable via R14 → card-height rule now names state toggles.
  - A picker dialog stacked on a form dialog (credential detail route) tripped R8 with no guidance → value-picker sentence added to disclosure depth.
  - A confirm that must show fetched impact data (`DeleteAICredentialDialog`) uses `Dialog`, which R6 read as a violation → named exception.
  - Wizard selection checklists were read as "lists in a card" → excluded, with a max-height rule.
  - The review brief said "including where the user lands", so one run reviewed the whole credential detail route and returned ITERATE for a flow the calibration expected to PASS → briefs name surfaces; side branches are reported as adjacent findings and do not set the verdict (see `cinna-core.ui.review`).
  - `ui-shot.mjs` stopped at 900 px (inner scroll region) and `--dark` was a no-op (the app persists its own theme) → scroll-region expansion, `--theme`, `--element` for dialogs; click settle raised to 1500 ms after a blank capture at 800 ms.
  - Build round (Handovers + onboarding admin, both PASS 10/10 after 1–2 rounds): P2's inline `Pencil` vs R5 → written as the only-action case; P3 hover-reveal pushed a menu-only View row into hiding → narrowed; P5 gained the route-vs-Sheet test and became real (`AgentHandovers`); P6's single-form variant was the reference's mechanics, not a defect → allowed; three moved cards booked three captures → host booking; the screenshot step caught a description squeezed beside a header button and a 240 px `Select` wrapping its label at 1024 — both fixed before review (P3 header skeleton). The Enabled badge kept `bg-emerald-500` because `bg-primary` is the default badge variant and no `--success` token exists (9 files share the idiom) → R14a + follow-up: add `--success` token and a `success` badge variant, then sweep. `QueryErrorAlert` extracted as the first shared error-state primitive.
  - Channels build: the Configure Sheet capture needed a state the dev data did not have; the builder set it through the product UI and reset it with the product's own delete after checking nothing was set before. Acceptable only in that exact shape (reversible, verified-clean beforehand, restored) — the rule stays "do not stage data"; a state that needs it is otherwise "required but not capturable". An Edit Sheet may auto-save when every field persists independently and its null means "inherit" server-side (buffering would duplicate the inherit rules in the client); an audited consent switch is a second reason.
  - Settings batch (opus): a 6-row list double-failed R2 and R4 because a block was "one row" → a list is one block. Person-level toggles inside a channel row only fell out of R1 by inference → control scope = row scope. `ListModelsButton` opens a `Dialog` inside every dialog that embeds it → shared controls rule. Two reviewers split on `Dialog`-with-impact-data confirms → R6 names it as allowed. Edit dialog → informational dialog after close was read as nesting → chained is allowed.
