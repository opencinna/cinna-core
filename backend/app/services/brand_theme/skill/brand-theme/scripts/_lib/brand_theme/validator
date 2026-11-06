"""Brand theme package validator — ``cinna.brand-theme/1`` (core.md §2).

``validate_package(theme_json, assets)`` returns every problem with a
package. ``check_package`` does the same work and also returns what core
stores when there are none: the normalised theme (colors as ``#rrggbb``) and
each referenced asset as the bytes to store (SVGs sanitised).

Inputs:

* ``theme_json`` — the parsed ``theme.json`` (a dict), or its raw bytes/str;
* ``assets`` — package path (``"assets/logo-dark.svg"``) → file bytes.

Every :class:`ValidationError` carries a dotted ``path``
(``modes.dark.text.muted``), a stable ``code`` and a sentence an agent can
act on. Codes are listed in :data:`ERROR_CODES` with the tag the shared
fixtures use: ``unsafe`` (Cinna Desktop must reject it too) or
``incompatible`` (only core refuses it).

No FastAPI or DB imports: the ``brand-theme`` authoring skill ships a copy
of this package and runs the very same checks offline.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from itertools import combinations
from typing import Any

from . import schema as S
from .colors import (
    ColorError,
    contrast_ratio,
    contrast_ratio_luminance,
    delta_e_ok,
    format_ratio,
    hex_to_rgb,
    hue_distance,
    normalise,
    relative_luminance,
    to_oklch,
)
from .images import RasterError, raster_info, sample_pixels, sniff_mime
from .svg_sanitizer import SvgRefused, sanitise_svg

UNSAFE = "unsafe"
INCOMPATIBLE = "incompatible"

# code → fixture tag. ``unsafe`` = a desktop safety check must also catch it.
ERROR_CODES: dict[str, str] = {
    "invalid_json": UNSAFE,
    "invalid_type": UNSAFE,
    "schema_mismatch": UNSAFE,
    "missing_key": UNSAFE,
    "missing_role": UNSAFE,
    "invalid_color": UNSAFE,
    "color_alpha": UNSAFE,
    "color_out_of_gamut": INCOMPATIBLE,
    "invalid_text": UNSAFE,
    "text_too_long": UNSAFE,
    "invalid_asset_path": UNSAFE,
    "missing_asset": UNSAFE,
    "unsupported_asset_type": UNSAFE,
    "asset_too_large": UNSAFE,
    "asset_corrupt": UNSAFE,
    "asset_animated": INCOMPATIBLE,
    "svg_encoding": UNSAFE,
    "svg_entities": UNSAFE,
    "svg_parse": UNSAFE,
    "svg_not_svg": UNSAFE,
    "svg_missing_viewbox": UNSAFE,
    "svg_too_complex": UNSAFE,
    # Desktop ignores unknown keys (forward compatibility within 1.x); only
    # core refuses them, so a typo cannot vanish silently on upload.
    "unknown_key": INCOMPATIBLE,
    "name_too_long": INCOMPATIBLE,
    "low_contrast": INCOMPATIBLE,
    "chart_slots_too_similar": INCOMPATIBLE,
    "bubble_indistinct": INCOMPATIBLE,
    "accent_reads_as_danger": INCOMPATIBLE,
    "logo_too_small": INCOMPATIBLE,
    "logo_aspect": INCOMPATIBLE,
    "background_too_small": INCOMPATIBLE,
    "background_unreadable": INCOMPATIBLE,
    "background_modes_swapped": INCOMPATIBLE,
}


@dataclass(frozen=True)
class ValidationError:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass
class StoredAsset:
    """One referenced asset, as core stores and serves it."""

    path: str
    mime: str
    data: bytes
    sha256: str
    width: int | None = None
    height: int | None = None

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class PackageResult:
    errors: list[ValidationError] = field(default_factory=list)
    # Not refusals: what the sanitiser removed, assets nobody references.
    warnings: list[ValidationError] = field(default_factory=list)
    # The normalised theme.json (``None`` while there are errors).
    theme: dict[str, Any] | None = None
    # Package path → stored asset, for every referenced path.
    assets: dict[str, StoredAsset] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_package(theme_json: Any, assets: Mapping[str, bytes]) -> list[ValidationError]:
    """Every reason the package would be refused; empty when it is valid."""
    return check_package(theme_json, assets).errors


def check_package(theme_json: Any, assets: Mapping[str, bytes]) -> PackageResult:
    return _Checker(assets).run(theme_json)


# ── helpers ────────────────────────────────────────────────────────────

_BIDI_CONTROLS = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F)}


def _bad_text_char(value: str) -> str | None:
    for ch in value:
        if ch in _BIDI_CONTROLS or unicodedata.category(ch) in ("Cc", "Cs", "Co", "Zl", "Zp"):
            return ch
    return None


def _percent(share: float) -> str:
    return f"{round(share * 100)} %"


class _Checker:
    def __init__(self, assets: Mapping[str, bytes]) -> None:
        self.raw_assets = dict(assets)
        self.result = PackageResult()
        self.colors: dict[str, dict[str, str]] = {}
        self.referenced: set[str] = set()
        # Package path → (width, height) proportions: pixels for rasters,
        # the viewBox for SVG.
        self.proportions: dict[str, tuple[float, float]] = {}

    # error plumbing
    def err(self, path: str, code: str, message: str) -> None:
        self.result.errors.append(ValidationError(path, code, message))

    def warn(self, path: str, code: str, message: str) -> None:
        self.result.warnings.append(ValidationError(path, code, message))

    def run(self, theme_json: Any) -> PackageResult:
        doc = self._load(theme_json)
        if doc is None:
            return self.result
        normalised: dict[str, Any] = {}
        self._top_level(doc, normalised)
        self._modes(doc.get("modes"), normalised)
        for mode in S.MODES:
            if mode in self.colors:
                self._contrast(mode)
        if "logo" in doc:
            self._logo(doc["logo"], normalised)
        if "background" in doc:
            self._background(doc["background"], normalised)
        if "copy" in doc:
            self._copy(doc["copy"], normalised)
        for path in sorted(set(self.raw_assets) - self.referenced):
            self.warn(path, "unreferenced_asset", f"{path} is not referenced by theme.json and will not be stored.")
        if not self.result.errors:
            self.result.theme = normalised
        else:
            self.result.assets = {}
        return self.result

    def _load(self, theme_json: Any) -> dict[str, Any] | None:
        if isinstance(theme_json, (bytes, bytearray, str)):
            try:
                theme_json = json.loads(theme_json)
            except (ValueError, UnicodeDecodeError) as exc:
                self.err("theme.json", "invalid_json", f"theme.json is not valid JSON ({exc}).")
                return None
            except RecursionError:
                self.err("theme.json", "invalid_json", "theme.json nests too deeply to be a brand theme.")
                return None
        if not isinstance(theme_json, dict):
            self.err("theme.json", "invalid_type", "theme.json must be a JSON object.")
            return None
        return theme_json

    def _unknown_keys(self, obj: dict[str, Any], allowed: tuple[str, ...], prefix: str) -> None:
        for key in obj:
            if key not in allowed:
                where = f"{prefix}.{key}" if prefix else key
                self.err(
                    where,
                    "unknown_key",
                    f"{where} is not part of {S.SCHEMA_ID}; allowed keys here are {', '.join(allowed)}. "
                    "Remove it or fix the spelling.",
                )

    def _top_level(self, doc: dict[str, Any], out: dict[str, Any]) -> None:
        self._unknown_keys(doc, S.TOP_LEVEL_KEYS, "")
        for key in S.REQUIRED_TOP_LEVEL_KEYS:
            if key not in doc:
                self.err(key, "missing_key", f"{key} is required.")
        if "schema" in doc:
            if doc["schema"] != S.SCHEMA_ID:
                self.err("schema", "schema_mismatch", f"schema must be exactly {S.SCHEMA_ID!r}, got {doc['schema']!r}.")
            else:
                out["schema"] = S.SCHEMA_ID
        if "name" in doc:
            name = doc["name"]
            if not isinstance(name, str) or not name.strip():
                self.err("name", "invalid_text", "name must be a non-empty string (it is the logo's alt text).")
            elif (bad := _bad_text_char(name)) is not None:
                self.err("name", "invalid_text", f"name contains the control character U+{ord(bad):04X}; use plain text.")
            elif len(name) > S.NAME_MAX_CHARS:
                self.err("name", "name_too_long", f"name is {len(name)} characters; at most {S.NAME_MAX_CHARS} are allowed.")
            else:
                out["name"] = name.strip()

    # ── colors ─────────────────────────────────────────────────────────
    def _modes(self, modes: Any, out: dict[str, Any]) -> None:
        if modes is None:
            return
        if not isinstance(modes, dict):
            self.err("modes", "invalid_type", "modes must be an object with 'dark' and 'light'.")
            return
        self._unknown_keys(modes, S.MODES, "modes")
        out_modes: dict[str, dict[str, str]] = {}
        for mode in S.MODES:
            roles = modes.get(mode)
            if roles is None:
                self.err(f"modes.{mode}", "missing_key", f"modes.{mode} is required; a theme supplies both modes.")
                continue
            if not isinstance(roles, dict):
                self.err(f"modes.{mode}", "invalid_type", f"modes.{mode} must be an object of role → color.")
                continue
            self._unknown_keys(roles, S.ROLES, f"modes.{mode}")
            parsed: dict[str, str] = {}
            for role in S.ROLES:
                path = f"modes.{mode}.{role}"
                if role not in roles:
                    self.err(path, "missing_role", f"{role} is missing from the {mode} mode ({S.ROLE_MEANINGS[role]}).")
                    continue
                try:
                    parsed[role] = normalise(roles[role])
                except ColorError as exc:
                    self.err(path, exc.code, f"{role}: {exc.message}")
            self.colors[mode] = parsed
            out_modes[mode] = parsed
        out["modes"] = out_modes

    def _pair(self, mode: str, fg: str, bg: str, minimum: float) -> None:
        c = self.colors[mode]
        if fg not in c or bg not in c:
            return
        ratio = contrast_ratio(hex_to_rgb(c[fg]), hex_to_rgb(c[bg]))
        if ratio < minimum:
            self.err(
                f"modes.{mode}.{fg}",
                "low_contrast",
                f"{fg} {c[fg]} on {bg} {c[bg]} is {format_ratio(ratio)}; needs ≥ {format_ratio(minimum)} "
                f"({mode} mode).",
            )

    def _contrast(self, mode: str) -> None:
        t, n = S.CONTRAST_TEXT, S.CONTRAST_NON_TEXT
        for bg in ("surface.base", "surface.raised", "surface.input"):
            self._pair(mode, "text.primary", bg, t)
        for bg in ("surface.base", "surface.raised"):
            self._pair(mode, "text.secondary", bg, t)
        self._pair(mode, "text.muted", "surface.base", n)
        self._pair(mode, "accent", "surface.base", n)
        self._pair(mode, "accent.on", "accent", t)
        self._pair(mode, "accent.on", "accent.hover", t)
        for kind in S.STATUS_KINDS:
            self._pair(mode, f"status.{kind}.text", "surface.base", t)
            self._pair(mode, f"status.{kind}", "surface.base", n)
        for role in (*S.CATEGORICAL_CHART_ROLES, "chart.pos", "chart.neg"):
            self._pair(mode, role, "surface.base", n)
        self._pair(mode, "text.primary", "bubble.user", t)

        c = self.colors[mode]
        for a, b in combinations(S.CATEGORICAL_CHART_ROLES, 2):
            if a in c and b in c:
                d = delta_e_ok(hex_to_rgb(c[a]), hex_to_rgb(c[b]))
                if d < S.CHART_MIN_DELTA_E:
                    self.err(
                        f"modes.{mode}.{b}",
                        "chart_slots_too_similar",
                        f"{a} {c[a]} and {b} {c[b]} are ΔE {d:.3f} apart in OKLab; categorical chart slots "
                        f"need ≥ {S.CHART_MIN_DELTA_E} so series stay distinguishable ({mode} mode). "
                        f"Change the hue or lightness of {b}.",
                    )
        if "bubble.user" in c and "surface.base" in c:
            d = delta_e_ok(hex_to_rgb(c["bubble.user"]), hex_to_rgb(c["surface.base"]))
            if d < S.BUBBLE_MIN_DELTA_E:
                self.err(
                    f"modes.{mode}.bubble.user",
                    "bubble_indistinct",
                    f"bubble.user {c['bubble.user']} is ΔE {d:.3f} from surface.base {c['surface.base']}; "
                    f"needs ≥ {S.BUBBLE_MIN_DELTA_E} so the user's messages stand out ({mode} mode).",
                )
        if "accent" in c and "status.danger" in c:
            _, chroma, hue = to_oklch(hex_to_rgb(c["accent"]))
            _, _, danger_hue = to_oklch(hex_to_rgb(c["status.danger"]))
            distance = hue_distance(hue, danger_hue)
            if chroma > S.ACCENT_DANGER_MIN_CHROMA and distance < S.ACCENT_DANGER_MAX_HUE_DEG:
                self.err(
                    f"modes.{mode}.accent",
                    "accent_reads_as_danger",
                    f"accent {c['accent']} (hue {hue:.0f}°) is {distance:.0f}° from status.danger "
                    f"{c['status.danger']} (hue {danger_hue:.0f}°), so primary actions read as errors "
                    f"({mode} mode). Keep them ≥ {S.ACCENT_DANGER_MAX_HUE_DEG:.0f}° apart: move "
                    "status.danger toward a different red (e.g. crimson or magenta-red) or desaturate the accent.",
                )

    # ── assets ─────────────────────────────────────────────────────────
    def _asset_path(self, where: str, value: Any) -> str | None:
        if not isinstance(value, str):
            self.err(where, "invalid_asset_path", f"{where} must be a path like 'assets/logo-dark.svg'.")
            return None
        parts = value.split("/")
        if (
            not value.startswith(S.ASSET_DIR)
            or len(value) > S.ASSET_PATH_MAX_CHARS
            or "\\" in value
            or any(p in ("", ".", "..") for p in parts)
        ):
            self.err(
                where,
                "invalid_asset_path",
                f"{value!r} is not a valid asset path; use a relative path under {S.ASSET_DIR!r} "
                "with no '..' segments.",
            )
            return None
        self.referenced.add(value)
        if value not in self.raw_assets:
            self.err(where, "missing_asset", f"{value} is referenced but not in the package.")
            return None
        return value

    def _load_asset(
        self, where: str, path: str, svg_max: int, raster_max: int
    ) -> StoredAsset | None:
        if path in self.result.assets:
            return self.result.assets[path]
        data = self.raw_assets[path]
        mime = sniff_mime(data)
        if mime is None:
            self.err(
                where,
                "unsupported_asset_type",
                f"{path} is not SVG, PNG, JPEG or WebP (type is read from the file's bytes, not its name).",
            )
            return None
        limit = svg_max if mime == S.MIME_SVG else raster_max
        if len(data) > limit:
            self.err(
                where,
                "asset_too_large",
                f"{path} is {len(data) // S.KIB} KB; {mime} here may be at most {limit // S.KIB} KB.",
            )
            return None
        width = height = None
        if mime == S.MIME_SVG:
            try:
                sanitised = sanitise_svg(data)
            except SvgRefused as exc:
                self.err(where, exc.code, f"{path}: {exc.message}")
                return None
            for item in sanitised.removed:
                self.warn(where, "svg_sanitised", f"{path}: removed {item}.")
            stored = sanitised.data
            vb = sanitised.view_box
            aspect_w, aspect_h = vb[2], vb[3]
        else:
            try:
                info = raster_info(data, mime)
            except RasterError as exc:
                self.err(where, exc.code, f"{path}: {exc.message}")
                return None
            stored = data
            width, height = info.width, info.height
            aspect_w, aspect_h = width, height
        asset = StoredAsset(
            path=path,
            mime=mime,
            data=stored,
            sha256=hashlib.sha256(stored).hexdigest(),
            width=width,
            height=height,
        )
        self.proportions[path] = (aspect_w, aspect_h)
        self.result.assets[path] = asset
        return asset

    def _pair_block(self, block: Any, name: str, nullable: bool) -> dict[str, Any] | None:
        if not isinstance(block, dict):
            self.err(name, "invalid_type", f"{name} must be an object with 'dark' and 'light'.")
            return None
        self._unknown_keys(block, S.MODES, name)
        for mode in S.MODES:
            if mode not in block:
                hint = " (use null for a flat surface.base)" if nullable else ""
                self.err(f"{name}.{mode}", "missing_key", f"{name}.{mode} is required when {name} is given{hint}.")
        return block

    def _logo(self, block: Any, out: dict[str, Any]) -> None:
        block = self._pair_block(block, "logo", nullable=False)
        if block is None:
            return
        result: dict[str, str] = {}
        for mode in S.MODES:
            where = f"logo.{mode}"
            if mode not in block:
                continue
            path = self._asset_path(where, block[mode])
            if path is None:
                continue
            asset = self._load_asset(where, path, S.LOGO_SVG_MAX_BYTES, S.LOGO_RASTER_MAX_BYTES)
            if asset is None:
                continue
            result[mode] = path
            if asset.mime != S.MIME_SVG and asset.height is not None and asset.height < S.LOGO_RASTER_MIN_HEIGHT:
                self.err(
                    where,
                    "logo_too_small",
                    f"{path} is {asset.height} px tall; raster logos need ≥ {S.LOGO_RASTER_MIN_HEIGHT} px "
                    "so they stay sharp on high-density screens (or use SVG).",
                )
            w, h = self.proportions[path]
            if w / h > S.LOGO_MAX_ASPECT:
                self.err(
                    where,
                    "logo_aspect",
                    f"{path} is {w / h:.1f}:1 wide; drawn into a {S.LOGO_BOX_WIDTH}×{S.LOGO_BOX_HEIGHT} box it "
                    f"would be under {S.LOGO_MIN_DRAWN_HEIGHT} px tall. Use a mark at most "
                    f"{S.LOGO_MAX_ASPECT:.0f}:1, e.g. a stacked lockup.",
                )
        out["logo"] = result

    def _background(self, block: Any, out: dict[str, Any]) -> None:
        block = self._pair_block(block, "background", nullable=True)
        if block is None:
            return
        result: dict[str, str | None] = {}
        medians: dict[str, float] = {}
        for mode in S.MODES:
            where = f"background.{mode}"
            if mode not in block:
                continue
            if block[mode] is None:
                result[mode] = None
                continue
            path = self._asset_path(where, block[mode])
            if path is None:
                continue
            asset = self._load_asset(where, path, S.BACKGROUND_SVG_MAX_BYTES, S.BACKGROUND_RASTER_MAX_BYTES)
            if asset is None:
                continue
            result[mode] = path
            if asset.mime != S.MIME_SVG and (
                (asset.width or 0) < S.BACKGROUND_MIN_WIDTH or (asset.height or 0) < S.BACKGROUND_MIN_HEIGHT
            ):
                self.err(
                    where,
                    "background_too_small",
                    f"{path} is {asset.width}×{asset.height} px; backgrounds need at least "
                    f"{S.BACKGROUND_MIN_WIDTH}×{S.BACKGROUND_MIN_HEIGHT} px (or use SVG).",
                )
                continue
            median = self._readability(mode, where, asset)
            if median is not None:
                medians[mode] = median
        if len(medians) == 2 and medians["dark"] >= medians["light"]:
            self.err(
                "background",
                "background_modes_swapped",
                "the dark background is not darker than the light one (median luminance "
                f"{medians['dark']:.2f} vs {medians['light']:.2f}); the two images look swapped.",
            )
        out["background"] = result

    def _readability(self, mode: str, where: str, asset: StoredAsset) -> float | None:
        c = self.colors.get(mode, {})
        if "surface.base" not in c or not all(r in c for r in S.READABILITY_ROLES):
            return None  # color errors are already reported; nothing to measure against
        try:
            pixels = sample_pixels(asset.data, asset.mime, hex_to_rgb(c["surface.base"]))
        except Exception as exc:  # rasteriser or decoder failure
            self.err(where, "asset_corrupt", f"{asset.path} could not be rendered for the readability check ({exc}).")
            return None
        lums = [relative_luminance(p) for p in pixels]
        median = statistics.median(lums)
        for role in S.READABILITY_ROLES:
            text_lum = relative_luminance(hex_to_rgb(c[role]))
            failing = [x for x in lums if contrast_ratio_luminance(text_lum, x) < S.CONTRAST_TEXT]
            share_ok = 1 - len(failing) / len(lums)
            if share_ok < S.READABILITY_MIN_SHARE:
                text_is_lighter = text_lum > statistics.median(failing)
                image_fix = "darken the image" if text_is_lighter else "lighten the image"
                text_fix = f"{'lighten' if text_is_lighter else 'darken'} {role}"
                self.err(
                    where,
                    "background_unreadable",
                    f"{mode} background: {role} fails on {_percent(1 - share_ok)} of pixels "
                    f"(needs ≥ {format_ratio(S.CONTRAST_TEXT)} on {_percent(S.READABILITY_MIN_SHARE)}); "
                    f"{image_fix} or {text_fix}. Soft, low-contrast images (mesh, blur, subtle texture) pass; "
                    "photos and busy patterns usually do not.",
                )
        return median

    # ── copy ───────────────────────────────────────────────────────────
    def _copy(self, block: Any, out: dict[str, Any]) -> None:
        if not isinstance(block, dict):
            self.err("copy", "invalid_type", "copy must be an object.")
            return
        self._unknown_keys(block, S.COPY_KEYS, "copy")
        result: dict[str, str] = {}
        for key in S.COPY_KEYS:
            if key not in block:
                continue
            value = block[key]
            where = f"copy.{key}"
            limit = S.COPY_MAX_CHARS[key]
            if not isinstance(value, str) or not value.strip():
                self.err(where, "invalid_text", f"{where} must be a non-empty string (or leave it out).")
            elif (bad := _bad_text_char(value)) is not None:
                self.err(
                    where,
                    "invalid_text",
                    f"{where} contains the control character U+{ord(bad):04X}; it must be one line of plain text.",
                )
            elif len(value) > limit:
                self.err(where, "text_too_long", f"{where} is {len(value)} characters; at most {limit} are allowed.")
            else:
                result[key] = value.strip()
        out["copy"] = result
