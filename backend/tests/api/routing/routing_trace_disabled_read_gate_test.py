"""`ROUTING_TRACE_ENABLED` as a READ gate (S6), not only the original write
gate (`persist()` no-ops when off).

Before this, the flag was consulted in exactly one place — `persist()` — so
turning tracing off left the admin API still serving up to
`ROUTING_TRACE_RETENTION_DAYS` of already-stored rows, message text included,
with no indication anything was off. That is the same asymmetry §7 already
rejected for `ROUTING_TRACE_STORE_MESSAGE_TEXT`, and §11a Rule 1 again: the
dangerous state (still showing stored rows) must not be indistinguishable
from the safe one (tracing genuinely off).

Now:
  - `list` returns an EMPTY page plus a `notice`, not a bare empty page — so
    "tracing is off" cannot be read as "nothing has ever routed here".
  - `get` 404s — the same status a missing id gets — but carries the notice
    as the detail, so the two 404 reasons are distinguishable.
  - `clear()` and the purge path are DELIBERATELY NOT gated: the erasure
    paths the two notices point an operator at (turning tracing off is not
    supposed to also remove the only way to delete what is already there).

Seeding uses `tests/utils/routing.py::seed_routing_trace` — the row must
exist independently of the flag under test, which the real channel path
cannot give a direct handle on without also depending on the webhook pipeline
being enabled.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.utils.routing import (
    clear_routing_traces,
    get_routing_trace,
    list_routing_traces,
    purge_routing_traces,
    seed_routing_trace,
)

_ENABLED = "app.core.config.settings.ROUTING_TRACE_ENABLED"


def test_disabled_flag_gates_list_and_get_but_not_clear_or_purge(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    1. Seed a row while tracing is (implicitly) enabled.
    2. With the flag OFF: `list` -> empty page + non-empty `notice`; `get` on
       the real id -> 404, with a detail distinguishable from the ordinary
       "not found" a bogus id gets.
    3. Still with the flag OFF: `clear` (via the admin route) succeeds and
       actually deletes the row — proven by re-enabling the flag afterwards
       and confirming the row is genuinely gone, not just hidden.
    4. A second row, same story for the direct `purge` path (the Rule-1
       exemption `purge_routing_traces` wraps): purging while disabled still
       deletes rows older than the cutoff.
    """
    row_id = seed_routing_trace(created_at=datetime.now(UTC))
    assert row_id is not None

    # ── Phase 1: enabled — the notice is absent, the row is visible ────────
    page = list_routing_traces(client, superuser_token_headers)
    assert page["notice"] is None
    assert any(r["id"] == str(row_id) for r in page["data"])
    get_routing_trace(client, superuser_token_headers, str(row_id), expected_status=200)

    # A genuinely-missing id, for comparison against the disabled-flag 404 below.
    ordinary_404 = get_routing_trace(
        client, superuser_token_headers, "00000000-0000-0000-0000-000000000000", expected_status=404
    )

    # ── Phase 2: disabled — list is an empty page WITH a notice ────────────
    with patch(_ENABLED, False):
        page = list_routing_traces(client, superuser_token_headers)
        assert page["data"] == []
        assert page["count"] == 0
        assert page["notice"], "list() must explain an empty page while tracing is off"

        # ── get() 404s too, but the two 404 reasons must be distinguishable ──
        disabled_404 = get_routing_trace(
            client, superuser_token_headers, str(row_id), expected_status=404
        )
        assert disabled_404["detail"], disabled_404
        assert disabled_404["detail"] != ordinary_404["detail"], (
            "the 404 for 'tracing is off' must not read identically to the "
            "404 for 'no such id' — the reader needs to be able to tell them "
            "apart (see RoutingTraceService.disabled_notice)"
        )

        # ── Phase 3: clear() still works while disabled ─────────────────────
        clear_routing_traces(client, superuser_token_headers, all_channels=True)

    # Re-enable and confirm the row is genuinely gone, not merely hidden by
    # the read gate while it was off.
    page = list_routing_traces(client, superuser_token_headers)
    assert not any(r["id"] == str(row_id) for r in page["data"])

    # ── Phase 4: the direct purge path also ignores the flag ───────────────
    old_row_id = seed_routing_trace(created_at=datetime(2000, 1, 1, tzinfo=UTC))
    assert old_row_id is not None
    with patch(_ENABLED, False):
        deleted = purge_routing_traces(db, retention_days=1)
    assert deleted == 1

    page = list_routing_traces(client, superuser_token_headers)
    assert not any(r["id"] == str(old_row_id) for r in page["data"])
