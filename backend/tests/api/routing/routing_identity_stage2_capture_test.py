"""Identity Stage 2 is instrumented — the Phase 1 deferral, closed.

Phase 1 instrumented the two channel passes and `app_agent_router`, so
`identity_stage2` persisted with a **prompt, a raw response, and zero
candidates**. That was a documented deferral rather than a recorder bug —
`IdentityRoutingService` built its binding list inline and shared no candidate
builder — but nothing in the stored row said so. Anyone opening that trace on
the tuning card reads "this stage considered nobody" as a fact about the
system, which is the exact failure mode plan §11a Rule 1 names: *when a
diagnostic's output can omit a step it actually performed, the omission reads
as a fact about the system rather than a gap in the instrument.*

Two properties are pinned here, both of which the previous build got wrong:

1. **The Stage-2 ballot is recorded**, with each binding's trigger prompt and
   `prompt_examples` — the same objects the classifier is handed, because both
   now come from `IdentityRoutingService._binding_candidates`.
2. **`match_method` tells the truth on a pattern hit.** `_try_pattern_match`
   emitted no `record_match` at all, so a pattern match persisted as
   `match_method=None` — a *lying* field, not a missing one: the stage said
   "nothing matched this way" about a match that had just happened. Its App MCP
   twin has always recorded it.

Driven through `POST /admin/routing/simulate`, which runs the real router over
the target's real state (see `routing_reachability_verdict_test.py`'s module
docstring for why the branches here use simulate rather than a webhook).
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.identity import create_identity_binding, toggle_identity_contact
from tests.utils.routing import (
    get_routing_trace,
    patched_routing_externals,
    simulate_routing,
)
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR


def _user(client: TestClient, superuser_headers: dict[str, str]) -> tuple[dict, dict]:
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _agent(client: TestClient, headers: dict[str, str], label: str) -> dict:
    agent = create_agent_via_api(
        client, headers, name=f"{label}-{random_lower_string()[:6]}"
    )
    drain_tasks()
    return agent


# `classify_no_match=True` on every scenario below, deliberately: the ballot is
# recorded *before* the classifier is consulted, so stubbing it keeps these
# tests off the network and off a live model's judgement while still exercising
# the code path that builds and records the candidate list. On the pattern
# scenario it doubles as a guard — if the pattern branch ever stopped winning,
# the stub would return no match and `match_method` would not read "pattern".


def _stage(trace: dict, name: str) -> dict | None:
    return next((s for s in trace["stages"] if s["stage"] == name), None)


def test_identity_stage2_records_its_candidate_ballot(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Two bindings, both accessible — both must appear as Stage-2 candidates.

    Two rather than one deliberately: a single binding short-circuits Stage 2 on
    the `only_one` path, which would prove the capture happens on the *easy*
    branch only. Two forces the classifier branch, which is the one whose ballot
    was invisible.
    """
    owner, owner_headers = _user(client, superuser_token_headers)
    sender, sender_headers = _user(client, superuser_token_headers)

    first = _agent(client, owner_headers, "IdentityFirst")
    second = _agent(client, owner_headers, "IdentitySecond")
    create_identity_binding(
        client,
        owner_headers,
        first["id"],
        trigger_prompt="Handle calendar questions",
        prompt_examples="when am I free\nbook me a slot",
        assigned_user_ids=[sender["id"]],
    )
    create_identity_binding(
        client,
        owner_headers,
        second["id"],
        trigger_prompt="Handle expense questions",
        assigned_user_ids=[sender["id"]],
    )
    toggle_identity_contact(client, sender_headers, owner["id"], True)

    with patched_routing_externals(classify_no_match=True):
        trace = simulate_routing(
            client,
            superuser_token_headers,
            message="when am I free on friday",
            as_user_id=sender["id"],
        )

    detail = get_routing_trace(client, superuser_token_headers, trace["id"])
    stage2 = _stage(detail, "identity_stage2")
    assert stage2 is not None, detail["stages"]

    names = {c["name"] for c in stage2["candidates"]}
    assert first["name"] in names, stage2["candidates"]
    assert second["name"] in names, stage2["candidates"]

    calendar = next(c for c in stage2["candidates"] if c["name"] == first["name"])
    assert calendar["trigger_prompt"] == "Handle calendar questions"
    # The owner's examples are on the ballot because the classifier now sees
    # them. Before Phase 5 the identity path never read the field at all.
    assert calendar["prompt_examples"] == "when am I free\nbook me a slot"
    assert calendar["source"] == "identity"


def test_identity_stage2_pattern_hit_reports_its_match_method(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A pattern match must not persist as `match_method=None`.

    The pattern is deliberately narrow and the message is chosen to hit it, so
    the classifier branch is never reached — which is what makes `match_method`
    the only thing distinguishing this decision from an AI one.
    """
    owner, owner_headers = _user(client, superuser_token_headers)
    sender, sender_headers = _user(client, superuser_token_headers)

    patterned = _agent(client, owner_headers, "IdentityPatterned")
    other = _agent(client, owner_headers, "IdentityOther")
    create_identity_binding(
        client,
        owner_headers,
        patterned["id"],
        trigger_prompt="Handle signature requests",
        message_patterns="sign this document *",
        assigned_user_ids=[sender["id"]],
    )
    create_identity_binding(
        client,
        owner_headers,
        other["id"],
        trigger_prompt="Handle everything else",
        assigned_user_ids=[sender["id"]],
    )
    toggle_identity_contact(client, sender_headers, owner["id"], True)

    with patched_routing_externals(classify_no_match=True):
        trace = simulate_routing(
            client,
            superuser_token_headers,
            message="sign this document please",
            as_user_id=sender["id"],
        )

    detail = get_routing_trace(client, superuser_token_headers, trace["id"])
    stage2 = _stage(detail, "identity_stage2")
    assert stage2 is not None, detail["stages"]
    assert stage2["match_method"] == "pattern", stage2
    assert stage2["matched_pattern"] == "sign this document *", stage2
