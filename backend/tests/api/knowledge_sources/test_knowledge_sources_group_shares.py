"""User-group shares on knowledge sources (User Groups P4).

Scenarios:
  1. Routes are superuser-only (403 for a plain user on list/add/remove) and
     manage the share list: add is idempotent, list shows name + member_count,
     unknown source/group/share answer 404, ``shared_group_count`` on the source.
  2. A group member queries a ``shared`` source; a non-member does not; removal
     from the group excludes the source on the next query.
  3. Direct user share and group share coexist: revoking one keeps the other.
  4. A group share on a ``private`` source grants nothing.
"""
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.api.knowledge_sources.test_knowledge_query import (
    _QUERY_URL,
    _agent_headers_for_owner,
    _insert_article,
)
from tests.stubs.knowledge_embeddings import (
    EMBEDDING_DIMS,
    QUERY_EMBEDDING,
    RELEVANT_EMBEDDING,
)
from tests.utils.knowledge_source import (
    add_shared_user,
    create_knowledge_source,
    get_knowledge_source,
    remove_shared_user,
)
from tests.utils.user import create_random_user, create_random_user_with_headers
from tests.utils.user_group import add_members, create_group, remove_member

_BASE = f"{settings.API_V1_STR}/knowledge-sources"


def _groups_url(source_id: str, group_id: str | None = None) -> str:
    url = f"{_BASE}/{source_id}/shared-groups"
    return f"{url}/{group_id}" if group_id else url


def _share_group(client, su, source_id: str, group_id: str) -> dict:
    r = client.post(_groups_url(source_id), headers=su, json={"group_id": group_id})
    assert r.status_code == 200, r.text
    return r.json()


def _unshare_group(client, su, source_id: str, group_id: str) -> None:
    r = client.delete(_groups_url(source_id, group_id), headers=su)
    assert r.status_code == 200, r.text


def _connected_source_with_article(client, db, su, access_level: str) -> tuple[dict, str]:
    source = create_knowledge_source(client, su, access_level=access_level)
    with patch(
        "app.services.knowledge.knowledge_source_service.verify_repository_access",
        return_value=(True, "Repository accessible"),
    ):
        r = client.post(f"{_BASE}/{source['id']}/check-access", headers=su)
        assert r.status_code == 200 and r.json()["accessible"] is True
    article = _insert_article(
        db,
        uuid.UUID(source["id"]),
        {
            "title": f"{access_level} article",
            "description": "group share article",
            "tags": [access_level],
            "features": [],
            "file_path": f"articles/{access_level}-{uuid.uuid4().hex[:6]}.md",
            "content": f"# {access_level}\n\nContent for group share tests.",
        },
        RELEVANT_EMBEDDING,
    )
    return source, str(article.id)


def _query_ids(client: TestClient, headers: dict[str, str]) -> set[str]:
    with patch(
        "app.services.knowledge.embedding_service.generate_query_embedding",
        return_value=(QUERY_EMBEDDING, EMBEDDING_DIMS),
    ):
        r = client.post(_QUERY_URL, headers=headers, json={"query": "content"})
    assert r.status_code == 200, r.text
    return {a["id"] for a in r.json()["articles"]}


