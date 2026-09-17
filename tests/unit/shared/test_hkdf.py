"""`shared/crypto/hkdf.py` (security-review-f4.md B-1): la clave de firma de
previews de creative deja de ser un campo de settings con `default=` publico
y pasa a derivarse en caliente de `ADS_SESSION_SECRET` -- este modulo prueba
la primitiva de derivacion en aislamiento, sin construir `ApiSettings`."""

from __future__ import annotations

from safent_ads.shared.crypto.hkdf import derive_key

_MASTER_SECRET = b"a" * 32
_INFO = b"safent-ads/creative-preview/v1"


def test_derive_key_is_deterministic_for_the_same_inputs() -> None:
    first = derive_key(_MASTER_SECRET, _INFO)
    second = derive_key(_MASTER_SECRET, _INFO)

    assert first == second


def test_derive_key_differs_for_a_different_info() -> None:
    creative_preview_key = derive_key(_MASTER_SECRET, _INFO)
    another_use_key = derive_key(_MASTER_SECRET, b"safent-ads/some-other-use/v1")

    assert creative_preview_key != another_use_key


def test_derive_key_differs_for_a_different_master_secret() -> None:
    first = derive_key(_MASTER_SECRET, _INFO)
    second = derive_key(b"b" * 32, _INFO)

    assert first != second


def test_derive_key_default_length_is_32_bytes() -> None:
    key = derive_key(_MASTER_SECRET, _INFO)

    assert len(key) == 32


def test_derive_key_honors_an_explicit_length() -> None:
    key = derive_key(_MASTER_SECRET, _INFO, length=16)

    assert len(key) == 16
