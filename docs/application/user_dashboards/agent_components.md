---
feature: agent_components
domain: application
one_liner: "Shows an agent's own dashboard components as tiles on a user dashboard, with cached refreshable data, a run budget, agent-driven highlights, and a per-dashboard chat."
docs:
  tech: agent_components_tech.md
affects: [user_dashboards, agent_webapp, chat_windows]
---
# Agent Components

## Purpose

An agent can ship **dashboard components**: small, declared views (markdown, table, list, metrics, or a custom html page) that live in its workspace under `dashboards/<name>/` or inside one of its skills. This feature lets the owner place such a component on a [User Dashboard](user_dashboards.md) as an `agent_component` block (a **tile**), keep it fresh from the cloud environment, and talk to the agent about it from a chat that knows what the tile shows.

The component format, the params model, the tile tools and the page bridge are defined by the Local Agent Kit dashboards contract (`docs/local_agent_kit/`, `protocols/dashboards.schema.json`, `protocols/dashboards-bridge.js`, kit contract 1.21.0 for the cloud wording). Cinna core is one host of that contract; Cinna Desktop is the other. This doc covers what the cloud host does.

---

## Core Concepts

- **Component**: a folder with a `component.json` descriptor (view, params, endpoints, refresh policy, optional entry page). Found by the environment's `/dashboards/components` listing.
- **Tile**: an `agent_component` block whose `config` names the component (and skill, if any), the stored params and optional refresh overrides.
- **Cached result**: the last good value is stored per block and served first, stale or not. The page then asks for a refresh with a reason.
- **Params**: the owner's (or the agent's) choices for the component's declared params. The whole set is replaced on write; keys left out return to their default; `current` and `today` resolve in the owner's timezone, else UTC.
- **Dashboard chat**: one chat session per (dashboard, agent), opened from the dashboard page.
- **Reference**: a `cinna://component/...` link in a chat message that renders as a card for the component.

---

## User Flows

### Add a component tile

1. In edit mode the owner adds a block, picks an agent and the view type "agent component".
2. The backend lists the agent's components (waking the environment if needed) and the owner picks one. Components with validation errors are listed with their findings and cannot be added.
3. The tile is saved with the component name, skill and any param values. Adding or editing a block checks that the component exists, the params fit, and the agent's required credentials are present.

### See a tile

1. Opening the dashboard serves the cached result immediately and requests a refresh with reason `open`.
2. A tile whose component has an html entry page is drawn in a sandboxed frame served from the `/d/` content host (see the tech doc).
3. Missing credentials, a failed run (the cached data stays, with a one-sentence mark) and an environment that predates this feature (`adapter_unsupported`, with a rebuild prompt) are reported on the tile rather than hidden.

### Refresh rules in the cloud

| Reason | When | Effect |
|--------|------|--------|
| `open`, `reshow` | The tile opens or is shown again | Wakes a sleeping environment even when the cache is fresh (D-C6); recomputes only if the cache is older than `max_age` (component default, tile override, else 15 minutes) or was computed with other params |
| `interval` (`while_open_every`) | A tick while the tile stays open (the page floors it at 60 s) | Returns the cache when the last fetch is under 60 s old; skipped unless the environment is already running; never wakes it and never extends its activity |
| `manual`, `after_turn`, `params` | Refresh button, an agent turn finished, a params change | Always recomputes |
| schedule-fed | The component declares a schedule | Recomputes lazily on read, once per settled schedule run; runs that settled while nobody looked collapse into one |

`max_age` is checked on open and on re-show only, never while a tile stays open. `on_app_start` is ignored in the cloud (no app launch; a login warm-up would wake suspended environments). Every recompute except `interval` keeps the environment awake the way webapp traffic does. Concurrent recomputes of one tile are deduplicated.

### Run budget

Each target has at most 30 counted runs per minute, held in the database so it holds across workers. Counted: a params change that resolves to different values, a page call to an endpoint, and an agent run. Not counted: opening, refreshing, an empty-params source preview. Over the limit the call fails with `rate_limited` and a retry hint. A params write that resolves to the values the tile already has (an "echo") is not a change and costs nothing.

