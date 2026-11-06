"""Brand themes: one server-wide look (colors, logo, background, copy) that
Cinna Desktop and core's sign-in pages adopt.

``schema``, ``colors``, ``svg_sanitizer``, ``images``, ``validator`` and
``served`` are pure (no FastAPI, DB or ``app`` imports) and use relative
imports only, so the ``brand-theme`` authoring skill can ship them verbatim.
``brand_theme_service`` is the DB side.
"""
