"""SVG sanitiser for brand theme logos and backgrounds.

The **sanitised bytes** are what core stores and serves, never the upload.
Both apps draw these files only as ``<img>``, so this is the second line of
defence, not the only one. Cinna Desktop re-applies the same allowlist as a
*check* (it rejects on any disallowed node instead of repairing), so the
allowlists below are part of the contract.

What happens to an upload:

* refused outright: not UTF-8, a DTD internal subset or any entity
  declaration (detected by expat itself, not by pattern matching), not
  well-formed XML, a root that is not
  ``<svg xmlns="http://www.w3.org/2000/svg">``, no usable ``viewBox``, more
  than :data:`MAX_ELEMENTS` elements or deeper than :data:`MAX_DEPTH`;
* removed: every element outside :data:`ALLOWED_ELEMENTS` with its subtree
  (``script``, ``foreignObject``, ``image``, ``use``, ``style``, ``a``, editor
  metadata, …), every attribute outside :data:`ALLOWED_ATTRIBUTES` (all
  ``on*`` handlers, ``class``, editor namespaces), any ``href`` that is not a
  local ``#id``, any value with a ``url(…)`` that is not ``url(#id)``, and
  ``style`` declarations that are not allowed presentation properties with
  safe values; comments, processing instructions, a plain DOCTYPE, and text
  outside ``<title>``.

The output is re-serialised by :func:`_serialise` (not by ElementTree), so
it is deterministic and the function is idempotent:
``sanitise_svg(sanitise_svg(x).data).data == sanitise_svg(x).data``.

Pure module: standard library only.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from xml.parsers import expat
from dataclasses import dataclass, field

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

ALLOWED_ELEMENTS: frozenset[str] = frozenset({
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline",
    "polygon", "defs", "linearGradient", "radialGradient", "stop",
    "clipPath", "mask", "title",
})

# Presentation properties: allowed as attributes and as ``style`` declarations.
PRESENTATION_PROPERTIES: frozenset[str] = frozenset({
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
    "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
    "stroke-dasharray", "stroke-dashoffset", "stroke-opacity", "opacity",
    "clip-path", "clip-rule", "mask", "stop-color", "stop-opacity", "color",
    "display", "visibility", "overflow", "shape-rendering", "vector-effect",
    "paint-order", "color-interpolation", "mix-blend-mode", "isolation",
})

# Geometry and structural attributes (attributes only, never ``style``).
GEOMETRY_ATTRIBUTES: frozenset[str] = frozenset({
    "id", "x", "y", "width", "height", "x1", "y1", "x2", "y2", "cx", "cy",
    "r", "rx", "ry", "fx", "fy", "fr", "d", "points", "pathLength",
    "viewBox", "preserveAspectRatio", "transform", "offset",
    "gradientUnits", "gradientTransform", "spreadMethod", "clipPathUnits",
    "maskUnits", "maskContentUnits", "version", "style", "href",
})

ALLOWED_ATTRIBUTES: frozenset[str] = PRESENTATION_PROPERTIES | GEOMETRY_ATTRIBUTES

MAX_ELEMENTS = 20_000
# Both tree walks below recurse; a logo or mesh never nests this deep.
MAX_DEPTH = 64

_LOCAL_REF = r"#[A-Za-z_][\w.\-]*"
_HREF_RE = re.compile(rf"^\s*{_LOCAL_REF}\s*$")
_URL_RE = re.compile(r"url\(", re.IGNORECASE)
_SAFE_URL_RE = re.compile(rf"url\(\s*(['\"]?){_LOCAL_REF}\1\s*\)", re.IGNORECASE)
_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)
_ID_RE = re.compile(r"^[A-Za-z_][\w.\-]*$")
_VIEWBOX_RE = re.compile(r"[\s,]+")
# Things no allowed attribute value or style declaration ever needs.
_FORBIDDEN_VALUE_RE = re.compile(
    r"javascript:|vbscript:|data:|expression\s*\(|@import|\\|<|>|/\*", re.IGNORECASE
)


class SvgRefused(ValueError):
    """The upload cannot be sanitised into a safe SVG. ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class SanitisedSvg:
    data: bytes
    view_box: tuple[float, float, float, float]
    removed: list[str] = field(default_factory=list)

    @property
    def aspect(self) -> float:
        return self.view_box[2] / self.view_box[3]


