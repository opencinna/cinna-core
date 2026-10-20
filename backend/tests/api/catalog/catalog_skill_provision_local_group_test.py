"""Desktop skill provision-local when the credential is also group-shared.

Rule: a revoked direct share is final for installs, except that a user-group
share still held is real access -- ``already_shared`` with the credential id and
no share row written. Without that group share the revocation still answers
``not_shareable``. A group-only user with no revocation gets the durable
``skill_local_install`` share as before.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import make_person as make_developer
from tests.api.catalog.catalog_skill_credential_revocation_test import (
    _publish_providing,
    _share_of,
    _shareable,
    _unshare,
)
from tests.api.catalog.catalog_skill_provision_local_test import (
    _outcomes,
    _provision,
    _shares_of,
)
from tests.utils.catalog import make_person
from tests.utils.credential import share_credential_via_api
from tests.utils.user_group import create_group

API = settings.API_V1_STR


def _group_share(client, headers, cid, group_id) -> None:
    r = client.post(
        f"{API}/credentials/{cid}/group-shares", headers=headers, json={"group_id": group_id}
    )
    assert r.status_code == 200, r.text


def _revoke(client, pub, cid, user) -> None:
    share_credential_via_api(client, pub.headers, cid, user.email)
    _unshare(client, pub.headers, cid, _share_of(client, pub.headers, cid, user.id)["id"])


def test_provision_local_revoked_user_with_group_share_is_already_shared_without_row(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    pub = make_developer(client, su)
    grantee = make_person(client, su)
    cred = _shareable(client, pub, "svc-grp-rev")
    pkg = _publish_providing(client, pub, cred, "grp-rev-tool", grantee)
    group = create_group(client, su, member_ids=[grantee.id])
    _revoke(client, pub, cred["id"], grantee)
    _group_share(client, pub.headers, cred["id"], group["id"])

    for _ in range(2):
        assert _outcomes(_provision(client, grantee.headers, pkg)) == {
            "svc-grp-rev": ("already_shared", cred["id"])
        }
    assert _shares_of(client, pub.headers, cred["id"]) == []


def test_provision_local_revoked_user_without_group_share_is_not_shareable(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    pub = make_developer(client, su)
    grantee = make_person(client, su)
    cred = _shareable(client, pub, "svc-grp-norev")
    pkg = _publish_providing(client, pub, cred, "grp-norev-tool", grantee)
    create_group(client, su, member_ids=[grantee.id])  # a group, but no credential share
    _revoke(client, pub, cred["id"], grantee)

    assert _outcomes(_provision(client, grantee.headers, pkg)) == {
        "svc-grp-norev": ("not_shareable", None)
    }
    assert _shares_of(client, pub.headers, cred["id"]) == []


def test_provision_local_group_only_user_without_revocation_gets_durable_share(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    pub = make_developer(client, su)
    grantee = make_person(client, su)
    cred = _shareable(client, pub, "svc-grp-only")
    pkg = _publish_providing(client, pub, cred, "grp-only-tool", grantee)
    group = create_group(client, su, member_ids=[grantee.id])
    _group_share(client, pub.headers, cred["id"], group["id"])

    out = _outcomes(_provision(client, grantee.headers, pkg))["svc-grp-only"]
    assert out[1] == cred["id"]
    assert out[0] in ("shared", "already_shared")
    share = _share_of(client, pub.headers, cred["id"], grantee.id)
    assert share["source"] == "skill_local_install"
