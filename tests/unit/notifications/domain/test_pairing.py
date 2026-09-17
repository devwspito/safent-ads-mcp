"""`OwnerTelegramChat` (FR-25, contracts/telegram.md §Emparejamiento):
formato de codigo, mascara de `chat_id` y el calculo de estado efectivo
cuando un codigo `pending` ya caduco."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.notifications.domain.pairing import (
    InvalidPairingCodeError,
    PairingCode,
    PairingStatus,
    effective_status,
    mask_chat_id,
)

_NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)


class TestPairingCode:
    def test_generate_matches_the_contract_pattern(self) -> None:
        code = PairingCode.generate()

        assert len(code.value) == 8
        assert code.value.isascii()
        assert all(ch.isupper() or ch.isdigit() for ch in code.value)
        assert "0" not in code.value
        assert "1" not in code.value

    def test_accepts_a_well_formed_code(self) -> None:
        assert PairingCode("ABCD2345").value == "ABCD2345"

    @pytest.mark.parametrize(
        "raw",
        [
            "abcd2345",  # minusculas
            "ABCD234",  # 7 caracteres
            "ABCD23456",  # 9 caracteres
            "ABCD0345",  # '0' fuera del alfabeto
            "ABCD1345",  # '1' fuera del alfabeto
            "ABCD 345",  # espacio
        ],
    )
    def test_rejects_malformed_codes(self, raw: str) -> None:
        with pytest.raises(InvalidPairingCodeError):
            PairingCode(raw)

    def test_hash_is_deterministic_sha256(self) -> None:
        code = PairingCode("ABCD2345")

        assert code.hash() == code.hash()
        assert len(code.hash()) == 64

    def test_different_codes_hash_differently(self) -> None:
        assert PairingCode("ABCD2345").hash() != PairingCode("WXYZ6789").hash()


class TestMaskChatId:
    def test_keeps_only_the_last_four_digits(self) -> None:
        assert mask_chat_id(111222333) == "***2333"

    def test_group_chat_ids_are_negative_but_mask_the_same(self) -> None:
        assert mask_chat_id(-111222333) == mask_chat_id(111222333)

    def test_pads_short_chat_ids(self) -> None:
        assert mask_chat_id(7) == "***0007"


class TestEffectiveStatus:
    def test_paired_stays_paired(self) -> None:
        result = effective_status(PairingStatus.PAIRED, code_expires_at=None, now=_NOW)

        assert result is PairingStatus.PAIRED

    def test_unpaired_stays_unpaired(self) -> None:
        result = effective_status(PairingStatus.UNPAIRED, code_expires_at=None, now=_NOW)

        assert result is PairingStatus.UNPAIRED

    def test_pending_with_live_code_stays_pending(self) -> None:
        result = effective_status(
            PairingStatus.PENDING, code_expires_at=_NOW + timedelta(minutes=1), now=_NOW
        )

        assert result is PairingStatus.PENDING

    def test_pending_with_expired_code_reads_as_unpaired(self) -> None:
        result = effective_status(
            PairingStatus.PENDING, code_expires_at=_NOW - timedelta(seconds=1), now=_NOW
        )

        assert result is PairingStatus.UNPAIRED

    def test_pending_without_expiry_reads_as_unpaired(self) -> None:
        """Fila corrupta/incompleta (nunca deberia pasar por la fabrica de
        la app, pero el dominio no confia en eso): sin fecha de caducidad,
        `pending` no puede ser un codigo vivo."""
        result = effective_status(PairingStatus.PENDING, code_expires_at=None, now=_NOW)

        assert result is PairingStatus.UNPAIRED
