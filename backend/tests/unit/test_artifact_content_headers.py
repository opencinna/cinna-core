"""Artifact content security pieces in isolation: CSP, caps, containment, CORS rewrite.

The HTTP-level behaviour (every response incl. 404 carries these headers, the
CORS wrapper sits outside CORSMiddleware, caps re-checked per request) is
covered in ``tests/api/artifacts/test_artifacts_content.py``.
"""
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core import security
from app.core.artifact_content_middleware import _rewrite
from app.core.config import settings
from app.models.artifacts.artifact import Artifact
from app.services.artifacts import artifact_cap
from app.services.artifacts.artifact_cap import CapClaims
from app.services.artifacts.artifact_content import (
    ArtifactContentService,
    ResolvedRevision,
    _ManifestCache,
    build_headers,
    content_csp,
    error_headers,
    frame_ancestor,
)


@pytest.fixture(autouse=True)
def storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ARTIFACT_STORAGE_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setattr(settings, "FRONTEND_HOST", "https://core.example.com/app")
    monkeypatch.setattr(settings, "ARTIFACTS_CONTENT_ORIGIN", "https://content.example.com/")
    return tmp_path / "artifacts"


# ── CSP ─────────────────────────────────────────────────────────────


def test_content_csp_is_exact():
    base = "https://content.example.com/a/v/CAP/"
    assert content_csp(base, "https://core.example.com") == (
        "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads "
        "allow-modals allow-forms; default-src 'none'; "
        "script-src https://content.example.com/a/v/CAP/ 'unsafe-inline' 'wasm-unsafe-eval' blob:; "
        "style-src https://content.example.com/a/v/CAP/ 'unsafe-inline'; "
        "img-src https://content.example.com/a/v/CAP/ data: blob:; "
        "font-src https://content.example.com/a/v/CAP/ data:; "
        "connect-src https://content.example.com/a/v/CAP/ data: blob:; "
        "media-src https://content.example.com/a/v/CAP/ data: blob:; "
        "worker-src blob:; frame-src https://content.example.com/a/v/CAP/; "
        "form-action 'none'; base-uri 'none'; frame-ancestors https://core.example.com"
    )


def test_build_headers_scope_sources_to_the_cap_path():
    cap, _ = artifact_cap.mint(uuid.uuid4(), uuid.uuid4(), artifact_cap.via_user(uuid.uuid4()))
    headers = build_headers(cap, etag="abc")
    csp = headers["Content-Security-Policy"]
    base = f"https://content.example.com/a/v/{cap}/"
    assert f"script-src {base} " in csp
    assert "'self'" not in csp
    assert "allow-same-origin" not in csp
    assert csp.endswith("frame-ancestors https://core.example.com")
    assert frame_ancestor() == "https://core.example.com"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "private, max-age=3600"
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert headers["ETag"] == '"abc"'
    assert "Cross-Origin-Resource-Policy" not in headers


def test_error_headers_never_cache_and_load_nothing():
    headers = error_headers()
    assert headers["Cache-Control"] == "no-store"
    csp = headers["Content-Security-Policy"]
    assert csp.startswith("sandbox ")
    assert "allow-same-origin" not in csp
    assert "default-src 'none'" in csp and "script-src" not in csp


# ── Caps ────────────────────────────────────────────────────────────


def test_cap_round_trip():
    aid, rid, uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cap, expires_at = artifact_cap.mint(aid, rid, artifact_cap.via_user(uid))
    claims = artifact_cap.verify(cap)
    assert claims == CapClaims(aid, rid, "u", uid, claims.expires_at)
    assert abs((claims.expires_at - expires_at).total_seconds()) < 2
    expected_ttl = timedelta(seconds=settings.ARTIFACT_VIEW_CAP_TTL_SECONDS)
    assert abs((expires_at - datetime.now(UTC)) - expected_ttl) < timedelta(seconds=5)
    link_id = uuid.uuid4()
    link_cap, _ = artifact_cap.mint(aid, rid, artifact_cap.via_link(link_id))
    assert artifact_cap.verify(link_cap).via_kind == "l"
    assert artifact_cap.content_base(cap) == f"https://content.example.com/a/v/{cap}/"
    assert artifact_cap.zip_url(cap) == f"https://content.example.com/a/z/{cap}"


def _signed(claims: dict, *, hours: float = 1) -> str:
    return security.create_access_token(
        subject=claims.pop("sub", str(uuid.uuid4())),
        expires_delta=timedelta(hours=hours),
        extra_claims=claims,
    )


@pytest.mark.parametrize(
    "claims,hours",
    [
        ({"aud": "artifact_view", "rev": str(uuid.uuid4()), "via": f"u:{uuid.uuid4()}"}, -1),  # expired
        ({"aud": "agent_env", "rev": str(uuid.uuid4()), "via": f"u:{uuid.uuid4()}"}, 1),  # wrong aud
        ({"rev": str(uuid.uuid4()), "via": f"u:{uuid.uuid4()}"}, 1),  # user-JWT shape, no aud
        ({"aud": "artifact_view", "via": f"u:{uuid.uuid4()}"}, 1),  # no rev
        ({"aud": "artifact_view", "rev": str(uuid.uuid4()), "via": f"x:{uuid.uuid4()}"}, 1),
        ({"aud": "artifact_view", "rev": "nope", "via": f"u:{uuid.uuid4()}"}, 1),
        ({"aud": "artifact_view", "rev": str(uuid.uuid4()), "via": f"u:{uuid.uuid4()}", "sub": "x"}, 1),
    ],
)
def test_verify_rejects(claims, hours):
    assert artifact_cap.verify(_signed(claims, hours=hours)) is None


