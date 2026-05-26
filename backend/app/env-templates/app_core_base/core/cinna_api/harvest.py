"""
One-shot, import-only OpenAPI spec harvest for the agent REST API.

Run as a short-lived subprocess by the supervisor::

    python -m core.cinna_api.harvest

It builds the FastAPI app (importing the agent's ``agent_api/`` modules), calls
``app.openapi()``, and prints a single JSON object to stdout:

    {"ok": true, "spec": {...}}                 # success
    {"ok": false, "error": "...", "traceback": "..."}   # boot/import failure

Running this in a separate, throwaway process preserves crash isolation: a bad
import or a module with side effects cannot corrupt the long-lived env-core
process, and the spec is obtained WITHOUT spawning the serving child.

stdout is a strict JSON channel: the supervisor parses exactly one JSON object
from it. Building the app imports arbitrary agent code and calls
``app.openapi()`` — either of which may ``print``, log, or emit warnings (e.g.
FastAPI's "Duplicate Operation ID" UserWarning). All of that is routed to
stderr while we build, so it can never corrupt the JSON on stdout (the cause of
the "spec harvest produced no JSON" failure).
"""
import contextlib
import json
import os
import sys
import traceback


def main() -> int:
    # Ensure cinna_api + workspace are importable when invoked as a module.
    sdk_parent = os.getenv("CINNA_API_SDK_PARENT", "/app/core")
    if sdk_parent not in sys.path:
        sys.path.insert(0, sdk_parent)

    base_url = os.getenv("CINNA_API_BASE_URL") or None

    # Keep a handle to the real stdout; emit only the final JSON on it.
    real_stdout = sys.stdout

    try:
        # Anything written to stdout during build/openapi (agent prints, library
        # logging, warnings) goes to stderr instead — never the JSON channel.
        with contextlib.redirect_stdout(sys.stderr):
            from cinna_api.discovery import build_app

            app = build_app(base_url=base_url)
            spec = app.openapi()
        payload = {"ok": True, "spec": spec}
        code = 0
    except Exception as exc:  # noqa: BLE001 — we want to capture everything
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-4000:],
        }
        code = 1

    json.dump(payload, real_stdout)
    real_stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
