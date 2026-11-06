"""Color parsing, normalisation and the color math the validator needs.

Accepted inputs (``cinna.brand-theme/1``, no alpha anywhere): ``#rgb``,
``#rrggbb``, ``rgb()``, ``hsl()`` and ``oklch()``. Every accepted value is
normalised to lowercase ``#rrggbb``, the only form a consumer ever receives.
Anything else (named colors, ``var(``, ``url(``, ``;``, ``}``, alpha) is refused
with a :class:`ColorError` whose message says what to write instead.

Pure module: standard library only.
"""
from __future__ import annotations

import math
import re

RGB = tuple[int, int, int]


class ColorError(ValueError):
    """A value that is not an accepted color. ``code`` is stable; the message
    is an instruction an agent can act on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


ACCEPTED_FORMS = "#rgb, #rrggbb, rgb(r g b), hsl(h s% l%) or oklch(L C H)"

_NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_HEX_RE = re.compile(r"^#([0-9a-fA-F]+)$")
_FUNC_RE = re.compile(r"^([a-zA-Z]+)\((.*)\)$", re.DOTALL)
_NUM_RE = re.compile(rf"^({_NUM})(%|deg)?$")
_MAX_COMPONENT = 100_000.0


def _hex(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _split_args(body: str, fn: str, raw: str) -> list[str]:
    if "/" in body:
        raise ColorError(
            "color_alpha",
            f"{raw!r} has an alpha channel; brand theme colors are opaque in v1. "
            f"Drop the '/ alpha' part of {fn}().",
        )
    parts = [p for p in re.split(r"[\s,]+", body.strip()) if p]
    if len(parts) == 4:
        raise ColorError(
            "color_alpha",
            f"{raw!r} has a fourth (alpha) component; brand theme colors are "
            f"opaque in v1. Give {fn}() exactly three components.",
        )
    if len(parts) != 3:
        raise ColorError(
            "invalid_color",
            f"{raw!r} needs exactly three components inside {fn}(); accepted forms are {ACCEPTED_FORMS}.",
        )
    return parts


def _number(token: str, raw: str) -> tuple[float, str]:
    match = _NUM_RE.match(token)
    if not match:
        raise ColorError(
            "invalid_color",
            f"{raw!r}: {token!r} is not a number; accepted forms are {ACCEPTED_FORMS}.",
        )
    value = float(match.group(1))
    # No color component needs more than a few hundred; refusing huge and
    # non-finite numbers keeps the math below from overflowing or wrapping.
    if not math.isfinite(value) or abs(value) > _MAX_COMPONENT:
        raise ColorError("invalid_color", f"{raw!r}: {token!r} is out of range for a color component.")
    return value, match.group(2) or ""


def _srgb_encode(linear: float) -> float:
    if linear <= 0.0031308:
        return 12.92 * linear
    return 1.055 * (linear ** (1 / 2.4)) - 0.055


def _srgb_decode(channel: float) -> float:
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _to_byte_checked(value: float, raw: str) -> int:
    """0–1 float to 0–255, refusing anything clearly outside sRGB."""
    tolerance = 0.5 / 255
    if value < -tolerance or value > 1 + tolerance or math.isnan(value):
        raise _OutOfGamut()
    return max(0, min(255, round(value * 255)))


class _OutOfGamut(Exception):
    pass


def _parse_rgb(body: str, raw: str) -> RGB:
    out = []
    for token in _split_args(body, "rgb", raw):
        value, unit = _number(token, raw)
        if unit == "%":
            value = value / 100 * 255
        elif unit:
            raise ColorError("invalid_color", f"{raw!r}: rgb() components are numbers 0–255 or percentages.")
        if value < 0 or value > 255:
            raise ColorError("invalid_color", f"{raw!r}: rgb() components must be within 0–255 (or 0%–100%).")
        out.append(round(value))
    return (out[0], out[1], out[2])


def _parse_hsl(body: str, raw: str) -> RGB:
    h_tok, s_tok, l_tok = _split_args(body, "hsl", raw)
    hue, h_unit = _number(h_tok, raw)
    if h_unit not in ("", "deg"):
        raise ColorError("invalid_color", f"{raw!r}: hsl() hue is a number of degrees.")
    sat, s_unit = _number(s_tok, raw)
    light, l_unit = _number(l_tok, raw)
    if s_unit != "%" or l_unit != "%":
        raise ColorError("invalid_color", f"{raw!r}: hsl() saturation and lightness are percentages, e.g. hsl(210 40% 50%).")
    if not (0 <= sat <= 100 and 0 <= light <= 100):
        raise ColorError("invalid_color", f"{raw!r}: hsl() saturation and lightness must be within 0%–100%.")
    s, lum, h = sat / 100, light / 100, (hue % 360) / 360

    def channel(n: float) -> float:
        k = (n + h * 12) % 12
        a = s * min(lum, 1 - lum)
        return lum - a * max(-1, min(k - 3, 9 - k, 1))

    return (round(channel(0) * 255), round(channel(8) * 255), round(channel(4) * 255))


def oklab_to_linear_srgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    return (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def _parse_oklch(body: str, raw: str) -> RGB:
    l_tok, c_tok, h_tok = _split_args(body, "oklch", raw)
    L, l_unit = _number(l_tok, raw)
    C, c_unit = _number(c_tok, raw)
    H, h_unit = _number(h_tok, raw)
    if l_unit == "%":
        L /= 100
    elif l_unit:
        raise ColorError("invalid_color", f"{raw!r}: oklch() lightness is 0–1 or a percentage.")
    if c_unit == "%":
        C = C / 100 * 0.4
    elif c_unit:
        raise ColorError("invalid_color", f"{raw!r}: oklch() chroma is a number (0–0.4) or a percentage.")
    if h_unit not in ("", "deg"):
        raise ColorError("invalid_color", f"{raw!r}: oklch() hue is a number of degrees.")
    if not (0 <= L <= 1) or C < 0:
        raise ColorError("invalid_color", f"{raw!r}: oklch() lightness must be within 0–1 and chroma ≥ 0.")
    rad = math.radians(H)
    lin = oklab_to_linear_srgb(L, C * math.cos(rad), C * math.sin(rad))
    encoded = [_srgb_encode(max(0.0, c)) if c >= 0 else c for c in lin]
    try:
        return tuple(_to_byte_checked(c, raw) for c in encoded)  # type: ignore[return-value]
    except _OutOfGamut:
        clamped = tuple(max(0, min(255, round(_srgb_encode(min(1.0, max(0.0, c))) * 255))) for c in lin)
        raise ColorError(
            "color_out_of_gamut",
            f"{raw!r} is outside the sRGB gamut, so it cannot be stored as a hex color. "
            f"Lower its chroma, or use the nearest sRGB color {_hex(clamped)}.",  # type: ignore[arg-type]
        ) from None


def parse_color(value: object) -> RGB:
    """Parse one accepted color form into an sRGB byte triple, or raise."""
    if not isinstance(value, str):
        raise ColorError("invalid_color", f"{value!r} is not a string; write the color as {ACCEPTED_FORMS}.")
    raw = value
    text = value.strip()
    lowered = text.lower()
    for marker, why in (("var(", "a CSS variable"), ("url(", "a URL"), ("calc(", "a calc() expression")):
        if marker in lowered:
            raise ColorError(
                "invalid_color",
                f"{raw!r} is {why}; brand theme roles take literal colors only ({ACCEPTED_FORMS}).",
            )
    if any(ch in text for ch in ";{}<>\"'\\"):
        raise ColorError(
            "invalid_color",
            f"{raw!r} contains characters that are not part of a color; write one of {ACCEPTED_FORMS}.",
        )
    hex_match = _HEX_RE.match(text)
    if hex_match:
        digits = hex_match.group(1).lower()
        if len(digits) == 3:
            return (int(digits[0] * 2, 16), int(digits[1] * 2, 16), int(digits[2] * 2, 16))
        if len(digits) == 6:
            return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
        if len(digits) in (4, 8):
            raise ColorError(
                "color_alpha",
                f"{raw!r} is a hex color with alpha; brand theme colors are opaque in v1. Use #rgb or #rrggbb.",
            )
        raise ColorError("invalid_color", f"{raw!r} is not a valid hex color; use #rgb or #rrggbb.")
    func_match = _FUNC_RE.match(text)
    if func_match:
        fn = func_match.group(1).lower()
        body = func_match.group(2)
        if fn in ("rgba", "hsla"):
            raise ColorError(
                "color_alpha",
                f"{raw!r} uses {fn}(), which carries alpha; brand theme colors are opaque in v1. "
                f"Use {fn[:-1]}() with three components.",
            )
        if fn == "rgb":
            return _parse_rgb(body, raw)
        if fn == "hsl":
            return _parse_hsl(body, raw)
        if fn == "oklch":
            return _parse_oklch(body, raw)
        raise ColorError("invalid_color", f"{raw!r}: {fn}() is not accepted; accepted forms are {ACCEPTED_FORMS}.")
    raise ColorError(
        "invalid_color",
        f"{raw!r} is not an accepted color (named colors are not accepted); write one of {ACCEPTED_FORMS}.",
    )


def normalise(value: object) -> str:
    """Parse and return the canonical lowercase ``#rrggbb``."""
    return _hex(parse_color(value))


