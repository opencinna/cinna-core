"""Brand theme validator internals: color parsing, readability messages and
the package reader. Rule-by-rule coverage is fixture-driven in
``test_brand_theme_fixtures.py``; the API path is in ``tests/api/brand_theme/``.
"""
from __future__ import annotations

import copy
import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app.services.brand_theme import schema as S
from app.services.brand_theme.colors import ColorError, contrast_ratio, format_ratio, normalise
from app.services.brand_theme.package import PackageError, read_folder, read_zip
from app.services.brand_theme.validator import check_package, validate_package

FIXTURES = Path(__file__).resolve().parents[2] / "app" / "services" / "brand_theme" / "fixtures"


def _minimal() -> dict:
    theme, _ = read_folder(str(FIXTURES / "valid" / "minimal"))
    return json.loads(theme)


# ── colors ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value, expected",
    [
        ("#ABC", "#aabbcc"),
        ("#0F0F0F", "#0f0f0f"),
        ("  #123456 ", "#123456"),
        ("rgb(255 0 0)", "#ff0000"),
        ("rgb(0, 128, 255)", "#0080ff"),
        ("RGB(100% 0% 50%)", "#ff0080"),
        ("hsl(0 100% 50%)", "#ff0000"),
        ("hsl(120deg, 100%, 25%)", "#008000"),
        ("oklch(1 0 0)", "#ffffff"),
        ("oklch(0% 0 0)", "#000000"),
    ],
)
def test_accepted_color_forms_normalise(value: str, expected: str) -> None:
    assert normalise(value) == expected
    assert normalise(normalise(value)) == expected


@pytest.mark.parametrize(
    "value, code",
    [
        ("red", "invalid_color"),
        ("var(--x)", "invalid_color"),
        ("url(#a)", "invalid_color"),
        ("calc(1px)", "invalid_color"),
        ("#fff;", "invalid_color"),
        ("#fff}", "invalid_color"),
        ("#12345", "invalid_color"),
        ("#ffff", "color_alpha"),
        ("#ffffff80", "color_alpha"),
        ("rgba(0,0,0,0.5)", "color_alpha"),
        ("rgb(0 0 0 / 50%)", "color_alpha"),
        ("rgb(0, 0, 0, 1)", "color_alpha"),
        ("rgb(256 0 0)", "invalid_color"),
        ("hsl(10 50 50)", "invalid_color"),
        ("oklch(0.7 0.4 150)", "color_out_of_gamut"),
        ("lab(50 0 0)", "invalid_color"),
        ("oklch(0.5 0.1 1e309)", "invalid_color"),
        ("oklch(0.5 1e200 10)", "invalid_color"),
        ("hsl(1e309 50% 50%)", "invalid_color"),
        ("rgb(nan 0 0)", "invalid_color"),
        (12, "invalid_color"),
        (None, "invalid_color"),
    ],
)
def test_refused_color_values(value: object, code: str) -> None:
    with pytest.raises(ColorError) as exc:
        normalise(value)
    assert exc.value.code == code


def test_out_of_gamut_message_suggests_a_hex() -> None:
    with pytest.raises(ColorError) as exc:
        normalise("oklch(0.7 0.4 150)")
    assert "nearest sRGB color #" in exc.value.message


def test_contrast_message_matches_the_contract_example() -> None:
    theme = _minimal()
    theme["modes"]["dark"]["text.muted"] = "#2a2a2a"
    errors = validate_package(theme, {})
    assert [(e.path, e.code) for e in errors] == [("modes.dark.text.muted", "low_contrast")]
    ratio = format_ratio(contrast_ratio((0x2A, 0x2A, 0x2A), (0x0F, 0x0F, 0x0F)))
    assert errors[0].message.startswith(f"text.muted #2a2a2a on surface.base #0f0f0f is {ratio}; needs ≥ 3:1")


def test_ratio_is_truncated_never_rounded_up() -> None:
    assert format_ratio(4.4999) == "4.49:1"
    assert format_ratio(3.0) == "3:1"


