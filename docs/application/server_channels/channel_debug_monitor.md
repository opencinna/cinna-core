# Channel Debug Monitor

## Purpose

Gives a superuser a live view of what a [server channel](server_channels.md) is actually receiving and sending, and what the inbound pipeline decided about each message — so configuring a channel against a real provider stops being a blind exercise.

The feature is deliberately named for *channels*, not for Google Chat: it reads its rows from the adapter-agnostic pipeline, so every present and future channel type gets it with no extra work.

## Core Concepts

- **Captured event** — one line in the feed: a message arriving, a rejection with its reason, a routing outcome, or an outbound delivery. Events are recorded at the decision points of the inbound pipeline, not merely at the door.
- **Event kind** — what the pipeline did, as a badge: `received`, `rejected`, `routed`, `installing`, `no_match`, `replied`, `send_failed`, `test_send`. An unrecognised kind still renders, so a new kind added to the backend never produces a blank row.
- **Capture window** — the feed is held in memory by the backend process. It is bounded (a ring buffer per channel), and it is emptied by a backend restart. The panel states the moment capture began for exactly this reason.
- **Recent sender** — someone the channel has seen, and the thread to reach them on. Drawn from thread bindings (durable) merged with the capture buffer (live).
- **Test target** — where an admin test message goes: an email picked from the recent senders, or a raw channel-native space/thread id.

## User Stories / Flows

### 1. Admin watches a message arrive

1. Superuser opens **Admin → Server Configuration → Channels** and clicks the bug icon on a channel row.
2. The panel polls while it is open and lists events newest-first, each with the time, an event-kind badge, the sender, the thread, the message text, and a short sentence saying what the pipeline decided.
3. The admin sends the app a message from the chat client and watches it appear within a few seconds.

### 2. Admin diagnoses "nothing happens when I message the bot"

1. **No events at all** — the provider is not reaching the webhook. Check the webhook URL and that the channel is enabled. (Note the "capturing since" time: a backend restart empties the feed, so an empty panel on a freshly restarted backend proves nothing.)
2. **`rejected` at the verify stage** — the request reached us but its signature did not check out; the usual cause is a wrong project number, since that value is the JWT audience.
3. **`rejected` at the whitelist stage** — the sender is not allowed. The row carries the whitelist as configured, which is the admin's next question anyway.
4. **`rejected` at user resolution** — no platform account for the sender and auto-registration is off.
5. **`no_match`** — the message arrived and was allowed, but neither the sender's installed agents nor the auto-install catalog matched it.

### 3. Admin replies into a thread they just saw

1. Any event carrying a thread gets a **Reply here** action.
2. It sends an admin test message into that exact thread — the conversation the admin just watched arrive, rather than a space id they have to find and paste.
3. The send appears in the feed as a `test_send` and is recorded in the security event feed.

### 4. Admin tests outbound delivery by email

1. In the channel's setup panel, **Test outbound** offers a picker of people the channel has seen.
2. The admin picks an address; the test lands in the conversation that person already has with the app.
3. Someone the platform has never seen cannot be picked, and typing their address returns an explanation rather than a provider error — see the resolution rule below.

## Business Rules

### Capture

- Capture is **always on** and needs no configuration.
- The feed is **in memory and process-local**. It is not an audit trail: it survives no restart, any superuser can clear it, and behind multiple backend workers a panel shows only the events its own worker handled. The durable record of denials and verification failures remains the security event feed.
- Message text is captured, and clamped past a length limit with the truncation marked rather than silent.
- **Consecutive identical events collapse into one row with a count**, and the row's timestamp becomes the latest occurrence. This keeps a retry storm readable, and it is also a defence: the ring is bounded and the webhook is reachable by anyone holding the token, so without collapsing, one repeated request could push every real event out of the feed an admin is trying to read. Only *consecutive* identical events merge — an intervening different event keeps them apart, and two rejections at different pipeline stages stay separate rows.
- A rejected-at-verification event carries **nothing from the payload** — it failed the very check that would let any of it be trusted.
- Capture can never affect delivery: a failure to record is swallowed, never propagated into the webhook or the outbound path.

### Reachability and test targets

- An email is resolved **locally**, to a thread already observed for that person on that channel. It is never handed to the provider.
- Consequently an address the platform has never seen **has no reachable destination**. This is a real limit of app-level authentication, not an omission: Google Chat's email alias for a user is documented as user-authentication-only, and channel adapters authenticate as an app. The admin gets that explanation instead of a provider 404.
- A test send must name **exactly one** target — an email or a raw thread id, never both and never neither.
- Recent senders merge two sources; a durable binding wins over a buffered sighting for the same address. The buffer half matters: someone who was just *denied* has no binding, and is exactly who an admin wants to test against while debugging.

### Access

- Every debug route is **superuser-only**, both reading and clearing, because the feed carries sender identity and message text.
- Deleting a channel drops its captured events.
- An admin test send writes a security event recording who sent something where — the message body is deliberately not included.

## Architecture Overview

```
Provider ──webhook──▶ inbound pipeline ──┐
                       (verify, whitelist,│ records decisions
                        route, install)   │
                                          ▼
outbound delivery ──────────────▶  in-memory capture buffer (per channel, bounded)
                                          │
        Admin UI ◀──polls while open──── superuser-only admin routes
             │
             └── "Reply here" ──▶ test send ──▶ adapter ──▶ Provider
                                       │
                                       └──▶ security event (durable)
```

## Integration Points

- **[Server Channels](server_channels.md)** — the parent feature. The monitor observes its inbound pipeline and outbound delivery; it adds no routing behaviour of its own.
- **[Agent Activities & Security Events](../agent_activities/agent_activities.md)** — the durable counterpart. Verification failures, whitelist denials, auto-registration, auto-install and admin test sends are recorded there and survive restarts; the monitor is the live view beside it.
- **Adapters** — the monitor reads only adapter-agnostic pipeline values, so a new channel type is covered without touching it.

## Technical Details

See [channel_debug_monitor_tech.md](channel_debug_monitor_tech.md).
