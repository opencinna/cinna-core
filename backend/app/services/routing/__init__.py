"""Routing observability.

``routing_trace`` is the in-process recorder for one routing decision.

**This package must not re-export anything, and ``routing_trace`` must stay
free of every ``app.*`` import.** Both rules are load-bearing, not tidiness:
``app/agents/provider_manager.py`` and ``app/agents/app_agent_router.py`` are
instrumentation points, and ``app/agents/`` sits *below* ``app/services/`` —
several services (``ai_functions``, ``identity``, ``agents``) import from it.
That inversion is harmless only while the imported module pulls in nothing but
the standard library.

A later phase adds ``routing_trace_service.py`` here, with models, a database
session and settings behind it. Re-exporting it from this ``__init__`` — or
importing it from ``routing_trace`` — would drag all of that into
``app.agents`` at import time and close the cycle. Import the submodule
directly (``from app.services.routing import routing_trace``), never through a
package-level name.
"""
