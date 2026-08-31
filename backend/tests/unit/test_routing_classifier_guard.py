"""The no-live-model guard for the routing classifier.

`AgentClassifier.classify` backs every routing consumer (Channel Pass 1 and
Pass 2, App MCP Stage 1, Identity Stage 2) and calls a real provider cascade
unless a test stubs it — and the test container has a live cascade configured,
so "unstubbed" means "dials out". Sixteen call sites in `tests/api/routing/`
used a helper whose *default* left it unstubbed. None of them turned out to
reach the classifier in practice (they short-circuit before it), which is the
uncomfortable part: the hazard was one setup change away and invisible until
something else paid for it — a provider quota exhausted during a Phase-5
measurement run, surfacing as a suite going red for a reason unrelated to the
code. A gate that can fail for reasons unrelated to the code is a gate people
learn to re-run instead of investigate.

Two mechanisms now stop that, and this file pins both, because both have a
failure mode where they keep existing while quietly doing nothing:

1. `block_llm_provider` (autouse, `tests/conftest.py`) patches the classifier's
   provider seam globally, so a test that never touches the routing helpers —
   in any domain, written a year from now — still cannot dial out.
2. `enter_classifier_patch` — the seam behind `post_channel_message`,
   `patched_routing_externals` and `server_channels_routing_test.py`'s `_post`
   — refuses to classify unless the caller names an answer, which puts the fix
   in the error message. It is one function because it used to be three copies
   of the decision, all three with the same bug.

Neither mechanism claims more than it does: both patch the *classifier's*
provider seam, not every AI function. `AIFunctionsService
.generate_router_trigger_prompt` and the other generators bind
`get_provider_manager` at import time and are not covered — see the scope note
in `tests/utils/fixtures.py`.

`test_a_plain_exception_at_the_same_seam_is_swallowed` is the load-bearing
one. Both mechanisms raise a `BaseException`, and that is not a stylistic
choice: every caller on the
routing path swallows `Exception` deliberately so a router outage cannot 500 a
webhook. A guard raising `Exception` would be caught by the code under test and
reported as an ordinary no-match — present, passing its own unit test, and
invisible in exactly the situation it exists for.
"""
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from app.services.routing.agent_classifier import AgentClassifier, Candidate
from tests.utils.fixtures import CLASSIFIER_PROVIDER_TARGET, UnstubbedLLMProvider
from tests.utils.routing import enter_classifier_patch, patched_routing_externals

CANDIDATES = [
    Candidate(
        ref_id="11111111-1111-1111-1111-111111111111",
        name="Scheduler",
        trigger_prompt="Handle calendar requests",
    ),
    Candidate(
        ref_id="22222222-2222-2222-2222-222222222222",
        name="Expenses",
        trigger_prompt="Handle expense questions",
    ),
]


def test_global_guard_makes_the_provider_seam_unreachable():
    """No stub anywhere — asking for the provider raises instead of returning one."""
    from app.services.routing import agent_classifier

    with pytest.raises(UnstubbedLLMProvider) as excinfo:
        agent_classifier.get_provider_manager()
    assert "REAL LLM provider" in str(excinfo.value)


def test_an_unstubbed_classify_raises_instead_of_calling_a_model():
    """`classify` has its own catch-all; the guard has to get past it.

    This is the whole point of raising a `BaseException`. `classify` wraps
    render/call/parse in `except Exception -> record_error -> return None`, so
    an `Exception`-based guard would be converted into a `None` — the same
    answer a well-behaved "no candidate fits" produces — and the test that
    forgot to stub would carry on and pass.
    """
    with pytest.raises(UnstubbedLLMProvider):
        AgentClassifier.classify(CANDIDATES, "please book me a meeting")


def test_a_plain_exception_at_the_same_seam_is_swallowed():
    """Why the guard cannot be an ordinary exception — pinned against the real code.

    Not a hypothetical: this is `classify`'s documented contract (a provider
    cascade failure is a negative outcome, not a crash). If this test ever goes
    red because the catch-all was narrowed, the `BaseException` above can be
    revisited — until then it is what keeps the guard visible.
    """
    with patch(CLASSIFIER_PROVIDER_TARGET, side_effect=RuntimeError("provider down")):
        assert AgentClassifier.classify(CANDIDATES, "please book me a meeting") is None


def test_bare_helper_refuses_and_names_the_fix():
    """`patched_routing_externals()` with no answer named is the loud case.

    The `only_one` assertion is what makes this test able to fail. Both guards
    raise the same type and both messages name the two keyword arguments, so
    asserting on those alone would still pass with the helper's own refusal
    deleted — the global seam guard would catch the call and the test would
    report success for a mechanism that no longer exists. The short-circuit
    sentence appears only in the helper's message, so this pins the helper.
    """
    with patched_routing_externals():
        with pytest.raises(UnstubbedLLMProvider) as excinfo:
            AgentClassifier.classify(CANDIDATES, "please book me a meeting")
    message = str(excinfo.value)
    assert "classify_result" in message
    assert "classify_no_match" in message
    assert "only_one" in message, message


def test_both_helpers_refuse_through_one_decision_point():
    """`enter_classifier_patch` is the seam both helpers delegate to.

    Pinned directly rather than through `post_channel_message`, which needs a
    channel, a signer and a live webhook delivery to reach the same three
    lines. The decision used to be copied into both helpers and the "no answer
    named means call a real model" bug was in both copies — so the property
    worth pinning is that there is now one copy and it refuses.
    """
    with ExitStack() as stack:
        enter_classifier_patch(stack)
        with pytest.raises(UnstubbedLLMProvider) as excinfo:
            AgentClassifier.classify(CANDIDATES, "please book me a meeting")
    # Same reason as the test above: assert on the seam's OWN message, or the
    # global guard would answer for a deleted mechanism and this would pass.
    assert "only_one" in str(excinfo.value), str(excinfo.value)


def test_a_caller_that_stubs_the_provider_itself_is_left_alone():
    """`classify_via_provider=True` removes the helper stub, not the global guard.

    The message-text-gating tests patch the provider seam one layer deeper so
    the real render/parse instrumentation fires; refusing at `classify` would
    pre-empt the code they exist to exercise. What they must NOT get is a live
    model if their own patch ever goes missing — which is what this asserts:
    with the helper stub declined and no provider stub of their own, the
    `tests/conftest.py` guard still fires.
    """
    with ExitStack() as stack:
        enter_classifier_patch(stack, classify_via_provider=True)
        with pytest.raises(UnstubbedLLMProvider):
            AgentClassifier.classify(CANDIDATES, "please book me a meeting")


def test_a_named_answer_is_still_honoured():
    """The guard must not swallow the scenarios the helper exists to express."""
    with patched_routing_externals(classify_no_match=True):
        assert AgentClassifier.classify(CANDIDATES, "hello") is None

    sentinel = object()
    with patched_routing_externals(classify_result=sentinel):
        assert AgentClassifier.classify(CANDIDATES, "hello") is sentinel
