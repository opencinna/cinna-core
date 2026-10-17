# Agent Artifacts — API Reference

Auto-generated from OpenAPI spec. Tag: `agent-artifacts`

## POST `/api/v1/agent/artifacts/publish`
**Agent Publish Artifact**

**Request body** (`AgentArtifactPublishRequest`):
  - `path`: string (required)
  - `title`: string (required)
  - `description`: string | null
  - `entry`: string | null
  - `artifact_id`: string | null
  - `source_session_id`: string | null

**Response:** `AgentArtifactPublished`

---
