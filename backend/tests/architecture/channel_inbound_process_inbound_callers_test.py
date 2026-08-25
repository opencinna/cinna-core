"""
Architecture allowlist test — ``ChannelInboundService.process_inbound`` callers.

Closes a commitment from Phase 4 of
``docs/plans/channels_identity_unification/``: this must land before Phase 5.
Phase 5 turned out not to add a caller here — App MCP composes
``ChannelCandidateProvider`` / ``IdentityCandidateProvider`` directly rather
than routing through ``ChannelInboundService.process_inbound`` — so the
allowlist below is unchanged; the guard against a *future* caller is what
this test still exists to hold.

WHY THIS EXISTS
---------------
``ChannelInboundService.process_inbound`` is a public ``@staticmethod`` on a
public service class — no leading underscore, nothing in its signature that
says "internal". Its module docstring and its own docstring are explicit that
it performs **no authentication of its own**:

    "The caller is the authentication chokepoint. Nothing below re-verifies
    the sender — ``inbound.sender_email`` is treated as the sender's identity
    from the first line, and it is what the whitelist, user resolution,
    auto-registration and identity routing all key on. A caller that reaches
    this method with an ``inbound`` it did not authenticate has voided the
    promise the whole module rests on."

Today that promise rests entirely on convention: nothing in the type system
or the runtime stops a new caller from constructing a ``ChannelInboundMessage``
from unauthenticated input and handing it straight to ``process_inbound``. This
test makes the convention a checked invariant: the set of ``app/`` modules that
call ``process_inbound`` must be exactly the allowlist below, and every entry
must carry a comment saying *how that caller authenticates* — because that
property, not the module name, is what is being allowlisted.

THE TWO LEGITIMATE CALLERS (verified against the source, not assumed)
-----------------------------------------------------------------------
  * ``app/services/server_channels/channel_inbound_service.py`` —
    ``handle_inbound`` calls ``process_inbound`` only after
    ``adapter.verify_inbound(request, channel, body)`` has succeeded (steps
    1-2 of the module's own ordering). A ``ChannelVerificationError`` is
    raised and propagated *before* the call, so ``process_inbound`` is only
    ever reached with a payload the platform's signature already proved.
  * ``app/services/server_channels/channel_poll_service.py`` —
    ``poll_enabled_channels`` calls ``process_inbound`` once per message
    returned by ``adapter.poll(channel)``. The polled transport authenticates
    *inside* its own ``poll()`` (see ``PolledChannelTransport.poll``'s
    docstring, which states the strength of that guarantee per transport);
    there is no separate verify step here because polling *is* the transport's
    authentication step.

A future caller must either authenticate its ``ChannelInboundMessage`` before
calling ``process_inbound`` — and be added here with a comment saying how —
or go through ``handle_inbound`` / ``poll_enabled_channels`` instead of
calling ``process_inbound`` directly.

HOW IT WORKS
------------
Mirrors ``channel_ingestion_callers_test.py``'s shape: walk ``backend/app/``
as plain text (no AST — the pattern is stable enough that substring/regex
matching is reliable and fast), find call sites, and diff the found module set
against an explicit allowlist.

The matcher is deliberately narrow: it requires the fully qualified
``ChannelInboundService.process_inbound`` immediately followed by an opening
paren. The module's own docstring and several sibling modules
(``channel_routing_service.py``, ``channel_poll_service.py``'s own docstring,
``adapters/base.py``, ``adapters/email.py``) mention bare ``process_inbound``
or double-backtick it in prose over a dozen times combined — none of those are
followed by ``ChannelInboundService.`` + ``(``, so the regex does not trip on
them. See ``test_matcher_ignores_prose_mentions`` below, which proves this
against the real file text rather than trusting the docstring's claim.
"""
from __future__ import annotations

import functools
import os
import pathlib
import re

import pytest


# ── Constants ──────────────────────────────────────────────────────────────────

APP_ROOT = pathlib.Path(__file__).parent.parent.parent / "app"

# The definition site — excluded so the method's own body/docstring never
# counts as a caller of itself (its one real self-call, `handle_inbound` ->
# `process_inbound`, is a call FROM this same file and IS in the allowlist).
_INBOUND_MODULE = APP_ROOT / "services" / "server_channels" / "channel_inbound_service.py"
_POLL_MODULE = APP_ROOT / "services" / "server_channels" / "channel_poll_service.py"

# Same pruning as channel_ingestion_callers_test.py: env-templates ships
# ~10k vendored .py files (bundled agent-runtime virtualenvs) that are never
# production callers and would dominate this test's walk time.
EXCLUDED_DIR_NAMES = frozenset({"env-templates", ".venv", "site-packages", "__pycache__", "node_modules"})

# The real call-site pattern: the fully qualified attribute access immediately
# followed by a call. Every prose mention in this codebase either omits the
# `ChannelInboundService.` prefix (bare ``process_inbound``` in a docstring)
# or omits the trailing paren (double-backticked as a bare name) — see the
# module docstring and test_matcher_ignores_prose_mentions.
_CALL_PATTERN = re.compile(
    r"\bChannelInboundService\.process_inbound\s*\(",
    re.MULTILINE,
)

# ── The allowlist ────────────────────────────────────────────────────────────
#
# Every entry MUST carry a comment stating how that caller authenticates the
# ChannelInboundMessage before handing it to process_inbound. That is the
# property being allowlisted, not the module path — a new entry added without
# one should look obviously wrong in review.
ALLOWED_CALLER_MODULES: dict[pathlib.Path, str] = {
    _INBOUND_MODULE: (
        "handle_inbound calls process_inbound only AFTER "
        "adapter.verify_inbound(request, channel, body) has returned "
        "successfully; a ChannelVerificationError raised by verify_inbound "
        "propagates out of handle_inbound and process_inbound is never "
        "reached for an unverified request."
    ),
    _POLL_MODULE: (
        "poll_enabled_channels calls process_inbound once per message "
        "returned by adapter.poll(channel), a PolledChannelTransport whose "
        "poll() authenticates against its own transport-specific source "
        "before ever constructing a ChannelInboundMessage — there is no "
        "separate verify step here because polling IS this transport's "
        "authentication step."
    ),
}


