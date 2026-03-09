# Future Improvements

## User Scenarios and Features

### Costs tracking per user

Every time when via stream we receive any costs info in the message meta info,
we should collect it that amount and make a record in the costs tracking to see when and how much user spent.
That should be disconnected data (meaning if session / message is deleted, we still have records of costs).

### Improved sandboxing for Agent-Env

Original articles shows main points used by the original Claude so far: https://support.claude.com/en/articles/12111783-create-and-edit-files-with-claude

We need better than that, with heuristic control over and post-validation of the prompt-conversation direction.
Here is a good overview on how to do that: https://www.lakera.ai/blog/guide-to-prompt-injection

### Automatic plugins discovery

Also add logic of manual approval / recommendations to show them in the UI correctly.
How it could work: https://claudemarketplaces.com/about

### Improved handover processing

When background-executed session (via CRON) is over and according to the session logs
no agent's handover happened, but in the configuration of the agent such handover config is present,
send one more user message to the agent to check that handover conditions were checked and handover
is really not necessary.

### User-expertise levels/roles

Certain users could not have enough expertise to build agents, but it's fine to provide them
conversational access to certain agents that were prepared for them by other users.

Features required:
- sharing agents from one user to another (one user builds, another user uses)
- copying of agents (one user builds, another user receives its own copy)

### User Artifacts

User wants to keep and reuse certain artifacts (files or even whole folders).

User story 1:
- user asks to generate reports about employees from Odoo ERP on 1 of every month
- user expects these reports to be saved in his storage (`artifacts`) for later access and archiving purpose (even if agent will be deleted later)

Features required:
- artifact's tool for the agent (CRUD) to manage artifact records on the backend, when local files of the agents could be saved in the backend DB.

### Developers Assistance Tools

User wants agent to do actions with the GitHub API.

User story:
- user gives command `check my recent PR and leave a comment about changes done in that PR` 
- agents checks-out repository given by the user and pushes back comment via API

Features required:
- ssh key setup inside the agent-env
- knowledge on how to communication with git-hub API and credentials for it
- maybe oauth credentials integrations as a GitHub app

### Agent Communication to a User

In certain scenarios agent should be able to notify its owner of a required action in the offline mode too.

User story:
- agent faced a problem with auth access to an API and needs a new token
- agent sends a message to a user that he encountered an error

Missing features:
- maybe tool for the Agent to access
- maybe local knowledge (skill?) that LLM could call as an API and notify the user

### External Conversational Knowledge Database

In certain scenarios user would want to receive answers from the agent, for example instructions
on how to do something.

User story 1:
- user asks agent on how to configure a deal in Odoo correctly

Required features:
- connection of knowledge database that is providing details from external sources (Confluence ROVO API?)


### Multi-Worker MCP Session Support — IMPLEMENTED

**Status:** Implemented via `SharedSessionManager` (DB-backed session registry).

**Solution:** `app/mcp/shared_session_manager.py` subclasses the SDK's `StreamableHTTPSessionManager` with a PostgreSQL-backed `mcp_transport_session` table. When a request arrives at a worker that doesn't have the session locally, it checks the DB and "warms" the session by creating a new local transport with `stateless=True` (bypasses re-initialization). The stale-session check in `MCPServerRegistry.__call__` was removed in favor of the session manager's DB fallback.

**Remaining limitation:** `MCPServerRegistry._active_sessions` (used for resource change notification broadcasts) is still per-worker. Notifications sent via `broadcast_resource_list_changed()` only reach sessions on the current worker. For cross-worker notifications, a pub/sub mechanism (e.g. Redis Pub/Sub) would be needed. This is acceptable because notifications are best-effort and the primary use case (notify after `send_message`) happens on the same worker that handled the request.

## RANDOM UNSTRUCTURED NOTES

- instructions for building an agent that all 'destructive' or potentially dangerous actions - make a user request
- downloading files from ODOO - instructions (document analysis in general) - maybe as a separate sub-services available to the agent??
- RESTRICT users from using modifications to the agents (let only certain users to rely on building mode, in most scenarios special team members are bulding, and other teams members are using) - role on the user (user / agent builder)
- agent handover during certain periods (when user leaves on vacation, another user can take care and use his agents for a while)