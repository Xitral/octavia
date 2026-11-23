"""Cleaning a picture she drew (an SVG) before a panel shows it.

The panel already shows a drawing as an image, where nothing in it can run a script or load anything, and its page
allows no outside requests at all. This is the first wall: the SVG is parsed and rebuilt from a list of drawing
elements and attributes known to be harmless, so a script, an event handler, a link out, an embedded page or any
CSS (``style`` attributes and elements included: presentation attributes do the drawing) never even reaches the
panel. A document type or entity declaration is refused before parsing, which
also rules out entity-expansion bombs.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
MAX_CHARS = 60_000
MAX_ELEMENTS = 2_000
MAX_DEPTH = 24

ELEMENTS = frozenset(
    {
        "svg", "g", "defs", "title", "desc", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
        "text", "tspan", "textPath", "linearGradient", "radialGradient", "stop", "marker", "clipPath", "mask",
        "pattern", "symbol", "use",
    }
)  # fmt: skip
ATTRIBUTES = frozenset(
    {
        "id", "class", "viewBox", "preserveAspectRatio", "width", "height", "x", "y", "x1", "y1", "x2", "y2",
        "cx", "cy", "r", "rx", "ry", "d", "points", "dx", "dy", "rotate", "transform", "fill", "fill-opacity",
        "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-linecap", "stroke-linejoin",
        "stroke-dasharray", "stroke-dashoffset", "stroke-miterlimit", "opacity", "color", "font-family",
        "font-size", "font-weight", "font-style", "text-anchor", "dominant-baseline", "alignment-baseline",
        "letter-spacing", "word-spacing", "text-decoration", "offset", "stop-color", "stop-opacity",
        "gradientUnits", "gradientTransform", "spreadMethod", "fx", "fy", "fr", "markerWidth", "markerHeight",
        "refX", "refY", "orient", "markerUnits", "clip-path", "clipPathUnits", "mask", "maskUnits",
        "maskContentUnits", "patternUnits", "patternContentUnits", "patternTransform", "marker-start",
        "marker-mid", "marker-end", "vector-effect", "paint-order", "visibility", "display", "pathLength",
        "startOffset", "textLength", "lengthAdjust", "shape-rendering", "href", "version",
    }
)  # fmt: skip
# A value may point inside the drawing (url(#id)) but nowhere else, and never at script.
_URL = re.compile(r"url\s*\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
_BAD_VALUE = re.compile(
    r"javascript:|vbscript:|data:|expression\s*\(|@import|behavior\s*:|-moz-binding|image-set\s*\(|\bimage\s*\(|"
    r"\bsrc\s*\(|cross-fade\s*\(|element\s*\(",
    re.I,
)
_DECLARATION = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


class DrawingError(ValueError):
    """The drawing can't be shown; the message says why, for her to fix it."""


def _local(name: str) -> tuple[str, str]:
    """``{namespace}tag`` as (namespace, tag)."""
    if name.startswith("{"):
        ns, _, tag = name[1:].partition("}")
        return ns, tag
    return "", name


def _safe_value(value: str) -> bool:
    if "\\" in value or _BAD_VALUE.search(value):  # no drawing value needs a backslash; CSS escapes hide words
        return False
    return all(target.strip().startswith("#") for _, target in _URL.findall(value))


def clean_svg(source: str, max_chars: int = MAX_CHARS) -> str:
    """The drawing rebuilt from harmless parts only, as SVG text. Raises ``DrawingError`` when it isn't an SVG at
    all, is too big, or declares a document type or entities."""
    text = source.strip()
    if not text:
        raise DrawingError("the drawing is empty")
    if len(text) > max_chars:
        raise DrawingError(f"the drawing is too big ({len(text)} characters; at most {max_chars})")
    if _DECLARATION.search(text):
        raise DrawingError("the drawing may not declare a DOCTYPE or entities")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise DrawingError(f"the drawing isn't valid SVG ({exc})") from None
    ns, tag = _local(root.tag)
    if tag != "svg" or ns not in ("", SVG_NS):
        raise DrawingError("the drawing has to be one <svg> element")
    count = 0

    def rebuild(node: ET.Element, depth: int) -> ET.Element | None:
        nonlocal count
        node_ns, node_tag = _local(node.tag)
        if node_ns not in ("", SVG_NS) or node_tag not in ELEMENTS or depth > MAX_DEPTH:
            return None
        count += 1
        if count > MAX_ELEMENTS:
            raise DrawingError(f"the drawing has too many elements (at most {MAX_ELEMENTS})")
        out = ET.Element(node_tag)  # plain names; the root carries the SVG namespace
        for name, value in node.attrib.items():
            attr_ns, attr = _local(name)
            if attr_ns not in ("", XLINK_NS) or attr not in ATTRIBUTES or not _safe_value(value):
                continue
            if attr == "href":
                if not value.strip().startswith("#"):  # only another part of the same drawing
                    continue
                if node_tag not in ("use", "textPath", "linearGradient", "radialGradient", "pattern"):
                    continue
            out.set(attr, value)
        out.text = node.text if node_tag in ("text", "tspan", "textPath", "title", "desc") else None
        for child in node:
            kept = rebuild(child, depth + 1)
            if kept is not None:
                kept.tail = child.tail if node_tag in ("text", "tspan", "textPath") else None
                out.append(kept)
        return out

    cleaned = rebuild(root, 0)
    assert cleaned is not None  # the root was checked above
    if "viewBox" not in cleaned.attrib and not ("width" in cleaned.attrib and "height" in cleaned.attrib):
        raise DrawingError('the drawing needs a viewBox (e.g. viewBox="0 0 400 300") so it can be scaled')
    cleaned.set("xmlns", SVG_NS)
    return ET.tostring(cleaned, encoding="unicode")