def _relative(path: pathlib.Path) -> str:
    return str(path.relative_to(APP_ROOT.parent))


# ── File-walker ───────────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=1)
def _source_files() -> tuple[tuple[pathlib.Path, str], ...]:
    """Read every candidate .py file under APP_ROOT exactly once."""
    files: list[tuple[pathlib.Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(APP_ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES]
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


def _collect_caller_modules() -> list[pathlib.Path]:
    """Distinct .py files under APP_ROOT containing a real process_inbound call site."""
    return [path for path, text in _source_files() if _CALL_PATTERN.search(text)]


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_process_inbound_callers_match_the_allowlist() -> None:
    """Every module calling ``ChannelInboundService.process_inbound(`` must be
    in ``ALLOWED_CALLER_MODULES``, each with its own authentication justification.

    This is the drift guard: a new caller that reaches
    ``process_inbound`` without first authenticating the message must fail
    this test until it is either routed through ``handle_inbound`` /
    ``poll_enabled_channels`` instead, or explicitly added here with a comment
    proving how it authenticates.
    """
    found = {p.resolve() for p in _collect_caller_modules()}
    allowed = {p.resolve() for p in ALLOWED_CALLER_MODULES}

    unexpected = found - allowed
    missing = allowed - found

    if unexpected:
        listed = "\n  ".join(_relative(p) for p in sorted(unexpected))
        pytest.fail(
            f"{len(unexpected)} module(s) call "
            f"ChannelInboundService.process_inbound( directly but are NOT in "
            f"ALLOWED_CALLER_MODULES:\n  {listed}\n\n"
            f"process_inbound performs NO authentication of its own — its "
            f"whole contract is 'the caller is the authentication chokepoint' "
            f"(see channel_inbound_service.py's module docstring and the "
            f"docstring on process_inbound itself). A caller that reaches it "
            f"with an unauthenticated ChannelInboundMessage voids that "
            f"promise, and everything below (whitelist, user resolution, "
            f"auto-registration, identity routing) trusts "
            f"inbound.sender_email as the sender's real identity from that "
            f"point on.\n\n"
            f"Fix this by doing ONE of the following:\n"
            f"  1. Route through ChannelInboundService.handle_inbound (webhook "
            f"transports) or ChannelPollService.poll_enabled_channels (polled "
            f"transports) instead of calling process_inbound directly, or\n"
            f"  2. If this really is a new, already-authenticating entry "
            f"point, add it to ALLOWED_CALLER_MODULES in this test file with "
            f"a comment explaining EXACTLY how it authenticates the message "
            f"before this call — the same way the two existing entries do."
        )

    if missing:
        listed = "\n  ".join(_relative(p) for p in sorted(missing))
        pytest.fail(
            f"{len(missing)} allowlisted module(s) no longer call "
            f"ChannelInboundService.process_inbound( at all — the allowlist "
            f"entry is stale and should be removed:\n  {listed}"
        )


def test_matcher_ignores_prose_mentions() -> None:
    """Proves the regex distinguishes real call sites from docstring/comment
    mentions of ``process_inbound``, rather than trusting the module docstring's
    claim on faith.

    Several sibling modules mention bare ``process_inbound`` (no
    ``ChannelInboundService.`` prefix, or no trailing call paren) more than a
    dozen times combined across this package. None of them are call sites and
    none of them may be picked up as one.
    """
    prose_only_files = (
        APP_ROOT / "services" / "server_channels" / "channel_routing_service.py",
        APP_ROOT / "services" / "server_channels" / "adapters" / "email.py",
        APP_ROOT / "services" / "server_channels" / "adapters" / "base.py",
    )
    for path in prose_only_files:
        text = path.read_text(encoding="utf-8")
        assert "process_inbound" in text, (
            f"{_relative(path)} was expected to mention process_inbound in "
            f"prose (docstring/comment) — if it no longer does, this fixture "
            f"file has drifted and should be updated to a module that still "
            f"proves the negative case."
        )
        assert not _CALL_PATTERN.search(text), (
            f"{_relative(path)} matched the call-site regex unexpectedly — "
            f"the matcher is no longer distinguishing prose mentions of "
            f"process_inbound from real ChannelInboundService.process_inbound( "
            f"call sites."
        )

    # The two real call sites, and only they, must match.
    found = {p.resolve() for p in _collect_caller_modules()}
    assert found == {p.resolve() for p in ALLOWED_CALLER_MODULES}, (
        "The matcher's found-caller set no longer equals the allowlist; see "
        "test_process_inbound_callers_match_the_allowlist for the detailed diff."
    )


def test_allowlist_entries_carry_a_real_justification() -> None:
    """Every allowlist entry must have a non-trivial justification comment.

    Guards against a future entry being added as e.g. ``ALLOWED[...] = ""`` or
    a placeholder — the whole point of the allowlist is that each entry names
    *how* that caller authenticates, not merely that it is permitted.
    """
    for path, justification in ALLOWED_CALLER_MODULES.items():
        assert justification and len(justification.strip()) >= 40, (
            f"Allowlist entry for {_relative(path)} has no meaningful "
            f"justification comment. State exactly how this caller "
            f"authenticates the ChannelInboundMessage before calling "
            f"process_inbound."
        )
