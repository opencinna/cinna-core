"""
Unit tests for ``AppAgentRouteService`` similarity helpers used by auto-managed
route conflict detection.

Pure arithmetic on the static methods ``_tokens_for_similarity`` and
``_jaccard_similarity`` (Jaccard threshold = 0.45). The endpoint behavior that
consumes these (conflict match returned vs empty) is covered in
``tests/api/app_mcp/app_mcp_auto_managed_route_test.py``.
"""
from app.services.app_mcp.app_agent_route_service import AppAgentRouteService


def test_jaccard_above_threshold_for_overlapping_prompts() -> None:
    tokens_a = AppAgentRouteService._tokens_for_similarity(
        "schedule meetings reminders calendar events"
    )
    tokens_b = AppAgentRouteService._tokens_for_similarity(
        "schedule calendar meetings reminders weekly"
    )
    sim_above = AppAgentRouteService._jaccard_similarity(tokens_a, tokens_b)
    assert sim_above >= 0.45, (
        f"Expected similarity >= 0.45 for overlapping prompts, got {sim_above:.3f}"
    )


def test_jaccard_below_threshold_for_unrelated_prompts() -> None:
    tokens_x = AppAgentRouteService._tokens_for_similarity(
        "draft legal contracts review compliance"
    )
    tokens_y = AppAgentRouteService._tokens_for_similarity(
        "suggest baking recipes cooking techniques"
    )
    sim_below = AppAgentRouteService._jaccard_similarity(tokens_x, tokens_y)
    assert sim_below < 0.45, (
        f"Expected similarity < 0.45 for unrelated prompts, got {sim_below:.3f}"
    )


def test_jaccard_exact_boundary() -> None:
    # "aaa bbb ccc" vs "aaa bbb ddd" → intersection={aaa,bbb}, union={aaa,bbb,ccc,ddd}
    # Jaccard = 2/4 = 0.5 → should match
    tokens_c = AppAgentRouteService._tokens_for_similarity("aaa bbb ccc")
    tokens_d = AppAgentRouteService._tokens_for_similarity("aaa bbb ddd")
    sim_exact = AppAgentRouteService._jaccard_similarity(tokens_c, tokens_d)
    assert sim_exact >= 0.45, f"Expected >= 0.45, got {sim_exact:.3f}"


def test_jaccard_zero_overlap() -> None:
    # "aaa bbb" vs "ccc ddd eee fff ggg" → intersection={}, Jaccard = 0 → no match
    tokens_e = AppAgentRouteService._tokens_for_similarity("aaa bbb")
    tokens_f = AppAgentRouteService._tokens_for_similarity("ccc ddd eee fff ggg")
    sim_zero = AppAgentRouteService._jaccard_similarity(tokens_e, tokens_f)
    assert sim_zero < 0.45, f"Expected < 0.45, got {sim_zero:.3f}"
