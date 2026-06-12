# External A2A — API Reference

Auto-generated from OpenAPI spec. Tag: `external-a2a`

## GET `/api/v1/external/a2a/agent/{agent_id}/`
**Get External Agent Card**

**Path parameters:**
- `agent_id`: uuid

**Query parameters:**
- `protocol`: string | null

---

## POST `/api/v1/external/a2a/agent/{agent_id}/`
**Handle External Agent Jsonrpc**

**Path parameters:**
- `agent_id`: uuid

**Query parameters:**
- `protocol`: string | null

---

## GET `/api/v1/external/a2a/agent/{agent_id}/.well-known/agent-card.json`
**Get External Agent Card Well Known**

**Path parameters:**
- `agent_id`: uuid

**Query parameters:**
- `protocol`: string | null

---

## GET `/api/v1/external/a2a/route/{route_id}/`
**Get External Route Card**

**Path parameters:**
- `route_id`: uuid

**Query parameters:**
- `protocol`: string | null

---

## POST `/api/v1/external/a2a/route/{route_id}/`
**Handle External Route Jsonrpc**

**Path parameters:**
- `route_id`: uuid

**Query parameters:**
- `protocol`: string | null

---

## GET `/api/v1/external/a2a/route/{route_id}/.well-known/agent-card.json`
**Get External Route Card Well Known**

**Path parameters:**
- `route_id`: uuid

**Query parameters:**
- `protocol`: string | null

---

## GET `/api/v1/external/a2a/identity/{owner_id}/`
**Get External Identity Card**

**Path parameters:**
- `owner_id`: uuid

**Query parameters:**
- `protocol`: string | null

---

## POST `/api/v1/external/a2a/identity/{owner_id}/`
**Handle External Identity Jsonrpc**

**Path parameters:**
- `owner_id`: uuid

**Query parameters:**
- `protocol`: string | null

---

## GET `/api/v1/external/a2a/identity/{owner_id}/.well-known/agent-card.json`
**Get External Identity Card Well Known**

**Path parameters:**
- `owner_id`: uuid

**Query parameters:**
- `protocol`: string | null

---
