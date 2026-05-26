"""
Serving entrypoint for the agent REST API child process.

uvicorn imports ``app`` from this module::

    uvicorn core.cinna_api.serve:app --reload --reload-dir /app/workspace/agent_api

The child binds to ``127.0.0.1:<internal_port>`` (localhost-only) and is reached
only through env-core's ``/agent-api/proxy`` route. Because this runs in its own
process, a bad import / blocking handler / leak in agent code cannot take down
env-core (crash isolation, plan §3.4).

On reload (uvicorn picks up a change under ``agent_api/``), this module is
re-imported, rebuilding the app from the latest source.
"""
import logging
import os

from .discovery import build_app

logger = logging.getLogger(__name__)

_base_url = os.getenv("CINNA_API_BASE_URL") or None

# Built at import time. A boot error here surfaces as a child-startup failure,
# which the supervisor captures from stderr. The cached spec (harvested
# import-only) is the authoritative error surface for the owner UI.
app = build_app(base_url=_base_url)
