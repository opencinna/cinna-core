"""Architecture drift test — ``AgentEnvironment.status`` has exactly two writers.

WHY THIS EXISTS
---------------
The status-repair sweep (``docs/plans/system_status_repair_plan.md``, Pass A)
decides whether a lifecycle operation is alive by reading
``AgentEnvironment.status_changed_at``: every status change goes through
``environment_lifecycle._set_status`` and every progress message through
``_touch_progress``, and both stamp that column unconditionally. The column is
therefore a *liveness heartbeat* — "when did this environment's lifecycle last
move" — and not merely "when did this status start".

That reading is only true while those two functions are the sole writers, and
the failure mode of breaking it is silent in both directions:

- A **raw ``env.status = ...``** leaves the clock pointing at the previous
  transition, so the row looks stale earlier than it is and the sweep can reap
  an operation that just started.
- A **raw ``env.status_message = ...``** does real work and leaves no trace of
  having done it, so a long, live operation looks silent and gets reaped
  mid-flight.

Neither shows up as a test failure anywhere else: the environment still works,
the UI still updates, and the only symptom is a reconciler that occasionally
kills something it should not have — days later, on someone else's machine.
That is precisely the class of invariant that has to be pinned structurally
rather than by review.

WHAT IT ENFORCES
----------------
No assignment to ``.status`` or ``.status_message`` on an environment-shaped
variable anywhere under ``app/services`` or ``app/api``, except at the sites in
``_ALLOWED`` — the two writers themselves, plus the one documented value-copy
in ``agent_status_service`` that carries ``status_changed_at`` across with the
status instead of re-stamping it (re-stamping there would hide a genuinely
stale row behind another session's fresh clock).

WHY A NAME HEURISTIC
--------------------
An AST walk cannot know that ``env`` is an ``AgentEnvironment``; only a type
checker can. So the target set is the names the codebase actually uses for one
(``_ENV_NAMES``), which is what makes this test cheap enough to be worth having
and is also why it is a drift *trip-wire* rather than a proof: a writer that
names its local something else escapes it. It catches the realistic edit — a
future "retry activation every N minutes" poller re-asserting ``starting`` — and
it fails with a message that names the two functions to use instead. False
positives are impossible to introduce accidentally, since any new name in
``_ENV_NAMES`` is a deliberate addition to this file.

Deliberately NOT scanning ``app/models``: the field declarations there assign to
``status``/``status_message`` as class attributes and are the definition, not a
write. Nor ``app/env-templates``, which is container-side code with its own
unrelated ``status`` fields.
"""
import ast
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
SCANNED_DIRS = ("services", "api")

# Local variable names the codebase uses for an AgentEnvironment. Anything
# assigned through one of these is treated as an environment write.
_ENV_NAMES = frozenset(
    {
        "environment",
        "env",
        "target_env",
        "source_env",
        "fresh_env",
        "agent_env",
        "agent_environment",
    }
)

_WATCHED_ATTRS = frozenset({"status", "status_message"})

# (module path relative to app/, enclosing function) pairs that are allowed to
# write these attributes directly. Keep this list short and keep the reason
# with it — an entry added without one is how an invariant stops being one.
_ALLOWED = {
    # The two central writers. They ARE the mechanism.
    ("services/environments/environment_lifecycle.py", "_set_status"),
    ("services/environments/environment_lifecycle.py", "_touch_progress"),
    # A value copy of an already-recorded transition from a different session's
    # row, which copies status_changed_at across in the same breath rather than
    # re-stamping it. Documented in place.
    ("services/agents/agent_status_service.py", "_ensure_environment_running"),
}


def _enclosing_function(tree: ast.AST, node: ast.AST) -> str | None:
    """Name of the innermost function containing ``node``, if any."""
    best: tuple[int, str] | None = None
    for candidate in ast.walk(tree):
        if not isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(candidate, "end_lineno", None)
        if end is None or not (candidate.lineno <= node.lineno <= end):
            continue
        # Innermost wins: the latest-starting enclosing function.
        if best is None or candidate.lineno > best[0]:
            best = (candidate.lineno, candidate.name)
    return best[1] if best else None


def _violations() -> list[str]:
    found: list[str] = []
    for scanned in SCANNED_DIRS:
        for path in sorted((APP_ROOT / scanned).rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            rel = path.relative_to(APP_ROOT).as_posix()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if (
                        not isinstance(target, ast.Attribute)
                        or target.attr not in _WATCHED_ATTRS
                        or not isinstance(target.value, ast.Name)
                        or target.value.id not in _ENV_NAMES
                    ):
                        continue
                    function = _enclosing_function(tree, node)
                    if (rel, function) in _ALLOWED:
                        continue
                    found.append(
                        f"{rel}:{node.lineno} in {function or '<module>'}() — "
                        f"{target.value.id}.{target.attr} = ..."
                    )
    return found


def test_environment_status_has_only_the_two_central_writers() -> None:
    violations = _violations()
    assert not violations, (
        "AgentEnvironment.status / .status_message must be written only through "
        "environment_lifecycle._set_status() or _touch_progress(), which stamp "
        "status_changed_at. A raw assignment breaks the liveness heartbeat the "
        "status-repair sweep reads (app/services/system/"
        "status_repair_environments.py) — silently, and in the direction that "
        "reaps live operations.\nOffending writes:\n  "
        + "\n  ".join(violations)
    )


@pytest.mark.parametrize("rel, function", sorted(_ALLOWED))
def test_allowlisted_writers_still_exist(rel: str, function: str) -> None:
    """An allowlist entry that no longer matches real code is dead weight.

    Without this, a renamed or deleted writer leaves a stale exemption behind
    that would silently excuse a *future* function of the same name.
    """
    tree = ast.parse((APP_ROOT / rel).read_text(), filename=rel)
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert function in names, (
        f"Allowlist entry ({rel}, {function}) no longer names a real function — "
        "remove it or point it at the function that replaced it."
    )
