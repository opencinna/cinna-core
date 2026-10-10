"""Push policy: a desktop agent's manifest ``id`` is bound for good.

Pure unit test (no DB, no TestClient, no git): drives
``push_policy.check_manifest`` with ``bound_id`` directly, and ``evaluate`` with
a stub git so the ``CINNA_BOUND_MANIFEST_ID`` environment hand-off is covered
too. Cases: equal, different, different case / whitespace, missing id, no
binding.

API-observable path (a real ``git push`` with a changed id is refused):
``tests/api/catalog/catalog_desktop_push_policy_test.py``.
"""
import io
import json
import uuid

import pytest

from app.services.cli.platform_knowledge_assets import local_kit_dir
from app.services.library import push_policy
from app.services.library.push_policy import (
    MSG_MANIFEST_ID_CHANGED,
    PushRefused,
    check_manifest,
)

BOUND = "0f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b"
SHA = "a" * 40
ZERO = "0" * 40


def _server_version() -> str:
    raw = (local_kit_dir() / "CONTRACT_VERSION").read_text().strip()
    return raw.split("-")[0]


def _manifest(**overrides) -> bytes:
    document = {
        "id": BOUND,
        "name": "My Agent",
        "slug": "my-agent",
        "description": "A test agent",
        "contract_version": _server_version(),
    }
    document.update(overrides)
    return json.dumps({k: v for k, v in document.items() if v is not None}).encode()


def _check(raw: bytes, bound_id: str | None) -> None:
    check_manifest(raw, local_kit_dir(), io.StringIO(), bound_id=bound_id)


# ── check_manifest ────────────────────────────────────────────────────────


def test_equal_id_is_accepted() -> None:
    _check(_manifest(), BOUND)


@pytest.mark.parametrize(
    "bound",
    [BOUND.upper(), f"  {BOUND}  ", BOUND.replace("0f1e", "0F1E")],
    ids=["upper", "padded", "mixed"],
)
def test_case_and_surrounding_whitespace_are_not_a_change(bound: str) -> None:
    _check(_manifest(), bound)
    _check(_manifest(id=BOUND.upper()), BOUND)


def test_different_id_is_refused_with_the_identity_sentence() -> None:
    with pytest.raises(PushRefused) as exc:
        _check(_manifest(id=str(uuid.uuid4())), BOUND)
    assert str(exc.value) == MSG_MANIFEST_ID_CHANGED
    assert MSG_MANIFEST_ID_CHANGED == (
        "This agent's identity changed; share it as a new agent instead."
    )


def test_missing_id_on_a_current_manifest_is_invalid_before_identity() -> None:
    """The schema requires ``id`` next to ``contract_version``, so the refusal
    is the invalid-manifest sentence, not the identity one."""
    with pytest.raises(PushRefused) as exc:
        _check(_manifest(id=None), BOUND)
    assert str(exc.value) != MSG_MANIFEST_ID_CHANGED
    assert "cinna-agent.json isn't valid" in str(exc.value)


def test_missing_id_on_a_legacy_manifest_is_an_identity_change() -> None:
    """A legacy folder (integer ``schema_version``, no ``contract_version``, no
    ``id``) parses, but it cannot carry the bound identity."""
    legacy = json.dumps(
        {"schema_version": 1, "name": "Old", "slug": "old", "description": "legacy"}
    ).encode()
    _check(legacy, None)  # accepted when nothing is bound
    with pytest.raises(PushRefused) as exc:
        _check(legacy, BOUND)
    assert str(exc.value) == MSG_MANIFEST_ID_CHANGED


@pytest.mark.parametrize("bound", [None, ""])
def test_no_binding_accepts_any_valid_id(bound: str | None) -> None:
    _check(_manifest(id=str(uuid.uuid4())), bound)


def test_invalid_manifest_is_refused_before_the_identity_check() -> None:
    with pytest.raises(PushRefused) as exc:
        _check(b"not json", BOUND)
    assert str(exc.value) != MSG_MANIFEST_ID_CHANGED


# ── evaluate: CINNA_BOUND_MANIFEST_ID reaches check_manifest ─────────────


class _StubGit:
    """Just enough of ``push_policy.Git`` for one clean update."""

    def __init__(self, manifest: bytes) -> None:
        self.manifest = manifest
        self.tip_symlinks: list[str] = []

    def new_blobs(self, new: str) -> list[tuple[str, int]]:
        return [("cinna-agent.json", len(self.manifest))]

    def tip_tree(self, new: str) -> list[tuple[str, int]]:
        return [("cinna-agent.json", len(self.manifest))]

    def new_symlinks(self, new: str) -> list[str]:
        return []

    def show_manifest(self, new: str) -> bytes | None:
        return self.manifest

    def config_kind(self) -> str | None:
        return "agent"


def _environ(bound: str | None) -> dict[str, str]:
    env = {"CINNA_KIT_DIR": str(local_kit_dir().resolve()), "CINNA_REPO_KIND": "agent"}
    if bound is not None:
        env["CINNA_BOUND_MANIFEST_ID"] = bound
    return env


def _evaluate(manifest: bytes, bound: str | None, kind: str = "agent") -> None:
    env = _environ(bound)
    env["CINNA_REPO_KIND"] = kind
    push_policy.evaluate(
        [(ZERO, SHA, "refs/heads/main")], env, io.StringIO(), git=_StubGit(manifest)
    )


def test_evaluate_passes_the_bound_id_from_the_environment() -> None:
    _evaluate(_manifest(), BOUND)
    with pytest.raises(PushRefused) as exc:
        _evaluate(_manifest(id=str(uuid.uuid4())), BOUND)
    assert str(exc.value) == MSG_MANIFEST_ID_CHANGED


@pytest.mark.parametrize("bound", [None, ""])
def test_evaluate_without_a_bound_id_accepts_any_id(bound: str | None) -> None:
    _evaluate(_manifest(id=str(uuid.uuid4())), bound)


def test_mirror_repos_are_not_bound() -> None:
    """The manifest rules (identity included) apply to agent repos only."""
    _evaluate(_manifest(id=str(uuid.uuid4())), BOUND, kind="mirror")