def test_all_errors_are_reported_together() -> None:
    theme = _minimal()
    theme["modes"]["dark"]["text.muted"] = "#2a2a2a"
    theme["modes"]["light"]["border"] = "blue"
    theme["name"] = "x" * 41
    codes = sorted(e.code for e in validate_package(theme, {}))
    assert codes == ["invalid_color", "low_contrast", "name_too_long"]


def test_deeply_nested_json_is_reported_not_raised() -> None:
    errors = validate_package(b"[" * 100_000 + b"]" * 100_000, {})
    assert [(e.path, e.code) for e in errors] == [("theme.json", "invalid_json")]


def test_roles_are_the_35_of_the_contract() -> None:
    assert len(S.ROLES) == 35 and len(set(S.ROLES)) == 35
    assert S.ROLES[0] == "surface.base" and S.ROLES[-1] == "logo.to"


# ── background readability ─────────────────────────────────────────────

def _png(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def test_readability_message_names_mode_role_and_share() -> None:
    theme = _minimal()
    theme["background"] = {"dark": None, "light": "assets/bg.png"}
    # Mid grey: text.primary #1a1a1a passes, text.secondary #555555 does not.
    errors = validate_package(theme, {"assets/bg.png": _png((1600, 1000), (150, 150, 150))})
    assert [(e.path, e.code) for e in errors] == [("background.light", "background_unreadable")]
    assert errors[0].message.startswith(
        "light background: text.secondary fails on 100 % of pixels"
    )
    assert "lighten the image or darken text.secondary" in errors[0].message


def test_flat_background_null_in_both_modes_is_accepted() -> None:
    theme = _minimal()
    theme["background"] = {"dark": None, "light": None}
    result = check_package(theme, {})
    assert result.ok and result.theme["background"] == {"dark": None, "light": None}  # type: ignore[index]


def test_svg_background_is_rasterised_for_the_readability_check() -> None:
    theme = _minimal()
    busy = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 10">'
        + "".join(f'<rect x="{x}" y="{y}" width="1" height="1" fill="#fff"/>'
                  for x in range(16) for y in range(10) if (x + y) % 2)
        + "</svg>"
    )
    calm = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 10"><rect width="16" height="10" fill="#141414"/></svg>'
    theme["background"] = {"dark": "assets/busy.svg", "light": None}
    errors = validate_package(theme, {"assets/busy.svg": busy.encode()})
    assert {e.code for e in errors} == {"background_unreadable"}
    theme["background"] = {"dark": "assets/calm.svg", "light": None}
    assert validate_package(theme, {"assets/calm.svg": calm.encode()}) == []


# ── package reader ─────────────────────────────────────────────────────

def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_zip_with_one_top_level_folder_is_read() -> None:
    theme, assets = read_zip(_zip({
        "acme/theme.json": b"{}",
        "acme/assets/logo.svg": b"<svg/>",
        "acme/README.md": b"ignored",
        "__MACOSX/acme/._theme.json": b"junk",
    }))
    assert theme == b"{}" and assets == {"assets/logo.svg": b"<svg/>"}


@pytest.mark.parametrize(
    "entries, code",
    [
        ({"../theme.json": b"{}"}, "invalid_package"),
        ({"/abs/theme.json": b"{}"}, "invalid_package"),
        ({"assets/x.png": b""}, "missing_theme"),
        ({f"assets/{i}.png": b"" for i in range(70)} | {"theme.json": b"{}"}, "invalid_package"),
    ],
)
def test_unsafe_or_incomplete_zips_are_refused(entries: dict[str, bytes], code: str) -> None:
    with pytest.raises(PackageError) as exc:
        read_zip(_zip(entries))
    assert exc.value.code == code


def test_not_a_zip_is_refused() -> None:
    with pytest.raises(PackageError):
        read_zip(b"not a zip")


def test_a_valid_package_zip_round_trips_through_the_validator() -> None:
    folder = FIXTURES / "valid" / "full"
    theme, assets = read_folder(str(folder))
    zipped = _zip({"theme.json": theme, **assets})
    theme2, assets2 = read_zip(zipped)
    assert check_package(theme2, assets2).ok
    assert copy.deepcopy(assets2) == assets
