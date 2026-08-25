"""The reachability verdict — why a decision went the way it did, in words.

Plan §9's headline output: an agent the classifier never saw cannot be
explained by the candidate table, because the candidate table has no row for
it. So the verdict says it out loud, to the admin staring at a ``no_match``:

    This user has 3 eligible candidates; Equation Assistant is not among them
    because it has neither a router trigger prompt nor example prompts — set
    one on its Configuration tab.

**That sentence used to be an App MCP sentence**, and a different one: *"it is
not a bundle install and has no App MCP route — add one from its Integrations
tab"*. It was emitted for every origin, Google Chat included, which made this
module the place the surface divergence was baked in — it instructed a channel
user to configure an *MCP exposure* to fix a *channel* problem
(``docs/plans/channel_routing_scope_split_plan.md`` §2.4). The scope split
answered that by giving the channel origins their own half. The
``AppAgentRoute`` deletion then removed the other half's subject entirely: App
MCP builds its ballot from the same two candidate providers a channel does, so
there is no route, assignment or ``channel_app_mcp`` flag left for any verdict
to name.

What survives of that split is :data:`_CHANNEL_ORIGINS` and the ``channel``
flag threaded through everything below — now carrying **wording**, not
findings. The two surfaces reach the same conclusions by the same tests; they
differ in what they call the thing the reader routes over, and in what Pass 2
means (a channel has one, App MCP does not). **The split is by origin, never by
candidate kind.** That distinction is worth keeping stated, because gating on
``kind == KIND_AGENT`` looks equivalent and is not: ``SKIP_ALREADY_INSTALLED``,
``SKIP_NOT_INSTALLABLE`` and ``SKIP_NO_REVISION`` are recorded by Pass 2's
auto-install scan as ``KIND_BUNDLE``, Pass 2 runs on channel decisions, and
``SKIP_ALREADY_INSTALLED``'s remedy used to read *"Check the installed agent's
App MCP route"* — a live §2.4 defect that a ``kind`` gate would have walked
straight past. ``kind`` is consulted in exactly one place
(:data:`_CHANNEL_AGENT_SKIP_EXPLANATIONS`) and only to tell two *facts* apart,
never to decide which surface the reader is on.

The line the split is drawn along:

- **What to change *now* follows the origin.** Every verdict derived from
  current configuration, every remedy, and every count noun that names what the
  candidates *are*, is written for the surface the decision ran on.
- **What the router *did* stays as recorded.** An explanation clause
  paraphrases a ``skip_reason`` the recorder wrote during that decision; it
  describes history, and history does not change with the surface reading it.
  An **action clause is always forward-looking**, so it must match the surface
  the reader is on even when the explanation beside it does not — which is why
  ``SKIP_ROUTE_INACTIVE`` on a channel trace still says the route was off (true,
  and the reason it was skipped) and no longer says to switch it back on (which
  would change nothing on a channel today).

**Why the sentence is built here and not in the card** (plan §10, Phase 4).
Three reasons, and the third is the one that matters:

1. It is testable. Every branch below has a test asserting the sentence it
   emits; wording that lives in TSX has nothing that fails when it goes wrong.
2. It lives with the rules it paraphrases. The clause "it has neither a router
   trigger prompt nor example prompts" is a restatement of the candidate
   providers' own eligibility test, and :func:`_has_router_wording` **calls**
   that test rather than restating it (see its docstring). Two files apart,
   they drift; one import apart, the drift is visible. The clause this
   paragraph used to defend — "it is not a bundle install and has no App MCP
   route", justified by being one import from
   ``AppAgentRouteService._create_auto_route_for_agent`` — outlived that
   import by a commit and then outlived the whole service. A rationale that
   cites a symbol is only as true as the symbol.
3. A wrong diagnosis is worse than none. This surface tells an admin what to
   change on somebody else's agent. A sentence assembled from whichever fields
   the card happened to have would be confidently wrong on the branches nobody
   thought about, and it would look exactly like the branches that are right.

**Two sources of truth, deliberately, in this order.** A trace records what the
router actually considered; the database records what is configured *now*. The
trace wins whenever it has an answer, because it describes the decision being
diagnosed — the route may well have been fixed since. The database is consulted
only where the trace has no answer, which is precisely the case the trace cannot
explain: it has no row for something that was never a candidate.

There are two such places, and the second was added by channel policy. The
first is an **expected agent the trace never mentions**
(:func:`_verdict_from_configuration` and its channel half). The second is **why
Pass 2 never ran** (:func:`_channel_pass_2_block`) — the router does record
that, but into ``StageTrace.reason``, which the message-text gate withholds, so
the trace's answer is unavailable on exactly the servers that need it most. Both
therefore describe **configuration as it stands now**, not as it stood at
decision time. That is deliberate for a surface whose question is "what do I
change to fix this", and it is said out loud in the sentences themselves rather
than left for a reader to infer.

**What this reads about the expected agent is an allowlist**, and the split
added to it deliberately rather than by drift. Two fields are *quoted*: the
agent's name and its owner's email. Two more are read for their **emptiness
only** — ``router_trigger_prompt`` and ``example_prompts``, in
:func:`_has_router_wording` — so what reaches the response is one bit ("set" or
"not set"), never the wording itself. All four clear the bar §7 sets for
``candidates[].trigger_prompt``: owner-authored configuration, not
sender-derived, already visible to this (superuser-only) audience — and the
trace surface already serves the *full text* of both, so a presence bit adds
nothing a reader of this page could not already see. Nothing else about the
agent reaches the response, and a fifth field would have to clear the same bar
when it is added rather than inherit these four's answer.

**Near-miss ranking reuses the shared Jaccard helpers by calling them**
(plan §3: "Reuse verbatim"). They live in
``app/services/routing/text_similarity.py`` —
``tokens_for_similarity`` / ``jaccard_similarity`` — alongside this module
rather than on the service that first happened to need them. A second copy of
the tokenizer would make "closest: X 0.31" and install-time conflict detection
disagree about what a token is, silently, the first time either was tuned; the
module is imported and its functions called through it, so a test can patch the
definition site and prove no copy crept in.

**Never break the thing it observes** (§11a Rule 2). This module runs on the
read path only — no capture is open, no routing decision is in flight — so a
failure here costs a diagnosis, not a delivery. :meth:`diagnose` is still
total, because its caller is ``RoutingTraceService.get``, which serves the
whole trace: a diagnosis that raised would take the trace detail with it, and
the trace is the more valuable half.

See ``docs/plans/auto_routing_tuning_plan.md`` §2, §9 and §10 Phase 4, and
``docs/plans/channel_routing_scope_split_plan.md`` §2.4, §3 and §5 Phase 4.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlmodel import Session as DBSession

from app.models import CHANNEL_AGENT_SCOPE_ALL, Agent, ServerChannel, User
from app.models.routing.routing_decision import (
    RoutingDecisionPublic,
    RoutingDiagnosisPublic,
    RoutingNearMiss,
)
from app.services.routing import routing_trace, text_similarity
from app.services.routing.channel_candidate_provider import example_prompt_text
from app.services.server_channels.channel_policy_service import ChannelPolicyService

logger = logging.getLogger(__name__)


# ── Verdict codes ────────────────────────────────────────────────────
#
# One code per sentence *per origin*, and that pairing is load-bearing: a test
# pins the code *and* the sentence, so a reworded verdict that no longer answers
# its branch fails rather than passing under a code that still looks right.
# Plain strings, matching the feature's convention — readers tolerate unknown
# values.
#
# **A code names a finding, not a wording.** Where a channel and an App MCP
# decision reach the same finding by the same test — this user had no
# candidates; this agent is not the sender's; it looks reachable now — they
# share the code and differ only in the sentence, because a client grouping or
# colouring by code is answering "what kind of problem is this", which is the
# same answer on both surfaces. (``frontend/.../routingCopy.ts`` tones every
# ``expected_agent_*`` code by prefix, so a new one needs no client change.) A
# code is added only for a finding with no counterpart on the other surface,
# which today is **four**, in two pairs, each marked "channel origins only"
# where it is declared: the ``no_candidates`` split by why Pass 2 never ran,
# and the ``expected_agent_*`` pair further down.
#
# **A code may carry more than one sentence**, and two of them now do. The rule
# is the one above restated: the code names the finding, so a second sentence
# is right exactly when the finding is unchanged and only the *remedy's
# subject* moves — and a remedy is the half that must never be wrong.
#
# - ``CODE_ROUTED`` reads differently when the decision matched on a pin,
#   because there the generic remedy (tighten the winner's trigger prompt) is
#   inert: no classifier ran. Unlike the ``expected_agent_*`` family,
#   ``"routed"`` is toned by the client *by name*, so a new code beside it
#   would render a success as an unrecognised value.
# - ``CODE_NO_CANDIDATES_CHANNEL_SCOPE`` reads differently depending on whether
#   the restricting scope is the sender's own or the channel's admin default,
#   because ``agent_scope`` is inheritable and the two live on different
#   people's screens. Same finding, two owners.
#
# See ``_general_verdict``.

#: No expected agent named.
CODE_ROUTED = "routed"
CODE_ERROR = "error"
CODE_NO_CANDIDATES = "no_candidates"
CODE_ALL_CANDIDATES_SKIPPED = "all_candidates_skipped"
CODE_NO_MATCH = "no_match"

#: Channel origins only, and the two of them are :data:`CODE_NO_CANDIDATES`
#: split by *why Pass 2 never ran*. The base sentence ends "…or add its bundle
#: to the auto-install list", which is a remedy that names a control that would
#: not change the outcome when the sender's channel policy bars the catalog
#: pass — the one thing this module treats as worse than a coarse answer.
#:
#: Their finding genuinely has no App MCP counterpart: channel policy is not
#: read on that surface at all, so this is the second *pair* under the rule the
#: comment above states, not an exception to it.
#:
#: :data:`CODE_NO_CANDIDATES_CHANNEL_SCOPE` carries **two** sentences, split on
#: whether the restricting scope is the sender's own or the channel's admin
#: default. Same finding, different screen to go and fix it on. See
#: :data:`_PASS_2_BLOCK_SCOPE_USER`.
#:
#: **Both are computed from the policy as it stands NOW**, not as it stood when
#: the decision ran — see :func:`_channel_pass_2_block`, which is where that
#: semantic is argued and where the sentences' "as they stand right now" clause
#: comes from.
#:
#: **Client tone is handled.** ``frontend/.../routingCopy.ts``'s
#: ``diagnosisTone`` matches ``no_candidates`` codes by *prefix*
#: (``code.startsWith("no_candidates")``), so these two land in the same
#: ``warn`` arm as the base ``"no_candidates"`` without needing their own
#: entries — and a later ``no_candidates_*`` variant inherits the tone too. The
#: verdict text — which is the feature — was never affected either way: the
#: card renders ``verdict`` and never picks a sentence out of that map.
CODE_NO_CANDIDATES_CHANNEL_SCOPE = "no_candidates_channel_scope"
CODE_NO_CANDIDATES_AUTO_INSTALL_OFF = "no_candidates_auto_install_off"

#: An expected agent was named and the trace has a row for it.
CODE_EXPECTED_SELECTED = "expected_agent_selected"
CODE_EXPECTED_CONSIDERED = "expected_agent_considered"
CODE_EXPECTED_SKIPPED = "expected_agent_skipped"

#: An expected agent was named and the trace has no row for it, so the answer
#: comes from current configuration instead.
#:
#: **All four are shared by both origins**, which is what the ``AppAgentRoute``
#: deletion bought. The App MCP half used to declare six codes of its own — a
#: route that was inactive, not on the App MCP channel, unassigned, or absent
#: on a standalone / bundle install — and every one of them asked about a
#: switch that no longer exists. Both surfaces now build their ballot from the
#: same two providers, so both reach the same four findings by the same tests,
#: and the module's own rule applies: a code names a finding, so they share it
#: and differ only in the sentence.
CODE_EXPECTED_UNKNOWN = "expected_agent_unknown"
#: The counterpart of the trace-side ``SKIP_NO_TRIGGER_PROMPT`` the candidate
#: provider records: the sender owns the agent, and it carries neither
#: ``router_trigger_prompt`` nor ``example_prompts``, so it is not a candidate.
#:
#: The ``channel`` in the spelling is historical — this was the channel half's
#: code while the App MCP half still had a route-shaped answer for the same
#: question. The **value** is kept rather than renamed because it is a wire
#: value a stored diagnosis and a client both read; only the audience widened.
CODE_EXPECTED_CHANNEL_NO_TRIGGER_PROMPT = "expected_agent_channel_no_trigger_prompt"
#: A candidate is defined *entirely* by who owns it, so a trace whose sender
#: account has since been deleted (``user_id`` is ``SET NULL``, deliberately —
#: see ``RoutingDecision.user_id``) has nothing left to check ownership
#: against. Its own code rather than a silent fallthrough into
#: :data:`CODE_EXPECTED_LOOKS_REACHABLE`, which would have asserted "this user
#: owns it" about no user at all.
CODE_EXPECTED_SENDER_GONE = "expected_agent_sender_gone"
CODE_EXPECTED_FOREIGN_OWNER = "expected_agent_foreign_owner"
CODE_EXPECTED_LOOKS_REACHABLE = "expected_agent_looks_reachable"

#: The diagnosis could not be computed at all.
CODE_UNAVAILABLE = "unavailable"


#: The origins whose decisions run the **two-pass channel pipeline** — Pass 1
#: over the sender's own agents, Pass 2 over the auto-install catalog, under a
#: resolved channel policy.
#:
#: Since the ``AppAgentRoute`` deletion this set no longer separates two ways of
#: being reachable — every surface routes over the same two candidate providers
#: — only two ways of being *described*. What still hangs off it: Pass 2 and
#: channel-policy sentences (an App MCP decision has neither), and the noun for
#: the surface itself.
#:
#: ``ORIGIN_SIMULATE`` is in the set because ``POST /admin/routing/simulate``
#: and ``.../traces/{id}/replay`` open their capture around
#: ``ChannelRoutingService.decide`` (``routing_tuning_service.py``) — a simulate
#: row *is* a channel decision, re-run by an admin. If simulate ever learns to
#: re-run an App MCP decision it must record which surface it simulated and this
#: set must read that, rather than go on assuming; a simulate row silently
#: diagnosed as a channel would send the reader to the wrong control, which is
#: the defect §2.4 names.
#:
#: An origin this set does not know — ``app_mcp``, ``identity``, or one added
#: later — gets the App MCP wording, which is now the *narrower* of the two: it
#: promises no Pass 2 and no channel policy, so an unknown origin is described
#: in terms of the machinery every surface has rather than machinery it may not.
_CHANNEL_ORIGINS = frozenset(
    {
        routing_trace.ORIGIN_SERVER_CHANNEL,
        routing_trace.ORIGIN_SIMULATE,
    }
)


#: Why a near-miss ranking is missing. Named rather than left as an empty list:
#: an empty ranking under a ``no_match`` reads as "nothing came close", which is
#: a finding, and it must not be indistinguishable from "we could not measure".
#: §11a Rule 1 on a read surface.
NEAR_MISS_NO_TEXT_NOTICE = (
    "Near-miss ranking needs the message that was routed, and this trace's "
    "text is not available — ROUTING_TRACE_STORE_MESSAGE_TEXT is off now, or "
    "was off when it was captured. The candidate list and the verdict are "
    "unaffected."
)
NEAR_MISS_NO_CANDIDATES_NOTICE = (
    "Nothing to rank: this decision considered no candidate with a trigger "
    "prompt or example messages."
)

#: Appended to both channel-policy verdicts' **actions**, so the read-time
#: semantic is stated in one place and the two sentences cannot drift about it.
#: On the action rather than the problem because it qualifies the remedy — "go
#: and look at this switch" is the claim that is time-sensitive — and because
#: ``action`` is a substring of ``verdict`` by construction, so it reaches both
#: renderings either way.
READ_TIME_POLICY_CAVEAT = (
    "Note that this names the channel's settings as they stand right now, not "
    "as they stood when the decision ran — a verdict answers what to change "
    "today."
)

#: :func:`_channel_pass_2_block`'s answers. Named constants rather than the
#: verdict codes themselves so the "why" and the sentence stay separable: the
#: same finding is reported by a different code the day another branch needs it,
#: and — as the two scope answers below already show — one code can need more
#: than one of them.
#:
#: **The agent scope splits by provenance, and it has to.** ``agent_scope`` is
#: an *inheritable* field: a sender with no ``channel_user_setting`` row follows
#: ``channel.default_agent_scope``, which is the admin's, and that is the normal
#: state for the auto-registered senders this whole feature exists for (see
#: ``ChannelPolicyService``'s docstring). A single sentence saying "this sender
#: has restricted it" would therefore blame an external Google Chat user — who
#: may have no account UI at all — for an admin's default, and send a superuser
#: to a screen where nothing is set. The provenance bit is free:
#: ``ChannelPolicyView.agent_scope_inherited`` already computes it and
#: ``ChannelPolicyService.resolve`` merely discards it, which is why
#: :func:`_channel_pass_2_block` calls ``describe`` instead.
_PASS_2_BLOCK_SCOPE_USER = "scope_user"
_PASS_2_BLOCK_SCOPE_DEFAULT = "scope_default"
_PASS_2_BLOCK_AUTO_INSTALL = "auto_install"

#: How many near-misses to return. The card shows a short list and the tail of
#: a Jaccard ranking is noise — every unrelated agent scores a little above zero
#: on stopword overlap.
NEAR_MISS_LIMIT = 5


# ── Skip-reason explanations ─────────────────────────────────────────
#
# Keyed by ``routing_trace``'s constants, never by literals, so renaming one
# breaks the import rather than silently falling through to the unknown branch.
# The fallback in :func:`_skip_explanation` is what makes these tables safe to
# leave incomplete: an unmapped reason is reported by name with an honest "this
# build has no explanation for it", which is a worse diagnosis but never a wrong
# one.
#
# Three tables, consulted narrowest-first — channel+agent, then channel, then
# this one, which is the App MCP / origin-neutral base. The overrides are
# deliberately sparse rather than a parallel copy: an explanation restates what
# the recorder wrote during that decision, and history reads the same on every
# surface. Only where a *remedy* would point at the wrong control, or where the
# same ``skip_reason`` names two different missing things, does an entry appear.

_SKIP_EXPLANATIONS: dict[str, tuple[str, str]] = {
    routing_trace.SKIP_ROUTE_INACTIVE: (
        "its App MCP route exists but is switched off",
        "Switch the route back on from the agent's Integrations tab.",
    ),
    routing_trace.SKIP_FOREIGN_OWNER: (
        "it belongs to a different account, and a channel session must run on "
        "the sender's own install",
        "Share the agent's bundle with this user and have them install it — "
        "routing to somebody else's install is refused by design.",
    ),
    routing_trace.SKIP_IDENTITY_ROUTE: (
        "it was reached through an identity contact route, which hands off to "
        "that person's agents in a second stage and is not selectable from a "
        "channel",
        "Route to the contact rather than to their agent, or give this user "
        "their own install of it.",
    ),
    # ── Servable since phase 6 ───────────────────────────────────────
    # Two facts about two different layers, each made true in a different
    # phase, and conflating them is what used to make this entry look
    # like working code when only half of it was.
    #
    # The skip **row** is genuinely produced, and has been since phase 3.
    # Whenever an identity owner has named this caller on a binding whose
    # binding or assignment is switched off,
    # ``IdentityCandidateProvider.build`` records
    # ``SKIP_IDENTITY_UNAVAILABLE``; a test in
    # ``tests/api/server_channels/server_channels_identity_trace_test.py``
    # drives one over the webhook and asserts it.
    #
    # The **explanation is served too, since phase 6**. It renders through
    # :func:`_verdict_from_trace`, reached when :func:`_find_candidate`
    # matches ``expected_agent_id`` against ``candidate["ref_id"]``. That
    # parameter used to be annotated ``uuid.UUID`` on both
    # :meth:`RoutingReachabilityService.diagnose` and the route behind it
    # (``admin_routing.get_routing_trace``), while an identity candidate
    # always writes the namespaced ``identity:{owner_id}`` — and the
    # annotation was the whole obstacle, because a UUID cannot name a
    # person. Phase 6 of the channels & identity unification widened it to
    # ``str``, so the surface can finally ask "why was this *person* not
    # reachable".
    #
    # Coverage, stated exactly because this file treats an unverifiable
    # claim about it as a defect. The **channel override below** is pinned
    # by ``tests/api/routing/routing_reachability_verdict_test.py``'s
    # ``test_verdict_for_an_identity_owner_who_shared_nothing_reachable``,
    # which drives a real webhook decision and reads it back with
    # ``?expected_agent_id=identity:{owner_id}`` — a real ref, not the
    # forged bare-UUID one that earlier comment refused, and still refuses.
    # **This base entry — the App MCP voice of the same reason — is
    # reachable now but has no test of its own.** Reaching it needs an
    # ``origin="app_mcp"`` trace carrying a candidate list, and a seeded
    # row carries none, so it belongs beside the other candidate-list
    # branches in ``tests/unit/test_routing_reachability.py`` rather than
    # on the API surface. Not written yet; said here rather than left to
    # be assumed.
    routing_trace.SKIP_IDENTITY_UNAVAILABLE: (
        "this person shared an agent with the sender, but none of what they "
        "shared is switched on right now, so they were not on the ballot at all",
        "Three switches can each cause this, and they live on two different "
        "people's screens. The owner's binding is inactive, or the owner "
        "disabled it for this specific caller — both on the owner's Settings > "
        "Channels > Identity Server card. Or the caller has not enabled the "
        "contact, which is the Identity Contacts section of the MCP Server "
        "card on the CALLER'S Settings > Channels. Check them in that order.",
    ),
    routing_trace.SKIP_NO_TRIGGER_PROMPT: (
        "it has no router trigger prompt, so the classifier had nothing to "
        "match the message against",
        "Set a router trigger prompt on the agent's Configuration tab (for a "
        "bundle, on the revision that gets published).",
    ),
    routing_trace.SKIP_ALREADY_INSTALLED: (
        "this user already has it installed, so the auto-install pass passed "
        "over it — it should have been reachable as one of their own routes "
        "instead",
        "Check the installed agent's App MCP route: an install whose route is "
        "missing or switched off falls into exactly this gap.",
    ),
    routing_trace.SKIP_NOT_INSTALLABLE: (
        "this user is not allowed to install its bundle",
        "Make the bundle public, or grant this user access to it, before "
        "expecting the auto-install pass to reach it.",
    ),
    routing_trace.SKIP_NO_REVISION: (
        "its bundle has no resolvable published revision",
        "Publish the bundle — an entry on the auto-install list with nothing "
        "published behind it can never win.",
    ),
    routing_trace.SKIP_AGENT_MISSING: (
        "the route that matched points at an agent id with no agent behind it",
        "Delete the dangling route and recreate it from the agent's "
        "Integrations tab.",
    ),
    routing_trace.SKIP_BUNDLE_MISSING: (
        "it won the auto-install pass and then could not be loaded — the "
        "bundle was deleted between this decision's catalog scan and the "
        "lookup that follows it",
        "Re-run this decision. Nothing is wrong with the routing rules: the "
        "bundle that matched simply stopped existing mid-decision.",
    ),
    # Lives in this base table rather than in the channel override below, for
    # the same reason SKIP_PASS_1_MATCHED does: only a channel decision can
    # record it (its producer is ``ChannelCandidateProvider``), and its remedy
    # already names a channel control — so there is no App MCP wording here for
    # an override to correct, and an entry in both tables would be one entry
    # twice.
    routing_trace.SKIP_NOT_IN_CHANNEL_SCOPE: (
        "this sender owns it, but it is not one of the agents they have "
        "switched on for this channel, so it was never put to the classifier",
        "Add it to their agent list for this channel in Settings > Channels, "
        "or set that channel back to using every agent they own. Nothing on "
        "the agent itself will help — its trigger prompt is not what excluded "
        "it.",
    ),
    routing_trace.SKIP_PASS_1_MATCHED: (
        "the auto-install pass could have offered it, but Pass 1 matched one "
        "of this sender's own agents first, so it was never put to the "
        "classifier",
        "Nothing to fix — a sender's own agent is meant to win over a bundle "
        "they have not installed. If the wrong one won, tighten the trigger "
        "prompt of the agent that claimed the message.",
    ),
}


#: Channel-origin overrides, **gated on the origin alone**. Every entry here is
#: a reason a channel decision can actually record, whose base remedy above
#: sends the reader to an App MCP control that a channel does not read.
#:
#: All three are reachable on a channel and none of them is ``KIND_AGENT``-only,
#: which is why this table is keyed by origin and not by kind:
#:
#: - ``SKIP_ALREADY_INSTALLED`` — recorded by Pass 2's auto-install scan as a
#:   ``KIND_BUNDLE`` candidate (``channel_routing_service._route_catalog``), on
#:   channel decisions, today. Its base remedy is the §2.4 defect verbatim.
#: - ``SKIP_AGENT_MISSING`` — recorded by channel Pass 1 when the winning
#:   candidate's row is gone by the time it is loaded. There is no route in that
#:   story at all any more: the candidate came from ``WHERE owner_id = sender``.
#: - ``SKIP_ROUTE_INACTIVE`` — no longer producible **anywhere**: its producer
#:   was ``AppAgentRouteService``, which is deleted along with the routes it
#:   filtered. Traces captured before that carry it and are still read, so the
#:   explanation stays: it is what happened. The action cannot, because there is
#:   no route left to switch back on, on either surface.
#: ``SKIP_IDENTITY_UNAVAILABLE`` is the fourth, and it arrived exactly as this
#: comment predicted it would. It used to say "no entry here, and that is
#: correct today" because only App MCP Stage 1 recorded the reason; Phase 3 of
#: the channels & identity unification put ``IdentityCandidateProvider`` on the
#: channel ballot, so a channel decision can now carry it, and the base entry —
#: written in App MCP's voice — would send a channel user to an MCP card. The
#: entry below landed in the same change, per this file's convention: adding the
#: reason to a surface and adding its override for that surface are one change.
#:
#: What that entry did **not** do until phase 6 was reach a reader, and this
#: comment used to say so. It does now: the only branch that renders a skip
#: explanation is keyed by ``expected_agent_id``, and phase 6 of the channels &
#: identity unification widened that from ``uuid.UUID`` to ``str``, so
#: ``identity:{owner_id}`` names the candidate the sentence is about. So "the
#: fourth" now means both halves — a channel decision carries the reason, and a
#: channel user can read this sentence. The entry's own comment names the test
#: that pins it.
_CHANNEL_SKIP_EXPLANATIONS: dict[str, tuple[str, str]] = {
    routing_trace.SKIP_ALREADY_INSTALLED: (
        "this user already has it installed, so the auto-install pass passed "
        "over it — it should have been reachable in Pass 1 as one of the "
        "agents they own instead",
        "Set a router trigger prompt (or example prompts) on the installed "
        "agent's Configuration tab: an install with neither is not a channel "
        "candidate, which is exactly this gap.",
    ),
    routing_trace.SKIP_AGENT_MISSING: (
        "the candidate that won names an agent id with no agent behind it — it "
        "was deleted between this decision's candidate scan and the lookup "
        "that follows it",
        "Re-run this decision. If the agent is meant to exist, recreate it and "
        "set a router trigger prompt (or example prompts) on its Configuration "
        "tab.",
    ),
    routing_trace.SKIP_ROUTE_INACTIVE: (
        "its App MCP route was switched off when this decision ran, and this "
        "trace was captured while channel routing still read App MCP routes",
        "Set a router trigger prompt (or example prompts) on the agent's "
        "Configuration tab — switching that route back on would not help, "
        "because channel routing no longer reads routes at all.",
    ),
    routing_trace.SKIP_IDENTITY_ROUTE: (
        "it was reached through an identity contact route, which hands off to "
        "that person's agents in a second stage and was never selectable from "
        "a channel",
        # The base entry's remedy — "route to the contact rather than to their
        # agent" — is an instruction about a candidate class a channel no
        # longer has. Its producer was this pass's own ``is_identity`` branch,
        # deleted by the scope split, so every row carrying it is history and
        # the only forward-looking answer is the one channel routing actually
        # reads.
        "Give this user their own install of the agent and set a router "
        "trigger prompt (or example prompts) on it — a channel routes over the "
        "sender's own agents and reads no identity contact at all.",
    ),
    # ── Served since phase 6, and pinned by a real decision ──────────
    # Spelled out here rather than cross-referenced, because this is the
    # half of the story that used to read as done and was not: phase 3
    # made the skip **row** live on a channel, and phase 6 made this
    # **sentence** reachable, by widening the ``?expected_agent_id=``
    # branch from ``uuid.UUID`` to ``str`` so it can name an
    # ``identity:{owner_id}`` candidate. Producible and explainable are
    # facts about different layers; both are true now.
    #
    # Pinned by ``tests/api/routing/routing_reachability_verdict_test.py``'s
    # ``test_verdict_for_an_identity_owner_who_shared_nothing_reachable``,
    # which drives a real webhook decision that records this reason and
    # reads the trace back with the identity ref — not the forged
    # bare-UUID one the base table's comment refused, and still refuses.
    routing_trace.SKIP_IDENTITY_UNAVAILABLE: (
        # The base entry says the same thing and then sends the reader to "the "
        # Identity Contacts section of the MCP Server card", which is an App MCP
        # control a channel does not read. The finding is unchanged; only the
        # remedy's subject moves — the shape this file's docstring describes.
        "this person had named the sender on an identity binding, so they were "
        "recorded on this decision, but nothing they shared was reachable when "
        "the message arrived — they were on the trace and never on the ballot",
        "The switch that fixes this is the identity owner's, not the sender's, "
        "in two of the three cases: on the owner's Settings > Channels > "
        "Identity Server card, either the binding itself is inactive or this "
        "sender's assignment to it is. The third is the sender's own contact "
        "toggle for that person, in their Settings > Channels. Check the "
        "owner's two first — a sender cannot enable a contact nobody has "
        "shared with them. What this is NOT is the sender's channel-level "
        "identity-routing switch: with that off, no identity appears on a "
        "channel trace at all, so this row is evidence it was already on.",
    ),
}


#: Channel-origin overrides for ``KIND_AGENT`` candidates only — the one place
#: ``kind`` is consulted, and not as a stand-in for the surface.
#:
#: ``SKIP_NO_TRIGGER_PROMPT`` has two producers a single channel decision can
#: reach: ``ChannelCandidateProvider`` records it for an **agent** the sender
#: owns that has neither a trigger prompt nor example prompts, and Pass 2's
#: auto-install scan records it for a **bundle** whose latest revision carried
#: no prompt. Same reason string, two different things missing — and a bundle
#: revision has no ``example_prompts`` of its own to offer, so telling its
#: reader to go set some would prescribe a field that is not there. The origin
#: alone cannot pick between them.
_CHANNEL_AGENT_SKIP_EXPLANATIONS: dict[str, tuple[str, str]] = {
    routing_trace.SKIP_NO_TRIGGER_PROMPT: (
        "it has neither a router trigger prompt nor example prompts, so the "
        "classifier had nothing to match the message against",
        "Set a router trigger prompt (or example prompts) on the agent's "
        "Configuration tab.",
    ),
}


class RoutingReachabilityService:
    """Plain-language verdicts and near-miss ranking for one stored decision."""

    @staticmethod
    def diagnose(
        db: DBSession,
        trace: RoutingDecisionPublic,
        *,
        expected_agent_id: str | uuid.UUID | None = None,
    ) -> RoutingDiagnosisPublic:
        """The verdict for ``trace``, optionally about one expected agent.

        Total: any failure comes back as :data:`CODE_UNAVAILABLE` rather than
        propagating. See the module docstring — the caller is serving the whole
        trace, and losing the diagnosis is much cheaper than losing that.

        ``trace`` is the **projected** public model, not the row. That is not
        incidental: it means the diagnosis sees exactly what the reader sees, so
        it can never quote a field the message-text gate withheld, and the
        near-miss ranking goes quiet on its own when the text is gated instead
        of needing a second rule that remembers to.

        ``expected_agent_id`` is a **candidate ref**, not an agent id: an
        identity candidate is named ``identity:{owner_id}``, which is how an
        admin asks "why was this *person* not reachable". The ``uuid.UUID`` arm
        of the union is kept because every in-process caller already holds a
        real ``Agent.id``, and normalising once here rather than at each call
        site is what keeps the two shapes from having to be thought about again
        further down.
        """
        ref = None if expected_agent_id is None else str(expected_agent_id)
        try:
            return _diagnose(db, trace, expected_agent_id=ref)
        except Exception:  # noqa: BLE001 — a diagnostic must not break the read
            logger.warning("Routing reachability diagnosis failed", exc_info=True)
            # Composed from problem + action like every other branch, and not
            # written out twice: the "action is a substring of verdict"
            # invariant has to hold on the failure path too, or a client that
            # renders them separately would show a remedy the sentence does not
            # contain exactly when something has already gone wrong.
            problem = (
                "This decision's diagnosis could not be computed, but the "
                "trace itself is intact."
            )
            action = (
                "Read the candidate list below, and see the server logs for "
                "why the summary failed."
            )
            return RoutingDiagnosisPublic(
                code=CODE_UNAVAILABLE,
                verdict=f"{problem} {action}",
                action=action,
                expected_agent_id=ref,
            )


# ── Implementation ───────────────────────────────────────────────────


def _diagnose(
    db: DBSession,
    trace: RoutingDecisionPublic,
    *,
    expected_agent_id: str | None,
) -> RoutingDiagnosisPublic:
    candidates = _candidates(trace.stages)
    eligible = [c for c in candidates if c.get("eligible")]
    skipped_by_reason: dict[str, int] = {}
    for candidate in candidates:
        if candidate.get("eligible"):
            continue
        reason = str(candidate.get("skip_reason") or "unspecified")
        skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1

    near_misses, near_miss_notice = _rank_near_misses(
        trace.message_text, candidates
    )

    # Resolved once, here, and threaded down rather than re-read: every sentence
    # in one verdict has to describe the same surface, and a second reader of
    # ``trace.origin`` further down is how half a verdict ends up written for
    # the other one.
    channel = _is_channel_origin(trace.origin)

    if expected_agent_id is None:
        code, problem, action = _general_verdict(
            db, trace, eligible, candidates, channel=channel
        )
        name = owner_email = None
    else:
        code, problem, action, name, owner_email = _expected_agent_verdict(
            db,
            trace,
            candidates,
            eligible,
            expected_agent_id,
            near_misses,
            channel=channel,
        )

    return RoutingDiagnosisPublic(
        code=code,
        # Composed, never written twice: ``action`` is a substring of
        # ``verdict`` by construction, so a client rendering the two separately
        # cannot show a remedy the sentence disagrees with.
        verdict=f"{problem} {action}".strip(),
        action=action,
        eligible_candidate_count=len(eligible),
        skipped_by_reason=skipped_by_reason,
        expected_agent_id=expected_agent_id,
        expected_agent_name=name,
        expected_agent_owner_email=owner_email,
        near_misses=near_misses,
        near_miss_notice=near_miss_notice,
    )


def _general_verdict(
    db: DBSession,
    trace: RoutingDecisionPublic,
    eligible: list[dict],
    candidates: list[dict],
    *,
    channel: bool,
) -> tuple[str, str, str]:
    """The verdict with no expected agent named: (code, problem, action).

    ``routed`` and ``error`` read the same on every surface — one names the
    agent that won, the other names the provider cascade, and neither mentions
    a route or a trigger prompt. The three negative branches all do, so all
    three are written twice.

    A pinned decision splits twice more, and **not by origin**: once under
    ``routed``, because the generic remedy (tighten the winner's trigger
    prompt) is inert against a pin, and once below the terminal-verdict
    branches, because a pin that *failed* would otherwise be diagnosed as a
    candidate scan whose winner was rejected — a scan that never happened.
    Both are splits by ``match_method``, which is a fact about what the router
    did rather than about which surface it ran on, so neither needs a
    counterpart in the App MCP half: nothing there records ``pinned``.

    ``db`` is here for exactly one branch — the channel ``no_candidates`` one,
    which asks :func:`_channel_pass_2_block` whether this sender's channel
    policy is what kept Pass 2 from running. The lookup is made **inside** that
    branch rather than up front deliberately: it is two to five ``SELECT``s
    depending on the channel's shape, this function runs on every trace read,
    and every other branch has its answer already.
    """
    if trace.outcome == routing_trace.OUTCOME_ERROR:
        return (
            CODE_ERROR,
            f"This decision failed before it reached a verdict: "
            f"{trace.error or 'no error was recorded'}.",
            "Check the provider attempts below — a routing failure with no "
            "attempt at all means no AI credential was usable, which is a "
            "server configuration problem rather than an agent one.",
        )

    if trace.outcome == routing_trace.OUTCOME_ROUTED:
        chosen = (
            trace.selected_agent_name
            or trace.selected_bundle_name
            or "the selected agent"
        )
        if trace.match_method == routing_trace.MATCH_PINNED:
            # Same code, second sentence — the one place this file does that,
            # and the reasoning is the comment above the codes: a code names a
            # *finding*, and the finding here is the same one ("it routed, and
            # here is what to"). What differs is the remedy's subject, and a
            # remedy is the half that must never be wrong. The generic sentence
            # below tells the reader to tighten a trigger prompt, which is inert
            # against a pin: no classifier ran, so no wording could have changed
            # the answer. That is a confidently wrong instruction, which this
            # module treats as worse than a coarse one.
            #
            # Not given a code of its own on purpose. ``frontend/.../
            # routingCopy.ts`` tones ``"routed"`` explicitly and everything
            # unrecognised neutrally, so a new code would arrive grey in a card
            # this change does not touch — the client would render a successful
            # routing as though it were unclassifiable.
            return (
                CODE_ROUTED,
                f"This message went to {chosen} because this sender has "
                f"pinned it to this channel: no classifier ran, and no other "
                f"agent was considered.",
                "Nothing to fix here. If their messages should be routed by "
                "content instead, clear the pinned agent in their Settings > "
                "Channels — while a pin is set, trigger prompts and example "
                "prompts have no effect on this channel.",
            )
        return (
            CODE_ROUTED,
            f"This message routed to {chosen}, chosen from "
            f"{_count(len(eligible), 'eligible candidate')}.",
            "Nothing to fix here. If it reached the wrong agent, compare the "
            "near-miss scores below and tighten the winner's trigger prompt "
            "so it stops claiming this kind of message.",
        )

    if trace.match_method == routing_trace.MATCH_PINNED:
        # The mirror of the routed-by-pin sentence above, and it needs saying
        # for the same reason. A pin that failed — its agent deleted, or moved
        # to another account, between the policy resolution and the routing
        # task — settles ``no_match`` with a single skipped candidate, which
        # walks straight into CODE_ALL_CANDIDATES_SKIPPED below and is
        # diagnosed as though a candidate scan had run and a winner had been
        # rejected. Every word of that is wrong here: no scan ran, nothing won,
        # and "re-run this decision" will not reproduce it, because the next
        # resolution clears the dangling pin (``ChannelPolicyService._owned_pin``).
        # The one control that matters — the pin itself — appears nowhere in
        # that verdict.
        #
        # Placed above the candidate-shape branches rather than inside them:
        # what makes this decision explicable is *how it matched*, not how many
        # rows it left behind, and the two failure branches (agent gone,
        # foreign owner) want the same sentence.
        return (
            CODE_ALL_CANDIDATES_SKIPPED,
            "This sender has an agent pinned to this channel, and routing "
            "could not use it: the pinned agent no longer exists, or it is no "
            "longer theirs. No classifier ran and no other agent was "
            "considered, because a pin is an instruction rather than a "
            "preference.",
            "Clear or re-point the pinned agent in their Settings > Channels. "
            "Nothing else will change this outcome — a pin is consulted before "
            "the candidate list is even built, and the auto-install pass is "
            "skipped for a pinned channel too.",
        )

    if not candidates:
        if channel:
            # Before the base sentence, because the base sentence's remedy
            # ("…or add its bundle to the auto-install list") is only true when
            # Pass 2 could actually have run for this sender. On a channel
            # whose policy bars the catalog pass it names a control that would
            # change nothing — a confidently wrong instruction, which this
            # module treats as worse than a coarse one.
            blocked = _channel_pass_2_block(db, trace)
            # One code, two sentences, keyed on whose setting it is. The
            # finding is identical — the channel's agent scope barred Pass 2 —
            # and only the remedy's subject differs, which is the same shape
            # (and the same justification) as ``CODE_ROUTED``'s pinned variant
            # above: a remedy is the half that must never be wrong.
            if blocked == _PASS_2_BLOCK_SCOPE_USER:
                return (
                    CODE_NO_CANDIDATES_CHANNEL_SCOPE,
                    "This user had no routing candidates at all: they own no "
                    "agent the classifier could consider, and the auto-install "
                    "pass could not offer them one either — as this channel's "
                    "settings stand right now, this sender has limited it to "
                    "an explicitly chosen set of their own agents, and an "
                    "agent installed from the catalog would not be in that "
                    "set.",
                    "Set this channel back to using every agent they own, in "
                    "their Settings > Channels, and then give them an agent "
                    "with a router trigger prompt (or example prompts). "
                    "Nothing on an agent alone will help while the limit "
                    "stands: a newly created agent would be outside the chosen "
                    "set too. " + READ_TIME_POLICY_CAVEAT,
                )
            if blocked == _PASS_2_BLOCK_SCOPE_DEFAULT:
                return (
                    CODE_NO_CANDIDATES_CHANNEL_SCOPE,
                    "This user had no routing candidates at all: they own no "
                    "agent the classifier could consider, and the auto-install "
                    "pass could not offer them one either — as this channel's "
                    "settings stand right now, its admin default limits every "
                    "sender to an explicitly chosen set of their own agents, "
                    "and an agent installed from the catalog would not be in "
                    "that set. This sender has set nothing of their own, so "
                    "they follow that default.",
                    "Set this channel's default agent scope back to every "
                    "agent a user owns, in its admin settings — there is "
                    "nothing to change on this sender's side, because they "
                    "have overridden nothing. Nothing on an agent alone will "
                    "help while the default stands: a newly created agent "
                    "would be outside the chosen set too. "
                    + READ_TIME_POLICY_CAVEAT,
                )
            if blocked == _PASS_2_BLOCK_AUTO_INSTALL:
                return (
                    CODE_NO_CANDIDATES_AUTO_INSTALL_OFF,
                    "This user had no routing candidates at all: they own no "
                    "agent the classifier could consider, and the auto-install "
                    "pass never ran — as this channel's settings stand right "
                    "now, installing a bundle for its senders is switched off.",
                    "Give this user an agent with a router trigger prompt (or "
                    "example prompts) on its Configuration tab, or switch "
                    "auto-installing bundles back on for this channel in its "
                    "admin settings. Adding a bundle to the auto-install list "
                    "will not help on its own — while that switch is off the "
                    "list is never read. " + READ_TIME_POLICY_CAVEAT,
                )
            return (
                CODE_NO_CANDIDATES,
                "This user had no routing candidates at all: they own no agent "
                "the classifier could consider and no auto-install bundle was "
                "eligible, so no message from them can route anywhere.",
                "Set a router trigger prompt (or example prompts) on the agent "
                "you expected, from its Configuration tab, or add its bundle "
                "to the auto-install list.",
            )
        return (
            CODE_NO_CANDIDATES,
            "This user had no routing candidates at all: they own no agent the "
            "classifier could consider, so no message from them can route "
            "anywhere.",
            "Give this user an agent with a router trigger prompt (or example "
            "prompts) on its Configuration tab.",
        )

    if not eligible:
        one = len(candidates) == 1
        return (
            CODE_ALL_CANDIDATES_SKIPPED,
            f"This user has no eligible candidates: "
            f"{_count(len(candidates), 'candidate')} "
            f"{'was' if one else 'were all'} excluded before the classifier "
            f"saw {'it' if one else 'them'} ({_reasons(candidates)}).",
            "Fix the exclusion on the agent you expected — the candidate "
            "table below names the reason for each one.",
        )

    return (
        CODE_NO_MATCH,
        f"This user has {_count(len(eligible), 'eligible candidate')} and "
        f"the classifier matched none of them.",
        "Widen the trigger prompt of the agent that should have won — the "
        "near-miss scores below say which came closest — or use Draft a "
        "recommendation to generate wording for its owner.",
    )


def _expected_agent_verdict(
    db: DBSession,
    trace: RoutingDecisionPublic,
    candidates: list[dict],
    eligible: list[dict],
    expected_agent_id: str,
    near_misses: list[RoutingNearMiss],
    *,
    channel: bool,
) -> tuple[str, str, str, str | None, str | None]:
    """The verdict about one named agent: (code, problem, action, name, email).

    The trace is consulted first and the database only for an agent the trace
    never mentions — see the module docstring on why that order and not the
    reverse.

    ``expected_agent_id`` is a candidate ref, so the ``Agent`` lookup below is
    guarded rather than unconditional — :func:`_agent_uuid` says why that guard
    is load-bearing and not defensive.
    """
    row = _find_candidate(candidates, expected_agent_id)
    as_agent_id = _agent_uuid(expected_agent_id)
    agent = None if as_agent_id is None else db.get(Agent, as_agent_id)
    name = _agent_label(row, agent, expected_agent_id)
    owner_email = _owner_email(db, row, agent)

    if row is not None:
        return (
            *_verdict_from_trace(trace, row, name, near_misses, channel=channel),
            name,
            owner_email,
        )

    if agent is None:
        if as_agent_id is None:
            # A namespaced ref — ``identity:{owner_id}`` is the only one any
            # producer writes — that matched no candidate row. The same code,
            # deliberately: the finding is identical ("what you named was never
            # on this decision"), and a new wire value would have to be
            # rendered by every client in order to say nothing new. Only the
            # noun moves, because "No agent identity:… exists on this server"
            # is false about a ref that never named an agent to begin with.
            return (
                CODE_EXPECTED_UNKNOWN,
                f"No candidate {expected_agent_id} appears on this decision, "
                f"so there is nothing recorded here to explain about it.",
                "Check the ref against the candidate table below — an identity "
                "candidate is named identity: followed by the owner's user id, "
                "and a person nobody recorded has no row on this trace at all.",
                None,
                None,
            )
        return (
            CODE_EXPECTED_UNKNOWN,
            f"No agent {expected_agent_id} exists on this server, so it could "
            f"never have been a routing candidate.",
            "Check the id against the agent's own page — a deleted agent and "
            "a mistyped id look the same from here.",
            None,
            None,
        )

    prefix = (
        f"This user has {_count(len(eligible), 'eligible candidate')}; "
        f"{name} is not among them because"
    )
    code, problem, action = _verdict_from_configuration(
        trace, agent, prefix, channel=channel
    )
    return code, problem, action, name, owner_email


def _verdict_from_trace(
    trace: RoutingDecisionPublic,
    row: dict,
    name: str,
    near_misses: list[RoutingNearMiss],
    *,
    channel: bool,
) -> tuple[str, str, str]:
    """The agent WAS in this decision's candidate list. Say what happened to it.

    This branch runs **before** any configuration lookup, and on a channel that
    ordering now decides where most sentences come from: the candidate provider
    records a wording-less owned agent as a *skipped candidate* rather than
    dropping it, so "the sender owns it and it has no trigger prompt" arrives
    here as ``SKIP_NO_TRIGGER_PROMPT`` and never reaches
    :func:`_channel_verdict_from_configuration`. The override table is the live
    path for that case; the configuration branch is the one for an agent the
    decision genuinely never saw.

    The same now goes for an agent excluded by the channel's ``agent_scope``:
    it arrives here as ``SKIP_NOT_IN_CHANNEL_SCOPE`` rather than as an absence,
    which is the entire reason the candidate provider records it instead of
    filtering it out.
    """
    if not row.get("eligible"):
        reason = str(row.get("skip_reason") or "")
        explanation, action = _skip_explanation(
            reason, kind=str(row.get("kind") or ""), channel=channel
        )
        return (
            CODE_EXPECTED_SKIPPED,
            f"{name} was considered for this decision and then excluded: "
            f"{explanation}.",
            action,
        )

    selected = str(trace.selected_agent_id or "")
    if selected and selected == str(row.get("ref_id") or ""):
        return (
            CODE_EXPECTED_SELECTED,
            f"{name} is the agent this decision chose.",
            "Nothing to fix — if the message still went nowhere, the failure "
            "is after routing (session setup or the outbound reply), not in "
            "it.",
        )

    closest = next(
        (m for m in near_misses if m.ref_id == str(row.get("ref_id") or "")), None
    )
    score = f" (token overlap {closest.similarity:.2f})" if closest else ""
    return (
        CODE_EXPECTED_CONSIDERED,
        f"{name} was an eligible candidate{score} and the classifier did not "
        f"pick it — reachability is not the problem here.",
        "Widen its trigger prompt to cover wording like this message, or use "
        "Draft a recommendation to generate that wording for its owner.",
    )


def _verdict_from_configuration(
    trace: RoutingDecisionPublic,
    agent: Agent,
    prefix: str,
    *,
    channel: bool,
) -> tuple[str, str, str]:
    """The agent was never a candidate. Explain from what is configured now.

    **One function for both origins**, and that is the shape of the fix. This
    used to be two: a channel half asking the three questions below, and an App
    MCP half asking four more about routes, assignments and the
    ``channel_app_mcp`` flag before it reached ownership at all. Every one of
    those four is about a switch that no longer exists — App MCP builds its
    ballot from ``ChannelCandidateProvider`` + ``IdentityCandidateProvider``,
    exactly as a channel does — so the two halves collapsed into the same three
    branches against the same two facts that make an agent a candidate:
    **who owns it**, and **whether its owner wrote anything for the classifier
    to match on**.

    ``channel`` therefore no longer picks the *findings*. It names the surface
    in one clause, and that is all it is still for here. Written as one
    function rather than two near-identical ones deliberately — two copies of a
    branch table drift, and this module's whole contract is that a verdict is
    never confidently wrong.

    Reads ``Agent`` and nothing else, so this function cannot drift back into
    prescribing a route: there is nothing here to prescribe one *from*, and no
    ``db`` to prescribe it with.

    **There is a third fact, and it is deliberately not asked here.** An agent
    the sender owns and has written wording for can still be excluded because
    it is not in the channel's ``agent_scope``. That exclusion is answered
    where it is recorded — the candidate provider writes a
    ``SKIP_NOT_IN_CHANNEL_SCOPE`` row, so :func:`_verdict_from_trace` handles
    it and control never arrives here. Asking again from configuration would
    mean loading the channel and re-resolving the policy on a read path whose
    contract is "reads ``Agent`` and nothing else", to answer a case that
    already has a better answer from the trace.
    What that leaves imprecise is the last branch below, for an agent the
    decision genuinely never saw: if it is out of scope *now*, "the re-run will
    show it as a candidate" overstates — the re-run will show it as a skip,
    with the right reason on it. A reader who follows the instruction gets the
    correct diagnosis one step later, which is the acceptable failure of the
    two available here.

    **A fourth fact is out of reach rather than skipped:** an agent somebody
    else owns can be reachable through an identity contact on either surface.
    The foreign-owner branch cannot say so, because saying so would mean
    enumerating whose identities name this sender — which is the enumeration
    the identity provider's own trace inversion exists to refuse.

    **When this is reached at all**, which is narrower than it looks. The
    candidate provider records *every* agent the sender owns — eligible ones as
    candidates, the rest as ``SKIP_NO_TRIGGER_PROMPT`` skips — so an agent owned
    at capture time always has a row, and :func:`_verdict_from_trace` answers
    for it first. What lands here is an agent the decision genuinely never saw:
    one created or transferred to this sender *since*, or a trace captured
    before its surface routed this way.
    """
    surface = "a channel" if channel else "App MCP"

    # Ownership asked positively, so the ``user_id is None`` case cannot fall
    # through into a sentence that asserts an owner. See CODE_EXPECTED_SENDER_GONE.
    if trace.user_id is None:
        return (
            CODE_EXPECTED_SENDER_GONE,
            f"{prefix} this decision's sender account no longer exists, and a "
            f"routing candidate is defined entirely by who owns it — with no "
            f"sender there is nothing left to check its owner against.",
            "Run Simulate for the account you actually mean; this trace can no "
            "longer answer a question about ownership.",
        )

    if agent.owner_id != trace.user_id:
        return (
            CODE_EXPECTED_FOREIGN_OWNER,
            f"{prefix} it belongs to a different account, and {surface} routes "
            f"over the caller's own agents.",
            "Share its bundle with this user and have them install it — the "
            "session runs on the caller's own install, so the install they own "
            "is the only thing this surface can reach. (Reaching somebody "
            "else's agent is what identity contacts are for, and that is a "
            "different question from this one.)",
        )

    if not _has_router_wording(agent):
        return (
            CODE_EXPECTED_CHANNEL_NO_TRIGGER_PROMPT,
            f"{prefix} it has neither a router trigger prompt nor example "
            f"prompts, so there is nothing for the classifier to match a "
            f"message against.",
            "Set a router trigger prompt (or example prompts) on the agent's "
            "Configuration tab. That pair is the whole of it — there is no "
            "route, assignment or per-agent toggle to configure anywhere.",
        )

    return (
        CODE_EXPECTED_LOOKS_REACHABLE,
        f"{prefix} it was not a candidate when this decision ran, even though "
        f"this user owns it and its router trigger prompt or example prompts "
        f"are set now.",
        "Re-run this decision — an agent created, transferred or given wording "
        "after the trace was captured explains exactly this, and the re-run "
        "will show it as a candidate.",
    )


# ── Near-miss ranking ────────────────────────────────────────────────


def _rank_near_misses(
    message: str | None, candidates: list[dict]
) -> tuple[list[RoutingNearMiss], str | None]:
    """Rank candidates by token overlap with the message. See module docstring.

    ``text_similarity.tokens_for_similarity`` and ``.jaccard_similarity`` are
    **called**, not copied: install-time conflict detection and this ranking
    have to agree on what a token is, and two copies of a tokenizer agree only
    until one of them is tuned. They are reached through the module rather than
    bound by name here so that patching the definition site reaches this call.

    Ranked on ``trigger_prompt`` **and** ``prompt_examples`` together, because
    together is what the classifier now receives. Phase 5 fixed Bug 1 — examples
    reach the rendered prompt — and this ranking moved in the same pass on
    purpose: a card that scores on a strict subset of what the model saw is this
    feature misreporting the system on its own diagnostic surface. An agent
    whose trigger prompt is vague and whose examples are exact would have been
    ranked last while actually winning the route.

    Scored as one concatenated document rather than as a max over two scores:
    Jaccard is over token *sets*, so joining them is the same question the
    classifier is answering ("does anything this owner wrote look like this
    message"), and it does not privilege whichever field happens to be shorter.
    """
    text = (message or "").strip()
    if not text:
        return [], NEAR_MISS_NO_TEXT_NOTICE

    message_tokens = text_similarity.tokens_for_similarity(text)
    ranked: list[RoutingNearMiss] = []
    for candidate in candidates:
        prompt = str(candidate.get("trigger_prompt") or "")
        examples = str(candidate.get("prompt_examples") or "")
        ref_id = str(candidate.get("ref_id") or "")
        if not (prompt or examples) or not ref_id:
            continue
        scored_text = "\n".join(part for part in (prompt, examples) if part)
        similarity = text_similarity.jaccard_similarity(
            message_tokens, text_similarity.tokens_for_similarity(scored_text)
        )
        ranked.append(
            RoutingNearMiss(
                ref_id=ref_id,
                kind=str(candidate.get("kind") or ""),
                name=str(candidate.get("name") or ref_id),
                similarity=round(similarity, 2),
                eligible=bool(candidate.get("eligible")),
                skip_reason=candidate.get("skip_reason"),
            )
        )
    if not ranked:
        return [], NEAR_MISS_NO_CANDIDATES_NOTICE

    # Name as the tiebreak, so a page of equally-scoring candidates renders in
    # a stable order instead of whatever the stage payload happened to hold.
    ranked.sort(key=lambda m: (-m.similarity, m.name))
    return ranked[:NEAR_MISS_LIMIT], None


# ── Small helpers ────────────────────────────────────────────────────


def _candidates(stages: Any) -> list[dict]:
    """Every candidate across every stage, de-duplicated by ``ref_id``.

    Defensive throughout, like the other ``stages`` readers: this is JSONB
    written by a recorder whose dataclasses keep changing, and a row written by
    an older build must not break a diagnosis.

    De-duplicated because a candidate can legitimately appear in more than one
    stage, and "3 eligible candidates" counted twice is a wrong number stated with
    confidence. The **last** occurrence wins: ``mark_candidate_skipped`` flips a
    row in place, so a later stage carries the more settled verdict.
    """
    found: dict[str, dict] = {}
    for stage in stages or []:
        for candidate in (stage or {}).get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            ref = str(candidate.get("ref_id") or "")
            if not ref:
                continue
            found[ref] = candidate
    return list(found.values())


def _agent_uuid(ref_id: str) -> uuid.UUID | None:
    """The bare-agent-id reading of a candidate ref, or ``None``.

    ``expected_agent_id`` is a **ref**, and an identity candidate's ref is
    ``identity:{owner_id}``. Handing that to ``db.get(Agent, ...)`` raises,
    :meth:`RoutingReachabilityService.diagnose`'s total guard swallows it, and
    the whole verdict comes back as ``CODE_UNAVAILABLE`` — which on screen
    reads as if nothing had been asked rather than as if something had broken.
    So the parse happens here, before the lookup, and a ref that does not name
    an agent never reaches the ``Agent`` table.

    The total guard is not the answer to this. It is the answer to a diagnosis
    failing *unexpectedly*; a ref shape this surface documents and invites is
    not unexpected, and letting it fall through there would turn the widening
    this parameter got into a silent no-op.
    """
    try:
        return uuid.UUID(ref_id)
    except (AttributeError, TypeError, ValueError):
        return None


def _find_candidate(candidates: list[dict], ref_id: str) -> dict | None:
    wanted = str(ref_id)
    return next(
        (c for c in candidates if str(c.get("ref_id") or "") == wanted), None
    )


def _agent_label(row: dict | None, agent: Agent | None, ref_id: str) -> str:
    """A display name for the expected agent, from whichever source has one.

    **The agent row first, the trace second.** The reverse order was tried and
    is wrong: a candidate's ``name`` is whatever the router labelled that
    candidate with, and for an identity contact route that is the *contact's*
    name, not the agent's — so a verdict about an agent id came back saying
    "someone@example.com was considered and then excluded", conflating a person
    with an agent. The caller asked about an agent id; the sentence names that
    agent. The trace's label is the fallback for an agent row that no longer
    exists, where it is the only name anybody has.
    """
    if agent is not None and (agent.name or "").strip():
        return agent.name
    if row is not None:
        name = str(row.get("name") or "").strip()
        if name:
            return name
    return str(ref_id)


def _owner_email(db: DBSession, row: dict | None, agent: Agent | None) -> str | None:
    if row is not None and row.get("owner_email"):
        return str(row["owner_email"])
    if agent is None:
        return None
    owner = db.get(User, agent.owner_id)
    return owner.email if owner is not None else None


def _skip_explanation(reason: str, *, kind: str, channel: bool) -> tuple[str, str]:
    """The (explanation, action) pair for one recorded ``skip_reason``.

    Narrowest table first: channel+agent, then channel, then the App MCP /
    origin-neutral base. The fallback is what makes all three safe to leave
    incomplete — an unmapped reason is reported by name with an honest "this
    build has no explanation for it", which is a worse diagnosis but never a
    wrong one.
    """
    if channel:
        if kind == routing_trace.KIND_AGENT:
            override = _CHANNEL_AGENT_SKIP_EXPLANATIONS.get(reason)
            if override is not None:
                return override
        override = _CHANNEL_SKIP_EXPLANATIONS.get(reason)
        if override is not None:
            return override
    return _SKIP_EXPLANATIONS.get(
        reason,
        (
            f"it was excluded with reason '{reason or 'unspecified'}', "
            f"which this build has no explanation for",
            "Read the candidate row below and the router's logs — this "
            "reason was added after the diagnosis was written.",
        ),
    )


def _channel_pass_2_block(
    db: DBSession, trace: RoutingDecisionPublic
) -> str | None:
    """Which channel setting stops Pass 2 for this sender — **as it stands now**.

    Returns :data:`_PASS_2_BLOCK_SCOPE_USER`,
    :data:`_PASS_2_BLOCK_SCOPE_DEFAULT`, :data:`_PASS_2_BLOCK_AUTO_INSTALL`, or
    ``None`` for "nothing in the channel's policy bars the catalog pass, or the
    policy cannot be resolved at all".

    ``ChannelPolicyService.describe`` rather than ``.resolve``, for one field:
    ``agent_scope_inherited``. ``agent_scope`` is inheritable, so a restricted
    scope is the admin's default about as often as it is the sender's own
    choice, and a remedy naming the wrong screen is the confidently-wrong
    diagnosis this module exists to refuse. ``describe`` computes that bit
    already and ``resolve`` throws it away, so this costs nothing beyond the
    call.

    ``allow_auto_install`` needs no such split: it is a column on
    ``ServerChannel`` with no per-user override at all, so there is only ever
    one screen to name.

    **The fact comes from the resolved policy, not from the trace.**
    ``ChannelRoutingService._record_pass_2_not_run`` writes a perfectly good
    note saying exactly this, into ``StageTrace.reason`` — and ``reason`` is
    deliberately absent from ``routing_trace.SAFE_STAGE_FIELDS``, so with
    ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` off the note never reaches the
    projection this module reads. A verdict that depended on it would be
    correct on some servers and silently generic on others, which is the worst
    of the three available behaviours. Giving ``StageTrace`` an allowlisted,
    server-authored "this pass did not run" code is the right structural fix
    and it is Phase 6's; until then this re-reads the source.

    **What that costs, stated rather than assumed.** A verdict is computed when
    somebody *reads* a trace, which can be long after the decision. Re-resolving
    the policy here therefore describes the configuration **as it stands now**,
    not as it stood at decision time — so a channel whose auto-install was on
    when the message arrived and has since been switched off is diagnosed as
    "switched off", and a Pass 2 that genuinely ran and found nothing is
    reported as a Pass 2 that could not run.

    That is judged correct for a *diagnosis*: this verdict's job is "what do I
    change to fix this", and the answer to that question is about today's
    configuration, not about a state nobody can act on any more. It is not
    left as a silent assumption — the sentences themselves say "as this
    channel's settings stand right now", and both actions carry
    :data:`READ_TIME_POLICY_CAVEAT`. The reader is told which clock they are
    reading.

    **Four ways the policy cannot be resolved, all answered ``None``** (the
    base sentence, i.e. the behaviour before this branch existed):

    - ``channel_id is None`` — a hand-typed ``POST /admin/routing/simulate``
      that named no channel. There is no ``ServerChannel`` row to resolve
      against, and that run really did decide under
      ``ResolvedChannelPolicy.for_no_channel`` — whose ``allow_auto_install``
      is ``True`` and whose scope is ``"all"`` — so the base sentence's
      auto-install remedy is *true* for it. This branch is checked first, and
      it is why nothing here defaults to a permissive policy of its own
      invention; see that classmethod's docstring on why a default would be a
      second, silently permissive policy source.
    - ``user_id is None`` — the sender's account was deleted
      (``RoutingDecision.user_id`` is ``SET NULL``). A policy is a fact about a
      person and a channel; with no person there is nothing to resolve.
    - The channel row is gone. ``RoutingDecision.channel_id`` is
      ``ON DELETE CASCADE``, so this is near-unreachable — the trace would have
      gone with it — but a read path does not get to assume that.
    - The resolution raised. Caught here rather than left to
      :meth:`RoutingReachabilityService.diagnose`'s catch-all, which would cost
      the whole diagnosis (the candidate table and its skip reasons included)
      to lose one clause of one sentence. Degrading to the base sentence is the
      pre-existing behaviour, which is coarse but never wrong.

    Scope is checked **before** ``allow_auto_install``, and the order is the
    diagnosis. Both bar Pass 2 identically, so either sentence would be true
    when both are set — but a restricted scope also invalidates the *other*
    verdict's remedy, because under it giving this sender a new agent with a
    trigger prompt does not make them routable either. Reporting the
    auto-install switch first would send the reader to fix one thing and come
    back to a channel that still routes nothing. Same order, same reason, as
    ``ChannelRoutingService._record_pass_2_not_run``'s note vocabulary.

    A pin is **not** an answer here, and the reason is the one seam in an
    otherwise read-time argument, so it is stated rather than glossed. A
    *pinned decision* never reaches this branch — it is settled above by
    :func:`_general_verdict`'s ``MATCH_PINNED`` branch, and it leaves a
    candidate row behind besides, so ``not candidates`` is false for it. But
    that is a fact about the decision, and this function is otherwise about
    *now*: a pin set **since** the decision is a read-time state with no answer
    here, and the verdict will tell an admin to widen a scope on a channel that
    would today route by pin. Accepted, because the pin is visible on the same
    settings screen both remedies already send the reader to, and because
    inventing a fourth sentence for it would describe a decision nobody is
    looking at.
    """
    if trace.channel_id is None or trace.user_id is None:
        return None
    try:
        channel = db.get(ServerChannel, trace.channel_id)
        if channel is None:
            return None
        view = ChannelPolicyService.describe(db, channel, trace.user_id)
    except Exception:  # noqa: BLE001 — one clause is not worth the diagnosis
        logger.warning(
            "Could not resolve channel policy for trace diagnosis (channel=%s)",
            trace.channel_id,
            exc_info=True,
        )
        return None
    if view.policy.agent_scope != CHANNEL_AGENT_SCOPE_ALL:
        return (
            _PASS_2_BLOCK_SCOPE_DEFAULT
            if view.agent_scope_inherited
            else _PASS_2_BLOCK_SCOPE_USER
        )
    if not view.policy.allow_auto_install:
        return _PASS_2_BLOCK_AUTO_INSTALL
    return None


def _is_channel_origin(origin: str | None) -> bool:
    """Did this decision route over the sender's own agents?

    See :data:`_CHANNEL_ORIGINS`, which also says where an unknown origin lands
    and why that is the safe end.
    """
    return (origin or "") in _CHANNEL_ORIGINS


def _has_router_wording(agent: Agent) -> bool:
    """Anything for the classifier to match on — the channel eligibility test.

    ``example_prompt_text`` is **called**, not restated: it is the same
    predicate ``ChannelCandidateProvider`` applies when it builds a ballot,
    including the parts that are not obvious (a non-list column yields nothing;
    ``[""]`` is not examples). A second copy would drift the first time either
    side was tuned, and it would drift *silently* — this module would go on
    saying "it has neither" about an agent the provider was happily admitting,
    which is precisely the class of wrong-but-confident diagnosis the module
    docstring exists to forbid.

    It used to be ``_example_text``, reached across a service boundary and
    flagged here as borrowed rather than laundered, with the standing condition
    "if a third caller appears, promote it". Channel policy produced that third
    caller — ``ChannelRoutingService._route_pinned_agent`` needs the identical
    predicate for the pinned agent's trace row — so it was promoted in the same
    change, rather than leaving this paragraph instructing against the tree.

    The agent-level pair, never a route's copy: ``Agent.example_prompts`` is the
    SSOT channel routing reads, and borrowing examples from whatever route
    happened to exist would leave standalone agents — the broken case — looking
    like they have none.
    """
    return bool(
        (agent.router_trigger_prompt or "").strip()
        or example_prompt_text(agent.example_prompts)
    )


def _count(n: int, noun: str) -> str:
    """``"3 eligible candidates"`` / ``"1 eligible candidate"``.

    Pluralised rather than "1 eligible candidate(s)": this sentence is the feature
    for the motivating case and it is read by a person.
    """
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _reasons(candidates: list[dict]) -> str:
    """``"foreign_owner, route_inactive"`` — the skip reasons, deduped, sorted."""
    reasons = sorted(
        {
            str(c.get("skip_reason") or "unspecified")
            for c in candidates
            if not c.get("eligible")
        }
    )
    return ", ".join(reasons) or "no reason recorded"