### Highlights

The agent can highlight rows or items on a tile (`list` and `table` views natively; an html page only once it has reported that it can take highlights). Highlights live in the owner's open dashboard page, per tile, and are lost on a full reload. The backend only relays them over the owner's socket room and stores whether each tile's current page document can take highlights, so `can_highlight` is answered correctly on any worker. That readiness expires after 15 minutes without a write: the page re-sends it every 10 minutes while the frame is attached and clears it when the frame detaches or the page is hidden. A refused params change clears the tile's highlight.

### Dashboard chat and the context block

The dashboard page hosts a chat with one of the agents that has a tile on it. The session is marked `session_metadata.dashboard_chat` and linked by `session.dashboard_id`; asking again for the same dashboard and agent resumes the latest such session, or a new one is started on request. Prompt-action sessions started from block overlays are separate and also receive `dashboard_id`.

On send the page may attach a `<dashboard_context>` block (what the owner sees: tiles, focused tile, selection, selected text). It is sanitised (control characters stripped, selected text capped at 2,000 characters, summary at 8,000, whole block at 12,000), kept in message metadata and never shown in the transcript. When assembling the agent's turn it is included in full the first time and omitted when identical to the last one already sent in that session. It is data about the page, never instructions.

### Agent tile tools

In a dashboard chat the agent has two tools:

- `dashboards_tiles`: the tiles of this chat's dashboard that belong to this agent, with params, view, summary or selection for the focused tile, and `can_highlight`.
- `dashboards_update_tile`: change a tile's params and/or highlight ids. A change recomputes the tile in the background and notifies the page; no change means no run.

The tools are listed in every conversation chat of both engines (OpenCode caches its tool list for the life of the connection), so outside a dashboard chat they answer `no_dashboard`. Building-mode sessions get none. The result is JSON text (the Claude SDK drops structured content); an error is the full `{"error": {...}}` JSON.

### References and cards

