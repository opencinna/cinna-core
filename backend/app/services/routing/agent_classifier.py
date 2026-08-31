"""One classifier for every routing consumer.

Channel Pass 1, Channel Pass 2, App MCP Stage 1 and Identity Stage 2 all ask the
same question — *which of these candidates should handle this message* — and
until this module existed each of them hand-built its own list of candidate
dicts before handing them to the same renderer. Three near-copies of one
structure is how a field gets dropped in one of them and nobody notices:

- ``AppMCPRoutingService._ai_classify`` passed ``prompt_examples`` through, and
  the renderer then ignored it (plan §2, Bug 1) — validated, stored, documented
  as a routing aid, and silently discarded before the prompt was built.
- ``IdentityRoutingService._ai_classify`` never even collected it, although
  ``IdentityAgentBinding.prompt_examples`` exists and is edited on the same
  screen as the App MCP one.
- ``ChannelRoutingService._route_catalog_in_thread`` builds bundle candidates,
  which have no examples at all, so the omission looked normal there.

**The point of this module is that there is exactly one renderer and exactly one
parser.** A candidate field added to :class:`Candidate` reaches every consumer
or none of them, and a prompt-contract change is made in one place.

Prompt-template length is **no longer load-bearing for privacy.** It used to be:
``app_agent_router_prompt.md`` overran ``TRACE_TEXT_MAX_CHARS`` before the
``## User Message`` section was appended, which was the only reason
``stages[].prompt`` did not contain the sender's words. Phase 2's write-gate
(``ROUTING_TRACE_STORE_MESSAGE_TEXT`` gating the *write*, not only the read)
removed that dependency deliberately, so editing the template is safe — but if
anyone ever reverts to gating on the read path only, template length becomes
load-bearing again and an ordinary prompt edit silently reintroduces a full-text
leak. See plan §7's write-gate paragraph before shortening it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.routing import routing_trace

logger = logging.getLogger(__name__)


def get_provider_manager():
    """The AI-function provider cascade — deliberately a module-level name here.

    Two jobs, and both are the reason this is a wrapper rather than a top-level
    ``from app.agents.provider_manager import get_provider_manager``:

    1. **It breaks an import cycle.** ``app.agents.__init__`` imports
       ``app_agent_router``, which imports this module; a module-level import of
       anything under ``app.agents`` here would close that loop and fail on a
       cold import of either side.
    2. **It keeps the provider seam patchable at classifier depth.** Routing
       tests patch the provider one layer below ``route_to_agent`` so the real
       render/parse path runs and genuinely populates ``stages[].prompt`` and
       ``stages[].raw_response`` before the message-text gate is exercised
       against them (see ``routing_message_text_gating_test.py``). A
       function-local import would have no module attribute to patch, and that
       test would quietly go back to proving nothing.
    """
    from app.agents.provider_manager import get_provider_manager as _get_manager

    return _get_manager()


#: The rendered template lives with the other agent prompts, next to the module
#: that used to own this code. Resolved from ``app/`` rather than imported from
#: ``app.agents.app_agent_router`` so this module has no import cycle with the
#: package whose ``__init__`` re-exports that module.
PROMPT_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "agents" / "prompts" / "app_agent_router_prompt.md"
)

#: Mirrors the ``prompt_examples`` validation enforced at write time in
#: ``api/routes/identity.py`` for ``IdentityAgentBinding`` (2000 chars / 10
#: non-empty lines) — the one surviving write-time validator on a routing
#: candidate's text. Re-applied at render time rather than trusted, because a
#: candidate can also be built from a bundle revision or a stored row written
#: before the validator existed.
#:
#: The line limit was enforced here from the start; the character limit was
#: only ever *documented* by this comment, which held while every path into a
#: candidate was bounded at write time by one of the route-layer validators
#: that existed then (the App MCP route family, now deleted, plus the
#: surviving ``IdentityAgentBinding`` one above).
#: ``ChannelCandidateProvider`` opened the first unbounded one:
#: ``Agent.example_prompts`` is a user-editable JSON column with no validator
#: anywhere, so a single pasted 500KB line would have reached the model
#: verbatim — ten lines of it is still ten lines.
#:
#: Applied to the raw text *before* splitting, which is what the write-time
#: validators check (``len(value) > 2000`` on the whole field) and which also
#: bounds the split itself: a megabyte of newlines would otherwise build a
#: million-element list on the way to taking ten of them.
#:
#: Render-time is the **only** home this limit gets. A fourth copy on the
#: model or route layer could not reach rows already in the database, which is
#: precisely the case here.
MAX_EXAMPLE_CHARS = 2000
MAX_EXAMPLE_LINES = 10


@dataclass(frozen=True)
class Candidate:
    """One thing the classifier may choose.

    ``ref_id`` is an agent id for every stage except Pass 2, where it is a
    bundle id — the classifier does not care which, and the caller maps the
    chosen ``ref_id`` back to its own object.
    """

    ref_id: str
    name: str
    trigger_prompt: str = ""
    prompt_examples: str | None = None


@dataclass
class ClassificationResult:
    """What the classifier made of the model's reply.

    ``agent_id`` (not ``ref_id``) keeps the name every existing consumer and
    test already reads, including Pass 2, where it has always carried a bundle
    id.

    ``confidence`` / ``reason`` / ``runner_up_id`` are **recorded, never acted
    on** (plan §8). Gating a route on a confidence score is a separate,
    data-backed decision that the traces this feature stores are meant to
    inform; doing it here would be the guess the traces exist to replace.
    """

    agent_id: str
    transformed_message: str | None = None
    confidence: float | None = None
    reason: str | None = None
    runner_up_id: str | None = None


# --- Prompt rendering -------------------------------------------------------


def _load_template() -> str:
    try:
        return PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(
            f"Failed to load prompt template from {PROMPT_TEMPLATE_PATH}: {e}"
        )


def _example_lines(raw: str | None) -> list[str]:
    """Non-empty example lines, bounded. Total — a bad value renders nothing.

    Bounded on **both** axes the comment on the constants promises: the text is
    clamped to ``MAX_EXAMPLE_CHARS`` before it is split, then to
    ``MAX_EXAMPLE_LINES`` non-empty lines. Truncation is silent by design — the
    alternative is dropping a candidate out of a routing decision because its
    examples were long, which trades a trimmed prompt for a routing failure.
    """
    if not raw:
        return []
    try:
        lines = [line.strip() for line in str(raw)[:MAX_EXAMPLE_CHARS].splitlines()]
    except Exception:  # noqa: BLE001
        return []
    return [line for line in lines if line][:MAX_EXAMPLE_LINES]


def _render_candidate(candidate: Candidate) -> str:
    block = (
        f"- **ID**: {candidate.ref_id}\n"
        f"  **Name**: {candidate.name}\n"
        f"  **Description**: {candidate.trigger_prompt}"
    )
    examples = _example_lines(candidate.prompt_examples)
    if examples:
        rendered = "\n".join(f"    - {line}" for line in examples)
        block += f"\n  **Example messages**:\n{rendered}"
    return block


def render_prompt(candidates: list[Candidate], message: str) -> str:
    """The full classifier prompt, examples included.

    Bug 1 lived in this function's predecessor: it built the agent block from
    ``id`` / ``name`` / ``trigger_prompt`` and dropped ``prompt_examples`` on the
    floor. Examples are rendered as their own labelled sub-list rather than
    appended to the description so the model can tell an owner's *instruction*
    from an owner's *sample message*.
    """
    agents_section = "\n".join(_render_candidate(c) for c in candidates)
    return f"""{_load_template()}