def test_shared_groups_routes_are_superuser_only_and_manage_the_list(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. Plain user gets 403 on list / add / remove
      2. Empty list; add returns name + member_count; add is idempotent
      3. List and ``shared_group_count`` reflect the share
      4. Unknown source / group / share answer 404
      5. Remove drops the row; a second remove is 404
    """
    su = superuser_token_headers
    _, plain = create_random_user_with_headers(client)
    member = create_random_user(client)
    source = create_knowledge_source(client, su, access_level="shared")
    sid = source["id"]
    group = create_group(client, su, member_ids=[member["id"]])
    gid = group["id"]

    assert client.get(_groups_url(sid), headers=plain).status_code == 403
    assert (
        client.post(_groups_url(sid), headers=plain, json={"group_id": gid}).status_code
        == 403
    )
    assert client.delete(_groups_url(sid, gid), headers=plain).status_code == 403

    r = client.get(_groups_url(sid), headers=su)
    assert r.status_code == 200 and r.json() == []
    assert get_knowledge_source(client, su, sid).get("shared_group_count", 0) == 0

    first = _share_group(client, su, sid, gid)
    assert first["group_id"] == gid
    assert first["name"] == group["name"]
    assert first["member_count"] == 1
    again = _share_group(client, su, sid, gid)
    assert again["group_id"] == gid

    listed = client.get(_groups_url(sid), headers=su).json()
    assert [g["group_id"] for g in listed] == [gid]
    assert listed[0]["member_count"] == 1 and listed[0]["created_at"]
    assert get_knowledge_source(client, su, sid)["shared_group_count"] == 1

    ghost = str(uuid.uuid4())
    assert client.get(_groups_url(ghost), headers=su).status_code == 404
    assert (
        client.post(_groups_url(ghost), headers=su, json={"group_id": gid}).status_code
        == 404
    )
    assert (
        client.post(_groups_url(sid), headers=su, json={"group_id": ghost}).status_code
        == 404
    )
    assert client.delete(_groups_url(sid, ghost), headers=su).status_code == 404

    _unshare_group(client, su, sid, gid)
    assert client.get(_groups_url(sid), headers=su).json() == []
    assert client.delete(_groups_url(sid, gid), headers=su).status_code == 404


def test_group_member_queries_shared_source_until_removed(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
      1. Member and non-member agents see only the public baseline at first
      2. Group share -> member sees the shared article; non-member does not
      3. Member removed from the group -> next query excludes the source
      4. Re-added, then the group share revoked -> excluded again
    """
    su = superuser_token_headers
    source, article_id = _connected_source_with_article(client, db, su, "shared")
    member = create_random_user(client)
    outsider = create_random_user(client)
    member_h = _agent_headers_for_owner(db, uuid.UUID(member["id"]), "ks-group-member-tok")
    outsider_h = _agent_headers_for_owner(db, uuid.UUID(outsider["id"]), "ks-group-outsider-tok")
    group = create_group(client, su, member_ids=[member["id"]])

    assert article_id not in _query_ids(client, member_h)

    _share_group(client, su, source["id"], group["id"])
    assert article_id in _query_ids(client, member_h)
    assert article_id not in _query_ids(client, outsider_h)
    with patch(
        "app.services.knowledge.embedding_service.generate_query_embedding",
        return_value=(QUERY_EMBEDDING, EMBEDDING_DIMS),
    ):
        r = client.post(
            _QUERY_URL, headers=member_h,
            json={"query": "content", "article_ids": [article_id]},
        )
    assert r.status_code == 200

    remove_member(client, su, group["id"], member["id"])
    assert article_id not in _query_ids(client, member_h)
    r = client.post(
        _QUERY_URL, headers=member_h,
        json={"query": "content", "article_ids": [article_id]},
    )
    assert r.status_code == 403

    add_members(client, su, group["id"], [member["id"]])
    assert article_id in _query_ids(client, member_h)
    _unshare_group(client, su, source["id"], group["id"])
    assert article_id not in _query_ids(client, member_h)


def test_direct_user_share_and_group_share_coexist(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Revoking the direct share keeps the group path; revoking the group keeps the direct one."""
    su = superuser_token_headers
    source, article_id = _connected_source_with_article(client, db, su, "shared")
    user = create_random_user(client)
    headers = _agent_headers_for_owner(db, uuid.UUID(user["id"]), "ks-coexist-tok")
    group = create_group(client, su, member_ids=[user["id"]])

    add_shared_user(client, su, source["id"], user["id"])
    _share_group(client, su, source["id"], group["id"])
    assert article_id in _query_ids(client, headers)

    assert remove_shared_user(client, su, source["id"], user["id"]).status_code == 200
    assert article_id in _query_ids(client, headers)

    add_shared_user(client, su, source["id"], user["id"])
    _unshare_group(client, su, source["id"], group["id"])
    assert article_id in _query_ids(client, headers)

    remove_shared_user(client, su, source["id"], user["id"])
    assert article_id not in _query_ids(client, headers)


def test_group_share_on_private_source_grants_nothing(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    su = superuser_token_headers
    source, article_id = _connected_source_with_article(client, db, su, "private")
    user = create_random_user(client)
    headers = _agent_headers_for_owner(db, uuid.UUID(user["id"]), "ks-private-tok")
    group = create_group(client, su, member_ids=[user["id"]])

    _share_group(client, su, source["id"], group["id"])
    assert article_id not in _query_ids(client, headers)
    r = client.post(
        _QUERY_URL, headers=headers,
        json={"query": "content", "article_ids": [article_id]},
    )
    assert r.status_code == 403
