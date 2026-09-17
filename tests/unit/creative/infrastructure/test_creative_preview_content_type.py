"""`resolve_creative_preview_content_type`: sniff de magic bytes para los
cuatro tipos que `creative` almacena (png/jpeg/mp4/mp3/wav, nunca la
extension guardada) y rechazo (415) de cualquier otro payload."""

from __future__ import annotations

import pytest

from safent_ads.creative.infrastructure.creative_preview_content_type import (
    UnpreviewableCreativeAssetError,
    resolve_creative_preview_content_type,
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
_MP3_ID3_BYTES = b"ID3" + b"\x00" * 32
_MP3_FRAME_SYNC_BYTES = b"\xff\xfb" + b"\x00" * 32
_WAV_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 32


@pytest.mark.parametrize(
    ("payload", "expected_content_type"),
    [
        (_PNG_BYTES, "image/png"),
        (_JPEG_BYTES, "image/jpeg"),
        (_MP4_BYTES, "video/mp4"),
        (_MP3_ID3_BYTES, "audio/mpeg"),
        (_MP3_FRAME_SYNC_BYTES, "audio/mpeg"),
        (_WAV_BYTES, "audio/wav"),
    ],
)
def test_sniffs_content_type_from_magic_bytes_ignoring_any_extension(
    payload: bytes, expected_content_type: str
) -> None:
    resolved = resolve_creative_preview_content_type(payload)

    assert resolved.content_type == expected_content_type
    assert resolved.body == payload


def test_a_png_named_with_a_mp4_extension_is_still_sniffed_as_png() -> None:
    """El nombre/extension guardados nunca deciden el `Content-Type` --
    solo los magic bytes del payload en si (mismo criterio que
    `local_asset_storage._looks_like` aplica al escribir)."""
    fake_mp4_named_payload = _PNG_BYTES

    resolved = resolve_creative_preview_content_type(fake_mp4_named_payload)

    assert resolved.content_type == "image/png"


def test_rejects_a_riff_payload_that_is_not_actually_wave() -> None:
    webp_like_riff = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 32

    with pytest.raises(UnpreviewableCreativeAssetError):
        resolve_creative_preview_content_type(webp_like_riff)


def test_rejects_a_payload_that_matches_no_known_signature() -> None:
    with pytest.raises(UnpreviewableCreativeAssetError):
        resolve_creative_preview_content_type(b"not a media file at all")
