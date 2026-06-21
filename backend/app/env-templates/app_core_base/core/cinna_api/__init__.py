"""
cinna_api — the agent-facing SDK for building a capability-narrowed REST API
inside an agent's container.

A producer agent writes plain typed functions under ``/app/workspace/agent_api/``
and decorates them with ``@api.get/post/...``. env-core discovers those modules,
mounts the router onto a fresh ``FastAPI()`` app, and supervises a uvicorn child
that serves it (lazily, on first call). The platform proxy fronts the child and
enforces ``policy.yaml`` guardrails (Phase 2).

Typical usage in ``agent_api/orders.py``::

    from cinna_api import api, credentials, Query, error

    @api.get("/orders")
    def list_orders(limit: int = Query(20, le=100)):
        cred = credentials.by_type("odoo")
        # ... call the upstream with cred ...
        return {"orders": [...]}

Design rules baked into this SDK:

- ``credentials`` reads ``credentials.json`` **fresh on every access** — the
  serving child is long-running, so caching the parsed file at import would
  serve stale secrets across an OAuth refresh / credential resync.
- ``caller`` is a request-scoped dependency exposing the platform-resolved
  identity of the calling user (``X-Cinna-Caller-*`` headers injected by the
  proxy). Annotate a handler param with it to do per-user authorization::

      from cinna_api import api, caller, Caller

      @api.get("/orders")
      def list_orders(me: Caller = caller):
          if me.is_anonymous: ...
- ``api`` is a pre-created ``APIRouter``; the ``@api.*`` decorators are plain
  pass-throughs to FastAPI, so all of FastAPI's validation + schema generation
  applies unchanged and the harvested OpenAPI spec is always accurate.
- Ergonomic re-exports (``UploadFile``, ``File``, ``Query``, ``Body``,
  ``StreamingResponse``, ``BaseModel``, ``Field``) let the author import from
  one place.
"""
from fastapi import APIRouter, Body, File, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .caller import Caller, caller
from .credentials import credentials
from .errors import error

# The single shared router the agent decorates. Discovery mounts this onto a
# fresh FastAPI() instance.
api = APIRouter()

__all__ = [
    "api",
    "credentials",
    "caller",
    "Caller",
    "error",
    # FastAPI ergonomic re-exports
    "UploadFile",
    "File",
    "Query",
    "Body",
    "StreamingResponse",
    # Pydantic ergonomic re-exports
    "BaseModel",
    "Field",
]
