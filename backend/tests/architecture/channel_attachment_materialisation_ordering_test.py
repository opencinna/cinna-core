"""
Architecture guard — attachment materialisation ordering
(docs/drafts/channel-message-attachments_plan.md §4.1 / §5.8).

WHY THIS EXISTS
---------------
Plan §4.1 is the entire security story of the channel-attachments feature:
nothing is fetched or stored before the sender is admitted. Concretely,
inside ``ChannelInboundService.process_inbound``, the call to
``ChannelAttachmentService.materialize`` (step 6.5) must appear textually
AFTER the whitelist gate (step 4), the user-resolution gate (step 5), and the
channel-policy gate (step 6) — each of which can end the request with an
early return, and none of which re-runs once passed.

This is an *ordering* property, not a structural one (no import graph, no
caller allowlist would catch it), so it survives a refactor only if
something checks it. A future edit that moved the attachment call earlier —
"while we're here, let's materialise before the policy check so the
skip-count shows up in the whitelist-miss debug entry too" — would silently
reopen exactly the hazard §4.1 closes: an unadmitted sender spending the
deployment's disk and quota before anything has decided they may.

HOW IT WORKS
------------
Parses ``channel_inbound_service.py``, finds the ``process_inbound`` method,
and locates the first call-site line number of each of the four named calls
within it. The four gates are sequential statements at the SAME nesting
level in the real source (each earlier gate returns early on failure rather
than nesting the rest of the function inside an ``if``), so a plain line-
number comparison is sufficient and does not need to reason about control
flow — verified against the real file by
``test_the_four_calls_are_actually_present_and_distinct``, which is this
file's own non-vacuousness check.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

APP_ROOT = pathlib.Path(__file__).parent.parent.parent / "app"
_INBOUND_MODULE = (
    APP_ROOT / "services" / "server_channels" / "channel_inbound_service.py"
)

_METHOD_NAME = "process_inbound"

# (label, dotted call name) in the order plan §4.1 requires them to appear.
# The dotted name is what `_call_name` below produces for a Call node —
# either "Attribute.attr" for a `Class.method(...)` call, or a bare name for
# a plain function call.
_GATES_IN_REQUIRED_ORDER = [
    ("whitelist (step 4)", "match_email_pattern"),
    ("user resolution (step 5)", "ChannelInboundService._resolve_user"),
    ("channel policy (step 6)", "ChannelPolicyService.describe"),
    ("attachment materialisation (step 6.5)", "ChannelAttachmentService.materialize"),
]


def _call_name(node: ast.Call) -> str | None:
    """``foo(...)`` -> ``"foo"``; ``Foo.bar(...)`` -> ``"Foo.bar"``; anything
    else (a call through a more complex expression) -> ``None``, which never
    matches any entry in ``_GATES_IN_REQUIRED_ORDER`` and is simply skipped."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    if isinstance(func, ast.Name):
        return func.id
    return None


def _find_method(tree: ast.AST, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(
        f"{name!r} not found in {_INBOUND_MODULE} — the ordering guard has "
        "nothing to check. Either the method was renamed (update this test) "
        "or the file failed to parse."
    )


def _first_call_lineno(method_node: ast.AST, target_name: str) -> int | None:
    lines = [
        node.lineno
        for node in ast.walk(method_node)
        if isinstance(node, ast.Call) and _call_name(node) == target_name
    ]
    return min(lines) if lines else None


def _process_inbound_node() -> ast.AST:
    tree = ast.parse(_INBOUND_MODULE.read_text(encoding="utf-8"))
    return _find_method(tree, _METHOD_NAME)


# ── Tests ────────────────────────────────────────────────────────────────


def test_the_four_calls_are_actually_present_and_distinct() -> None:
    """Non-vacuousness check: if any of the four named calls stops being
    findable (renamed, refactored into a helper, wrapped differently), the
    ordering test below would either silently stop checking anything (all
    linenos ``None``, comparison vacuously true) or start comparing the
    wrong things. Fail loudly here instead, naming which one went missing."""
    method_node = _process_inbound_node()
    linenos = {
        label: _first_call_lineno(method_node, name)
        for label, name in _GATES_IN_REQUIRED_ORDER
    }
    missing = [label for label, lineno in linenos.items() if lineno is None]
    assert not missing, (
        f"Could not find a call site for: {missing} inside "
        f"{_METHOD_NAME}(). The ordering guard in this file's other test "
        "cannot check anything until this is fixed — either the call was "
        "renamed/refactored (update _GATES_IN_REQUIRED_ORDER) or removed "
        "(which would itself be the security regression §4.1 exists to "
        "prevent)."
    )
    assert len(set(linenos.values())) == len(linenos), (
        f"Two of the four gate calls resolved to the SAME line number: "
        f"{linenos} — the ordering comparison below needs them distinct."
    )


def test_materialize_is_called_after_the_whitelist_user_resolution_and_policy_gates() -> None:
    method_node = _process_inbound_node()
    linenos = [
        (label, _first_call_lineno(method_node, name))
        for label, name in _GATES_IN_REQUIRED_ORDER
    ]
    for (label, lineno) in linenos:
        assert lineno is not None, (
            f"{label} call site not found — see "
            "test_the_four_calls_are_actually_present_and_distinct for the "
            "detailed failure."
        )

    ordered = [lineno for _label, lineno in linenos]
    assert ordered == sorted(ordered), (
        "ChannelAttachmentService.materialize is not textually ordered "
        "after the whitelist, user-resolution and channel-policy gates "
        f"inside process_inbound(). Found line numbers in source order: "
        f"{linenos}. This is plan §4.1's entire security story: nothing may "
        "be fetched or stored before the sender is admitted. If this "
        "changed on purpose, it needs a security review, not a test update."
    )