def hex_to_rgb(value: str) -> RGB:
    return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))


# ── WCAG ───────────────────────────────────────────────────────────────

def relative_luminance(rgb: RGB) -> float:
    r, g, b = (_srgb_decode(c / 255) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio_luminance(l1: float, l2: float) -> float:
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


def contrast_ratio(a: RGB, b: RGB) -> float:
    return contrast_ratio_luminance(relative_luminance(a), relative_luminance(b))


def format_ratio(ratio: float) -> str:
    """Truncate (never round up) so a failing 4.496 does not print as 4.5."""
    truncated = math.floor(ratio * 100) / 100
    return f"{truncated:.2f}".rstrip("0").rstrip(".") + ":1"


# ── OKLab / OKLCH ──────────────────────────────────────────────────────

def to_oklab(rgb: RGB) -> tuple[float, float, float]:
    r, g, b = (_srgb_decode(c / 255) for c in rgb)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (math.copysign(abs(x) ** (1 / 3), x) for x in (l, m, s))
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def to_oklch(rgb: RGB) -> tuple[float, float, float]:
    L, a, b = to_oklab(rgb)
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


def delta_e_ok(a: RGB, b: RGB) -> float:
    la, lb = to_oklab(a), to_oklab(b)
    return math.dist(la, lb)


def hue_distance(h1: float, h2: float) -> float:
    d = abs(h1 - h2) % 360
    return min(d, 360 - d)
