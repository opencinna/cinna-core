"""
Pydantic schemas for the External Agent Access API — Discovery and Session layers.

These are response-only schemas (no DB tables). They describe the unified
target list returned by GET /api/v1/external/agents and session metadata
returned by GET /api/v1/external/sessions, used by native clients (Cinna
Desktop, Cinna Mobile, etc.) to render the home screen and restore thread lists.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class BundleVersionInfo(BaseModel):
    """Installed-vs-latest bundle version state for an installed agent.

    Surfaced on ``ExternalTargetPublic.bundle_version`` so native clients
    (Cinna Desktop, Cinna Mobile) can show "v1.0 → v1.2 update available"
    and offer an in-client update. Populated only for consumer installs
    (``target_type="agent"`` with a ``bundle_uuid`` and
    ``is_publisher_install=False``); ``None`` for the publisher's own
    working copy, shared routes, and identity contacts (none of which the
    caller updates).

    Computed read-only — building this never mutates ``Agent.pending_update``
    (the discovery endpoint is write-free). ``update_available`` is derived
    purely from the monotonic ``revision_number`` comparison; the human
    ``*_version`` labels are the publisher-supplied strings and may be absent
    on legacy revisions.
    """

    installed_revision_number: int | None = None
    installed_version: str | None = None
    latest_revision_number: int | None = None
    latest_version: str | None = None
    update_available: bool = False
    update_mode: str | None = None
    last_update_status: str | None = None


class ExternalTargetPublic(BaseModel):
    """A single addressable target returned by the external agent discovery endpoint.

    Covers three source types:
    - "agent"         — personal agent owned by (or cloned to) the user
    - "app_mcp_route" — agent shared with the user via an AppAgentRoute assignment
    - "identity"      — another user who has exposed agents via the Identity MCP server
    """

    target_type: Literal["agent", "app_mcp_route", "identity"]
    target_id: uuid.UUID
    name: str
    description: str | None = None
    entrypoint_prompt: str | None = None
    example_prompts: list[str] = []
    session_mode: Literal["conversation", "building"] | None = None
    ui_color_preset: str | None = None  # populated only for target_type="agent"
    agent_card_url: str
    protocol_versions: list[str] = ["1.0", "0.3.0"]
    metadata: dict[str, Any] = {}
    # cinna.mcp descriptor — mirror of the capabilities.extensions[] entry on the
    # A2A card (uri "urn:cinna:mcp"). Lets native clients (Cinna Desktop) wrap
    # this target as an emulated MCP tool from the refresh-friendly discovery
    # payload without re-fetching every card. The card remains the source of
    # truth; this is the cache. None when no descriptor could be built.
    mcp: dict[str, Any] | None = None
    # Installed-vs-latest bundle version state. Populated only for consumer
    # installs (target_type="agent" with a bundle, is_publisher_install=False);
    # None for publisher working copies, shared routes, and identity contacts.
    bundle_version: BundleVersionInfo | None = None


class ExternalAgentListResponse(BaseModel):
    """Response schema for GET /api/v1/external/agents.

    Targets are ordered: personal agents first, then MCP shared agents,
    then identity contacts — each section sorted by name ascending.
    """

    targets: list[ExternalTargetPublic] = []


class ExternalSessionPublic(BaseModel):
    """Session metadata returned by GET /api/v1/external/sessions endpoints.

    A slim, read-only view of a session that native clients use to restore
    their thread list at launch. The chat path uses A2A Task objects; this
    schema is only for the list/metadata REST layer so the client can render
    a thread picker before opening any conversation.

    Uses pydantic.BaseModel (not SQLModel) to avoid the metadata field shadow
    warning — same pattern as ExternalTargetPublic established in Phase 1.
    """

    id: uuid.UUID
    title: str | None = None
    integration_type: str | None = None
    status: str
    interaction_status: str
    result_state: str | None = None
    result_summary: str | None = None
    last_message_at: datetime | None = None
    created_at: datetime
    agent_id: uuid.UUID | None = None
    # Agent display name — joined from Agent table.
    # For identity_mcp sessions falls back to session_metadata["identity_owner_name"]
    # stamped by Phase 4's _stamp_session_context when Stage-2 routing runs.
    agent_name: str | None = None
    caller_id: uuid.UUID | None = None
    identity_caller_id: uuid.UUID | None = None
    # Optional client attribution read from session_metadata (Phase 6 stamps them).
    # Present only when the originating auth flow included the claims.
    client_kind: str | None = None
    external_client_id: str | None = None
    # Derived fields — let the native client re-fetch the right A2A card without
    # storing or parsing integration_type themselves.
    target_type: str | None = None   # "agent" | "app_mcp_route" | "identity"
    target_id: uuid.UUID | None = None