"""The two invariants the account-creation chokepoint is named for.

**1. No third-party provider is contacted while an account is created.** Every
key handed out at signup is already in the database. A provider call on this
path turns a provider outage into a failed login, which is why phase 5's
key-minting goes in a background task instead.

**2. Account creation never fails because provisioning failed.** A person is
not told "your account could not be created" because an admin pasted an
expired key last month. Failures are recorded and skipped; the account stands.

HOW EACH IS PROVED, AND WHY NOT THE OBVIOUS WAY
-----------------------------------------------
*Invariant 1* is a claim about **absent** code, and the usual technique —
patch today's provider client, assert the mock was not called — proves the
mock. A call added tomorrow through a different client, or the same client
reached by a different import, passes it silently. So the guard sits one layer
below every SDK, at the httpx transport (see ``tests/utils/network_guard.py``),
and every "nothing dialled out" assertion is preceded by
``assert_guard_is_armed``, which makes a deliberate outbound call and requires
it to be refused. Without that control a guard that failed to install would
make this file pass by proving nothing.

*Invariant 2* is where the interesting failures live. The developer who built
phase 2 broke it three separate ways during development and none was caught by
1877 existing tests or by a happy-path smoke script, because all three only
appear once the **Postgres transaction is aborted** — not when Python merely
raises. In that state the *recovery code* is what throws: a
``logger.warning("... %s", user.id)`` whose argument is a lazily-loaded ORM
attribute is a query; so is the audit ``session.commit()``; so is the caller's
next commit. A test that injects a ``ValueError`` exercises the ``try`` and
learns nothing. So the cases below abort the transaction for real — one with a
failing SQL statement mid-``add_members``, one by handing over a session that
is already aborted, and one by breaking the audit row's own INSERT so that the
*recovery* path is what runs on a broken session — through the documented
Rule-1 exemption in ``tests/utils/account_provisioning.py``.

**Two parents, not one.** With a single auto-provisioned parent the failure
lands while ``user`` is still fresh from ``create_account``'s ``refresh``:
every attribute a broken handler might touch is already loaded, and the test
passes against broken code. The second parent is the one that bites, because
the first one's committed child has expired the identity map by then. Every
failure scenario here therefore configures at least two.
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.account_provisioning import (
    CHILD_CREDENTIAL_INSERT,
    MEMBERSHIP_LOOKUP,
    SECURITY_EVENT_INSERT,
    abort_transaction,
    emit_audit_event,
    failing_sql_statement,
    get_user_row,
    provision_account,
    session_is_usable,
)
from tests.utils.google_oauth import login_with_google, random_google_id
from tests.utils.managed_ai_credential import (
    create_managed_credential,
    get_managed_credential,
    member_user_ids,
)
from tests.utils.network_guard import assert_guard_is_armed, no_outbound_http
from tests.utils.user import user_authentication_headers
from tests.utils.utils import random_email, random_lower_string

API = settings.API_V1_STR

# No agents, no environments, and — the point of the first test — no stubbed
# external services either. ``patched_external_services`` would mock the very
# seams the outbound-HTTP guard exists to watch, so a provider call added to
# the creation path would be absorbed by the stub instead of tripping the
# guard. The guard is the only thing between this code and the network here.
NEEDS_AGENT_STUBS = False


# ── Local helpers ──────────────────────────────────────────────────────


def _parent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    *,
    name: str,
    set_user_sdk_defaults: bool = False,
) -> dict:
    """One auto-provisioning managed credential covering ``agent-user``."""
    result = create_managed_credential(
        client,
        superuser_token_headers,
        name=name,
        auto_provision_roles=["agent-user"],
        set_user_sdk_defaults=set_user_sdk_defaults,
        sdk_default_modes=["conversation"] if set_user_sdk_defaults else None,
    )
    return result["record"]


def _signup(client: TestClient) -> tuple[dict, dict[str, str]]:
    """Create an account through the public signup route; return it + headers."""
    email = random_email()
    password = random_lower_string()
    response = client.post(
        f"{API}/users/signup", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    user = response.json()
    headers = user_authentication_headers(
        client=client, email=email, password=password
    )
    return user, headers


def _failure_events(client: TestClient, headers: dict[str, str]) -> list[dict]:
    response = client.get(
        f"{API}/security-events/",
        headers=headers,
        params={"event_type": "admin.ai_credential.auto_provision_failed"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ── Invariant 1: nothing dials a provider on the creation path ─────────


def test_no_provider_is_contacted_anywhere_on_the_account_creation_path(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Account creation must never wait on OpenAI, Anthropic or anyone else:
      1. Two managed credentials are configured to auto-provision, so the
         provisioning path is genuinely exercised rather than skipped
      2. The outbound-HTTP guard is installed — and proved armed
      3. A password signup runs inside it
      4. A Google first-login runs inside it
      5. An admin ``POST /users/`` runs inside it
      6. Nothing attempted a single outbound HTTP call
      7. All three accounts really were provisioned, so step 6 is a statement
         about the provisioning path and not about a path that never ran

    **What makes this fail if someone reintroduces a call.** The guard replaces
    ``httpx.HTTPTransport.handle_request`` and its async twin — the transport
    every real outbound request in this backend goes through, including the
    ``openai`` / ``anthropic`` / ``google-genai`` SDKs, ``model_discovery_
    service`` and ``oauth_credentials_service``, whichever object or import
    path reaches them. ``urllib.request.urlopen`` and
    ``http.client.HTTPConnection.connect`` are covered too. It is not bound to
    any particular provider module, so it cannot be dodged by adding a *new*
    client; the only way past it is not to make an HTTP call. ``TestClient``'s
    own ``ASGITransport`` is untouched (it never leaves the process) and raw
    sockets are untouched, so Postgres and SMTP behave normally.
    """
    # ── Phase 1: Two parents, so provisioning has real work to do ──────
    first = _parent(client, superuser_token_headers, name="Company A")
    second = _parent(client, superuser_token_headers, name="Company B")

    with no_outbound_http() as attempts:
        # ── Phase 2: The control. Without this the rest is vacuous. ────
        assert_guard_is_armed(attempts)

        # ── Phase 3: Password signup ───────────────────────────────────
        signup_user, _ = _signup(client)

        # ── Phase 4: Google first login ────────────────────────────────
        google_headers = login_with_google(
            client, email=random_email(), google_id=random_google_id()
        )
        google_me = client.get(f"{API}/users/me", headers=google_headers)
        assert google_me.status_code == 200, google_me.text
        google_user = google_me.json()

        # ── Phase 5: Admin-created account ─────────────────────────────
        # Patched at the *module-local* names both call sites bound at import
        # (memory: ``project_test_users_send_email_patch_bug``). SMTP is a
        # socket and therefore outside this guard anyway; it is stubbed only
        # so the scenario does not depend on a mail container being up.
        with (
            patch("app.api.routes.users.send_email", return_value=None),
            patch(
                "app.services.users.email_confirmation_service.send_email",
                return_value=None,
            ),
        ):
            admin_created = client.post(
                f"{API}/users/",
                headers=superuser_token_headers,
                json={"email": random_email(), "password": random_lower_string()},
            )
        assert admin_created.status_code == 200, admin_created.text
        admin_user = admin_created.json()

        # ── Phase 6: Nothing left the process ──────────────────────────
        assert attempts == [], (
            "A third-party HTTP call was attempted while creating an account. "
            "Account creation and login must never wait on a provider — move "
            "it into a background task (zero-touch-onboarding invariant 1)."
        )

    # ── Phase 7: …and the guarded path was the real one ────────────────
    expected = {signup_user["id"], google_user["id"], admin_user["id"]}
    for parent in (first, second):
        record = get_managed_credential(
            client, superuser_token_headers, parent["id"]
        )
        assert expected <= member_user_ids(record), (
            "The three accounts were not provisioned, so 'no provider was "
            f"contacted' says nothing. Members: {member_user_ids(record)}"
        )


