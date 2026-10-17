"""The env-side ``artifacts_publish`` client: what it forwards to the backend.

OpenCode 1.14 sends tools without ``"strict": false``; OpenAI's Responses API
then makes every property required, and gpt-5.4-mini / gpt-5.6-luna fill the
optional ``artifact_id`` and ``entry`` with placeholders (seen live on a core
agent: ``" "``, ``"/"``, ``":uuid:"``, the nil UUID, the published ``.md`` as
``entry``) and loop on the rejections. The client drops what can never be
valid and keeps what can.
"""
import json
import uuid

import httpx
import pytest

from core.server.tools import cinna_artifacts_protocol as protocol
from core.server.tools.cinna_artifacts_protocol import CinnaArtifactsClient

_ARTIFACT_ID = "3f9a2b1c-0d4e-4f5a-8b6c-7d8e9f0a1b2c"


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "artifacts" / "site").mkdir(parents=True)
    (tmp_path / "artifacts" / "site" / "index.html").write_text("<p>hi</p>")
    (tmp_path / "artifacts" / "site" / "other.html").write_text("<p>other</p>")
    (tmp_path / "artifacts" / "q3-summary.md").write_text("# Q3")
    return tmp_path


@pytest.fixture()
def backend(monkeypatch):
    """Record each request body; answer with ``backend.status`` / ``backend.body``."""

    class _Backend:
        status = 200
        body: dict = {
            "artifact_id": _ARTIFACT_ID, "revision": 1, "url": "http://x/artifact/1",
            "kind": "files", "file_count": 1, "size_bytes": 4, "excluded": [],
        }
        sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _Backend.sent.append(json.loads(request.content))
        return httpx.Response(_Backend.status, json=_Backend.body)

    real_client = httpx.Client
    monkeypatch.setattr(
        protocol.httpx, "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    _Backend.sent = []
    return _Backend


def _publish(workspace, **args):
    client = CinnaArtifactsClient(
        backend_url="http://backend", auth_token="t", env_id="e", workspace_dir=workspace,
    )
    return client.call("artifacts_publish", args, None)


@pytest.mark.parametrize(
    "placeholder",
    ["", " ", "/", ":uuid:", ":new", ".placeholder", "q3-summary",
     "/app/workspace/artifacts/q3-summary.md", "00000000-0000-0000-0000-000000000000"],
)
def test_a_placeholder_artifact_id_is_dropped(workspace, backend, placeholder):
    text, is_error = _publish(
        workspace, path="artifacts/q3-summary.md", title="Q3", artifact_id=placeholder,
    )
    assert is_error is False, text
    assert "artifact_id" not in backend.sent[0]


def test_a_real_artifact_id_is_forwarded(workspace, backend):
    _publish(workspace, path="artifacts/q3-summary.md", title="Q3", artifact_id=f" {_ARTIFACT_ID.upper()} ")
    assert backend.sent[0]["artifact_id"] == _ARTIFACT_ID


def test_an_unknown_artifact_id_says_to_omit_it(workspace, backend):
    backend.status, backend.body = 404, {"detail": "Artifact not found"}
    text, is_error = _publish(
        workspace, path="artifacts/q3-summary.md", title="Q3", artifact_id=str(uuid.uuid4()),
    )
    assert is_error is True
    assert text.startswith("Error: Artifact not found.")
    assert "omit it" in text


def test_a_404_without_artifact_id_keeps_the_plain_error(workspace, backend):
    backend.status, backend.body = 404, {"detail": "Agent not found"}
    text, _ = _publish(workspace, path="artifacts/q3-summary.md", title="Q3")
    assert text == "Error: Agent not found"


@pytest.mark.parametrize(
    "entry",
    ["", "  ", "artifacts/q3-summary.md", "q3-summary.md", "index.html"],
)
def test_entry_is_dropped_for_a_single_file(workspace, backend, entry):
    text, is_error = _publish(workspace, path="artifacts/q3-summary.md", title="Q3", entry=entry)
    assert is_error is False, text
    assert "entry" not in backend.sent[0]


@pytest.mark.parametrize(
    ("entry", "sent"),
    [
        ("other.html", "other.html"),
        ("./other.html", "other.html"),
        ("artifacts/site/other.html", "other.html"),
        ("missing.html", None),
        ("index.md", None),
        ("", None),
    ],
)
def test_entry_for_a_folder_must_be_one_of_its_html_files(workspace, backend, entry, sent):
    _publish(workspace, path="artifacts/site/", title="Site", entry=entry)
    assert backend.sent[0]["path"] == "artifacts/site"
    assert backend.sent[0].get("entry") == sent


def test_a_clean_call_is_forwarded_unchanged(workspace, backend):
    _publish(workspace, path="artifacts/site", title="Site", description="d")
    assert backend.sent[0] == {"path": "artifacts/site", "title": "Site", "description": "d"}
