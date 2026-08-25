"""
Unit tests for the shared token-overlap helpers in
``app.services.routing.text_similarity``.

Pure arithmetic on ``tokens_for_similarity`` and ``jaccard_similarity``
(Jaccard threshold = 0.45). The surface that consumes these — near-miss ranking
on the routing reachability card — is covered in
``tests/unit/test_routing_reachability.py`` (which additionally asserts that
*these* helpers are the ones being called, not a copy) and in
``tests/api/routing/routing_reachability_verdict_test.py``.
"""
from app.services.routing.text_similarity import (
    jaccard_similarity,
    tokens_for_similarity,
)


def test_jaccard_above_threshold_for_overlapping_prompts() -> None:
    tokens_a = tokens_for_similarity(
        "schedule meetings reminders calendar events"
    )
    tokens_b = tokens_for_similarity(
        "schedule calendar meetings reminders weekly"
    )
    sim_above = jaccard_similarity(tokens_a, tokens_b)
    assert sim_above >= 0.45, (
        f"Expected similarity >= 0.45 for overlapping prompts, got {sim_above:.3f}"
    )


def test_jaccard_below_threshold_for_unrelated_prompts() -> None:
    tokens_x = tokens_for_similarity(
        "draft legal contracts review compliance"
    )
    tokens_y = tokens_for_similarity(
        "suggest baking recipes cooking techniques"
    )
    sim_below = jaccard_similarity(tokens_x, tokens_y)
    assert sim_below < 0.45, (
        f"Expected similarity < 0.45 for unrelated prompts, got {sim_below:.3f}"
    )


def test_jaccard_exact_boundary() -> None:
    # "aaa bbb ccc" vs "aaa bbb ddd" → intersection={aaa,bbb}, union={aaa,bbb,ccc,ddd}
    # Jaccard = 2/4 = 0.5 → should match
    tokens_c = tokens_for_similarity("aaa bbb ccc")
    tokens_d = tokens_for_similarity("aaa bbb ddd")
    sim_exact = jaccard_similarity(tokens_c, tokens_d)
    assert sim_exact >= 0.45, f"Expected >= 0.45, got {sim_exact:.3f}"


def test_jaccard_zero_overlap() -> None:
    # "aaa bbb" vs "ccc ddd eee fff ggg" → intersection={}, Jaccard = 0 → no match
    tokens_e = tokens_for_similarity("aaa bbb")
    tokens_f = tokens_for_similarity("ccc ddd eee fff ggg")
    sim_zero = jaccard_similarity(tokens_e, tokens_f)
    assert sim_zero < 0.45, f"Expected < 0.45, got {sim_zero:.3f}"
