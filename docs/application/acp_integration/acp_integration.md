---
feature: acp_integration
domain: application
one_liner: "Exposes agents to external Agent Client Protocol clients through authenticated WebSocket connectors with persistent conversations, streamed replies, and cancellation."
docs:
  tech: acp_integration_tech.md
affects: [agent_credentials, agent_sessions, flow_message_ingress, flow_auth_token_taxonomy]
---
# Agent Client Protocol integration

## Purpose

An agent owner can expose an agent to external software through **Agent Client Protocol (ACP)** alongside its MCP connections. An ACP client opens a conversation, sends prompts, receives streamed responses, reloads its history, and cancels work while Cinna runs the agent in its existing hosted environment.

This is Agent **Client** Protocol, not the unrelated Agent Communication Protocol. The integration implements an experimental WebSocket transport and a restricted hosted-agent profile. It does not promise compatibility with every feature of every ACP editor.

## Core concepts

- **ACP connector** — A named access point for one agent, with a fixed conversation/building mode, enabled state, and simultaneous connection limit.
- **Access token** — A secret issued for one connector and external client. It acts under the connector owner's identity and is shown only once. Tokens have independent expiration and permanent revocation.
- **ACP session** — A persistent Cinna conversation belonging to the connector and the token that created it. The same token can load it after reconnecting.
- **Hosted workspace** — The agent's `/app/workspace` directory. ACP does not mount the client's local project or run tools on the client's computer.
- **Stdio bridge** — A provided local subprocess that forwards newline-delimited ACP messages over authenticated WebSocket for clients that launch ACP agents through stdio.

## User stories / flows

### Connect an external application

1. The agent owner opens the agent's **Integrations** tab and chooses **Add connector** in **ACP Connectors**.
2. The owner supplies a name and selects a mode; maximum connections defaults to ten.
3. The connector's **Access tokens** action issues a labeled token, valid for ninety days by default. Copy the full token before leaving its one-time display.
4. **Connection details** provides the endpoint and required working directory. The client sends the token in its WebSocket `Authorization: Bearer` header.
5. The client initializes ACP, creates a session using `/app/workspace` and an empty MCP-server list, then submits text prompts.
6. Replies stream into the client. Conversations also remain in the agent owner's Cinna session history.

The management UI follows the existing integrations role restrictions: direct connector management appears in the full owner integrations view. Building mode additionally requires the owner to have developer privileges.

### Continue a conversation

1. Retain the `sessionId` returned by session creation.
2. After reconnecting with the same connector and token, initialize and load that session.
3. Cinna replays the stored user and agent text before completing the load operation.
4. Further prompts continue the same conversation. A different token cannot load it, even if issued for the same connector.

### Stop work or revoke access

- A client cancellation asks Cinna to interrupt the running environment and completes the active prompt as cancelled.
- Disconnecting closes that connection's active work. The conversation remains available for a later load with the same valid token.
- Disabling a connector prevents further use of all its tokens; enabling it restores valid credentials.
- Revoking a token is permanent. Issue another token for new access; it does not inherit the old token's conversations.
- Deleting a connector deletes its credentials and removes external access. Existing conversations remain in Cinna for the owner.

## Business rules

- Only the agent owner manages its connectors and tokens. Knowing an endpoint or session ID does not grant access.
- A connector supports conversation or building mode. Changing its mode makes sessions created with a different mode unavailable through ACP; start a session matching the new mode.
- Token lifetime is between one and 365 days. Token lists show identifying prefixes and lifecycle dates, never reusable secrets.
- An inactive owner or agent, a deleted/disabled connector, an expired/revoked token, or lost building privileges stops authorization.
- Session loading and prompting are serialized. A second overlapping operation receives a busy error instead of silently queuing a duplicate turn.
- Only a limited number of prompts and session replays run at once, both across the whole server and per connector; once a limit is reached, further attempts receive a capacity error until an existing one finishes. This budget is now tied to the server's database capacity rather than a fixed number, so it can be smaller on a modestly sized deployment — capacity errors can arrive sooner than before under concurrent load. See the [technical reference](acp_integration_tech.md#authentication-and-limits) for exact figures.
- Prompts support text only. Images, audio, embedded resources, client-provided MCP servers, additional directories, local file access, and client terminal access are not supported. Cinna's existing owner-configured agent tools continue to run inside its environment.
- A client that sends a local working directory must use the provided bridge, which maps it to `/app/workspace`, or explicitly support the hosted directory. The bridge does not upload local files or inject local MCP tools.
- MCP connectors and ACP connectors are separate; their credentials are not interchangeable.

## Architecture overview

External ACP client → authenticated WebSocket connector → scoped Cinna session → existing environment streaming pipeline → ACP session updates.

The stdio bridge adds a local forwarding process before the WebSocket connection. It leaves execution and storage in Cinna.

## Compatibility and limits

The shipped remote endpoint is **WebSocket only**. HTTP/SSE ACP transport, OAuth login through ACP, session listing/deletion, model/mode configuration, and client-side tool delegation are not advertised. Clients must support this profile directly or invoke the provided bridge; the bridge does not make unsupported ACP methods available.

Remote ACP transport is still experimental in the [official ACP transport documentation](https://agentclientprotocol.com/protocol/v1/transports). Cinna uses official Python SDK protocol models and dispatch while implementing the authenticated WebSocket boundary. See the [technical reference](acp_integration_tech.md) for exact deployment requirements, bounds, client files, and errors.

## Integration points

- [MCP integration](../mcp_integration/agent_mcp_architecture.md) exposes agents as tools; ACP exposes a conversation lifecycle to an external client.
- [Agent sessions](../agent_sessions/agent_sessions.md) supplies durable history and existing agent/environment lifecycle behavior.
- [Message ingress](../../flows/message_ingress.md) describes the shared streaming pipeline and ACP's distinct authenticated ingress.
- [Token taxonomy](../../flows/auth_token_taxonomy.md) distinguishes ACP bearer credentials from platform, A2A, and MCP tokens.

Tokens you issue under your connectors also appear in **/credentials › Issued Credentials** (as "ACP token" rows; a row reads *paused* while the connector is inactive or the connection would be rejected for agent, owner or building-mode reasons); see [Credential Sharing — Issued Credentials](../../agents/agent_credentials/credential_sharing.md#issued-credentials-issued).

## Changelog

- 2026-09-11: Added owner-managed ACP connectors, hashed access tokens, hosted WebSocket sessions, and an external-client stdio bridge.
- 2026-09-12: Tied the concurrent prompt/replay budget to server database capacity (was a fixed number) and added a per-connector share of it, so capacity errors can arrive sooner under load and one connector can no longer exhaust the whole server's budget; clarified the revoke/delete-token copy shown to owners.
- 2026-11-17: ACP connector tokens are listed in /credentials › Issued Credentials.