---

## Available Agents

{agents_section}

---

## User Message

{message}

---

Return JSON only:
"""


# --- Defensive field extraction ---------------------------------------------
#
# Every extractor below returns ``None`` rather than raising or rejecting the
# whole reply. Plan §8: local and small models routinely ignore fields added to
# a JSON contract, and a strict parse would convert a *tuning* feature into a
# routing outage. The classifier's contract is therefore: ``agent_id`` decides,
# everything else is best-effort colour.


def _parse_confidence(value: Any) -> float | None:
    """A 0.0–1.0 score, or ``None``.

    A value outside 0–1 is **dropped, not rescaled**. A model answering ``85``
    probably means 85%, but "probably" is how a diagnostic starts stating things
    it does not know: recording 0.85 from an ``85`` would put a fabricated
    number in a column an operator reads as the model's own answer. ``None``
    says "the model gave us nothing usable", which is true.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # NaN and the infinities are floats and would serialize into JSONB as
    # something no consumer expects.
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if 0.0 <= number <= 1.0:
        return number
    return None


def _parse_reason(value: Any) -> str | None:
    """The model's one-line justification, or ``None``.

    **This is sender-derived text.** A model asked why it picked an agent quotes
    the message back, so ``stages[].reason`` is a rewrite of what the sender
    wrote — exactly like ``raw_response`` — and it is gated accordingly (it was
    removed from ``SAFE_STAGE_FIELDS`` in the same change that started
    populating it from the model).
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _parse_runner_up(value: Any, known_ids: set[str]) -> str | None:
    """The second-place candidate id, or ``None``.

    Validated against the candidate set rather than merely against UUID shape:
    a runner-up that names nothing on the ballot is not a diagnosis, it is a
    field the tuning card would render as a dangling reference. Allowlist, not
    a format check — the same reasoning as ``SAFE_STAGE_FIELDS``.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped.upper() == "NONE":
        return None
    return stripped if stripped in known_ids else None


def _parse_transformed_message(value: Any, message: str) -> str | None:
    """The core task with any routing prefix stripped, or ``None``.

    Sanity guards preserved verbatim from the original router: a rewrite that is
    empty, identical to the input, or more than twice its length is the model
    inventing content rather than stripping a prefix.
    """
    if not value or not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped == message or len(stripped) > 2 * len(message):
        return None
    return stripped


def _strip_code_fences(raw: str) -> str:
    if not raw.startswith("```"):
        return raw
    return "\n".join(
        line for line in raw.splitlines() if not line.startswith("```")
    ).strip()