def _local(tag: str) -> tuple[str | None, str]:
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return None, tag


def _safe_value(value: str) -> bool:
    if _FORBIDDEN_VALUE_RE.search(value):
        return False
    # Every url( must be exactly url(#id).
    return len(_URL_RE.findall(value)) == len(_SAFE_URL_RE.findall(value))


def _clean_style(style: str, where: str, removed: list[str]) -> str:
    kept: list[str] = []
    for declaration in style.split(";"):
        if not declaration.strip():
            continue
        prop, sep, value = declaration.partition(":")
        prop = prop.strip().lower()
        value = value.strip()
        if sep and prop in PRESENTATION_PROPERTIES and value and _safe_value(value):
            kept.append(f"{prop}:{value}")
        else:
            removed.append(f"style declaration {declaration.strip()[:60]!r} on {where}")
    return ";".join(kept)


def _parse_view_box(value: str | None) -> tuple[float, float, float, float]:
    if not value:
        raise SvgRefused(
            "svg_missing_viewbox",
            "the <svg> root has no viewBox; add one (e.g. viewBox=\"0 0 480 124\") so the image scales.",
        )
    parts = [p for p in _VIEWBOX_RE.split(value.strip()) if p]
    try:
        numbers = tuple(float(p) for p in parts)
    except ValueError:
        numbers = ()
    if len(numbers) != 4 or numbers[2] <= 0 or numbers[3] <= 0:
        raise SvgRefused(
            "svg_missing_viewbox",
            f"viewBox {value!r} is not four numbers with a positive width and height.",
        )
    return numbers  # type: ignore[return-value]


def _escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;")
    )


def _escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _serialise(el: ET.Element, is_root: bool, out: list[str]) -> None:
    _, name = _local(el.tag)
    attrs = []
    if is_root:
        attrs.append(f'xmlns="{SVG_NS}"')
    for key, value in el.attrib.items():
        attrs.append(f'{key}="{_escape_attr(value)}"')
    head = "<" + name + ("" if not attrs else " " + " ".join(attrs))
    children = list(el)
    text = el.text if name == "title" and el.text else ""
    if not children and not text:
        out.append(head + "/>")
        return
    out.append(head + ">")
    if text:
        out.append(_escape_text(text))
    for child in children:
        _serialise(child, False, out)
    out.append(f"</{name}>")


def _clean(el: ET.Element, removed: list[str]) -> None:
    _, name = _local(el.tag)
    cleaned: dict[str, str] = {}
    for key, value in el.attrib.items():
        ns, attr = _local(key)
        if ns == XLINK_NS and attr == "href":
            attr_name = "href"
        elif ns is None:
            attr_name = attr
        else:
            removed.append(f"attribute {key} on <{name}>")
            continue
        if attr_name not in ALLOWED_ATTRIBUTES:
            removed.append(f"attribute {attr_name} on <{name}>")
            continue
        if attr_name == "href":
            if not _HREF_RE.match(value) or name not in ("linearGradient", "radialGradient"):
                removed.append(f"href {value[:60]!r} on <{name}>")
                continue
            value = value.strip()
        elif attr_name == "id":
            if not _ID_RE.match(value):
                removed.append(f"id {value[:60]!r} on <{name}>")
                continue
        elif attr_name == "style":
            value = _clean_style(value, f"<{name}>", removed)
            if not value:
                continue
        elif not _safe_value(value):
            removed.append(f"attribute {attr_name}={value[:60]!r} on <{name}>")
            continue
        cleaned[attr_name] = value
    el.attrib.clear()
    el.attrib.update(cleaned)
    el.tail = None
    if name == "title":
        for child in list(el):
            removed.append(f"<{_local(child.tag)[1]}> inside <title>")
            el.remove(child)
        return
    el.text = None
    for child in list(el):
        ns, child_name = _local(child.tag)
        if ns != SVG_NS or child_name not in ALLOWED_ELEMENTS or child_name == "svg":
            removed.append(f"<{child_name}>")
            el.remove(child)
            continue
        _clean(child, removed)


