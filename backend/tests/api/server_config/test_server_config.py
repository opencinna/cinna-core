"""
Server configuration — the admin-only singleton behind `/admin/server-config`.

The row now carries two unrelated concerns: the login disclaimer, whose version
is a *user-visible* counter (bumping it makes every user re-acknowledge), and
instance switches for public surfaces such as the Local Agent Kit. The tests
below pin the boundary between them, because the update path applies one
payload to both and a single over-broad bump condition would silently re-prompt
every user of the instance every time an admin flipped an unrelated switch.

The kit surface's own 200↔404 response to the flag is covered end-to-end in
``tests/api/cli/test_local_agent_kit.py``.
"""

from fastapi.testclient import TestClient

from app.core.config import settings

CONFIG_URL = f"{settings.API_V1_STR}/admin/server-config"


def _get_config(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.get(CONFIG_URL, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_local_agent_kit_flag_defaults_to_enabled(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Opt-out, not opt-in: an instance nobody configured still publishes."""
    assert _get_config(client, superuser_token_headers)["local_agent_kit_enabled"] is True


def test_toggling_the_kit_flag_does_not_bump_the_disclaimer_version(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An unrelated switch must not re-prompt every user for the disclaimer.

    ``disclaimer_version`` is what decides whether an acknowledged user sees the
    disclaimer again. The update endpoint takes one partial payload for the
    whole row, so this is the assertion that keeps the bump condition scoped to
    the disclaimer's own fields.
    """
    before = _get_config(client, superuser_token_headers)

    response = client.put(
        CONFIG_URL,
        headers=superuser_token_headers,
        json={"local_agent_kit_enabled": False},
    )

    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["local_agent_kit_enabled"] is False
    assert updated["disclaimer_version"] == before["disclaimer_version"]
    # The unrelated disclaimer fields are untouched by a partial update.
    assert updated["disclaimer_enabled"] == before["disclaimer_enabled"]
    assert updated["disclaimer_markdown"] == before["disclaimer_markdown"]


def test_changing_the_disclaimer_still_bumps_the_version(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The control for the test above: the bump condition still has teeth.

    Without this, an update path that never bumped anything would pass the
    "no bump" assertion vacuously.
    """
    before = _get_config(client, superuser_token_headers)

    response = client.put(
        CONFIG_URL,
        headers=superuser_token_headers,
        json={"disclaimer_markdown": "# Terms\n\nUse responsibly."},
    )

    assert response.status_code == 200, response.text
    assert response.json()["disclaimer_version"] == before["disclaimer_version"] + 1


def test_the_kit_flag_survives_a_disclaimer_edit(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A partial update of one concern must not reset the other."""
    client.put(
        CONFIG_URL,
        headers=superuser_token_headers,
        json={"local_agent_kit_enabled": False},
    )

    response = client.put(
        CONFIG_URL,
        headers=superuser_token_headers,
        json={"disclaimer_enabled": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["local_agent_kit_enabled"] is False


def test_non_superuser_cannot_read_or_change_the_config(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    """The instance switches are an admin surface, including the new one."""
    assert client.get(CONFIG_URL, headers=normal_user_token_headers).status_code == 403
    assert (
        client.put(
            CONFIG_URL,
            headers=normal_user_token_headers,
            json={"local_agent_kit_enabled": False},
        ).status_code
        == 403
    )
