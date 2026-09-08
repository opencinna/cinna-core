"""The container half of a catalog-skill install — fetch, verify, extract.

``AgentEnvService._ensure_catalog_plugin`` is the only place a catalog skill's
files ever land in an agent, and it is the only place the sha256 the backend
published is checked. Both halves of that contract are exercised here against
an archive built by the REAL backend builder
(``SkillCatalogService._build_archive_bytes``), so a change to either side that
breaks the pair fails here rather than in production: the digest the backend
stores at publish is the digest the container refuses to install without.

HTTP is the only thing stubbed. Everything else — tar, digests, the staging
swap, the synthesised ``plugin.json``, the ``.cinna_plugin_ref`` marker — is
the shipping code.

Pure by construction: no database, no TestClient. The API-observable side of
the same flow (an install creating the link, the manifest that carries these
archive coordinates, a ``failed`` result surfacing as ``partial_failures``)
lives in ``tests/api/agents/core/agents_skills_catalog_install_test.py``.
Archive determinism and ``_safe_extract_tar`` are in
``tests/unit/test_skill_catalog_archive.py``.
"""
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from app.services.skills.skill_catalog_service import SkillCatalogService

REF = "8f14e45f-ea8f-4f3b-b0a9-1c2d3e4f5a6b"
URL = "http://backend:8000/api/v1/skills/packages/p/revisions/1/archive"


# ── Fixtures: a real archive, and a stubbed HTTP client ────────────────────


@pytest.fixture
def archive(tmp_path: Path) -> bytes:
    """A published ``pdf-report`` skill, packed the way the backend packs it."""
    skill_dir = tmp_path / "published" / "skills" / "pdf-report"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: pdf-report\ndescription: Makes PDFs\n---\nbody\n"
    )
    script = skill_dir / "scripts" / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    os.chmod(script, 0o755)
    return SkillCatalogService._build_archive_bytes(skill_dir, "pdf-report")


class _FakeStream:
    def __init__(self, status_code: int, payload: bytes):
        self.status_code = status_code
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        # Chunked on purpose: the download cap is enforced against a running
        # total, so a single-chunk stub could not exercise it.
        for start in range(0, len(self._payload), 8):
            yield self._payload[start : start + 8]


