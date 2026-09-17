"""Sobre de gasto (`panel_managed`) de `config/caps.yaml` -- spec 008 T028.

Invariantes de la revision T027 que se prueban aqui:
- **i1**: sin `panel_managed`, la resolucion para escritura es byte a byte
  la de hoy. Es la invariante de la que cuelga el Checkpoint D: la VM que
  no declara el bloque no cambia de comportamiento.
- **i8**: `-1`, `1.5`, `true`, `NaN`, `Infinity`, `1e3`, `10**400` y una
  divisa mal formada se rechazan.
- **i14**: un campo que el panel no fija (`floor_minor`, `max_step_pct`,
  `max_changes_per_day`, `autonomy_enabled`) dentro del sobre se rechaza
  RUIDOSAMENTE, nunca se ignora en silencio.
- Sobre incompleto: el fichero no valida y el broker no arranca, y el
  error NOMBRA el campo que falta.
"""

from __future__ import annotations

import json

import pytest

from safent_ads.broker.infrastructure.caps_config import (
    MAX_MINOR_AMOUNT,
    CapsConfigError,
    SpendEnvelope,
    parse_caps_config,
)
from safent_ads.broker.presentation.dispatcher import _reject_json_constant

_ENVELOPE_FIELDS = (
    "max_daily_cap_minor",
    "max_monthly_cap_minor",
    "max_ceiling_minor",
    "min_floor_minor",
    "max_accounts",
    "max_cap_changes_per_day",
    "currency",
)

_FILE_ONLY_YAML = """
defaults:
  max_step_pct: 30
  max_changes_per_day: 2
  autonomy_enabled: false

accounts:
  "1234567890":
    daily_cap_minor: 10000
    monthly_cap_minor: 300000
    floor_minor: 500
    ceiling_minor: 50000
"""

_ENVELOPE_BLOCK = """
panel_managed:
  currency: EUR
  max_daily_cap_minor: 5000
  max_monthly_cap_minor: 100000
  max_ceiling_minor: 20000
  min_floor_minor: 500
  max_accounts: 3
  max_cap_changes_per_day: 10
"""


def _valid_envelope_mapping() -> dict[str, object]:
    return {
        "max_daily_cap_minor": 5000,
        "max_monthly_cap_minor": 100000,
        "max_ceiling_minor": 20000,
        "min_floor_minor": 500,
        "max_accounts": 3,
        "max_cap_changes_per_day": 10,
        "currency": "EUR",
    }


# --- i1: sin sobre, nada cambia ------------------------------------------


def test_i1_file_without_envelope_keeps_todays_resolution_byte_for_byte() -> None:
    without = parse_caps_config(_FILE_ONLY_YAML)
    with_envelope = parse_caps_config(_FILE_ONLY_YAML + _ENVELOPE_BLOCK)

    assert without.panel_managed is None
    assert without.resolve("1234567890") == with_envelope.resolve("1234567890")


def test_i1_absent_envelope_is_not_an_empty_permissive_default() -> None:
    """Ausente no es "sobre vacio": es "el panel no puede fijar nada"."""
    assert parse_caps_config(_FILE_ONLY_YAML).panel_managed is None


def test_declared_envelope_is_parsed_with_its_seven_fields() -> None:
    envelope = parse_caps_config(_FILE_ONLY_YAML + _ENVELOPE_BLOCK).panel_managed

    assert envelope == SpendEnvelope(**_valid_envelope_mapping())  # type: ignore[arg-type]


# --- Sobre incompleto: no valida y nombra el campo -----------------------


@pytest.mark.parametrize("missing", _ENVELOPE_FIELDS)
def test_incomplete_envelope_fails_the_file_and_names_the_missing_field(missing: str) -> None:
    mapping = {key: value for key, value in _valid_envelope_mapping().items() if key != missing}
    document = (
        _FILE_ONLY_YAML
        + "\npanel_managed:\n"
        + "".join(f"  {key}: {value!r}\n" for key, value in mapping.items())
    )

    with pytest.raises(CapsConfigError) as error:
        parse_caps_config(document)

    assert missing in str(error.value)


# --- i8: tipado estricto en los importes ---------------------------------


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("max_daily_cap_minor", -1),
        ("max_daily_cap_minor", 1.5),
        ("max_daily_cap_minor", True),
        ("max_daily_cap_minor", 1e3),
        ("max_daily_cap_minor", "5000"),
        ("max_daily_cap_minor", 10**400),
        ("max_daily_cap_minor", MAX_MINOR_AMOUNT + 1),
        ("max_accounts", 0),
        ("max_accounts", True),
        ("max_cap_changes_per_day", 0),
        ("currency", "eur"),
        ("currency", "EURO"),
        ("currency", "E1R"),
        ("currency", ""),
        ("currency", 978),
    ],
)
def test_i8_envelope_rejects_malformed_amounts_and_currency(field_name: str, value: object) -> None:
    mapping = _valid_envelope_mapping() | {field_name: value}

    with pytest.raises(ValueError):
        SpendEnvelope(**mapping)  # type: ignore[arg-type]


def test_i8_nan_and_infinity_never_survive_the_brokers_json_parser() -> None:
    """`json.loads` los acepta por defecto y pydantic los toma por `float`
    validos: `NaN` no es mayor que nada, asi que toda comparacion contra el
    sobre saldria falsa. Se rechazan en el parseo."""
    for literal in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            json.loads(
                f'{{"max_daily_cap_minor": {literal}}}', parse_constant=_reject_json_constant
            )


def test_i8_ordering_between_envelope_bounds_is_enforced() -> None:
    for broken in (
        {"max_monthly_cap_minor": 1},
        {"min_floor_minor": 999999},
        {"max_daily_cap_minor": 999999},
    ):
        with pytest.raises(ValueError):
            SpendEnvelope(**(_valid_envelope_mapping() | broken))  # type: ignore[arg-type]


# --- i14: nada que el panel no fija entra en el sobre --------------------


@pytest.mark.parametrize(
    "extra_field",
    ["floor_minor", "max_step_pct", "max_changes_per_day", "autonomy_enabled", "daily_cap_minor"],
)
def test_i14_envelope_rejects_fields_the_panel_never_sets(extra_field: str) -> None:
    mapping = _valid_envelope_mapping() | {extra_field: 1}

    with pytest.raises(ValueError) as error:
        SpendEnvelope(**mapping)  # type: ignore[arg-type]

    assert extra_field in str(error.value)


# --- Claves canonicas cuando el panel puede escribir ---------------------


def test_non_canonical_account_key_stops_the_broker_when_the_envelope_is_declared() -> None:
    document = _FILE_ONLY_YAML.replace('"1234567890"', '"123-456-7890"') + _ENVELOPE_BLOCK

    with pytest.raises(CapsConfigError) as error:
        parse_caps_config(document)

    assert "123-456-7890" in str(error.value)


def test_non_canonical_account_key_is_still_accepted_without_an_envelope() -> None:
    """i1 otra vez: el fichero sin sobre admite cualquier clave, como hoy."""
    document = _FILE_ONLY_YAML.replace('"1234567890"', '"123-456-7890"')

    assert parse_caps_config(document).resolve("123-456-7890").daily_cap_minor == 10000