class AgentClassifier:
    """Render → call → parse → record, for every routing consumer."""

    @staticmethod
    def classify(
        candidates: list[Candidate],
        message: str,
        *,
        provider_kwargs: dict | None = None,
    ) -> ClassificationResult | None:
        """Pick the best candidate for ``message``, or ``None``.

        Returns ``None`` for every negative outcome — no candidates, a
        non-JSON reply, an explicit ``NONE``, a malformed id, or a provider
        cascade failure — and records *which* of those it was on the active
        routing trace. The caller decides what a ``None`` means for the request
        as a whole; this function never settles a trace's outcome.
        """
        if not candidates:
            return None

        try:
            prompt = render_prompt(candidates, message)

            # Debug, not info: the message is EXTERNAL user text on the channel
            # path, and the trace below is where it belongs.
            logger.debug(
                "[AIRouter] Classifying message=%r | %d candidates: %s",
                message[:120],
                len(candidates),
                ", ".join(f"{c.name} ({c.ref_id[:8]}…)" for c in candidates),
            )
            routing_trace.record_prompt(prompt)

            manager = get_provider_manager()
            response = manager.generate_content(prompt, **(provider_kwargs or {}))

            raw = response.text.strip()
            routing_trace.record_raw_response(raw)
            # The raw reply echoes a rewritten form of the user's message.
            logger.debug("[AIRouter] LLM raw response: %r", raw[:300])

            raw = _strip_code_fences(raw)

            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                # Debug, not warning: `raw` is the full unclamped reply, which
                # echoes the user's message — the same content the line above
                # was downgraded for, and the case most likely to fire on a
                # weak model.
                logger.debug("[AIRouter] Non-JSON response: %r", raw)
                routing_trace.record_parse_outcome(
                    reason="classifier reply was not JSON"
                )
                return None

            if not isinstance(data, dict):
                logger.debug("[AIRouter] JSON reply was not an object: %r", raw)
                routing_trace.record_parse_outcome(
                    reason="classifier reply was JSON but not an object"
                )
                return None

            agent_id = data.get("agent_id", "")
            if not agent_id or agent_id == "NONE":
                logger.info("[AIRouter] LLM returned NONE — no agent matched")
                routing_trace.record_parse_outcome(
                    reason="classifier chose NONE — no candidate fit the message"
                )
                return None

            known_ids = {c.ref_id for c in candidates}

            # Validate it looks like a UUID (basic check) — **unless the model
            # echoed back a ref that is literally on the ballot.**
            #
            # ``Candidate.ref_id`` is a plain ``str``, and not every provider
            # puts a UUID in it: ``IdentityCandidateProvider`` namespaces its
            # refs (``identity:{owner_id}``) precisely so a person cannot be
            # mistaken for an agent. A shape check alone would reject those,
            # turning "the model picked the person it was offered" into
            # "malformed reply". The allowlist arm is strictly wider than the
            # shape check — a reply that passed before still passes — and it is
            # the stronger of the two tests anyway: being on the ballot says
            # more than looking like a UUID does.
            #
            # ``isinstance`` is checked first and separately: a model can reply
            # with a list or an object here, and an unhashable value would make
            # the ``in`` test raise rather than reject.
            if not isinstance(agent_id, str) or (
                agent_id not in known_ids
                and not (len(agent_id) == 36 and agent_id.count("-") == 4)
            ):
                logger.warning("[AIRouter] Unexpected agent_id format: %r", agent_id)
                routing_trace.record_parse_outcome(
                    reason="classifier returned an agent_id that is not a UUID"
                )
                return None

            result = ClassificationResult(
                agent_id=agent_id,
                transformed_message=_parse_transformed_message(
                    data.get("message"), message
                ),
                confidence=_parse_confidence(data.get("confidence")),
                reason=_parse_reason(data.get("reason")),
                runner_up_id=_parse_runner_up(data.get("runner_up"), known_ids),
            )

            routing_trace.record_parse_outcome(
                reason=result.reason,
                confidence=result.confidence,
                runner_up_id=result.runner_up_id,
            )
            # Lift the score to the decision level too, so the tuning card's
            # list page can show it without opening every trace. ``note_``,
            # not ``record_outcome``: a stage picking something is not the
            # request finishing, and a later filter may still reject it — at
            # which point ``_settle_locked`` clears this the same way it clears
            # the selection.
            routing_trace.record_confidence(result.confidence)

            matched_name = next(
                (c.name for c in candidates if c.ref_id == agent_id), "?"
            )
            # Debug, not info: transformed_message is a rewrite of the user's
            # text.
            logger.debug(
                "[AIRouter] Result: agent=%s (%s) | transformed_message=%r | "
                "confidence=%s runner_up=%s",
                matched_name,
                agent_id,
                result.transformed_message[:120] if result.transformed_message else None,
                result.confidence,
                result.runner_up_id,
            )
            return result

        except Exception as e:
            logger.error("[AIRouter] Routing failed: %s", e, exc_info=True)
            routing_trace.record_error(e)
            return None
