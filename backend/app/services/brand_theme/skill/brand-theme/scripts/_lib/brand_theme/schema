"""The brand theme contract, ``cinna.brand-theme/1``: the roles and the limits.

**This is the only place the roles are defined.** The validator, the served
projection, the core frontend's role map (C2) and Cinna Desktop's
``roles.ts`` all take their names from :data:`ROLES`. Adding a role here in a
``1.x`` revision is a contract change: it must be optional (the desktop
derives it when absent), so it goes in a separate optional tuple, never into
:data:`ROLES`, which stays the required v1 set.

Pure module: no FastAPI, DB or app imports, so the authoring skill (C4) can
ship a verbatim copy and a core test can check the copy has not drifted.
"""
from __future__ import annotations

SCHEMA_ID = "cinna.brand-theme/1"
# Major version advertised in discovery (``brand_theme.schema``).
SCHEMA_MAJOR = 1

MODES: tuple[str, ...] = ("dark", "light")

# Role → one sentence on what it paints. Order is the README §3.1 order and is
# the order every consumer lists roles in.
ROLE_MEANINGS: dict[str, str] = {
    "surface.base": "Window background.",
    "surface.raised": "Cards, sidebar and popovers.",
    "surface.tertiary": "Nested surfaces and muted fills.",
    "surface.hover": "Hovered rows and buttons.",
    "surface.input": "Text inputs and the chat composer.",
    "border": "Hairlines and outlines.",
    "text.primary": "Body text and headings.",
    "text.secondary": "Secondary text: labels, metadata.",
    "text.muted": "Least prominent text: hints, placeholders, timestamps.",
    "accent": "Primary action color: main buttons, links, focus rings.",
    "accent.hover": "The accent color while hovered.",
    "accent.on": "Text and icons drawn on top of the accent color.",
    "bubble.user": "Background of the user's own chat message bubble.",
    "highlight": "Agent highlight in dashboards.",
    "status.danger": "Fill and icon color for errors and destructive actions.",
    "status.warning": "Fill and icon color for warnings.",
    "status.success": "Fill and icon color for success.",
    "status.info": "Fill and icon color for neutral information.",
    "status.danger.text": "Error text on surface.base.",
    "status.warning.text": "Warning text on surface.base.",
    "status.success.text": "Success text on surface.base.",
    "status.info.text": "Information text on surface.base.",
    "chart.1": "Categorical chart slot 1, usually the accent.",
    "chart.2": "Categorical chart slot 2.",
    "chart.3": "Categorical chart slot 3.",
    "chart.4": "Categorical chart slot 4.",
    "chart.5": "Categorical chart slot 5.",
    "chart.6": "Categorical chart slot 6.",
    "chart.7": "Categorical chart slot 7.",
    "chart.8": "Categorical chart slot 8.",
    "chart.pos": "Positive end of a diverging scale.",
    "chart.neg": "Negative end of a diverging scale.",
    "chart.grid": "Chart grid hairlines.",
    "logo.from": "Start of the Cinna wordmark's stroke gradient (used when no logo is given).",
    "logo.to": "End of the Cinna wordmark's stroke gradient (used when no logo is given).",
}

ROLES: tuple[str, ...] = tuple(ROLE_MEANINGS)

CATEGORICAL_CHART_ROLES: tuple[str, ...] = tuple(f"chart.{i}" for i in range(1, 9))
STATUS_KINDS: tuple[str, ...] = ("danger", "warning", "success", "info")

# ── Package shape ──────────────────────────────────────────────────────
TOP_LEVEL_KEYS: tuple[str, ...] = ("schema", "name", "modes", "logo", "background", "copy")
REQUIRED_TOP_LEVEL_KEYS: tuple[str, ...] = ("schema", "name", "modes")
COPY_KEYS: tuple[str, ...] = ("newChatHeading",)

NAME_MAX_CHARS = 40
COPY_MAX_CHARS: dict[str, int] = {"newChatHeading": 60}

# Asset paths in theme.json are relative to the package root and live under
# ``assets/``.
ASSET_DIR = "assets/"
ASSET_PATH_MAX_CHARS = 200

# ── Asset limits ───────────────────────────────────────────────────────
MIME_SVG = "image/svg+xml"
MIME_PNG = "image/png"
MIME_JPEG = "image/jpeg"
MIME_WEBP = "image/webp"
RASTER_MIMES: tuple[str, ...] = (MIME_PNG, MIME_JPEG, MIME_WEBP)

KIB = 1024
MIB = 1024 * KIB

LOGO_SVG_MAX_BYTES = 256 * KIB
LOGO_RASTER_MAX_BYTES = 1 * MIB
LOGO_RASTER_MIN_HEIGHT = 124
# The logo is drawn `contain` into a 240×62 box and must stay ≥ 24 px tall:
# 240 / 24 = 10.
LOGO_BOX_WIDTH = 240
LOGO_BOX_HEIGHT = 62
LOGO_MIN_DRAWN_HEIGHT = 24
LOGO_MAX_ASPECT = LOGO_BOX_WIDTH / LOGO_MIN_DRAWN_HEIGHT

BACKGROUND_SVG_MAX_BYTES = 512 * KIB
BACKGROUND_RASTER_MAX_BYTES = 3 * MIB
BACKGROUND_MIN_WIDTH = 1600
BACKGROUND_MIN_HEIGHT = 1000

RASTER_MAX_SIDE = 4096

# ── Contrast and color-distance rules (core.md §2) ─────────────────────
CONTRAST_TEXT = 4.5
CONTRAST_NON_TEXT = 3.0
CHART_MIN_DELTA_E = 0.08          # OKLab ΔE between any two categorical slots
BUBBLE_MIN_DELTA_E = 0.03         # bubble.user vs surface.base
ACCENT_DANGER_MAX_HUE_DEG = 20.0  # OKLCH hue distance that reads as "the same hue"
ACCENT_DANGER_MIN_CHROMA = 0.1    # below this the accent is grey enough not to read as danger

# ── Background readability (core.md §2) ────────────────────────────────
READABILITY_SAMPLE_SIZE = (64, 40)
READABILITY_MIN_SHARE = 0.95
READABILITY_ROLES: tuple[str, ...] = ("text.primary", "text.secondary")
