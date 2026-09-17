"""Saneado de SVG remoto antes de almacenarlo (F-6, CWE-79 latente):
`website_brand_extractor.py` clasifica como `LOGO_VECTOR` cualquier
payload que empiece por `<?xml`/`<svg`, y `local_brand_asset_storage.py`
lo acepta y escribe `.svg` tal cual. Hoy no hay ruta que sirva esos bytes
(`serializers.py` solo devuelve la clave), pero el dia que el panel los
sirva o un renderizador los abra, un SVG rastreado de un sitio hostil con
`<script>`, `on*` o un `<use href>` externo se convierte en XSS.

Estrategia de lista blanca, no negra (checklists/website-brand-extractor-
review.md, F-6): se recorre el arbol y se RECHAZA el documento entero (se
devuelve `None`, "reject if anything remains") en cuanto aparece una
etiqueta fuera del allow-list -- nunca se intenta adivinar como podar solo
esa rama. Los atributos de riesgo conocido (manejadores de evento,
`href`/`xlink:href` externos) se ELIMINAN en el sitio, conservando el
resto del elemento; cualquier otro atributo no reconocido tambien se
elimina (menor riesgo que una etiqueta desconocida). `<!DOCTYPE`/`<!ENTITY`
se rechazan antes de parsear -- defensa en profundidad de tamano (bombas
de entidades) independiente del tope de bytes que ya aplica el llamante."""

from __future__ import annotations

import re
from typing import Final
from xml.etree import ElementTree

_DOCTYPE_OR_ENTITY_PATTERN: Final = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)

# Elementos SVG suficientes para un logo/icono tipico (formas, gradientes,
# agrupacion, texto) -- deliberadamente sin `script`, `foreignObject`,
# `style` (CSS embebido es su propia superficie) ni `animate*` (pueden
# disparar recursos externos via `xlink:href`).
_ALLOWED_TAGS: Final = frozenset(
    {
        "svg",
        "g",
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "defs",
        "lineargradient",
        "radialgradient",
        "stop",
        "clippath",
        "mask",
        "title",
        "desc",
        "symbol",
        "use",
    }
)

_ALLOWED_ATTRS: Final = frozenset(
    {
        "id",
        "class",
        "d",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "width",
        "height",
        "viewbox",
        "transform",
        "fill",
        "fill-rule",
        "fill-opacity",
        "stroke",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-dasharray",
        "opacity",
        "points",
        "offset",
        "stop-color",
        "stop-opacity",
        "gradientunits",
        "gradienttransform",
        "xmlns",
        "version",
        "preserveaspectratio",
        "clip-path",
        "mask",
        "href",
    }
)


def sanitize_remote_svg(payload: bytes) -> bytes | None:
    """Devuelve el SVG saneado, o `None` si no se puede sanear con
    garantias (rechazo, no reparo best-effort): payload sin parsear como
    XML, `DOCTYPE`/`ENTITY` presentes, o cualquier etiqueta fuera de
    `_ALLOWED_TAGS` en cualquier profundidad del arbol."""
    if _DOCTYPE_OR_ENTITY_PATTERN.search(payload):
        return None
    try:
        # DOCTYPE/ENTITY ya rechazados arriba (defensa de tamano contra
        # bombas de entidad); expat no resuelve entidades EXTERNAS por
        # defecto desde Python 3.7.1 (sin XXE de red/fichero). Sin
        # `defusedxml` a proposito (REUSE-BEFORE-WRITING, misma decision
        # que `html_brand_parser.py` sobre HTML/CSS).
        root = ElementTree.fromstring(payload)  # noqa: S314
    except ElementTree.ParseError:
        return None
    if _local_name(root.tag) != "svg":
        return None
    if not _sanitize_subtree(root):
        return None
    return bytes(ElementTree.tostring(root, encoding="utf-8"))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _sanitize_subtree(element: ElementTree.Element) -> bool:
    if _local_name(element.tag) not in _ALLOWED_TAGS:
        return False
    _strip_unsafe_attributes(element)
    return all(_sanitize_subtree(child) for child in element)


def _strip_unsafe_attributes(element: ElementTree.Element) -> None:
    for name in list(element.attrib):
        local = _local_name(name)
        if local.startswith("on"):
            del element.attrib[name]
        elif local == "href":
            if not element.attrib[name].startswith("#"):
                del element.attrib[name]
        elif local not in _ALLOWED_ATTRS:
            del element.attrib[name]
