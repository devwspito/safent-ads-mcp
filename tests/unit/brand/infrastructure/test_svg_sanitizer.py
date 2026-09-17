"""`sanitize_remote_svg` (F-6, CWE-79 latente): lista blanca de
etiquetas/atributos sobre un SVG rastreado de un sitio no confiable --
rechazo entero ante cualquier etiqueta desconocida, eliminacion selectiva
de atributos de riesgo conocido (manejadores de evento, href externo)."""

from __future__ import annotations

from safent_ads.brand.infrastructure.svg_sanitizer import sanitize_remote_svg


def test_remote_svg_with_script_is_not_stored() -> None:
    payload = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

    assert sanitize_remote_svg(payload) is None


def test_accepts_a_benign_svg_with_paths_and_gradients() -> None:
    payload = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        b'<defs><linearGradient id="g"><stop offset="0" stop-color="#112233"/>'
        b"</linearGradient></defs>"
        b'<path d="M0 0 L10 10" fill="url(#g)"/>'
        b"</svg>"
    )

    sanitized = sanitize_remote_svg(payload)

    assert sanitized is not None
    assert b"<path" in sanitized or b"path" in sanitized


def test_strips_event_handler_attributes_but_keeps_the_element() -> None:
    payload = (
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b'<path d="M0 0" onload="evil()" onclick="evil()"/>'
        b"</svg>"
    )

    sanitized = sanitize_remote_svg(payload)

    assert sanitized is not None
    assert b"onload" not in sanitized
    assert b"onclick" not in sanitized
    assert b"path" in sanitized


def test_strips_external_href_but_keeps_the_element() -> None:
    payload = (
        b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
        b'<use xlink:href="https://evil.example/payload.svg#x"/>'
        b"</svg>"
    )

    sanitized = sanitize_remote_svg(payload)

    assert sanitized is not None
    assert b"evil.example" not in sanitized


def test_keeps_internal_fragment_href() -> None:
    payload = (
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b'<defs><path id="p" d="M0 0"/></defs>'
        b'<use href="#p"/>'
        b"</svg>"
    )

    sanitized = sanitize_remote_svg(payload)

    assert sanitized is not None
    assert b'href="#p"' in sanitized


def test_rejects_foreign_object() -> None:
    payload = (
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b'<foreignObject><body xmlns="http://www.w3.org/1999/xhtml">hi</body></foreignObject>'
        b"</svg>"
    )

    assert sanitize_remote_svg(payload) is None


def test_rejects_any_tag_outside_the_allow_list() -> None:
    payload = b'<svg xmlns="http://www.w3.org/2000/svg"><style>body{}</style></svg>'

    assert sanitize_remote_svg(payload) is None


def test_rejects_doctype_with_entity_before_parsing() -> None:
    payload = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg"><title>&xxe;</title></svg>'
    )

    assert sanitize_remote_svg(payload) is None


def test_rejects_malformed_xml() -> None:
    assert sanitize_remote_svg(b"<svg><path></svg>") is None


def test_rejects_when_root_is_not_svg() -> None:
    payload = b'<notsvg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></notsvg>'

    assert sanitize_remote_svg(payload) is None


def test_strips_unrecognized_attributes() -> None:
    payload = (
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b'<path d="M0 0" data-tracking="abc123"/>'
        b"</svg>"
    )

    sanitized = sanitize_remote_svg(payload)

    assert sanitized is not None
    assert b"data-tracking" not in sanitized
