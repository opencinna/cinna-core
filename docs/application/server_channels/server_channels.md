# Server Channels

## Purpose

Let people **outside the platform** — company employees on a chat app, starting with Google Chat — talk to platform agents without ever logging in. A superuser configures one or more channel instances server-wide; inbound messages are verified, whitelisted, routed to the right agent (installing one automatically if needed), bound to a persistent conversation thread, and answered back through the same chat app.

## Core Concepts

- **Server Channel** — one admin-configured instance of a channel type (currently only Google Chat). Owns its own webhook URL, outbound credential, email whitelist, and auto-registration toggle. Superuser-only, managed from the **Channels** tab on `/admin/server-configuration`.
- **Channel Adapter** — the transport-specific implementation (Google Chat today) behind a shared contract: verify an inbound request, parse it into a normalized message, send a reply, describe its own setup steps. Adding a new channel type (Discord, Telegram, …) means writing one adapter module and registering it — no pipeline change, no migration.
- **Webhook Token** — an unguessable, per-channel path segment (`POST /api/v1/channels/{webhook_token}/inbound`) that is the channel's only "address." It is not a secret in the cryptographic sense (it doesn't prove who sent the request — the adapter's signature check does that); it is what keeps the endpoint from being guessed.
- **Email Whitelist** — a comma-separated list of glob patterns (`*@example.com, devops.*@support.com`) gating which verified senders may reach agents through this channel. Shared matching logic with the email integration's sender allowlist.
- **Auto-Registration** — when enabled, a whitelisted sender with no platform account gets one created automatically, passwordless and already email-confirmed.
- **Channel Thread Binding** — the record that pins one external conversation thread to one platform `(user, agent, session)` triple. This is the feature's conversation state; there is no user-facing view of it — its lifecycle is only observable through the replies a sender sees and the sessions that appear under their account.
- **Server-Wide Auto-Install List** — a curated list of catalog bundles the platform may install automatically for a sender whose message doesn't match any agent they already have.
- **Two-Pass Routing** — every new thread is routed first against the sender's *own* installed agents, then — only on a miss — against the auto-install list.

## User Stories / Flows

### 1. Admin sets up a channel

