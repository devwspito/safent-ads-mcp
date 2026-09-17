"""Funciones puras de `proposals/presentation/rest.py` para las rutas
nuevas de esta rama (`PUT .../owner-context`, `POST .../postpone`,
`PATCH /proposals/{id}`): parseo/validacion del cuerpo y la coercion de
`valor_propuesto`. El resto (autorizacion, 404, transiciones ilegales) se
prueba de extremo a extremo en `tests/integration/composition/
test_proposal_admin_rest.py`, porque necesita Postgres real detras de
`SqlProposalRepository`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.iam.presentation.errors import ApiError
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import MAX_OWNER_CONTEXT_LENGTH
from safent_ads.proposals.presentation.rest import (
    _coerce_proposed_value,
    _require_datetime_field,
    _require_number,
    _require_owner_context_text,
)


class TestRequireOwnerContextText:
    def test_accepts_a_plain_string(self) -> None:
        text = "nota del propietario"

        assert _require_owner_context_text({"text": text}) == text

    def test_accepts_an_empty_string(self) -> None:
        assert _require_owner_context_text({"text": ""}) == ""

    def test_missing_key_is_422(self) -> None:
        with pytest.raises(ApiError) as excinfo:
            _require_owner_context_text({})

        assert excinfo.value.status_code == 422

    def test_non_string_is_422(self) -> None:
        with pytest.raises(ApiError) as excinfo:
            _require_owner_context_text({"text": 123})

        assert excinfo.value.status_code == 422

    def test_at_the_limit_is_accepted(self) -> None:
        text = "a" * MAX_OWNER_CONTEXT_LENGTH

        assert _require_owner_context_text({"text": text}) == text

    def test_over_the_limit_is_422(self) -> None:
        with pytest.raises(ApiError) as excinfo:
            _require_owner_context_text({"text": "a" * (MAX_OWNER_CONTEXT_LENGTH + 1)})

        assert excinfo.value.status_code == 422


class TestRequireDatetimeField:
    def test_parses_an_aware_iso_string(self) -> None:
        parsed = _require_datetime_field({"until": "2026-09-10T12:00:00+00:00"}, "until")

        assert parsed == datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

    def test_accepts_the_z_suffix(self) -> None:
        parsed = _require_datetime_field({"until": "2026-09-10T12:00:00Z"}, "until")

        assert parsed.utcoffset() is not None

    def test_missing_key_is_422(self) -> None:
        with pytest.raises(ApiError) as excinfo:
            _require_datetime_field({}, "until")

        assert excinfo.value.status_code == 422

    def test_malformed_string_is_422(self) -> None:
        with pytest.raises(ApiError) as excinfo:
            _require_datetime_field({"until": "not-a-date"}, "until")

        assert excinfo.value.status_code == 422

    def test_naive_datetime_is_422(self) -> None:
        with pytest.raises(ApiError) as excinfo:
            _require_datetime_field({"until": "2026-09-10T12:00:00"}, "until")

        assert excinfo.value.status_code == 422


class TestRequireNumber:
    def test_accepts_int_and_float(self) -> None:
        assert _require_number({"valor_propuesto": 42}, "valor_propuesto") == 42.0
        assert _require_number({"valor_propuesto": 42.5}, "valor_propuesto") == 42.5

    def test_rejects_bool(self) -> None:
        with pytest.raises(ApiError):
            _require_number({"valor_propuesto": True}, "valor_propuesto")

    def test_rejects_string(self) -> None:
        with pytest.raises(ApiError):
            _require_number({"valor_propuesto": "42"}, "valor_propuesto")

    def test_missing_key_is_422(self) -> None:
        with pytest.raises(ApiError) as excinfo:
            _require_number({}, "valor_propuesto")

        assert excinfo.value.status_code == 422


class TestCoerceProposedValue:
    def test_wraps_a_number_as_money_preserving_currency(self) -> None:
        current = Money.of("100", "EUR")

        coerced = _coerce_proposed_value(current, 55.0)

        assert coerced == Money.of("55.0", "EUR")

    def test_passes_through_non_money_values(self) -> None:
        assert _coerce_proposed_value("paused", 1.0) == 1.0