def test_verify_rejects_tampering_and_garbage():
    cap, _ = artifact_cap.mint(uuid.uuid4(), uuid.uuid4(), artifact_cap.via_user(uuid.uuid4()))
    header, payload, sig = cap.split(".")
    assert artifact_cap.verify(f"{header}.{payload}.{sig[::-1]}") is None
    assert artifact_cap.verify("x; script-src *") is None
    assert artifact_cap.verify("") is None
    other_key = jwt.encode(
        {"sub": str(uuid.uuid4()), "aud": "artifact_view", "rev": str(uuid.uuid4()),
         "via": f"u:{uuid.uuid4()}", "exp": datetime.now(UTC) + timedelta(hours=1)},
        "not-the-secret-key-not-the-secret-key",
        algorithm="HS256",
    )
    assert artifact_cap.verify(other_key) is None


def test_cap_is_rejected_by_the_user_token_decode():
    """``get_current_user`` decodes without an audience: an aud-bearing cap must fail there."""
    cap, _ = artifact_cap.mint(uuid.uuid4(), uuid.uuid4(), artifact_cap.via_user(uuid.uuid4()))
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(cap, settings.SECRET_KEY, algorithms=[security.ALGORITHM])


# ── Path resolution ─────────────────────────────────────────────────


def _revision(manifest_paths: list[str]) -> ResolvedRevision:
    artifact = Artifact(id=uuid.uuid4(), owner_id=uuid.uuid4(), title="t", source_kind="upload")
    claims = CapClaims(artifact.id, uuid.uuid4(), "u", uuid.uuid4(), datetime.now(UTC))
    revision = ResolvedRevision(
        claims=claims,
        artifact=artifact,
        revision_number=1,
        manifest={p: {"path": p, "size": 1, "sha256": "x", "mime": "text/plain"} for p in manifest_paths},
    )
    revision.revision_dir.mkdir(parents=True)
    return revision


def test_lookup_serves_manifest_files_and_index_fallback():
    rev = _revision(["index.html", "docs/index.html", "a.txt"])
    for p in ["index.html", "docs/index.html", "a.txt"]:
        (rev.revision_dir / p).parent.mkdir(parents=True, exist_ok=True)
        (rev.revision_dir / p).write_text("x")
    assert ArtifactContentService.lookup(rev, "").entry["path"] == "index.html"
    assert ArtifactContentService.lookup(rev, "docs/").entry["path"] == "docs/index.html"
    assert ArtifactContentService.lookup(rev, "a.txt").abs_path == (rev.revision_dir / "a.txt").resolve()
    assert ArtifactContentService.lookup(rev, "missing.txt") is None
    assert ArtifactContentService.lookup(rev, "docs") is None


def test_lookup_contains_paths_even_for_a_poisoned_manifest(storage_root):
    rev = _revision(["../escape.txt", "link.txt", "missing-on-disk.txt"])
    (rev.revision_dir.parent / "escape.txt").write_text("outside")
    outside = storage_root / "secret.txt"
    outside.write_text("secret")
    (rev.revision_dir / "link.txt").symlink_to(outside)
    assert ArtifactContentService.lookup(rev, "../escape.txt") is None
    assert ArtifactContentService.lookup(rev, "link.txt") is None
    assert ArtifactContentService.lookup(rev, "missing-on-disk.txt") is None


def test_manifest_cache_is_bounded_lru():
    cache = _ManifestCache(2)
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cache.put(a, {"a": {}})
    cache.put(b, {"b": {}})
    assert cache.get(a) is not None  # a is now most recent
    cache.put(c, {"c": {}})
    assert cache.get(b) is None
    assert cache.get(a) is not None and cache.get(c) is not None


# ── CORS rewrite ────────────────────────────────────────────────────


def test_rewrite_replaces_reflected_cors_with_wildcard():
    message = {
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"access-control-allow-origin", b"https://evil.example"),
            (b"access-control-allow-credentials", b"true"),
            (b"vary", b"Accept-Encoding, Origin"),
            (b"content-security-policy", b"sandbox allow-scripts; default-src 'none'"),
            (b"cache-control", b"private, max-age=3600"),
        ],
    }
    _rewrite(message)
    headers = {k.decode(): v.decode() for k, v in message["headers"]}
    assert headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in headers
    assert headers["vary"] == "Accept-Encoding"
    assert headers["cache-control"] == "private, max-age=3600"
    assert headers["content-security-policy"] == "sandbox allow-scripts; default-src 'none'"


def test_rewrite_stamps_baseline_on_bare_error():
    message = {"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"application/json")]}
    _rewrite(message)
    headers = {k.decode(): v.decode() for k, v in message["headers"]}
    assert headers["content-security-policy"].startswith("sandbox ")
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"
    assert headers["access-control-allow-origin"] == "*"