class _DtdRefused(Exception):
    pass


def _has_doctype(text: str) -> bool:
    """Scan the document with expat and refuse any DTD internal subset or
    entity declaration. Returns whether a (harmless) DOCTYPE was present.

    Done by the parser, not a pattern: a ``>`` inside a quoted system id or a
    decoy DOCTYPE in a comment defeats any regex, and an internal subset can
    inject attribute defaults (``<!ATTLIST``) even without entities.
    """
    seen: list[bool] = []
    parser = expat.ParserCreate()

    def on_doctype(_name, _system_id, _public_id, has_internal_subset):  # noqa: ANN001
        if has_internal_subset:
            raise _DtdRefused()
        seen.append(True)

    def on_entity(*_args):  # noqa: ANN002
        raise _DtdRefused()

    parser.StartDoctypeDeclHandler = on_doctype
    parser.EntityDeclHandler = on_entity
    parser.UnparsedEntityDeclHandler = on_entity
    try:
        parser.Parse(text.encode("utf-8"), True)
    except _DtdRefused:
        raise SvgRefused(
            "svg_entities",
            "the SVG declares a DTD internal subset or entities; remove the DOCTYPE and any <!ENTITY>/"
            "<!ATTLIST> declarations (re-export as plain SVG).",
        ) from None
    except expat.ExpatError as exc:
        raise SvgRefused("svg_parse", f"the SVG is not well-formed XML ({exc}).") from None
    return bool(seen)


def _depth(root: ET.Element) -> int:
    deepest, stack = 0, [(root, 1)]
    while stack:
        el, d = stack.pop()
        deepest = max(deepest, d)
        if deepest > MAX_DEPTH:
            break
        stack.extend((child, d + 1) for child in el)
    return deepest


def sanitise_svg(data: bytes) -> SanitisedSvg:
    """Return the sanitised SVG, or raise :class:`SvgRefused`."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SvgRefused("svg_encoding", "the SVG is not UTF-8 text; re-export it as UTF-8.") from None
    removed: list[str] = []
    if _has_doctype(text):
        removed.append("DOCTYPE")
    text = _XML_DECL_RE.sub("", text, count=1)
    if text.count("<") > MAX_ELEMENTS * 2:
        raise SvgRefused("svg_too_complex", f"the SVG has more than {MAX_ELEMENTS} elements; simplify it.")
    try:
        # A plain DOCTYPE (no internal subset) is ignored by the parser:
        # external DTDs are never fetched, and the serialiser never emits it.
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SvgRefused("svg_parse", f"the SVG is not well-formed XML ({exc}).") from None
    ns, name = _local(root.tag)
    if name != "svg" or ns != SVG_NS:
        raise SvgRefused(
            "svg_not_svg",
            'the root element must be <svg xmlns="http://www.w3.org/2000/svg">.',
        )
    if sum(1 for _ in root.iter()) > MAX_ELEMENTS:
        raise SvgRefused("svg_too_complex", f"the SVG has more than {MAX_ELEMENTS} elements; simplify it.")
    if _depth(root) > MAX_DEPTH:
        raise SvgRefused("svg_too_complex", f"the SVG nests elements more than {MAX_DEPTH} deep; flatten its groups.")
    view_box = _parse_view_box(root.attrib.get("viewBox"))
    _clean(root, removed)
    out: list[str] = []
    _serialise(root, True, out)
    return SanitisedSvg(data="".join(out).encode("utf-8"), view_box=view_box, removed=removed)
