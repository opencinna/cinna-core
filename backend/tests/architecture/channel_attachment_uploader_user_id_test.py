"""
Architecture guard — ``uploader_user_id`` caller set
(docs/drafts/channel-message-attachments_plan.md §5.5 / §5.8).

WHY THIS EXISTS
---------------
``uploader_user_id`` is the one deliberate authorization widening this
feature adds: ``MessageService.prepare_user_message_with_files`` normally
refuses a file whose ``user_id`` does not match the *session owner*, and this
parameter lets a caller say "no — this file belongs to someone else, and
that is expected" (plan §3.4/§5.5, the identity-routing case). The plan is
explicit that this is safe in exactly one place: ``channel_inbound_service
.py``'s ``_ingest``, which has *already enforced* ``user.id ==
binding.user_id`` before constructing the value it passes — the parameter
answers "who uploaded these bytes", never "may they".

A second call site that computes its OWN non-None value — anywhere else in
``app/`` — would be making that same safety claim without the invariant
``_ingest`` checks first. Today that would only be caught in review. This
test makes it a checked invariant: a new caller must either become a plain
passthrough of an already-received value (safe by construction, since it
manufactures nothing new) or edit this test explicitly, which is the point.

WHAT COUNTS AS "SAFE ANYWHERE" VS. "ORIGIN-ONLY"
-------------------------------------------------
Every ``uploader_user_id=<value>`` keyword found anywhere in ``app/`` is
classified by the shape of ``<value>``:

* the literal ``None`` — every signature's own default, spelled out
  explicitly. Safe anywhere.
* a bare ``Name`` node whose id is also ``uploader_user_id`` — a
  **passthrough**: the call is forwarding a value it already received under
  the same parameter name, not computing a new one. Both
  ``ChannelIngestionService.ingest_inbound_message`` (which forwards into
  ``SessionService.send_session_message``) and ``SessionService
  .send_session_message`` itself (which forwards into ``MessageService
  .prepare_user_message_with_files``) do exactly this — plan §5.5 calls this
  shape a "passthrough" for both signatures. Safe anywhere, because
  whatever it holds was already legitimate wherever it was first produced.
* **anything else** — an attribute access (``binding.user_id``), a
  conditional expression (``binding.user_id if file_ids else None`` — the
  ACTUAL shape ``_ingest`` uses today, deliberately narrowed so the widened
  ownership check only rides along with real files), a function call, a
  computed expression of any kind — is an ORIGINATING value. Allowed only in
  ``channel_inbound_service.py``.

The classifier does not special-case any particular AST node type as "must
be origin-only" — it only special-cases the two shapes that are safe
everywhere and treats every other shape, whatever it is, as requiring the
file check. A future refactor changing ``_ingest``'s conditional into some
other expression form is still caught correctly without this test needing an
update, as long as it stays in ``channel_inbound_service.py``; a *new
caller* anywhere else is caught regardless of what expression shape it uses.

See ``test_self_check_a_computed_value_from_a_disallowed_module_is_detected``
for a positive proof that the classifier can actually fail — a guard that
cannot be observed catching a violation is not a guard.
"""
from __future__ import annotations

import ast
import functools
import os
import pathlib

import pytest

# ── Constants ──────────────────────────────────────────────────────────────

APP_ROOT = pathlib.Path(__file__).parent.parent.parent / "app"

_ORIGIN_MODULE = (
    APP_ROOT / "services" / "server_channels" / "channel_inbound_service.py"
)

_KEYWORD_NAME = "uploader_user_id"

# Same pruning as the sibling channel_* architecture tests: env-templates
# ships ~10k vendored .py files that are never production callers.
_EXCLUDED_DIR_NAMES = frozenset(
    {"env-templates", ".venv", "site-packages", "__pycache__", "node_modules"}
)


# ── The classifier ───────────────────────────────────────────────────────


def _is_none_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_passthrough_name(node: ast.expr) -> bool:
    """``uploader_user_id=uploader_user_id`` — forwarding an already-received
    parameter of the identical name. Never manufactures a new value, so it
    is safe regardless of which module it appears in."""
    return isinstance(node, ast.Name) and node.id == _KEYWORD_NAME


def _is_safe_anywhere(node: ast.expr) -> bool:
    return _is_none_constant(node) or _is_passthrough_name(node)


def _uploader_user_id_keyword_values(tree: ast.AST) -> list[ast.expr]:
    """Every ``uploader_user_id=<value>`` keyword VALUE node found anywhere
    in ``tree`` — deliberately not restricted to any particular call target,
    so a future signature threading this parameter further is covered
    without needing to be named here."""
    values: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == _KEYWORD_NAME:
                    values.append(kw.value)
    return values


# ── File-walker (mirrors the sibling channel_* architecture tests) ────────


