"""The served shape of a published theme (README §3, ``GET /api/v1/brand-theme``).

One function builds it, :func:`served_document`, used by the public endpoint
and by the shared fixtures' ``served/`` snapshots, so what the desktop's
parser is tested against is byte-for-byte what core sends.

Asset URLs are **relative** to the server (``/api/v1/brand-theme/assets/<sha256>``),
as in the contract: Cinna Desktop resolves them against the server URL it
already talks to, and core's web app against its API base.

Pure module.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import schema as S

ASSET_URL_PREFIX = "/api/v1/brand-theme/assets/"


@dataclass(frozen=True)
class AssetMeta:
    sha256: str
    mime: str
    size: int
    width: int | None = None
    height: int | None = None


def asset_ref(meta: AssetMeta, url_prefix: str = ASSET_URL_PREFIX) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "url": f"{url_prefix}{meta.sha256}",
        "type": meta.mime,
        "bytes": meta.size,
        "sha256": meta.sha256,
    }
    if meta.width is not None and meta.height is not None:
        ref["width"] = meta.width
        ref["height"] = meta.height
    return ref


def served_document(
    theme: Mapping[str, Any],
    assets_by_path: Mapping[str, AssetMeta],
    revision: int,
    url_prefix: str = ASSET_URL_PREFIX,
) -> dict[str, Any]:
    """``{schema, revision, theme, assets}`` with every asset path in the
    theme replaced by its asset object, and the same objects listed under
    ``assets`` keyed ``"<block>.<mode>"``."""
    served_theme = copy.deepcopy(dict(theme))
    assets: dict[str, dict[str, Any]] = {}
    for block in ("logo", "background"):
        if block not in served_theme:
            continue
        replaced: dict[str, Any] = {}
        for mode in S.MODES:
            path = served_theme[block].get(mode)
            if path is None or path not in assets_by_path:
                # A stored theme always has its assets; a missing one serves
                # as absent (the desktop then rejects a logo-less logo block
                # and shows stock Cinna) rather than failing the response.
                replaced[mode] = None
                continue
            ref = asset_ref(assets_by_path[path], url_prefix)
            replaced[mode] = ref
            assets[f"{block}.{mode}"] = ref
        served_theme[block] = replaced
    return {
        "schema": S.SCHEMA_ID,
        "revision": revision,
        "theme": served_theme,
        "assets": assets,
    }