class _FakeHTTP:
    """Records every request and replays one canned response."""

    def __init__(self, payload: bytes, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.requests: list[dict] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "_FakeHTTP":
        recorder = self

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def stream(self, method, url, headers=None):
                recorder.requests.append(
                    {"method": method, "url": url, "headers": headers or {}}
                )
                return _FakeStream(recorder.status_code, recorder.payload)

        monkeypatch.setattr(httpx, "Client", _Client)
        return self


@pytest.fixture(autouse=True)
def env_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here runs as a configured environment unless it opts out."""
    monkeypatch.setenv("AGENT_AUTH_TOKEN", "env-token-abc")
    monkeypatch.setenv("ENV_ID", "11111111-2222-3333-4444-555555555555")


def _service(tmp_path: Path):
    """An ``AgentEnvService`` over a throwaway workspace.

    Imported inside the helper because the env-template tree reaches
    ``sys.path`` through this package's conftest, not through the app.
    """
    from core.server.agent_env_service import AgentEnvService

    return AgentEnvService(str(tmp_path / "workspace"))


def _entry(archive_bytes: bytes | None, *, sha: str | None = None, **overrides):
    """A manifest entry the way ``build_plugin_manifest`` emits one."""
    entry = {
        "marketplace_name": "cinna-skills",
        "plugin_name": "pdf-report",
        "source": "catalog",
        "git": None,
        "version": "1.0",
        "archive": None
        if archive_bytes is None
        else {
            "url": URL,
            "sha256": sha or hashlib.sha256(archive_bytes).hexdigest(),
            "ref": REF,
            "description": "Makes PDFs",
        },
    }
    entry.update(overrides)
    return entry


# ── The happy path ─────────────────────────────────────────────────────────


def test_a_verified_archive_is_extracted_with_its_synthesised_metadata(
    tmp_path: Path, archive: bytes, monkeypatch
) -> None:
    """Fetch → verify → extract → mark, on a tarball the backend really built.

    The digest is not hand-written here: it is the sha256 of the bytes the
    publish path would have stored, so this asserts the two sides agree rather
    than that the container agrees with itself.
    """
    http = _FakeHTTP(archive).install(monkeypatch)
    service = _service(tmp_path)
    plugin_dir = service.plugins_dir / "cinna-skills" / "pdf-report"

    status, error = service._ensure_catalog_plugin(plugin_dir, _entry(archive))

    assert (status, error) == ("installed", None)
    assert (plugin_dir / "skills" / "pdf-report" / "SKILL.md").read_text().startswith(
        "---"
    )
    # The executable bit survives: a skill's scripts are meant to be run.
    assert (plugin_dir / "skills" / "pdf-report" / "scripts" / "run.sh").stat().st_mode & 0o111

    # `.claude-plugin/plugin.json` is synthesised, never shipped in the archive
    # — the fields are editable package metadata that cuts no new revision.
    plugin_json = json.loads(
        (plugin_dir / ".claude-plugin" / "plugin.json").read_text()
    )
    assert plugin_json == {
        "name": "pdf-report",
        "version": "1.0",
        "description": "Makes PDFs",
    }
    assert (plugin_dir / ".cinna_plugin_ref").read_text().strip() == REF

    # The download is authenticated as this environment, not as its owner.
    assert len(http.requests) == 1
    request = http.requests[0]
    assert request["url"] == URL
    assert request["headers"]["Authorization"] == "Bearer env-token-abc"
    assert request["headers"]["X-Agent-Env-Id"] == (
        "11111111-2222-3333-4444-555555555555"
    )


def test_a_matching_marker_skips_the_download_but_refreshes_the_metadata(
    tmp_path: Path, archive: bytes, monkeypatch
) -> None:
    """A revision is immutable, so the marker can be trusted — the blurb cannot.

    Renaming a package cuts no new revision, so if ``skipped`` also skipped the
    metadata rewrite the rename would never reach any agent that already had
    the skill.
    """
    http = _FakeHTTP(archive).install(monkeypatch)
    service = _service(tmp_path)
    plugin_dir = service.plugins_dir / "cinna-skills" / "pdf-report"
    service._ensure_catalog_plugin(plugin_dir, _entry(archive))
    assert len(http.requests) == 1

    renamed = _entry(archive, version="1.1")
    renamed["archive"]["description"] = "Renamed in the catalog"

    status, error = service._ensure_catalog_plugin(plugin_dir, renamed)

    assert (status, error) == ("skipped", None)
    assert len(http.requests) == 1, "the archive must not be re-downloaded"
    plugin_json = json.loads(
        (plugin_dir / ".claude-plugin" / "plugin.json").read_text()
    )
    assert plugin_json["description"] == "Renamed in the catalog"
    assert plugin_json["version"] == "1.1"


# ── Refusals ───────────────────────────────────────────────────────────────


def test_a_checksum_mismatch_installs_nothing_and_keeps_what_is_there(
    tmp_path: Path, archive: bytes, monkeypatch
) -> None:
    """Unverified bytes are never extracted, and never destroy a good install.

    The staging-then-swap order is what makes the second half true: a
    verification failure returns before the existing plugin directory is
    touched, so an agent that already had a working copy keeps it.
    """
    _FakeHTTP(archive).install(monkeypatch)
    service = _service(tmp_path)
    plugin_dir = service.plugins_dir / "cinna-skills" / "pdf-report"
    service._ensure_catalog_plugin(plugin_dir, _entry(archive))

    tampered = _entry(archive, sha="0" * 64)
    tampered["archive"]["ref"] = "a-newer-revision-id"

    status, error = service._ensure_catalog_plugin(plugin_dir, tampered)

    assert status == "failed"
    assert "checksum mismatch" in error
    # The previous, verified revision is still on disk and still marked.
    assert (plugin_dir / "skills" / "pdf-report" / "SKILL.md").is_file()
    assert (plugin_dir / ".cinna_plugin_ref").read_text().strip() == REF


def test_a_missing_archive_block_reports_the_revision_as_gone(
    tmp_path: Path, monkeypatch
) -> None:
    """The backend emits an entry with ``archive=null`` for an orphaned link.

    Reported as a per-plugin failure rather than pruned: the agent keeps the
    files it already has, and the UI can say "source unavailable" (§9).
    """
    _FakeHTTP(b"").install(monkeypatch)
    service = _service(tmp_path)
    plugin_dir = service.plugins_dir / "cinna-skills" / "pdf-report"

    status, error = service._ensure_catalog_plugin(plugin_dir, _entry(None))

    assert (status, error) == ("failed", "catalog_revision_missing")
    assert not plugin_dir.exists()


@pytest.mark.parametrize("missing", ["AGENT_AUTH_TOKEN", "ENV_ID"])
def test_an_environment_without_credentials_refuses_to_download(
    tmp_path: Path, archive: bytes, monkeypatch, missing: str
) -> None:
    """No token or no env id → the request is never made."""
    http = _FakeHTTP(archive).install(monkeypatch)
    monkeypatch.delenv(missing)
    service = _service(tmp_path)

    status, error = service._ensure_catalog_plugin(
        service.plugins_dir / "cinna-skills" / "pdf-report", _entry(archive)
    )

    assert status == "failed"
    assert "not configured" in error
    assert http.requests == []


def test_a_non_200_answer_is_reported_with_its_status(
    tmp_path: Path, archive: bytes, monkeypatch
) -> None:
    """403 is the answer for an env whose agent has no link for the revision."""
    _FakeHTTP(archive, status_code=403).install(monkeypatch)
    service = _service(tmp_path)

    status, error = service._ensure_catalog_plugin(
        service.plugins_dir / "cinna-skills" / "pdf-report", _entry(archive)
    )

    assert status == "failed"
    assert "HTTP 403" in error


def test_an_oversized_download_is_cut_off_mid_stream(
    tmp_path: Path, archive: bytes, monkeypatch
) -> None:
    """The cap bounds MEMORY, so it is checked while reading, not after.

    Lowered on the instance rather than feeding the stub 24 MB.
    """
    _FakeHTTP(archive).install(monkeypatch)
    service = _service(tmp_path)
    service._CATALOG_ARCHIVE_MAX_BYTES = 8

    status, error = service._ensure_catalog_plugin(
        service.plugins_dir / "cinna-skills" / "pdf-report", _entry(archive)
    )

    assert status == "failed"
    assert "larger than" in error


def test_a_non_archive_body_is_rejected_before_anything_is_written(
    tmp_path: Path, monkeypatch
) -> None:
    """Bytes that verify but are not a tarball still install nothing."""
    payload = b"this is not a tar.gz"
    _FakeHTTP(payload).install(monkeypatch)
    service = _service(tmp_path)
    plugin_dir = service.plugins_dir / "cinna-skills" / "pdf-report"

    status, error = service._ensure_catalog_plugin(plugin_dir, _entry(payload))

    assert status == "failed"
    assert "could not be read" in error
    assert not plugin_dir.exists()
