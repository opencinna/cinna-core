# Agent Api Public — API Reference

Auto-generated from OpenAPI spec. Tag: `agent-api-public`

## GET `/api/v1/agent-api/{agent_id}/openapi.json`
**Consumer Spec**

**Path parameters:**
- `agent_id`: uuid

---

## POST `/api/v1/agent-api/{agent_id}/_cinna/improvement-requests`
**Consumer Create Capability Request**

**Path parameters:**
- `agent_id`: uuid

**Request body:**
  - `title`: string (required)
  - `need`: string (required)
  - `proposed_endpoint`: object | null
  - `example_request`: string | null
  - `example_response`: string | null
  - `spec_version_seen`: string | null

**Response:** `AgentApiCapabilityRequestPublic`

---

## GET `/api/v1/agent-api/{agent_id}/_cinna/improvement-requests`
**Consumer List Capability Requests**

**Path parameters:**
- `agent_id`: uuid

**Response:** `AgentApiCapabilityRequestsPublic`

---
