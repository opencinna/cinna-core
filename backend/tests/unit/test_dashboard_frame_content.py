"""Dashboard frame content host (``/d/v/<cap>/``) pieces in isolation (P3).

The HTTP behaviour is covered in ``tests/api/dashboards/test_dashboard_frames.py``.
"""
import logging
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from app.core import security
from app.core.access_log_filter import (
    DropUnloggedPathsFilter,
    install_access_log_filter,
    sentry_before_send,
    sentry_before_send_transaction,
)
from app.core.config import settings
from app.services.artifacts import artifact_cap
from app.services.dashboards import frame_cap
from app.services.dashboards.frame_content import (
    BRIDGE_RELATIVE_PATH,
    build_headers,
    error_headers,
    inject_bridge,
    is_safe_path,
    read_bridge,
)
from app.services.environments.platform_protocol_files import kit_snapshot_dir

BRIDGE = b"/*bridge*/window.__b=1;"


@pytest.fixture(autouse=True)
def origins(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_HOST", "https://core.example.com/app")
    monkeypatch.setattr(settings, "ARTIFACTS_CONTENT_ORIGIN", "https://content.example.com/")


def _cap() -> str:
    cap, _ = frame_cap.mint(uuid.uuid4(), frame_cap.block_target(uuid.uuid4()))
    return cap


# ── Headers ─────────────────────────────────────────────────────────


def test_content_headers_carry_the_exact_csp():
    cap = _cap()
    headers = build_headers(cap)
    src = f"https://content.example.com/d/v/{cap}/"
    assert headers["Content-Security-Policy"] == (
        "sandbox allow-scripts allow-forms allow-modals; "
        "default-src 'none'; "
        f"connect-src {src}; "
        f"script-src {src} 'unsafe-inline'; "
        f"style-src {src} 'unsafe-inline'; "
        f"img-src {src} data: blob:; "
        f"font-src {src} data:; "
        f"media-src {src} blob:; "
        "form-action 'none'; base-uri 'none'; frame-src 'none'; worker-src 'none'; "
        "frame-ancestors https://core.example.com"
    )
    assert "allow-same-origin" not in headers["Content-Security-Policy"]
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"


def test_error_headers_sandbox_load_nothing_and_never_cache():
    headers = error_headers()
    csp = headers["Content-Security-Policy"]
    assert csp == (
        "sandbox allow-scripts allow-forms allow-modals; default-src 'none'; "
        "form-action 'none'; base-uri 'none'; frame-ancestors https://core.example.com"
    )
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Access-Control-Allow-Origin"] == "*"


# ── Bridge injection ────────────────────────────────────────────────


def _wrapped() -> bytes:
    return b"<script>" + BRIDGE + b"</script>"


def test_bridge_goes_right_after_head_case_insensitive():
    html = b"<!doctype html><HTML><HEAD data-x=1><title>t</title></HEAD><body><script>a()</script></body></HTML>"
    out = inject_bridge(html, BRIDGE)
    marker = b"<HEAD data-x=1>"
    at = out.index(marker) + len(marker)
    assert out[at : at + len(_wrapped())] == _wrapped()
    assert out.replace(_wrapped(), b"") == html


def test_header_tag_is_not_taken_for_head():
    html = b"<body><header>x</header><script>a()</script></body>"
    out = inject_bridge(html, BRIDGE)
    assert out == b"<body><header>x</header>" + _wrapped() + b"<script>a()</script></body>"


def test_no_head_bridge_goes_before_first_script():
    html = b"<html><body><p>hi</p><script src=a.js></script><script>b()</script></body></html>"
    out = inject_bridge(html, BRIDGE)
    assert out.index(_wrapped()) == html.index(b"<script")
    assert out.replace(_wrapped(), b"") == html


def test_no_head_no_script_falls_back_to_html_then_doctype_then_start():
    assert inject_bridge(b"<html lang=en><p>x</p></html>", BRIDGE) == (
        b"<html lang=en>" + _wrapped() + b"<p>x</p></html>"
    )
    assert inject_bridge(b"<!DOCTYPE html><p>x</p>", BRIDGE) == (
        b"<!DOCTYPE html>" + _wrapped() + b"<p>x</p>"
    )
    assert inject_bridge(b"<p>x</p>", BRIDGE) == _wrapped() + b"<p>x</p>"


def test_head_inside_a_comment_is_skipped():
    html = b"<!-- <head> old --><html><head><title>t</title></head><script>a()</script></html>"
    out = inject_bridge(html, BRIDGE)
    real = html.index(b"<head><title>") + len(b"<head>")
    assert out[real : real + len(_wrapped())] == _wrapped()
    assert out.replace(_wrapped(), b"") == html
    assert out.count(_wrapped()) == 1


def test_head_string_inside_a_script_is_not_taken_for_head():
    html = b"<html><body><script>var s = '<head>';</script></body></html>"
    out = inject_bridge(html, BRIDGE)
    assert out.index(_wrapped()) == html.index(b"<script")
    assert out.replace(_wrapped(), b"") == html


def test_utf8_bom_is_preserved_in_front_of_the_bridge():
    bom = b"\xef\xbb\xbf"
    out = inject_bridge(bom + b"<p>x</p>", BRIDGE)
    assert out.startswith(bom + _wrapped())
    out = inject_bridge(bom + b"<head></head>", BRIDGE)
    assert out.startswith(bom + b"<head>" + _wrapped())


def test_bridge_bytes_equal_the_kit_file():
    expected = (kit_snapshot_dir(Path(settings.ENV_TEMPLATES_DIR)) / BRIDGE_RELATIVE_PATH).read_bytes()
    assert BRIDGE_RELATIVE_PATH == "protocols/dashboards-bridge.js"
    assert read_bridge() == expected
    assert expected
    out = inject_bridge(b"<head></head>", expected)
    assert out == b"<head><script>" + expected + b"</script></head>"


# ── Paths ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["index.html", "assets/app.js", "a/b/c.css"])
def test_safe_paths_are_accepted(path):
    assert is_safe_path(path)


@pytest.mark.parametrize(
    "path", ["", "api/x", "api", "../x", "a/../b", "./x", "a//b", "/etc/passwd", "a\\b", "a\x00b", "a/"]
)
def test_unsafe_paths_are_refused(path):
    assert not is_safe_path(path)


# ── Cap audience ────────────────────────────────────────────────────


def test_frame_cap_round_trips_and_wrong_aud_is_rejected():
    user_id, block_id = uuid.uuid4(), uuid.uuid4()
    cap, _ = frame_cap.mint(user_id, frame_cap.block_target(block_id))
    claims = frame_cap.verify(cap)
    assert claims is not None and claims.user_id == user_id and claims.target.block_id == block_id

    wrong = security.create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(minutes=5),
        extra_claims={"aud": "something_else", "tgt": frame_cap.block_target(block_id)},
    )
    assert frame_cap.verify(wrong) is None
    no_aud = security.create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(minutes=5),
        extra_claims={"tgt": frame_cap.block_target(block_id)},
    )
    assert frame_cap.verify(no_aud) is None
    assert frame_cap.verify("not-a-cap") is None