An agent can mention a component as `cinna://component/<agent-ref>/<component>` or `cinna://component/<agent-ref>/<skill>/<component>`. `<agent-ref>` is `bundle:<bundle id>` (every agent row has one; resolved among the viewer's own agents, the chat's agent winning), `id:<agent id>` or the agent slug (both only against the chat's own agent). The backend resolves refs to a status (`ok`, `invalid`, `ambiguous`, `not_installed`, `gone`, `env_asleep`, `adapter_unsupported`) plus component title, view, findings and where it is already placed. Rendering a card never wakes an environment. Agent tool calls, manual / `after_turn` / `params` refreshes, page calls, params changes and ref resolution read the component list fresh; other reads use a 10-second cache.

---

## Frontend

- **Adding a tile.** The Add block dialog has an agent popover, a view-type pill group and, for "agent component", a Component field (a picker of the agent's components with their findings). The Edit dialog adds a Refresh section with the overrides: `max_age`, "While open" (every minute or longer, or Manual; the page floors any value at 60 s) and "After each turn". There is no `on_app_start` control. Remove asks for confirmation in an alert dialog, and prompt actions are hidden on component tiles.
- **The tile.** One shared view draws markdown, table, list, metrics or an html page. A params row sits above the content (enum params offer a "Default" option). The footer shows when the data was updated, stale / mark / notice flags or the missing-credentials sentence, a highlight stepper, Refresh and Ask. Ask appears only when a chat panel is registered, so not on fullscreen. The focused tile gets a ring.
- **Refresh.** The first time a tile is on screen it asks for `open`; each time it comes back (scrolled into view, or the browser tab shown again) `reshow`. "While open" runs only while the tile is on screen and sends `interval`, which the server skips when the environment sleeps. The page re-reads the cached data every 30 seconds while visible.
- **Html tiles.** The page runs in a sandboxed frame without `allow-same-origin`, so its origin is `null`. The host talks to it by `postMessage` per the kit contract: a fresh nonce on every `ui/initialize`, messages without the current nonce dropped, the tile identified by `event.source`, data sent after `initialized`, replies posted to `'*'`. A page's link request opens a confirmation, "Open <host> in a new tab?" (or "Write to <address>?" for mail); a page's `ui/message` fills the chat composer and never sends, the latest wins, and fullscreen answers "No dashboard chat here."; failed `tools/call` replies carry `{error: {code, message}}` and `env_unavailable` / `adapter_unsupported` are sent as `call_failed`. Native list and table tiles open their `url` values as plain links.
- **Highlights.** Kept in memory per tile in the owner's page; set by the agent's highlight event (an empty list clears), stepped with the footer stepper, replayed to an html page when it reports ready.
- **The chat panel.** On `/dashboards/$dashboardId` a toggle opens a right column (320 px from 1024 px, 360 px from 1280 px); below 1024 px it is a sheet that always starts closed. It is hidden in edit mode and on fullscreen. One conversation per agent that has a tile on the dashboard; focusing another agent's tile switches the conversation, and each agent keeps its draft. A context chip names the focused tile and can be removed (focusing the tile again brings it back). Starter prompts come from the component's `prompts[]`. New chat is disabled until the conversation has a message; the composer placeholder is "Ask about this dashboard".
- **After a turn.** When a turn in the panel ends, the panel invalidates that agent's component specs and list and the session's resolved reference cards, then runs the tiles' `after_turn` or re-read.
- **Context on send.** The panel collects the tile's page context (with a 500 ms timeout, only while the panel is visible) and sends the `<dashboard_context>` payload with the message. `focused.data_age` is the fetch time, not an age, so the context hash stays stable. Over 12,000 characters it drops `other_tiles`, `also_yours`, the summary, `context`, `selected_text`, then halves `selection`. No focused tile means no context.
- **Reference cards in the panel and session chats.** See [Chat Windows](../chat_interface/chat_windows.md#component-references). The card offers Preview (a dialog that runs the component in preview mode, using the shared preview budget) and "Add to dashboard..." (sizes 3x2, 6x2, 6x4); Preview closes before the add dialog opens.

---

## Business Rules

- A component is only reachable by its agent's owner. Every `/d/` request re-checks ownership; the frame cap is a signed capability, not a session.
- Tiles never persist the dashboard context block in the visible transcript.
- Tile tools act only on the chat's own dashboard and only on the chat's own agent's tiles.
- Components whose params, endpoints or view fail the kit checker are not offered and cannot be added.
- An html tile wakes its environment on open (accepted); a card never does.

## Edge Cases

- Environment predates `/dashboards/*`: `adapter_unsupported`, rebuild prompt.
- Component deleted or renamed after placement: the tile reports `not_found`; chat refs show `gone`.
- Replacing the component or skill of a block drops its cached result.
- Highlight relay uses the worker-local Socket.IO emit; the compose deployment runs one worker.
- A 5 MiB cap applies to a cached result.

## Integration Points

- [User Dashboards](user_dashboards.md): the `agent_component` view type, block add/edit, and the chat panel host.
- [Agent Webapp](../../agents/agent_webapp/agent_webapp.md): data scripts and component endpoints share env-core's endpoint runner; the webapp keeps plain `python`.
- [Chat Windows](../chat_interface/chat_windows.md): reference cards and link handling in chat markdown.
- [Agent Environment Core](../../agents/agent_environment_core/agent_environment_core.md): the `/dashboards/*` routes and the `dashboards_*` tools.
- [Artifacts](../artifacts/artifacts.md): the `/d/` host reuses the content-origin middleware, cap pattern and frame ancestor logic.
- [Local Agent Kit](../local_agent_kit/local_agent_kit.md): the contract, skill and bridge shipped to agents.
- [Agent Bundles](../../agents/agent_bundles/agent_bundles.md): the install readiness gate's per-credential check is reused for tile credentials.

## Changelog

- Initial documentation: backend, environment and frontend.