# ── Invariant 2a: a failing SQL statement inside add_members ───────────


def test_a_failing_sql_statement_inside_add_members_never_loses_the_account(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """
    The transaction dies halfway through provisioning; the account does not:
      1. Two managed credentials auto-provision to ``agent-user``
      2. The *second* child-credential INSERT is made to abort the Postgres
         transaction — a real statement failure, not a Python exception
      3. Signup still answers 200 and the account can log in
      4. The session the request was served on is usable afterwards — the
         request's own later commits (the confirmation-email step, the audit
         row) would otherwise die with "current transaction is aborted"
      5. The first parent's child survived; the second parent's did not
      6. The failure is on the record: a medium-severity
         ``auto_provision_failed`` event in the new account's own feed

    Step 2 aims at the *second* insert on purpose. On the first, ``user`` is
    still fresh from ``create_account``'s ``refresh`` and every attribute a
    failure handler might touch is already loaded, so a handler that reads an
    ORM attribute before rolling back would survive — and the test would pass
    against exactly the code it exists to catch.
    """
    # ── Phase 1 ────────────────────────────────────────────────────────
    _parent(client, superuser_token_headers, name="Company A")
    _parent(client, superuser_token_headers, name="Company B")

    email = random_email()
    password = random_lower_string()

    # ── Phase 2 + 3 ────────────────────────────────────────────────────
    with failing_sql_statement(
        db, when_statement_contains=CHILD_CREDENTIAL_INSERT, occurrence=2
    ) as injection:
        response = client.post(
            f"{API}/users/signup", json={"email": email, "password": password}
        )

    assert injection["fired"], (
        "The injected statement never matched — no failure was reproduced and "
        "this test is asserting nothing."
    )
    assert response.status_code == 200, (
        f"Signup failed because provisioning failed: {response.text}"
    )
    user = response.json()

    # ── Phase 4: the session survived, not just the response ───────────
    assert session_is_usable(db), (
        "The request's session was left with an aborted transaction. The "
        "response happened to be 200, but the next commit on this session — "
        "the confirmation email, the audit row, anything the caller does "
        "after provisioning — would fail."
    )

    headers = user_authentication_headers(
        client=client, email=email, password=password
    )
    me = client.get(f"{API}/users/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["id"] == user["id"]

    # ── Phase 5: one grant landed, one did not ─────────────────────────
    holders = [
        record
        for record in client.get(
            f"{API}/admin/llm-providers/", headers=superuser_token_headers
        ).json()
        if user["id"] in {m["user_id"] for m in record["members"]}
    ]
    assert len(holders) == 1, (
        "Exactly one of the two parents should have provisioned; got "
        f"{[r['name'] for r in holders]}"
    )

    # ── Phase 6: the failure was recorded, not swallowed ───────────────
    failures = _failure_events(client, headers)
    assert len(failures) == 1, failures
    assert failures[0]["severity"] == "medium"
    assert failures[0]["details"]["origin"] == "signup"
    assert failures[0]["details"]["actor"] == "system"
    assert failures[0]["details"]["reason"] in {
        "provision_failed",
        "add_members_failed",
    }


def test_a_failing_membership_query_inside_add_members_never_loses_the_account(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """The same invariant, with the failure escaping ``add_members`` entirely.

    The previous test's failure is caught *inside* ``add_members`` (the
    per-child ``except`` turns it into a ``skipped`` entry). This one aborts
    the transaction on the membership lookup that runs *before* any child is
    touched, so the exception propagates out of ``add_members`` and into
    ``AccountProvisioningService``'s own per-parent net — a different piece of
    recovery code, reached only this way, and the one that has to roll back
    before it may log.

      1. Two parents auto-provision to ``agent-user``
      2. The second parent's membership lookup aborts the transaction
      3. Signup still succeeds and the session is still usable
      4. The first parent's grant stands; the second is reported failed
    """
    _parent(client, superuser_token_headers, name="Company A")
    _parent(client, superuser_token_headers, name="Company B")

    email = random_email()
    password = random_lower_string()

    with failing_sql_statement(
        db, when_statement_contains=MEMBERSHIP_LOOKUP, occurrence=2
    ) as injection:
        response = client.post(
            f"{API}/users/signup", json={"email": email, "password": password}
        )

    assert injection["fired"], "The injected statement never matched."
    assert response.status_code == 200, response.text
    user = response.json()

    assert session_is_usable(db)

    headers = user_authentication_headers(
        client=client, email=email, password=password
    )
    assert client.get(f"{API}/users/me", headers=headers).status_code == 200

    holders = [
        record
        for record in client.get(
            f"{API}/admin/llm-providers/", headers=superuser_token_headers
        ).json()
        if user["id"] in {m["user_id"] for m in record["members"]}
    ]
    assert len(holders) == 1, [r["name"] for r in holders]

    failures = _failure_events(client, headers)
    assert len(failures) == 1, failures
    assert failures[0]["details"]["reason"] == "add_members_failed"


# ── Invariant 2b: an already-aborted session handed over ───────────────


def test_provisioning_survives_being_handed_an_already_aborted_session(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """
    ``on_account_created`` promises never to raise — *including* when the
    session it is given is already unusable:

      1. Two managed credentials auto-provision to ``agent-user``
      2. An account exists and its row is loaded while the session is healthy
      3. The transaction is aborted, and confirmed aborted
      4. ``on_account_created`` is called on that session
      5. It returns a report instead of raising — on an aborted session even
         ``user.id`` is a query, which is why the identifier snapshot is
         *inside* the outer ``try`` and not above it
      6. The session is usable again afterwards, because the caller
         (``register_user``) still has a confirmation email to commit
      7. The account is still there

    Today's only production caller commits and refreshes immediately before
    calling in, so it cannot produce this state; phase 3's invite path is a new
    caller. A promise that holds only for the callers that exist today is not
    one worth documenting, so it is tested for the ones that do not exist yet.
    """
    # ── Phase 1 + 2 ────────────────────────────────────────────────────
    _parent(client, superuser_token_headers, name="Company A")
    _parent(client, superuser_token_headers, name="Company B")

    email = random_email()
    password = random_lower_string()
    created = client.post(
        f"{API}/users/signup", json={"email": email, "password": password}
    )
    assert created.status_code == 200, created.text
    user_id = created.json()["id"]
    user_row = get_user_row(db, user_id)

    # ── Phase 3 ────────────────────────────────────────────────────────
    abort_transaction(db)
    assert not session_is_usable(db)

    # ── Phase 4 + 5 ────────────────────────────────────────────────────
    report = provision_account(db, user_row)
    assert report.added == [], report
    assert report.skipped == [], report

    # ── Phase 6 ────────────────────────────────────────────────────────
    assert session_is_usable(db), (
        "The session was left unusable. The caller still has work to do after "
        "provisioning returns — register_user commits a confirmation email — "
        "so leaving it broken fails the account creation just as surely as "
        "re-raising would."
    )

    # ── Phase 7 ────────────────────────────────────────────────────────
    headers = user_authentication_headers(
        client=client, email=email, password=password
    )
    me = client.get(f"{API}/users/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["id"] == user_id


# ── Invariant 2c: the audit write is the thing that fails ──────────────


def test_a_failing_audit_write_is_swallowed_and_the_caller_can_still_commit(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """
    ``_emit`` is the audit writer both provisioning entry points use, and its
    contract has two halves — only one of which a "does it raise?" test sees:

      1. An account exists, and its row is loaded while the session is healthy
      2. The ``SecurityEvent`` INSERT is broken **at the database**, so the
         ``commit`` inside ``_emit`` is the statement that fails and the
         transaction is left in Postgres' aborted state
      3. ``_emit`` returns instead of raising — the cheap half
      4. The session is usable again
      5. **The caller's next commit succeeds.** This is the half the fix is
         for. ``_emit``'s failure handler repairs the session *before* it
         logs; a handler that repairs second, or not at all, hands the caller
         a session whose next ``commit()`` dies of a ``PendingRollbackError``
         somewhere entirely unrelated — an invite that already committed its
         account row, a signup on its way to commit a confirmation email.

    Step 5 is asserted with a real request rather than a bare ``db.commit()``:
    a rollback with nothing pending commits happily on a broken session, so
    the assertion has to be a write that actually goes through the app.

    A ``caplog`` assertion would prove nothing here — ``setup_db``'s
    ``fileConfig`` disables the application loggers for the whole session, so
    the handler's ``logger.exception`` is compared against an empty string
    (see the README). The observable behaviour is the only honest evidence.
    """
    # ── Phase 1 ────────────────────────────────────────────────────────
    email = random_email()
    password = random_lower_string()
    created = client.post(
        f"{API}/users/signup", json={"email": email, "password": password}
    )
    assert created.status_code == 200, created.text
    user_id = created.json()["id"]
    user_row = get_user_row(db, user_id)

    # ── Phase 2 + 3 ────────────────────────────────────────────────────
    with failing_sql_statement(
        db, when_statement_contains=SECURITY_EVENT_INSERT
    ) as injected:
        # No ``pytest.raises``: not raising *is* the assertion.
        emit_audit_event(db, user_row)
    assert injected["fired"], "the injection never fired — nothing was proved"

    # ── Phase 4 ────────────────────────────────────────────────────────
    assert session_is_usable(db), (
        "the session was left aborted, so the failed audit row has become "
        "everyone else's failure"
    )

    # ── Phase 5 ────────────────────────────────────────────────────────
    renamed = client.patch(
        f"{API}/users/{user_id}",
        headers=superuser_token_headers,
        json={"full_name": "Committed After The Audit Failure"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["full_name"] == "Committed After The Audit Failure"

    # And the account is intact — the repair rolled back to the savepoint, not
    # over the row that was committed before any of this.
    headers = user_authentication_headers(
        client=client, email=email, password=password
    )
    me = client.get(f"{API}/users/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["id"] == user_id


# ── No SMTP + auto-provisioning must still answer with the account ─────


def test_account_creation_survives_provisioning_on_an_instance_without_smtp(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Creating an account must not depend on an email being sendable.

    **The mechanism this guards.** ``create_account`` refreshes ``user``, then
    ``AccountProvisioningService`` commits — a child credential, then an audit
    row. ``Session.commit()`` expires every instance, so the ``user`` handed
    back has no loaded attributes. The route then calls
    ``user_to_public(session, user)``, whose first argument is
    ``**user.model_dump()`` — and ``model_dump()`` reads ``__dict__``
    directly, so unlike ordinary attribute access it does **not** trigger
    SQLAlchemy's refresh-on-expired load. Left alone, ``email`` and ``id`` are
    simply absent, ``UserPublic`` fails validation, and the request 500s
    *after* the account row is committed: the person exists and is told they
    do not. ``user_to_public`` now un-expires the row before dumping it, which
    is why this passes.

    **Why nothing caught it for so long.** With ``settings.emails_enabled``
    true the route calls
    ``EmailConfirmationService.send_confirmation_email(user=user)`` between the
    two, which touches the instance and un-expires it. The test container sets
    ``SMTP_HOST=mailcatcher``, so every other test in the suite runs the masked
    path. Turning email off — the default for a fresh install, and a normal
    deployment — unmasks it. Blanking ``SMTP_HOST`` here is therefore load
    -bearing: without it this test passes against the broken code.

    Covers ``POST /users/`` and ``POST /users/signup``; ``POST
    /private/users/`` shares the builder and the same repair. The Google
    callback is unaffected because it answers with a token rather than a
    ``UserPublic``.
    """
    parent = create_managed_credential(
        client,
        superuser_token_headers,
        name="Company A",
        auto_provision_roles=["agent-user"],
    )["record"]
    with patch("app.core.config.settings.SMTP_HOST", ""):
        admin_created = client.post(
            f"{API}/users/",
            headers=superuser_token_headers,
            json={"email": random_email(), "password": random_lower_string()},
        )
        signed_up = client.post(
            f"{API}/users/signup",
            json={"email": random_email(), "password": random_lower_string()},
        )

    assert admin_created.status_code == 200, admin_created.text[:400]
    assert signed_up.status_code == 200, signed_up.text[:400]

    # Not only a 200 — the payload has to describe the account that was
    # created. The failure mode was a *validation* error on a partially
    # populated projection, so a body missing ``id`` or ``email`` is the same
    # bug one layer along.
    for response in (admin_created, signed_up):
        body = response.json()
        assert body.get("id"), body
        assert body.get("email"), body

    # The control, in the same spirit as ``assert_guard_is_armed`` above: the
    # 500 only happens when provisioning commits between the refresh and the
    # projection. If a later change stops these two routes from provisioning
    # at all, the assertions above go green while covering nothing — and this
    # is the only test that runs the un-masked path.
    provisioned = member_user_ids(
        get_managed_credential(client, superuser_token_headers, parent["id"])
    )
    assert {admin_created.json()["id"], signed_up.json()["id"]} <= provisioned, (
        "Neither account was auto-provisioned, so the 200s above prove "
        "nothing about the bug this test exists for."
    )