1. Superuser opens **Admin → Server Configuration → Channels**.
2. Clicks **Add Channel**, picks a type from the card list (Google Chat), then on the form that opens names it and enters the GCP project number (the value Google puts in the webhook JWT's audience claim).
3. Pastes the Google Chat service-account JSON key — a write-only field; once saved it can only be replaced, never re-displayed.
4. Sets the email whitelist (patterns, or `*` for "anyone Google verifies") and decides whether unknown senders should be auto-registered.
5. Saves. The **setup instructions** panel opens automatically, showing the webhook URL to paste into the Google Chat app's configuration and a step-by-step checklist.
6. Uses **Test outbound** to confirm the stored credential actually works before rolling it out. The target is picked by *email* from the people this channel has already seen; a raw space/thread id stays available as an escape hatch. An email can only be resolved once the app has received a message from that person — the provider's email alias requires user authentication and the app authenticates as an app — so before anyone has written in, the raw id is the only option.
7. Opens the **debug panel** (bug icon on the channel row) to watch messages arrive live and see what the pipeline decided about each one — see [Channel Debug Monitor](channel_debug_monitor.md).
8. Pastes the webhook URL into the Google Cloud Console Chat app config. The channel is live.

The webhook URL is built from the backend's public origin (`BACKEND_BASE_URL`, falling back to `FRONTEND_HOST`) — not from whatever host the admin reached the admin page on. On a deployment where the SPA and the API live on different hostnames, `BACKEND_BASE_URL` must name the API one, or the URL an admin copies points at the SPA and no event ever arrives. For testing a local backend against a real Google Chat app, `make webhook-tunnel` + `make webhook-set-url URL=...` point it at a public HTTPS tunnel (see server_channels_tech.md → Configuration).

### 2. Employee's first message (no UI — this happens entirely outside the platform)

1. An employee DMs the Google Chat app, or the app is added to a space and mentioned there.
2. The message arrives at the webhook, is verified, whitelist-checked, and the sender is resolved (auto-registered if new and allowed).
3. Since this is a brand-new thread, the platform tries to route it: first against the sender's own installed agents, then — if none match — against the server's auto-install list.
4. If a match is found on the auto-install list, the matching bundle is installed for that sender behind the scenes and a "Setting up **X** for you…" reply appears in the thread while the environment builds.
5. Once ready, the agent's real reply lands in the same thread, and a short "Your assistant is ready" notice precedes it if there was a wait.
6. If nothing matches, the sender gets a polite "couldn't find an agent for that — contact your admin" reply.

### 3. Continuing a conversation

1. The employee replies in the same Google Chat thread.
2. Because the thread already has an active binding, the platform skips routing entirely and feeds the message straight into the same session — the same principle App MCP uses for a caller's already-resolved context.
3. The agent's reply streams back to the thread as it completes.

### 4. Environment build fails during auto-install

1. A Pass-2 auto-install starts building an environment; the sender's message is parked in the meantime.
2. If the build fails outright, the binding is marked failed and the sender gets a "setting up your assistant failed — contact your admin" notice.
3. The *next* message the sender sends into that same thread deletes the failed binding and re-runs routing from scratch — a transient failure never permanently wedges the thread.

### 5. Cross-user thread collision (group spaces)

1. Two different whitelisted people can, in principle, post into the same brand-new thread in a shared space before either message has finished routing.
2. Whoever the system finishes creating a binding for first "owns" that thread's session going forward.
3. The other person is told the conversation belongs to someone else and is asked to start a new thread — their message is never merged into a stranger's session, whichever step of the pipeline the collision is caught at.

## Business Rules

### Trust chain and fail-closed defenses

The inbound webhook is **platform-unauthenticated by design** — anyone on the internet can `POST` to it. What actually stands between the open internet and creating an agent session, in the order it is checked:

1. **Rate limiting and a body-size cap** run before anything else is even parsed.
2. **Webhook-token resolution** — an unknown token *and* a disabled channel both answer with an identical, detail-free 404. Disabling a channel must not become an oracle that tells a prober "this token used to exist."
3. **Adapter verification is the single authentication chokepoint and runs before anything else touches the payload.** For Google Chat this means checking a bearer JWT against Google's public keys (issuer, audience, signature) — nothing downstream re-checks it, and the whole request is rejected (403, no detail) if it fails.
4. **The email whitelist fails closed.** A null or empty whitelist denies *everyone* — an unconfigured channel is a channel nobody can use, not an open one. `*` is the only pattern that allows all verified senders. The whitelist is a comma-separated list matched by any-token-matches semantics, so `"*, ops@corp.com"` is still a blanket allow, not a scoped list with one extra address — this is the single easiest thing to misread about the feature, and both the admin help text and the admin UI itself surface it as an explicit warning rather than leaving it implicit.
5. Only after all of the above does a sender's email get resolved to (or used to create) a platform account.

A sender's email is trusted only because the adapter verified it came from a payload the transport itself signed — the same trust tier the email integration extends to a verified IMAP sender.

### Registration

- **The channel's own email whitelist is the sole registration gate.** The platform-wide `AUTH_WHITELIST_USER_DOMAINS` signup allowlist is deliberately **not** re-checked — same precedent as the email integration's auto-user creation, so the two features can't silently disagree about who's allowed to have an account.
- Auto-registered users are ordinary, passwordless, already-email-confirmed `agent-user` accounts — every downstream limit and gate (agent-count limits, credential isolation, catalog visibility) applies to them exactly as it would to anyone else. If the person later logs in with Google OAuth on the same address, the existing by-email account linking picks the account up naturally.
- An inactive account (`is_active=False`) is treated as a denial, indistinguishable from a whitelist miss.
- Registration only happens when `auto_register_users` is on; otherwise an unknown sender is simply denied.

### Routing

- **Pass 1 — the sender's own installed agents.** Uses the same App MCP routing machinery as the App MCP Server, but with a hard ownership filter on top: an identity route (which by construction points at someone *else's* agent) is rejected outright, and the resolved agent must literally be owned by the sender. Without this, a caller could otherwise be routed into another user's workspace through a route that happens to be "effective for" them.
- **Pass 2 — the server-wide auto-install list**, tried only when Pass 1 finds nothing. Candidates are the union of bundles on the list that the sender hasn't already installed, filtered through the same catalog visibility check every other install path uses, and that carry a router trigger prompt to classify against. **Membership on the auto-install list is never an implicit permission grant** — a non-public bundle nobody has shared with the sender simply never becomes a candidate for them, no matter how loudly it "matches."
- A bundle already installed by the sender (including the publisher's own working install) is excluded from Pass 2 — it had its chance to match in Pass 1.

### Thread binding lifecycle

- A binding starts `pending_install` while an auto-installed environment builds, and messages that arrive in the meantime are parked (up to a cap; beyond it, the newest arrival is refused with an explicit "I've got a lot queued, ask again" reply rather than silently dropped).
- It becomes `active` once the environment reaches `running` and any parked messages have been delivered in order.
- `failed` is not a dead end: the **next** inbound message on that thread deletes the failed binding and re-routes from scratch, so a one-off build failure never wedges a thread permanently.
- **A thread belongs to exactly one person.** If a different whitelisted person posts into a thread already bound to someone else, they're told the conversation belongs to someone else rather than being silently merged into that person's session — this applies both when the binding already existed and when two people's first messages in a brand-new thread race each other (the loser of that race gets the same refusal, whether their message is caught at the immediate-ingest step or while it's being parked behind a slower auto-install).
- Uninstalling the bound agent cascades away the binding (next message re-routes, and the reinstall picks the same App Data back up); deleting the bound session only clears the pointer (next message opens a fresh session on the same agent).

