"""
Agent Improvement Requests — account-CLI surfaces (plan §5.7).

``GET /account/improvement-requests`` is the cross-agent list for everything
the account user owns, and the archive route is a dedicated binary endpoint
(the JSON-only ``api-proxy`` cannot carry a ZIP body). Both delegate to the
same ``ImprovementRequestService`` the web routes use, so ownership rules
cannot drift between the two transports.
"""
import zipfile
from io import BytesIO
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.cli import account_cli_headers, bootstrap_account_token
from tests.utils.message import send_message
from tests.utils.session import create_session_via_api

API = settings.API_V1_STR


def _seed_message(
    client: TestClient, headers: dict[str, str], session_id: str, content: str = "Needs work."
) -> None:
    stub = StubAgentEnvConnector(response_text="Noted.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, headers, session_id, content=content)
        drain_tasks()


def test_account_cli_cross_agent_list_and_archive_download(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Owner has two standalone agents, each receiving one self-targeted
       improvement request.
    2. GET /account/improvement-requests (account CLI token) returns rows
       across BOTH agents, newest first; the ``agent_id`` filter narrows it.
    3. GET .../{id}/archive returns a valid binary ZIP via the CLI route.
    4. PATCH via the CLI route updates status, matching the web route.
    5. GET .../{id} (CLI detail route) includes the frozen context block.
    """
    account_jwt, _ = bootstrap_account_token(client, superuser_token_headers)
    acc_headers = account_cli_headers(account_jwt)

    agent_a = create_agent_via_api(client, superuser_token_headers, name="CLI Agent A")
    agent_b = create_agent_via_api(client, superuser_token_headers, name="CLI Agent B")
    drain_tasks()

    session_a = create_session_via_api(client, superuser_token_headers, agent_a["id"])
    _seed_message(client, superuser_token_headers, session_a["id"])
    r_a = client.post(
        f"{API}/improvement-requests",
        headers=superuser_token_headers,
        json={"session_id": session_a["id"], "comment": "Agent A issue."},
    )
    assert r_a.status_code == 201, r_a.text
    request_a = r_a.json()

    session_b = create_session_via_api(client, superuser_token_headers, agent_b["id"])
    _seed_message(client, superuser_token_headers, session_b["id"])
    r_b = client.post(
        f"{API}/improvement-requests",
        headers=superuser_token_headers,
        json={"session_id": session_b["id"], "comment": "Agent B issue."},
    )
    assert r_b.status_code == 201, r_b.text
    request_b = r_b.json()

    # ── Phase 2: cross-agent list via the account CLI ────────────────────
    listing = client.get(f"{API}/cli/account/improvement-requests", headers=acc_headers)
    assert listing.status_code == 200, listing.text
    ids = {row["id"] for row in listing.json()["data"]}
    assert {request_a["id"], request_b["id"]} <= ids

    filtered = client.get(
        f"{API}/cli/account/improvement-requests",
        headers=acc_headers,
        params={"agent_id": agent_a["id"]},
    )
    assert filtered.status_code == 200, filtered.text
    assert {row["id"] for row in filtered.json()["data"]} == {request_a["id"]}

    # ── Phase 3: binary archive download ─────────────────────────────────
    archive = client.get(
        f"{API}/cli/account/improvement-requests/{request_a['id']}/archive",
        headers=acc_headers,
    )
    assert archive.status_code == 200, archive.text
    assert archive.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(BytesIO(archive.content))
    assert zf.testzip() is None
    assert "README.md" in zf.namelist()

    # ── Phase 4: status update via the CLI route ─────────────────────────
    patch_r = client.patch(
        f"{API}/cli/account/improvement-requests/{request_a['id']}",
        headers=acc_headers,
        json={"status": "completed", "resolution_note": "Fixed in v2."},
    )
    assert patch_r.status_code == 200, patch_r.text
    assert patch_r.json()["status"] == "completed"
    assert patch_r.json()["resolution_note"] == "Fixed in v2."

    # ── Phase 5: detail via the CLI route ─────────────────────────────────
    detail = client.get(
        f"{API}/cli/account/improvement-requests/{request_a['id']}", headers=acc_headers
    )
    assert detail.status_code == 200, detail.text
    assert "context" in detail.json()
    assert detail.json()["status"] == "completed"


def test_new_status_requests_ordered_before_others_across_web_and_cli(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Regression guard for ``ImprovementRequestService._list``'s ordering:
    requests with status "new" sort before every other status, and only
    THEN by created_at descending within each group.

    The OLDER of two requests is submitted first and left "new"; the NEWER
    one is moved to "in_progress". Plain ``created_at DESC`` would put the
    newer one first — this only passes if the status-first CASE ordering is
    actually in effect.

    Asserted over both the web card (GET /agents/{id}/improvement-requests)
    and the CLI cross-agent list (GET /cli/account/improvement-requests):
    both are backed by the same shared ``_list``, so the two transports
    cannot disagree. Both calls are unfiltered — filtering on a status
    collapses the CASE to a constant, which would silently not exercise
    this ordering at all.
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="Ordering Agent")
    drain_tasks()
    session = create_session_via_api(client, superuser_token_headers, agent["id"])
    _seed_message(client, superuser_token_headers, session["id"])

    older = client.post(
        f"{API}/improvement-requests",
        headers=superuser_token_headers,
        json={"session_id": session["id"], "comment": "older, stays new"},
    )
    assert older.status_code == 201, older.text
    older_id = older.json()["id"]

    newer = client.post(
        f"{API}/improvement-requests",
        headers=superuser_token_headers,
        json={"session_id": session["id"], "comment": "newer, moved to in_progress"},
    )
    assert newer.status_code == 201, newer.text
    newer_id = newer.json()["id"]

    patch_r = client.patch(
        f"{API}/improvement-requests/{newer_id}",
        headers=superuser_token_headers,
        json={"status": "in_progress"},
    )
    assert patch_r.status_code == 200, patch_r.text

    # ── Web card, unfiltered → new (older) first, then in_progress (newer) ──
    card = client.get(
        f"{API}/agents/{agent['id']}/improvement-requests", headers=superuser_token_headers
    )
    assert card.status_code == 200, card.text
    assert [row["id"] for row in card.json()["data"]] == [older_id, newer_id]

    # ── CLI cross-agent list, unfiltered → same order ────────────────────
    account_jwt, _ = bootstrap_account_token(client, superuser_token_headers)
    acc_headers = account_cli_headers(account_jwt)
    cli_listing = client.get(f"{API}/cli/account/improvement-requests", headers=acc_headers)
    assert cli_listing.status_code == 200, cli_listing.text
    assert [row["id"] for row in cli_listing.json()["data"]] == [older_id, newer_id]