@functools.lru_cache(maxsize=1)
def _source_files() -> tuple[tuple[pathlib.Path, str], ...]:
    files: list[tuple[pathlib.Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(APP_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_NAMES]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            if filename.endswith("_test.py") or filename.startswith("test_"):
                continue
            path = pathlib.Path(dirpath) / filename
            try:
                files.append((path, path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue
    return tuple(sorted(files, key=lambda pt: pt[0]))


def _relative(path: pathlib.Path) -> str:
    return str(path.relative_to(APP_ROOT.parent))


# ── Tests ────────────────────────────────────────────────────────────────


def test_only_channel_inbound_service_may_originate_a_non_none_uploader_user_id() -> None:
    violations: list[tuple[pathlib.Path, str]] = []
    origin_found = False

    for path, text in _source_files():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        values = _uploader_user_id_keyword_values(tree)
        if not values:
            continue
        for value in values:
            if _is_safe_anywhere(value):
                continue
            if path.resolve() == _ORIGIN_MODULE.resolve():
                origin_found = True
                continue
            violations.append((path, ast.dump(value)))

    if violations:
        listed = "\n  ".join(f"{_relative(p)}: {expr}" for p, expr in violations)
        pytest.fail(
            "The following call site(s) pass a non-None, non-passthrough "
            f"uploader_user_id= from OUTSIDE channel_inbound_service.py:\n"
            f"  {listed}\n\n"
            "uploader_user_id answers 'who uploaded these bytes', not 'may "
            "they' — the authorization itself is established elsewhere "
            "(assert_access, re-checked on every message). "
            "channel_inbound_service.py's _ingest is the one place that has "
            "already enforced user.id == binding.user_id before "
            "constructing this value (plan §5.5). A new caller computing "
            "its own value here needs the same invariant checked at its own "
            "call site and reviewed as a design decision — not silently "
            "allowed by this test. Either route the value through as a "
            "passthrough of an already-received uploader_user_id parameter, "
            "or add this module here with a comment stating what invariant "
            "makes it safe, mirroring _ingest's own."
        )

    assert origin_found, (
        "channel_inbound_service.py no longer originates a non-None "
        "uploader_user_id at all. Either the feature's one authorization "
        "widening was removed (update/delete this test to match), or the "
        "call site changed shape in a way this AST walk no longer "
        "recognises as a keyword named 'uploader_user_id' on a Call node — "
        "which would be a false negative, not a genuine pass. Verify by "
        "hand before assuming the former."
    )


def test_self_check_a_computed_value_from_a_disallowed_module_is_detected() -> None:
    """
    Proves the classifier above can actually FAIL, the same way the
    "failing message in the middle of the poll tick" test proves a wedge
    guard rather than merely asserting today's happy path. Without this, a
    detector that silently stopped recognising real violations (e.g. a
    change to what AST node ``kw.value`` produces, or a typo in the keyword
    name comparison) would leave the test above vacuously green forever.
    """
    # A synthetic, in-memory snippet — never written to app/ — mimicking a
    # hypothetical new, disallowed caller that computes its own value.
    snippet = (
        "class SomeOtherService:\n"
        "    @staticmethod\n"
        "    def do_something(db, binding):\n"
        "        return ChannelIngestionService.ingest_inbound_message(\n"
        "            uploader_user_id=binding.user_id,\n"
        "        )\n"
    )
    tree = ast.parse(snippet)
    values = _uploader_user_id_keyword_values(tree)
    assert len(values) == 1, "the walker failed to find the keyword at all"
    assert not _is_safe_anywhere(values[0]), (
        "the self-check snippet's computed value (binding.user_id, an "
        "Attribute node) was classified as safe-anywhere — if this "
        "predicate says that, the real test above can never flag an "
        "equivalent real violation either, and is not actually a guard"
    )

    # And the two genuinely safe shapes must still be recognised as such —
    # otherwise the classifier isn't discriminating, it is just "always
    # unsafe", which would fail the main test against every legitimate
    # passthrough call site in the real tree (a false positive is also a
    # broken guard, just a noisier one).
    none_tree = ast.parse("foo(uploader_user_id=None)")
    passthrough_tree = ast.parse("foo(uploader_user_id=uploader_user_id)")
    assert _is_safe_anywhere(_uploader_user_id_keyword_values(none_tree)[0])
    assert _is_safe_anywhere(_uploader_user_id_keyword_values(passthrough_tree)[0])

    # And the exact shape _ingest uses TODAY (a conditional expression, not
    # a bare attribute) must also be recognised as origin-only rather than
    # accidentally falling through some node-type check the two asserts
    # above wouldn't exercise.
    ifexp_tree = ast.parse("foo(uploader_user_id=binding.user_id if file_ids else None)")
    assert not _is_safe_anywhere(_uploader_user_id_keyword_values(ifexp_tree)[0])