### Two accepted divergences from the original plan

- **A `critical_state` environment does not fail a binding.** `critical_state` means the container is up and running, but a post-start provisioning step (a package install, a credential sync) failed — sessions, chat, and terminals keep working. Since the environment would actually have answered the sender, only a genuine `status == "error"` (or `deprecated`) fails a binding; a degraded-but-running environment just keeps waiting like any other in-progress build.
- **The Google JWKS verification hardening was fixed where the keys are fetched, not where they're used**, so both this feature and the two pre-existing Google OAuth login paths get the fix for free with identical external behavior. A JWKS fetch failure is treated as "cannot verify right now" (denied, but logged and distinguishable from a forged signature) rather than misreported as an invalid token.

### Admin surface rules

- Every admin route is superuser-only — there is no partial, role-based access to channel administration, because a channel holds an outbound credential and decides who can reach agents at all.
- The outbound secret is write-only: it is never echoed back by any read endpoint, and editing any other field of a channel leaves a previously-stored secret untouched.
- Regenerating the webhook token is a deliberate, confirmed action — it immediately invalidates the URL pasted into the external chat app's configuration.

## Architecture Overview

```
Google Chat ──webhook──▶ POST /api/v1/channels/{webhook_token}/inbound
                              │ 1. rate limit + body-size cap
                              │ 2. resolve channel (404: unknown OR disabled)
                              │ 3. adapter.verify_inbound — Google JWT, fails closed (403)
                              │ 4. redelivery dedup
                              │ 5. whitelist check — fails closed
                              │ 6. resolve / auto-register user
                              ▼
                    binding lookup (channel, thread)
                    ── active ─────▶ continue thread (background) ─▶ ChannelIngestionService
                    ── pending ────▶ park message, "still setting up" reply
                    ── failed ─────▶ delete binding, fall through to routing
                    ── missing ────▶ webhook acks immediately; routing runs as a background task:
                                        Pass 1: sender's own installed agents (App MCP routing, ownership-filtered)
                                        Pass 2: server auto-install list (AI classification, catalog-gated)
                                          → install bundle, binding(pending_install), park message

channel_pending_scheduler (every 45s, TESTING-gated)
                              │ env running        → deliver parked messages, binding → active
                              │ env error/deprecated → binding → failed, notify sender
                              │ env critical_state  → NOT a failure — still waiting
                              │ still building, too long → binding → failed (bounded wait)

Agent reply (STREAM_COMPLETED) ──▶ ChannelOutboundService ──▶ binding lookup by session_id ──▶ adapter.send_message
Agent error (STREAM_ERROR)     ──▶ same path, generic failure notice
```

