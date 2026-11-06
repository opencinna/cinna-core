"""Image helpers for the validator: MIME sniffing, raster facts and the
pixel sample the background readability rule runs on.

MIME is decided from the bytes, never from a file name. SVG backgrounds are
rasterised with ``resvg-py`` (self-contained wheels, no system libraries),
imported lazily so the rest of the validator works without it.

Pure module: Pillow and resvg-py only; no app imports.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

from PIL import Image

from .schema import MIME_JPEG, MIME_PNG, MIME_SVG, MIME_WEBP, RASTER_MAX_SIDE, READABILITY_SAMPLE_SIZE

_SVG_HEAD_RE = re.compile(
    # A DOCTYPE may carry an internal subset ``[...]``: sniff it as SVG so the
    # sanitiser can refuse it with the specific reason (entities).
    rb"^(?:\xef\xbb\xbf)?\s*(?:<\?xml[^>]*\?>\s*)?"
    rb"(?:<!--.*?-->\s*|<!DOCTYPE[^>\[]*(?:\[.*?\])?\s*>\s*)*<svg[\s>]",
    re.DOTALL | re.IGNORECASE,
)


def sniff_mime(data: bytes) -> str | None:
    """The asset's real type from its leading bytes, or None if not accepted."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return MIME_PNG
    if data.startswith(b"\xff\xd8\xff"):
        return MIME_JPEG
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return MIME_WEBP
    if _SVG_HEAD_RE.match(data[:4096]):
        return MIME_SVG
    return None


class RasterError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RasterInfo:
    width: int
    height: int


_PIL_FORMATS = {MIME_PNG: "PNG", MIME_JPEG: "JPEG", MIME_WEBP: "WEBP"}


def raster_info(data: bytes, mime: str) -> RasterInfo:
    """Header facts, then a full decode once the size is known to be sane."""
    try:
        with Image.open(io.BytesIO(data), formats=[_PIL_FORMATS[mime]]) as im:
            width, height = im.size
            animated = bool(getattr(im, "is_animated", False))
    except Exception:
        raise RasterError("asset_corrupt", "the image could not be decoded; re-export it.") from None
    if width > RASTER_MAX_SIDE or height > RASTER_MAX_SIDE:
        raise RasterError(
            "asset_too_large",
            f"the image is {width}×{height} px; at most {RASTER_MAX_SIDE} px a side is accepted.",
        )
    if animated:
        raise RasterError("asset_animated", "animated images are not accepted; export a single still frame.")
    try:
        with Image.open(io.BytesIO(data), formats=[_PIL_FORMATS[mime]]) as im:
            im.load()
    except Exception:
        raise RasterError("asset_corrupt", "the image is truncated or corrupt; re-export it.") from None
    return RasterInfo(width, height)


def _flatten(im: Image.Image, base_rgb: tuple[int, int, int]) -> Image.Image:
    rgba = im.convert("RGBA")
    canvas = Image.new("RGBA", rgba.size, base_rgb + (255,))
    canvas.alpha_composite(rgba)
    return canvas.convert("RGB")


def sample_pixels(
    data: bytes, mime: str, base_rgb: tuple[int, int, int]
) -> list[tuple[int, int, int]]:
    """Downscale the image to the readability sample size (about 64×40) and
    return its pixels, with any transparency composited over ``base_rgb``
    (the mode's ``surface.base``, which is what shows through)."""
    width, height = READABILITY_SAMPLE_SIZE
    if mime == MIME_SVG:
        import resvg_py  # lazy: only SVG backgrounds need it

        png = resvg_py.svg_to_bytes(
            svg_string=data.decode("utf-8"),
            width=width * 4,
            height=height * 4,
            skip_system_fonts=True,
        )
        im = Image.open(io.BytesIO(bytes(png)))
    else:
        im = Image.open(io.BytesIO(data), formats=[_PIL_FORMATS[mime]])
        im.draft("RGB", (width * 4, height * 4))
    with im:
        flat = _flatten(im, base_rgb)
    small = flat.resize((width, height), Image.Resampling.BOX)
    return [small.getpixel((x, y)) for y in range(height) for x in range(width)]  # type: ignore[misc]
