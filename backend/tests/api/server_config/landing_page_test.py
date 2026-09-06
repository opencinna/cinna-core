"""The public landing page: `GET /server-config/landing`.

Phase 4 gave the admin-authored `/start` welcome copy its own endpoint rather
than folding it into the access-policy projection (see the
``landing_markdown`` comment in ``access_policy_test.py``'s
``PUBLIC_POLICY_FIELDS``): the policy is six small scalars shared by every
login and signup page load, while the landing copy is an unbounded
admin-authored blob that only `/start` renders. This file covers:

* ``GET /server-config/landing`` — public, unauthenticated, exact response
  shape, and rate-limited on the *same* budget as the access-policy read (a
  second limiter object would hand one caller two budgets).
* ``PUT /admin/server-config`` writing ``landing_markdown``: the
  omission-vs-empty-string distinction (an omitted key must leave existing
  copy intact, never overwrite it — this bug class has bitten this feature
  before), the ``disclaimer_version`` boundary, and the length cap that exists
  because this is the first markdown column ever served to an anonymous
  caller.
* The ``_no_raw_html`` field validator on the same field (``reject_raw_html``
  in ``app/models/server_config/server_config.py``): ``/start`` renders this
  copy with ``MarkdownRenderer``, which has no sanitiser configured — raw HTML
  is inert *only* because ``react-markdown`` doesn't parse it without
  ``rehype-raw``. If that plugin is ever added for an unrelated feature, every
  stored landing page becomes stored XSS for anonymous visitors, in a diff
  that never touches ``/start``. The frontend guard isn't visible to this
  backend suite, so the durable guard is this validator, and this is where it
  earns its keep: HTML-shaped input rejected, prose/autolinks/code spans that
  merely look HTML-shaped accepted, and a rejected write never partially
  applies alongside an otherwise-valid field on the same request.

``password_signup_available``'s behavioural matrix lives in
``access_policy_test.py``, next to the rest of the public projection it rides
on.
"""
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.server_config import (
    ACCESS_POLICY_URL,
    LANDING_URL,
    SERVER_CONFIG_URL,
    get_landing_page,
    get_server_config,
    set_access_policy,
)

# Mirrors ``LANDING_MARKDOWN_MAX_LENGTH`` in
# ``app/models/server_config/server_config.py`` (16 KiB). Not imported: Rule 1
# in ``tests/README.md`` restricts ``tests/api/`` to importing only
# ``app.core.config.settings`` and ``app.utils`` from the app package. Re-read
# the constant by hand if this cap is ever meant to change.
_MAX_LEN = 16 * 1024


@pytest.fixture(autouse=True)
def _fresh_landing_limiter(monkeypatch):
    """Give each test in this file its own rate-limit bucket.

    Same reasoning as ``access_policy_test.py``'s
    ``_fresh_access_policy_limiter``: ``_access_policy_limiter`` is
    module-global and process-local, and this endpoint shares that exact
    object with the access-policy read, so a spent bucket from one file's
    tests would leak into another's 429 boundary.
    """
    from app.api.routes import server_config
    from app.services.common.rate_limiter import RateLimiter

    monkeypatch.setattr(server_config, "_access_policy_limiter", RateLimiter())


# ── Public read ───────────────────────────────────────────────────────


def test_landing_page_is_public_and_empty_on_a_fresh_instance(
    client: TestClient,
) -> None:
    """No admin has written anything yet: reachable, and an empty string."""
    response = client.get(LANDING_URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"landing_markdown"}
    assert body["landing_markdown"] == ""