## Integration Points

- [Agent Sessions / Channel Ingestion](../agent_sessions/channel_ingestion.md) — Server Channels is a new caller of the canonical inbound pipeline, adding a `channel_caller` `SessionSender` kind that behaves like `task_executor`: the session is always owned by the sender's own resolved user, never the agent's publisher.
- [App MCP Server](../app_mcp_server/app_mcp_server.md) — Pass-1 routing reuses `AppMCPRoutingService.route_message` as-is; install-time auto-created App MCP routes are what make a freshly auto-installed agent immediately reachable for future messages with no extra wiring.
- [Agent Bundles & Installs](../../agents/agent_bundles/agent_bundles.md) — Pass-2 auto-install uses the same idempotent `InstallService.install_bundle` entry point every other programmatic install uses, and is gated by `CatalogService.user_can_install` visibility.
- [Email Integration](../email_integration/email_integration.md) — Server Channels deliberately mirrors the email integration's precedents: fail-closed sender-pattern matching, "the feature's own whitelist is the registration gate, not the signup allowlist," and best-effort outbound delivery with a persistent queue as a future enhancement, not a v1 requirement. A shared user-creation helper (`UserService.create_external_user`) now backs both features' auto-registration.
- [Google OAuth](../auth/google_oauth.md) — the Google Chat adapter's JWT verification reuses the same generalized, cached JWKS verifier the Google OAuth login path uses, pointed at a different issuer and key set.
- [Server Configuration](../server_configuration/disclaimer.md) — Channels is a new tab on the same `/admin/server-configuration` admin page as the Disclaimer feature, following the same superuser-guard and HashTabs conventions.
- [Realtime Events](../realtime_events/event_bus_system.md) — outbound delivery subscribes to `STREAM_COMPLETED` / `STREAM_ERROR` the same way the email integration's sending service does.
- [AI Functions](../../development/backend/ai_functions_development.md) — Pass-2 classification is a second caller of the same `AIFunctionsService.route_to_agent` helper the App MCP router uses.

## Known Limitations

- **The admin UI has not been visually verified in a browser.** It passed TypeScript, lint, and build checks, but no interactive/visual QA pass was available during development — treat the first real admin session as the first real look at it.
- **Outbound delivery is best-effort**: three immediate retries inside the adapter, then the failure is recorded on the binding and logged. There is no persistent outbound queue (the email integration's `OutgoingEmailQueue` pattern is a listed future enhancement) — a sender whose reply was lost can only ask again.
- **The pending-binding scheduler assumes a single backend process.** There is no leader election; a multi-worker deployment would need the same advisory-lock leader pattern (and the same connection-pool caveat) used by the model-discovery scheduler, or parked messages could be delivered more than once.
- **Deferred (known, intentionally not fixed here):** the shared App MCP routing code Pass 1 calls into (`app_agent_router.py`) still logs message text at INFO level for what is now, via this feature, externally-sourced traffic — it was written for internal routing traffic and needs a scoped fix, not one buried inside this feature.
- **Deferred (known, intentionally not fixed here):** rejection/verification-failure audit events are attributed to the channel's creator (`ServerChannel.created_by`); if that superuser account is later deleted, the foreign key nulls out and denial/verification-failure auditing for that channel silently stops being written as `SecurityEvent` rows (it still reaches the application log).

---

*Last updated: 2026-08-21*
