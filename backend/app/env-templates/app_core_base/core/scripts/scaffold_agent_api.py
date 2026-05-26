#!/usr/bin/env python3
"""
Scaffold a working agent REST API starter under /app/workspace/agent_api/.

Writes two files (skipping any that already exist, so it never clobbers an
agent's work):

  - agent_api/orders.py   — a read-only @api.get("/orders") that proxies one
                            upstream GET, using the cinna_api SDK + credentials.
  - agent_api/policy.yaml — read_only: true guardrails.

Surfaced to the building agent as a CLI command (``/run:scaffold-agent-api``)
when the agent declares it in docs/CLI_COMMANDS.yaml, or runnable directly:

    python /app/core/scripts/scaffold_agent_api.py

Use --force to overwrite existing files.
"""
import argparse
import sys
from pathlib import Path

AGENT_API_DIR = Path("/app/workspace/agent_api")

ORDERS_STUB = '''\
"""
Starter agent REST API module.

Edit this file to expose a capability-narrowed REST API in front of a powerful
upstream credential. Each decorated function becomes a real, typed, validated
HTTP endpoint; the platform harvests the OpenAPI spec from the live app, so the
spec is always accurate.

Keep it read-only (GET/HEAD) unless you have a clear reason not to — the
platform proxy enforces the method envelope declared in policy.yaml.
"""
import os

import httpx

from cinna_api import api, Query, error, credentials


# OpenAPI metadata for your API (optional). Set these in any agent_api module to
# label the spec consumers and you see. Without them, a generic default is used.
API_TITLE = "Orders API"
API_DESCRIPTION = "A narrow, read-only API in front of the upstream orders system."
API_VERSION = "1.0.0"


# The upstream base URL. Prefer reading it from a credential rather than
# hardcoding. This default points at a public demo API you can call without
# auth so the scaffold works out of the box.
UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "https://jsonplaceholder.typicode.com")


@api.get("/orders")
def list_orders(limit: int = Query(10, ge=1, le=100)):
    """
    List orders from the upstream system.

    ``limit`` is validated by FastAPI (1..100) and appears in the OpenAPI spec,
    so callers know the allowed shape. This is the "fine-grained parameter
    control" pattern: clamp inputs in code, and the spec reflects it.
    """
    # Example of reading a powerful upstream credential without exposing it.
    # cred = credentials.by_type("odoo")  # -> {"credential_data": {...}} or None
    # if cred is None:
    #     raise error(503, "Upstream credential not configured")

    try:
        resp = httpx.get(f"{UPSTREAM_BASE_URL}/todos", params={"_limit": limit}, timeout=15.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        # Never leak upstream credentials in error messages.
        raise error(502, f"Upstream request failed: {type(exc).__name__}")

    items = resp.json()
    return {"orders": items, "count": len(items)}


@api.get("/orders/{order_id}")
def get_order(order_id: int):
    """Fetch a single order by id."""
    try:
        resp = httpx.get(f"{UPSTREAM_BASE_URL}/todos/{order_id}", timeout=15.0)
    except httpx.HTTPError as exc:
        raise error(502, f"Upstream request failed: {type(exc).__name__}")
    if resp.status_code == 404:
        raise error(404, "Order not found")
    resp.raise_for_status()
    return resp.json()
'''

POLICY_STUB = '''\
# Platform-enforced guardrails for this agent's REST API.
# These are applied at the platform proxy edge, BEFORE a request reaches your
# code, so they hold regardless of what your handlers do.
#
# read_only enforces the *method envelope* (rejects non-GET/HEAD). It does NOT
# guarantee semantic read-only — a GET handler can still mutate upstream. Keep
# your handlers genuinely read-only when read_only is true.

read_only: true            # reject non-GET/HEAD requests at the proxy edge
auth: required             # a valid agent_api token is mandatory (Phase 2)
max_body_bytes: 10485760   # 10 MB request-body cap
rate_limit: "60/min"       # per-token rate limit
expose_spec: true          # allow consumers to fetch /openapi.json
allowed_paths: ["*"]       # optional path-prefix allowlist for extra narrowing
'''


def _write(path: Path, content: str, force: bool) -> str:
    if path.exists() and not force:
        return f"skipped (exists): {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote: {path}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold an agent REST API starter")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    args = parser.parse_args()

    results = [
        _write(AGENT_API_DIR / "orders.py", ORDERS_STUB, args.force),
        _write(AGENT_API_DIR / "policy.yaml", POLICY_STUB, args.force),
    ]
    for line in results:
        print(line)
    print(
        "\nDone. Enable the Agent REST API for this agent, then preview the spec "
        "at GET /agents/{id}/agent-api/openapi.json. Edit orders.py to front your "
        "real upstream credential, and keep it read-only."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
