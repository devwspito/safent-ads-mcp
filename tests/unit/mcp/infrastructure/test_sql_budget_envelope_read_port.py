"""Aritmetica pura de `get_budget_envelope` (sin DB): proyeccion a fin de
mes y ensamblado del DTO con/sin tope conocido. El camino completo
(cuenta real, guardarrail sembrado o ausente) va en
`tests/integration/mcp/test_budget_envelope_read_port_contract.py`."""

from __future__ import annotations

from datetime import date

from safent_ads.mcp.infrastructure.sql_budget_envelope_read_port import (
    _envelope_with_account,
    _envelope_without_account,
    _project_month_end,
)
from safent_ads.rules.domain.guardrail import GuardrailPolicy

_TODAY = date(2026, 9, 15)  # dia 15 de un mes de 30: mitad exacta


def test_project_month_end_extrapolates_the_current_daily_run_rate() -> None:
    # 15.000 minor gastados en 15 dias de un mes de 30 -> ritmo constante
    # proyecta el doble a fin de mes.
    projected = _project_month_end(15_000, _TODAY, date(2026, 9, 1))

    assert projected == 30_000


def test_envelope_without_account_nulls_every_numeric_field() -> None:
    envelope = _envelope_without_account("biz-1", _TODAY)

    assert envelope.monthly_cap_minor is None
    assert envelope.spent_month_to_date_minor is None
    assert envelope.headroom_minor is None
    assert envelope.projected_month_end_minor is None
    assert envelope.currency is None
    assert envelope.reason == "no_platform_account"


def test_envelope_with_account_but_without_caps_entry_still_reports_real_spend() -> None:
    envelope = _envelope_with_account(
        business_id="biz-1",
        today=_TODAY,
        currency="EUR",
        spent_minor=15_000,
        projected_minor=30_000,
        policy=None,
    )

    assert envelope.monthly_cap_minor is None
    assert envelope.headroom_minor is None
    assert envelope.spent_month_to_date_minor == 15_000
    assert envelope.projected_month_end_minor == 30_000
    assert envelope.currency == "EUR"
    assert envelope.reason == "no_caps_entry"


def test_envelope_with_a_known_cap_computes_headroom_without_clamping_overspend() -> None:
    policy = GuardrailPolicy(
        daily_cap_minor=2_000,
        monthly_cap_minor=20_000,
        floor_minor=0,
        ceiling_minor=20_000,
        max_step_pct=20.0,
        max_changes_per_day=5,
    )

    envelope = _envelope_with_account(
        business_id="biz-1",
        today=_TODAY,
        currency="EUR",
        spent_minor=25_000,  # ya por encima del tope mensual
        projected_minor=50_000,
        policy=policy,
    )

    assert envelope.monthly_cap_minor == 20_000
    assert envelope.headroom_minor == -5_000
    assert envelope.reason is None