def test_landing_page_reflects_admin_edits_and_leaks_no_other_field(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The endpoint's whole job, and its whole boundary:
      1. Admin writes the welcome copy alongside an unrelated, distinctive
         field on the same row
      2. The public read returns exactly the markdown — the documented single
         key, nothing else
      3. The unrelated field's value is not discoverable through this
         endpoint under any key or embedded in the text
    """
    # ── Phase 1: Admin write ──────────────────────────────────────────
    set_access_policy(
        client,
        superuser_token_headers,
        landing_markdown="# Welcome\n\nAsk your admin for an invite.",
        default_user_role="agent-developer",
    )

    # ── Phase 2: Exact shape ───────────────────────────────────────────
    body = get_landing_page(client)
    assert set(body) == {"landing_markdown"}
    assert body["landing_markdown"] == "# Welcome\n\nAsk your admin for an invite."

    # ── Phase 3: Nothing else leaks ────────────────────────────────────
    assert "agent-developer" not in client.get(LANDING_URL).text


# ── Admin writes ──────────────────────────────────────────────────────


def test_admin_update_persists_landing_markdown_without_bumping_disclaimer(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A landing-copy edit is not disclaimer content:
      1. Write it
      2. It round-trips through both the admin and the public read
      3. ``disclaimer_version`` is untouched — the same invariant
         ``test_server_config.py`` pins for ``local_agent_kit_enabled``
    """
    before = get_server_config(client, superuser_token_headers)

    updated = set_access_policy(
        client, superuser_token_headers, landing_markdown="Hello, world."
    )
    assert updated["landing_markdown"] == "Hello, world."
    assert updated["disclaimer_version"] == before["disclaimer_version"]

    fetched = get_server_config(client, superuser_token_headers)
    assert fetched["landing_markdown"] == "Hello, world."
    assert get_landing_page(client)["landing_markdown"] == "Hello, world."


def test_omitting_landing_markdown_leaves_it_intact_but_empty_string_clears_it(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The durable-fix rule for this feature, stated as two distinct outcomes:
    ``None`` (an omitted key) means "not being changed"; ``""`` means "clear
    it". A write that collapses these two is exactly the regression this test
    exists to catch.
      1. Persist a value
      2. An update that omits the field entirely (touching something else)
         leaves the existing copy intact
      3. An update that explicitly sends "" clears it
      4. The clear is visible through the public read too
    """
    # ── Phase 1: Persist a value ───────────────────────────────────────
    set_access_policy(
        client, superuser_token_headers, landing_markdown="Persisted copy."
    )

    # ── Phase 2: Omission leaves it alone ──────────────────────────────
    unrelated = set_access_policy(
        client, superuser_token_headers, local_agent_kit_enabled=False
    )
    assert unrelated["landing_markdown"] == "Persisted copy."
    assert unrelated["local_agent_kit_enabled"] is False
    assert get_landing_page(client)["landing_markdown"] == "Persisted copy."

    # ── Phase 3: An explicit "" clears it ──────────────────────────────
    cleared = set_access_policy(client, superuser_token_headers, landing_markdown="")
    assert cleared["landing_markdown"] == ""

    # ── Phase 4: The clear is public ───────────────────────────────────
    assert get_landing_page(client)["landing_markdown"] == ""


def test_landing_markdown_length_cap_at_the_boundary(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The cap on the first markdown column ever served to an anonymous caller:
      1. Exactly the cap is accepted and persists
      2. One character over is rejected as a validation error
      3. The rejected write did not partially apply
    """
    # ── Phase 1: At the boundary ────────────────────────────────────────
    at_cap = "A" * _MAX_LEN
    updated = set_access_policy(client, superuser_token_headers, landing_markdown=at_cap)
    assert updated["landing_markdown"] == at_cap
    assert len(updated["landing_markdown"]) == _MAX_LEN

    # ── Phase 2: One over ────────────────────────────────────────────────
    over_cap = "A" * (_MAX_LEN + 1)
    response = client.put(
        SERVER_CONFIG_URL,
        headers=superuser_token_headers,
        json={"landing_markdown": over_cap},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert any(entry.get("loc", [])[-1:] == ["landing_markdown"] for entry in detail), detail

    # ── Phase 3: No partial write ────────────────────────────────────────
    assert get_server_config(client, superuser_token_headers)["landing_markdown"] == at_cap


# ── Raw-HTML guard (`_no_raw_html` / `reject_raw_html`) ────────────────────


def _assert_landing_markdown_rejected(
    client: TestClient, headers: dict[str, str], value: str
) -> dict:
    """PUT the given value and assert it was refused as a `landing_markdown`
    validation error, in the shape the length-cap test above already uses.
    Returns the parsed error body for callers that want to inspect it further.
    """
    response = client.put(
        SERVER_CONFIG_URL, headers=headers, json={"landing_markdown": value}
    )
    assert response.status_code == 422, response.text
    body = response.json()
    detail = body["detail"]
    assert any(entry.get("loc", [])[-1:] == ["landing_markdown"] for entry in detail), detail
    return body


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("<script>alert(1)</script>", id="script-tag"),
        pytest.param(
            '<iframe src="http://evil.example"></iframe>', id="iframe-tag"
        ),
        pytest.param(
            '<object data="http://evil.example"></object>', id="object-tag"
        ),
        pytest.param('<embed src="http://evil.example">', id="embed-tag"),
        pytest.param("<svg/onload=alert(1)>", id="svg-onload"),
        # No `<` anywhere in this payload, so if it is rejected it can only be
        # `_EVENT_HANDLER_RE` firing — `_HTML_TAG_RE` has nothing to match.
        pytest.param(
            'Random notes onmouseover="doEvil()" appended here.',
            id="bare-event-handler-attribute",
        ),
        pytest.param("[click me](javascript:alert(1))", id="javascript-scheme"),
        pytest.param("[click me](vbscript:msgbox(1))", id="vbscript-scheme"),
        pytest.param(
            "[click me](data:text/html,alert(1))", id="data-text-html-scheme"
        ),
        pytest.param("<!-- a comment -->", id="html-comment"),
        pytest.param("</div>", id="bare-closing-tag"),
    ],
)
def test_html_shaped_landing_markdown_is_rejected(
    client: TestClient, superuser_token_headers: dict[str, str], payload: str
) -> None:
    """Each payload here is HTML-shaped in a distinct way the guard names in
    its refusal message (tag, event-handler attribute, or script-bearing URL
    scheme). A validation rule firing before any resource state changes is
    exactly the case `tests/README.md` calls out as justified as a standalone
    (here: parametrized) test rather than folded into a lifecycle scenario.
    """
    _assert_landing_markdown_rejected(client, superuser_token_headers, payload)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            "Revenue grew: a < b and x > y this quarter.",
            id="prose-bare-comparisons",
        ),
        # `<` followed by `h`, `t`, `t`, `p`, `s` (all `[a-zA-Z0-9-]`) and then
        # `:` — `:` is not in `[\s/>]`, so `_HTML_TAG_RE`'s tag alternative
        # never gets to close. This must NOT be treated as an HTML tag.
        pytest.param(
            "See <https://example.com> for details.", id="autolink-url"
        ),
        # Same reasoning: `<a` then `@` immediately, which is neither
        # `[a-zA-Z0-9-]` nor `[\s/>]`, so the tag alternative fails here too.
        pytest.param("Contact <a@b.com> for help.", id="autolink-email"),
        pytest.param(
            "Use `<n>` as the placeholder.", id="inline-code-span-single-line"
        ),
        pytest.param(
            "Example:\n\n```html\n<script>alert(1)</script>\n```\n",
            id="fenced-code-block-with-script",
        ),
    ],
)
def test_html_shaped_false_positives_are_accepted(
    client: TestClient, superuser_token_headers: dict[str, str], payload: str
) -> None:
    """The guard's whole point is to be conservative about false positives —
    a rule that refuses an admin's code sample or a bare `<`/`>` comparison is
    a bug, not caution. Each of these looks HTML-shaped at a glance but is
    excluded by `_strip_code_regions` (fenced block, inline code span) or
    never matches `_HTML_TAG_RE` in the first place (prose comparison,
    autolink). Persisted via both the admin and the public read.
    """
    updated = set_access_policy(
        client, superuser_token_headers, landing_markdown=payload
    )
    assert updated["landing_markdown"] == payload
    assert get_landing_page(client)["landing_markdown"] == payload


