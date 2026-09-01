"""Every terminal stream event carries turn identity — all five emission sites.

``agent_message_id`` in the meta of ``STREAM_COMPLETED`` / ``STREAM_ERROR`` /
``STREAM_INTERRUPTED`` is what lets an outbound consumer deliver *this turn's*
message instead of the newest agent row in the session. The contract has two
halves, and the interesting one is the second:

1. **The key is always present.** A consumer distinguishes "the key is absent"
   (a legacy event, keep the old newest-row behaviour) from "the key is
   present and ``None``" (this turn wrote nothing — deliver nothing). An
   emission site that simply forgot the key does not fail loudly; it silently
   re-enables the bug on that path, on that path only, and looks fine
   everywhere else. That is a drift hazard, so it is guarded structurally.
2. **An explicit ``None`` survives the trip.** ``_emit_activity_event``
   splats ``**extra_meta`` into the event's meta, and a plausible-looking
   "tidy up empty values" change there would erase the difference between the
   two halves of that distinction for every site at once.

So this file has two shapes of test. The first reads
``sessions/message_service.py``'s own syntax tree and asserts the property
about the source that no runtime test of one path can assert about all five
(the alternative — driving ``stream_message_with_events`` five ways — needs a
database, an environment and a connector, which makes it an integration test
and puts it in ``tests/api/``). The second is an ordinary runtime unit test of
the one helper every site funnels through.

Where each site's *behaviour* is proven end to end:

* command stream (``None``) → ``tests/api/server_channels/
  server_channels_turn_identity_test.py`` — the headline reproducer;
* LLM ``STREAM_COMPLETED`` (an id, and ``None`` for a batch with no storable
  events) → the same file, the Google Chat and email scenarios;
* ``STREAM_INTERRUPTED`` → the interrupted scenarios there, and
  ``tests/api/server_channels/server_channels_stop_command_test.py``;
* ``STREAM_ERROR`` → ``server_channels_streaming_updates_test.py``'s
  mid-stream failure.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from typing import Any
from unittest import mock

from app.services.sessions import message_service

#: The events an outbound consumer treats as "this turn is over". Every
#: emission of one of these has to say which agent message the turn wrote.
_TERMINAL = {"STREAM_COMPLETED", "STREAM_ERROR", "STREAM_INTERRUPTED"}

#: The local the command-stream site passes as its event type — one call site
#: that dispatches all three terminal events through a variable, which is why
#: the scan below cannot key on ``EventType.X`` attributes alone.
_DISPATCH_LOCALS = {"post_event_type"}

_META_KEY = "agent_message_id"


def _emissions() -> list[tuple[ast.Call, str]]:
    """Every ``_emit_activity_event(...)`` call in ``message_service``, labelled.

    The label is the event type as written at the call site:
    ``"STREAM_ERROR"`` for a literal ``EventType.STREAM_ERROR`` (or for the
    equivalent bare string ``"stream_error"``, uppercased so both spellings
    land in ``_TERMINAL``), or the local's name for the command-stream site
    that picks its type at runtime.

    **Every call gets a label — an unrecognised shape fails here, loudly.**
    The first version recorded only ``Attribute`` and ``Name`` first args and
    silently dropped the rest, which left the guard blind to a sixth site
    spelled ``_emit_activity_event("stream_completed", …)`` (a valid shape)
    or dispatching through a local this file has not heard of: both kept
    ``len(terminal) == 5`` true while an unguarded emission shipped.
    """
    source = Path(inspect.getfile(message_service)).read_text()
    found: list[tuple[ast.Call, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "_emit_activity_event"):
            continue
        # The event type is the first positional at every terminal site, but
        # at least one non-terminal caller (the TODO_LIST_UPDATED emission)
        # passes everything by keyword — same expression shapes, different
        # spelling, equally in scope for the loud-failure rule below.
        if node.args:
            first: ast.expr | None = node.args[0]
        else:
            first = next(
                (kw.value for kw in node.keywords if kw.arg == "event_type"),
                None,
            )
        assert first is not None, (
            f"no event type found on _emit_activity_event at line {node.lineno}"
        )
        if isinstance(first, ast.Attribute):
            found.append((node, first.attr))
        elif isinstance(first, ast.Name):
            label = first.id
            assert label in _DISPATCH_LOCALS, (
                f"_emit_activity_event dispatches through local {label!r} at "
                f"line {node.lineno}, which this guard does not know — add it "
                "to _DISPATCH_LOCALS so its terminal emissions are checked"
            )
            found.append((node, label))
        elif isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append((node, first.value.upper()))
        else:
            raise AssertionError(
                f"unrecognised event-type expression at line {node.lineno}: "
                f"{ast.dump(first)} — teach _emissions() this shape rather "
                "than letting the drift guard skip the call"
            )
    return found


def _keyword(call: ast.Call, name: str) -> ast.keyword | None:
    return next((kw for kw in call.keywords if kw.arg == name), None)


def _meta_value(call: ast.Call) -> ast.expr | None:
    """The value a site passes under the turn-identity key — or ``None``.

    The required shape is ``**{AGENT_MESSAGE_ID_META_KEY: <value>}``: a
    dict-splat whose single key is the **shared constant by name**, imported
    from ``app.models.events.event``. That shape is itself part of the
    contract — a site spelling the key as a literal keyword or a literal
    string re-splits the producer and consumer into two spellings joined only
    by a string, which fails silently on a rename (the consumer reads an
    unknown key as a legacy event and falls back to the newest row). So a
    site using any other spelling is reported as missing, on purpose.
    """
    for kw in call.keywords:
        if kw.arg is not None or not isinstance(kw.value, ast.Dict):
            continue
        if len(kw.value.keys) != 1:
            continue
        key = kw.value.keys[0]
        if isinstance(key, ast.Name) and key.id == "AGENT_MESSAGE_ID_META_KEY":
            return kw.value.values[0]
    return None


def test_every_terminal_stream_emission_names_the_turns_agent_message() -> None:
    """The drift guard: a new terminal emission must carry turn identity.

    Five sites today — the LLM batch's ``STREAM_COMPLETED``, its
    ``STREAM_INTERRUPTED``, the mid-stream ``STREAM_ERROR``, the outer
    ``STREAM_ERROR``, and the command stream's single dispatching site (which
    is all three event types at once). A sixth added without the key would
    re-open the stale-turn bug on that path alone, against an otherwise green
    suite, because a consumer reads a missing key as "legacy event — use the
    newest agent row".

    The scan's own reachability is asserted first: a scan that matched nothing
    would satisfy "every terminal emission carries the key" vacuously and
    forever.
    """
    emissions = _emissions()
    assert emissions, "the AST scan found no _emit_activity_event calls at all"

    labels = [label for _call, label in emissions]
    # The scan sees both shapes of call site — literal event types and the
    # command stream's dispatching local. Missing either would make the guard
    # blind to a whole class of site.
    assert _TERMINAL & set(labels), labels
    assert _DISPATCH_LOCALS & set(labels), labels

    terminal = [
        (call, label)
        for call, label in emissions
        if label in _TERMINAL or label in _DISPATCH_LOCALS
    ]
    assert len(terminal) == 5, [label for _c, label in terminal]

    missing = [label for call, label in terminal if _meta_value(call) is None]
    assert missing == [], (
        f"terminal stream emissions without {_META_KEY!r} via the shared "
        f"AGENT_MESSAGE_ID_META_KEY constant: {missing}. A consumer reads an "
        "absent key as a legacy event and falls back to the newest agent "
        "message in the session, which is the bug turn identity exists to "
        "close — and a site spelling the key any other way re-splits the two "
        "sides into spellings that drift silently."
    )


def test_the_emitter_uses_the_shared_symbol_not_a_lookalike() -> None:
    """The name at the call sites is the ONE shared constant, not a copy.

    ``_meta_value`` matches on the identifier ``AGENT_MESSAGE_ID_META_KEY``,
    which a same-named local defined inside ``message_service`` would satisfy
    while re-splitting the producer and consumer into two spellings — the
    exact silent-rename failure the shared symbol exists to close. Identity,
    not equality, is the assertion: the emitter's name must BE the models
    constant. The consumer's re-export and this file's own ``_META_KEY``
    literal are pinned against it in the same breath.
    """
    from app.models.events import event as event_model
    from app.services.server_channels import channel_outbound_service

    assert (
        message_service.AGENT_MESSAGE_ID_META_KEY
        is event_model.AGENT_MESSAGE_ID_META_KEY
    )
    assert (
        channel_outbound_service.AGENT_MESSAGE_ID_META_KEY
        is event_model.AGENT_MESSAGE_ID_META_KEY
    )
    assert message_service.AGENT_MESSAGE_ID_META_KEY == _META_KEY


def test_the_command_stream_says_none_literally_rather_than_omitting_it() -> None:
    """A command turn's ``None`` is a statement, not an omission.

    A command stream writes one ``role="system"`` message and never an agent
    row, so there is genuinely nothing to name — and that is exactly the case
    where leaving the key out would send a consumer to the newest agent row
    and re-deliver the previous turn's answer as if it were the command's
    result. The literal ``None`` is the difference between saying "nothing"
    and saying nothing.
    """
    sites = [
        call for call, label in _emissions() if label in _DISPATCH_LOCALS
    ]
    assert len(sites) == 1, sites
    value = _meta_value(sites[0])
    assert value is not None
    assert isinstance(value, ast.Constant) and value.value is None, ast.dump(value)


def test_the_llm_sites_pass_the_id_they_hold_or_none() -> None:
    """The four LLM-path sites stringify whatever row the batch wrote.

    Not a style check: the meta crosses a process boundary as JSON, so a raw
    ``UUID`` would either fail to serialise or arrive as something the
    consumer's ``uuid.UUID(str(...))`` guard has to rescue. Every site spells
    it the same way — ``str(x) if x else None`` — and that sameness is the
    point, because the consumer has one parse for all of them.
    """
    llm_sites = [call for call, label in _emissions() if label in _TERMINAL]
    assert len(llm_sites) == 4, len(llm_sites)
    for call in llm_sites:
        value = _meta_value(call)
        assert value is not None
        assert isinstance(value, ast.IfExp), ast.dump(value)
        # ``str(...)`` on the truthy side, a literal ``None`` on the other.
        assert isinstance(value.body, ast.Call), ast.dump(value)
        assert isinstance(value.body.func, ast.Name)
        assert value.body.func.id == "str"
        assert isinstance(value.orelse, ast.Constant)
        assert value.orelse.value is None


def _emitted_meta(**extra: Any) -> dict[str, Any]:
    """Run ``_emit_activity_event`` and hand back the meta it published."""
    import uuid

    captured: dict[str, Any] = {}

    async def _emit(*, event_type: Any, model_id: Any, meta: dict, user_id: Any):
        captured.update(meta)

    with mock.patch(
        "app.services.events.event_service.event_service.emit_event",
        side_effect=_emit,
    ):
        asyncio.run(
            message_service._emit_activity_event(
                "stream_completed",
                uuid.uuid4(),
                uuid.uuid4(),
                "conversation",
                uuid.uuid4(),
                **extra,
            )
        )
    return captured


def test_an_explicit_none_reaches_the_meta_as_a_present_key() -> None:
    """The runtime half: ``None`` must survive the ``**extra_meta`` splat.

    Present-and-``None`` and absent are opposite instructions to every
    consumer, and both of them pass through this one helper. A change here
    that dropped empty values — the kind of tidying that reads as harmless —
    would silently convert every "this turn wrote nothing" into "this is a
    legacy event, use the newest agent row", on all five sites at once. The
    two halves are asserted as a pair so the distinction is the thing under
    test rather than either value alone.
    """
    with_none = _emitted_meta(agent_message_id=None)
    assert _META_KEY in with_none, with_none
    assert with_none[_META_KEY] is None

    without = _emitted_meta()
    assert _META_KEY not in without, without
