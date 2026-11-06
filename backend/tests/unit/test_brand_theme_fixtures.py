"""The shared brand theme fixtures (``app/services/brand_theme/fixtures/``)
against the validator and the SVG sanitiser.

These fixtures are copied into Cinna Desktop (README §3.3), so they are part
of the contract: every ``invalid/`` package must fail with exactly the errors
its ``expect.json`` lists, its tag must agree with ``ERROR_CODES``, and every
``served/served.json`` must be what core's own projection produces.

Regenerate the ``served/`` snapshots after an intended change with::

    BRAND_THEME_REGEN_FIXTURES=1 python -m pytest tests/unit/test_brand_theme_fixtures.py

API-observable behaviour (upload, publish, the public endpoint) is covered
in ``tests/api/brand_theme/``.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image

from app.models.brand_theme.brand_theme import BrandThemePublic
from app.services.brand_theme import schema as S
from app.services.brand_theme.images import sniff_mime
from app.services.brand_theme.package import read_folder
from app.services.brand_theme.served import AssetMeta, served_document
from app.services.brand_theme.svg_sanitizer import SvgRefused, sanitise_svg
from app.services.brand_theme.validator import ERROR_CODES, INCOMPATIBLE, UNSAFE, check_package

FIXTURES = Path(__file__).resolve().parents[2] / "app" / "services" / "brand_theme" / "fixtures"
REGEN = os.environ.get("BRAND_THEME_REGEN_FIXTURES") == "1"
SERVED_REVISION = 1


def _dirs(kind: str) -> list[Path]:
    return sorted(p for p in (FIXTURES / kind).iterdir() if p.is_dir())


def _ids(paths: list[Path]) -> list[str]:
    return [p.name for p in paths]


VALID = _dirs("valid")
INVALID = _dirs("invalid")
SVG_CASES = _dirs("svg")


# ── validity ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("fixture", VALID, ids=_ids(VALID))
def test_valid_fixture_passes(fixture: Path) -> None:
    theme, assets = read_folder(str(fixture))
    result = check_package(theme, assets)
    assert result.errors == [], [str(e) for e in result.errors]
    assert result.theme is not None
    for mode in S.MODES:
        assert set(result.theme["modes"][mode]) == set(S.ROLES)
        for value in result.theme["modes"][mode].values():
            assert len(value) == 7 and value == value.lower() and value.startswith("#")


@pytest.mark.parametrize("fixture", INVALID, ids=_ids(INVALID))
def test_invalid_fixture_fails_with_exactly_its_expected_errors(fixture: Path) -> None:
    expect = json.loads((fixture / "expect.json").read_text())
    theme, assets = read_folder(str(fixture))
    result = check_package(theme, assets)
    got = sorted((e.path, e.code) for e in result.errors)
    want = sorted((e["path"], e["code"]) for e in expect["errors"])
    assert got == want, [str(e) for e in result.errors]
    assert result.theme is None and result.assets == {}
    # Every message names what is wrong in a sentence, not just a code.
    assert all(len(e.message) > 20 for e in result.errors)


@pytest.mark.parametrize("fixture", INVALID, ids=_ids(INVALID))
def test_invalid_fixture_tag_agrees_with_error_codes(fixture: Path) -> None:
    expect = json.loads((fixture / "expect.json").read_text())
    tags = {ERROR_CODES[e["code"]] for e in expect["errors"]}
    assert expect["tag"] == (UNSAFE if UNSAFE in tags else INCOMPATIBLE)


def test_every_rule_has_a_fixture() -> None:
    """Each error code reachable from a package has at least one fixture
    (codes only reachable through malformed shapes are listed explicitly)."""
    covered = {
        e["code"]
        for f in INVALID
        for e in json.loads((f / "expect.json").read_text())["errors"]
    }
    without_fixture = {"invalid_type", "missing_key", "asset_corrupt", "asset_animated",
                       "svg_encoding", "svg_parse", "svg_too_complex", "invalid_text"}
    assert set(ERROR_CODES) - covered <= without_fixture


def test_color_forms_normalise_to_lowercase_hex() -> None:
    theme, assets = read_folder(str(FIXTURES / "valid" / "color-forms"))
    modes = check_package(theme, assets).theme["modes"]  # type: ignore[index]
    assert modes["dark"]["surface.base"] == "#0f0f0f"
    assert modes["dark"]["border"] == "#333333"
    assert modes["dark"]["text.primary"] == "#e0e0e0"
    assert modes["dark"]["text.secondary"] == "#999999"
    assert modes["dark"]["chart.grid"] == "#1f1f1f"
    assert modes["light"]["surface.base"] == "#ffffff"
    assert modes["light"]["text.primary"] == "#1a1a1a"
    assert modes["light"]["accent"] == "#2e88b8"


def test_normalisation_round_trip_is_stable() -> None:
    """The normalised theme validates again to itself, which is what publish
    relies on when it re-runs the validator on stored drafts."""
    for fixture in VALID:
        theme, assets = read_folder(str(fixture))
        first = check_package(theme, assets)
        stored = {p: a.data for p, a in first.assets.items()}
        second = check_package(first.theme, stored)
        assert second.errors == []
        assert second.theme == first.theme
        assert {p: a.sha256 for p, a in second.assets.items()} == {p: a.sha256 for p, a in first.assets.items()}


# ── sanitiser cases ────────────────────────────────────────────────────

@pytest.mark.parametrize("case", SVG_CASES, ids=_ids(SVG_CASES))
def test_svg_case(case: Path) -> None:
    expect = json.loads((case / "expect.json").read_text())
    data = (case / "input.svg").read_bytes()
    if expect["core"] == "refused":
        with pytest.raises(SvgRefused) as exc:
            sanitise_svg(data)
        assert exc.value.code == expect["code"]
        return
    out = sanitise_svg(data)
    text = out.data.decode()
    for needle in expect.get("must_not_contain", []):
        assert needle not in text
    for needle in expect.get("must_contain", []):
        assert needle in text
    assert sanitise_svg(out.data).data == out.data, "sanitiser must be idempotent"
    golden = case / "sanitised.svg"
    if REGEN:
        golden.write_bytes(out.data)
    assert golden.read_bytes() == out.data


# ── served snapshots (what the desktop parser is tested against) ──────

def _raw_meta(data: bytes) -> AssetMeta:
    mime = sniff_mime(data) or "application/octet-stream"
    width = height = None
    if mime in S.RASTER_MIMES:
        try:
            with Image.open(io.BytesIO(data)) as im:
                width, height = im.size
        except Exception:
            pass
    return AssetMeta(hashlib.sha256(data).hexdigest(), mime, len(data), width, height)


def _served_unchecked(fixture: Path) -> tuple[dict | None, dict[str, bytes]]:
    """What a server that skipped validation would send: the raw theme with
    each asset path swapped for an asset object over the raw bytes. A path
    with no file is served as null, exactly as core serves a stored theme
    whose asset has gone missing."""
    theme, assets = read_folder(str(fixture))
    try:
        doc = json.loads(theme)
    except ValueError:
        return None, {}
    metas: dict[str, AssetMeta] = {}
    for block in ("logo", "background"):
        for mode in S.MODES:
            path = (doc.get(block) or {}).get(mode)
            if isinstance(path, str):
                if path in assets:
                    metas[path] = _raw_meta(assets[path])
    return served_document(doc, metas, SERVED_REVISION), {}


def _served_valid(fixture: Path) -> tuple[dict, dict[str, bytes]]:
    theme, assets = read_folder(str(fixture))
    result = check_package(theme, assets)
    metas = {p: AssetMeta(a.sha256, a.mime, a.size, a.width, a.height) for p, a in result.assets.items()}
    package_hashes = {hashlib.sha256(b).hexdigest() for b in assets.values()}
    rewritten = {a.sha256: a.data for a in result.assets.values() if a.sha256 not in package_hashes}
    return served_document(result.theme, metas, SERVED_REVISION), rewritten  # type: ignore[arg-type]


@pytest.mark.parametrize("fixture", VALID + INVALID, ids=[f"{p.parent.name}/{p.name}" for p in VALID + INVALID])
def test_served_snapshot(fixture: Path) -> None:
    doc, rewritten = _served_valid(fixture) if fixture.parent.name == "valid" else _served_unchecked(fixture)
    served_dir = fixture / "served"
    if REGEN:
        shutil.rmtree(served_dir, ignore_errors=True)
        if doc is not None:
            (served_dir / "assets").mkdir(parents=True) if rewritten else served_dir.mkdir(parents=True)
            (served_dir / "served.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
            for sha, data in rewritten.items():
                (served_dir / "assets" / sha).write_bytes(data)
    if doc is None:
        assert not served_dir.exists()
        return
    assert json.loads((served_dir / "served.json").read_text()) == doc
    on_disk = {p.name: p.read_bytes() for p in (served_dir / "assets").iterdir()} if (served_dir / "assets").exists() else {}
    assert on_disk == rewritten


@pytest.mark.parametrize("fixture", VALID, ids=_ids(VALID))
def test_served_document_matches_public_model(fixture: Path) -> None:
    doc, _ = _served_valid(fixture)
    parsed = BrandThemePublic.model_validate(doc)
    assert parsed.revision == SERVED_REVISION
    assert set(parsed.theme.modes) == set(S.MODES)
