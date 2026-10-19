"""User groups share-picker search: GET /user-groups/search.

Open to every authenticated user (agent-user and agent-developer alike);
returns name and member_count only, never members.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.user_group import create_group, search_groups
from tests.utils.utils import random_lower_string

SEARCH = f"{settings.API_V1_STR}/user-groups/search"


def test_any_authenticated_role_can_search_and_sees_only_name_and_count(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    tag = random_lower_string()[:10]
    member, _ = create_random_user_with_headers(client)
    group = create_group(client, h, name=f"Search-{tag}", member_ids=[member["id"]])

    agent_user, user_h = create_random_user_with_headers(client)
    dev, dev_h = create_random_user_with_headers(client)
    promote_to_developer(client, h, dev["id"])

    for headers in (user_h, dev_h, h):
        hits = search_groups(client, headers, q=tag)
        assert hits == [{"id": group["id"], "name": f"Search-{tag}", "member_count": 1}]

    # Case-insensitive
    assert len(search_groups(client, user_h, q=tag.upper())) == 1

    # Unauthenticated rejected
    assert client.get(SEARCH, params={"q": tag}).status_code in (401, 403)


def test_search_limit_clamp_empty_query_and_ordering(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    _, uh = create_random_user_with_headers(client)
    tag = random_lower_string()[:8]
    for i in range(27):
        create_group(client, h, name=f"{tag}-{i:02d}")

    # Empty q returns the first `limit` by name; default limit 10
    assert len(search_groups(client, uh)) == 10
    assert len(search_groups(client, uh, limit=3)) == 3
    # Clamped to 25 above, to 1 below
    assert len(search_groups(client, uh, q=tag, limit=1000)) == 25
    assert len(search_groups(client, uh, q=tag, limit=0)) == 1
    assert len(search_groups(client, uh, q=tag, limit=-5)) == 1
    names = [g["name"] for g in search_groups(client, uh, q=tag, limit=25)]
    assert names == sorted(names)


def test_search_escapes_like_wildcards(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    _, uh = create_random_user_with_headers(client)
    tag = random_lower_string()[:8]
    pct = create_group(client, h, name=f"{tag}-100%-done")
    und = create_group(client, h, name=f"{tag}_x_y")
    create_group(client, h, name=f"{tag}-100X-done")
    create_group(client, h, name=f"{tag}-axxy")

    assert [g["id"] for g in search_groups(client, uh, q="100%")] == [pct["id"]]
    assert [g["id"] for g in search_groups(client, uh, q="%")] == [pct["id"]]
    assert [g["id"] for g in search_groups(client, uh, q=f"{tag}_x_")] == [und["id"]]
    assert [g["id"] for g in search_groups(client, uh, q="_x_y")] == [und["id"]]
    assert search_groups(client, uh, q="\\") == []
