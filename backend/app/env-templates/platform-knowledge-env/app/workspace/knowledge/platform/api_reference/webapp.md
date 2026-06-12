# Webapp — API Reference

Auto-generated from OpenAPI spec. Tag: `webapp`

## GET `/api/v1/agents/{agent_id}/webapp/status`
**Get Webapp Status**

**Path parameters:**
- `agent_id`: uuid

---

## POST `/api/v1/agents/{agent_id}/webapp/api/{endpoint}`
**Webapp Data Api**

**Path parameters:**
- `agent_id`: uuid
- `endpoint`: string


---

## GET `/api/v1/agents/{agent_id}/webapp/owner-status`
**Get Webapp Owner Status**

**Path parameters:**
- `agent_id`: uuid

**Query parameters:**
- `token`: string | null

---

## GET `/api/v1/agents/{agent_id}/webapp/{path}`
**Serve Webapp File**

**Path parameters:**
- `agent_id`: uuid
- `path`: string

**Query parameters:**
- `token`: string | null

---