def test_indented_code_block_is_rejected_by_design(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Documents intentional behavior, not a bug: unlike a fenced block, a
    four-space-indented code block is NOT excluded from the check. Per the
    comment at `server_config.py:47-53`, telling an indented code block apart
    from a list item's continuation line needs a real block parser, and
    over-excluding here is the direction that would hide markup from the
    guard — so this is deliberately conservative in the refusing direction.
    The refusal message points the admin at a fenced block instead, which
    does work (see `test_html_shaped_false_positives_are_accepted`).
    """
    payload = "Example:\n\n    <script>alert(1)</script>\n"
    _assert_landing_markdown_rejected(client, superuser_token_headers, payload)


def test_rejected_landing_markdown_does_not_partially_apply_alongside_other_fields(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Proves the guard fails the whole request closed, not just the field:
      1. Persist a known-good `landing_markdown` and note the current
         `local_agent_kit_enabled`
      2. One PUT changes both `local_agent_kit_enabled` (validly) and
         `landing_markdown` (to a rejected, HTML-shaped value) in the same
         JSON body
      3. The request is rejected wholesale (422) — Pydantic validates the
         payload before the route handler ever runs, so there is no
         partial-apply window
      4. Neither field changed: `landing_markdown` is still the known-good
         value, and `local_agent_kit_enabled` is still what it was before
         this request
    """
    # ── Phase 1: Known-good baseline ────────────────────────────────────
    before = set_access_policy(
        client, superuser_token_headers, landing_markdown="Known-good copy."
    )
    original_kit_flag = before["local_agent_kit_enabled"]
    flipped_kit_flag = not original_kit_flag

    # ── Phase 2: One PUT, one valid field + one rejected field ──────────
    response = client.put(
        SERVER_CONFIG_URL,
        headers=superuser_token_headers,
        json={
            "local_agent_kit_enabled": flipped_kit_flag,
            "landing_markdown": "<script>alert(1)</script>",
        },
    )

    # ── Phase 3: Rejected wholesale ──────────────────────────────────────
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert any(entry.get("loc", [])[-1:] == ["landing_markdown"] for entry in detail), detail

    # ── Phase 4: Neither field changed ───────────────────────────────────
    fetched = get_server_config(client, superuser_token_headers)
    assert fetched["landing_markdown"] == "Known-good copy."
    assert fetched["local_agent_kit_enabled"] == original_kit_flag


# ── Rate limiting ────────────────────────────────────────────────────────


def test_landing_page_shares_the_access_policy_rate_limit_budget(
    client: TestClient, monkeypatch
) -> None:
    """One limiter object serves both anonymous reads (D6/T3), proven by
    spending the whole budget on one endpoint and finding the other already
    throttled — a second, independent ``RateLimiter`` would not do this.
    """
    monkeypatch.setattr(settings, "ACCESS_POLICY_RATE_LIMIT_PER_MIN", 3)

    for _ in range(3):
        assert client.get(LANDING_URL).status_code == 200

    exhausted_here = client.get(LANDING_URL)
    assert exhausted_here.status_code == 429, exhausted_here.text

    throttled_elsewhere = client.get(ACCESS_POLICY_URL)
    assert throttled_elsewhere.status_code == 429, throttled_elsewhere.text
