"""`resolve_preview`: sniff de magic bytes para raster (nunca la
extension guardada), saneado obligatorio de SVG antes de servirlo, y
rechazo (415) de cualquier payload que no sea ninguna de las dos cosas."""

from __future__ import annotations

import pytest

from safent_ads.brand.infrastructure.asset_preview_content_type import (
    UnpreviewableBrandAssetError,
    resolve_preview,
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_GIF_BYTES = b"GIF89a" + b"\x00" * 32
_ICO_BYTES = b"\x00\x00\x01\x00" + b"\x00" * 32
_WEBP_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 32
_SAFE_SVG = b"<svg xmlns='http://www.w3.org/2000/svg'><rect width='1' height='1'/></svg>"
_HOSTILE_SVG = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"


@pytest.mark.parametrize(
    ("payload", "expected_content_type"),
    [
        (_PNG_BYTES, "image/png"),
        (_JPEG_BYTES, "image/jpeg"),
        (_GIF_BYTES, "image/gif"),
        (_ICO_BYTES, "image/x-icon"),
        (_WEBP_BYTES, "image/webp"),
    ],
)
def test_sniffs_raster_content_type_from_magic_bytes_ignoring_any_extension(
    payload: bytes, expected_content_type: str
) -> None:
    resolved = resolve_preview(payload)

    assert resolved.content_type == expected_content_type
    assert resolved.body == payload


def test_serves_a_safe_svg_as_svg_content_type() -> None:
    resolved = resolve_preview(_SAFE_SVG)

    assert resolved.content_type == "image/svg+xml"


def test_a_png_named_with_a_svg_extension_is_still_sniffed_as_png() -> None:
    """El nombre/extension guardados nunca deciden el `Content-Type` --
    solo los magic bytes del payload en si."""
    fake_svg_named_payload = _PNG_BYTES

    resolved = resolve_preview(fake_svg_named_payload)

    assert resolved.content_type == "image/png"


def test_rejects_a_hostile_svg_with_a_script_tag() -> None:
    with pytest.raises(UnpreviewableBrandAssetError):
        resolve_preview(_HOSTILE_SVG)


def test_the_body_served_for_a_svg_is_the_sanitized_one_not_the_original() -> None:
    svg_with_event_handler = (
        b"<svg xmlns='http://www.w3.org/2000/svg' onload='steal()'>"
        b"<rect width='1' height='1'/></svg>"
    )

    resolved = resolve_preview(svg_with_event_handler)

    assert b"onload" not in resolved.body


def test_rejects_a_payload_that_is_neither_a_known_raster_nor_a_valid_svg() -> None:
    with pytest.raises(UnpreviewableBrandAssetError):
        resolve_preview(b"not an image at all")