def test_artifact_cap_is_not_a_frame_cap_and_vice_versa():
    art = security.create_access_token(
        subject=str(uuid.uuid4()),
        expires_delta=timedelta(minutes=5),
        extra_claims={"aud": artifact_cap.CAP_AUDIENCE, "tgt": frame_cap.block_target(uuid.uuid4())},
    )
    assert frame_cap.verify(art) is None
    assert artifact_cap.verify(_cap()) is None


# ── Access-log filter ───────────────────────────────────────────────


def _record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "GET", path, "1.1", 200), None,
    )


def test_access_log_filter_drops_d_paths_and_keeps_others():
    f = DropUnloggedPathsFilter()
    assert f.filter(_record("/d/v/secretcap/index.html?x=1")) is False
    assert f.filter(_record("/d/")) is False
    assert f.filter(_record("/api/v1/utils/health-check/")) is True
    assert f.filter(_record("/a/v/cap/index.html")) is True
    assert f.filter(_record("/data/x")) is True
    plain = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "hello", None, None)
    assert f.filter(plain) is True


def test_install_access_log_filter_is_idempotent():
    access = logging.getLogger("uvicorn.access")
    before = [x for x in access.filters if isinstance(x, DropUnloggedPathsFilter)]
    try:
        install_access_log_filter()
        install_access_log_filter()
        after = [x for x in access.filters if isinstance(x, DropUnloggedPathsFilter)]
        assert len(after) == max(1, len(before))
    finally:
        for x in list(access.filters):
            if isinstance(x, DropUnloggedPathsFilter) and x not in before:
                access.removeFilter(x)


# ── Sentry scrub ────────────────────────────────────────────────────


def test_sentry_error_event_for_a_d_url_is_scrubbed():
    event = {
        "request": {"url": "https://content.example.com/d/v/SECRETCAP/index.html?q=1", "query_string": "q=1"},
        "transaction": "/d/v/SECRETCAP/index.html",
        "breadcrumbs": {"values": [
            {"message": "GET /d/v/SECRETCAP/app.js", "data": {"url": "https://c.example.com/d/v/SECRETCAP/x?y=2", "http.query": "y=2"}},
        ]},
        "exception": {"values": [{"stacktrace": {"frames": [{"function": "serve", "vars": {"cap": "SECRETCAP"}}]}}]},
        "threads": {"values": [{"stacktrace": {"frames": [{"function": "serve", "vars": {"path": "x"}}]}}]},
    }
    out = sentry_before_send(event, {})
    assert out["request"]["url"] == "https://content.example.com/d/[Filtered]"
    assert "query_string" not in out["request"]
    assert "SECRETCAP" not in str(out)
    assert "http.query" not in out["breadcrumbs"]["values"][0]["data"]
    assert all("vars" not in f for s in ("exception", "threads") for v in out[s]["values"] for f in v["stacktrace"]["frames"])


def test_sentry_event_for_other_urls_is_untouched():
    event = {"request": {"url": "https://api.example.com/api/v1/users/me", "query_string": "a=1"}, "transaction": "/api/v1/users/me"}
    out = sentry_before_send(dict(event), {})
    assert out["request"]["url"] == "https://api.example.com/api/v1/users/me"
    assert out["request"]["query_string"] == "a=1"
    assert sentry_before_send_transaction(dict(event), {}) is not None


def test_sentry_transactions_for_content_urls_are_dropped():
    for url in ("https://c.example.com/d/v/CAP/index.html", "https://c.example.com/a/v/CAP/index.html"):
        assert sentry_before_send_transaction({"request": {"url": url}}, {}) is None
    assert sentry_before_send_transaction({"transaction": "/d/v/CAP/x"}, {}) is None
