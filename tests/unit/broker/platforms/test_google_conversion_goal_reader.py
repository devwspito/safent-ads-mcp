"""`verify_conversion_goals` (threat-model.md
S-1/S-2/S-3; tasks.md T032): re-lee por GAQL antes de que el broker mute
nada, exige el mismo `customer_id` de destino y `ENABLED`, y da el mismo
error para inexistente/pausada/ajena."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from safent_ads.broker.platforms.google_conversion_goal_reader import (
    ConversionGoalVerificationError,
    VerifiedConversionGoal,
    verify_conversion_goals,
)

_OWN = "customers/1112223333/conversionActions/456"
_OTHER_ACCOUNT = "customers/9998887777/conversionActions/456"


class _FakeSearchClient:
    def __init__(self, rows: list[Mapping[str, Any]]) -> None:
        self._rows = rows
        self.queries: list[tuple[str, str]] = []

    def search_stream(self, customer_id: str, query: str) -> Iterator[Mapping[str, Any]]:
        self.queries.append((customer_id, query))
        yield from self._rows


def _row(
    resource_name: str,
    *,
    status: str = "ENABLED",
    category: str = "PURCHASE",
    origin: str = "WEBSITE",
) -> Mapping[str, Any]:
    return {
        "conversion_action.resource_name": resource_name,
        "conversion_action.status": status,
        "conversion_action.category": category,
        "conversion_action.origin": origin,
    }


def test_meta_enabled_y_propia_verifica() -> None:
    client = _FakeSearchClient([_row(_OWN)])

    result = verify_conversion_goals(client, "1112223333", [_OWN])

    assert result == (
        VerifiedConversionGoal(resource_name=_OWN, category="PURCHASE", origin="WEBSITE"),
    )
    assert len(client.queries) == 1


def test_meta_de_otra_cuenta_no_verifica() -> None:
    client = _FakeSearchClient([_row(_OTHER_ACCOUNT)])

    with pytest.raises(ConversionGoalVerificationError):
        verify_conversion_goals(client, "1112223333", [_OTHER_ACCOUNT])

    # S-1: cero llamadas a la lectura de GAQL siquiera -- el prefijo se
    # rechaza antes de tocar el doble del SDK.
    assert client.queries == []


def test_meta_pausada_no_verifica() -> None:
    client = _FakeSearchClient([_row(_OWN, status="PAUSED")])

    with pytest.raises(ConversionGoalVerificationError):
        verify_conversion_goals(client, "1112223333", [_OWN])


def test_meta_inexistente_no_verifica() -> None:
    client = _FakeSearchClient([])

    with pytest.raises(ConversionGoalVerificationError):
        verify_conversion_goals(client, "1112223333", [_OWN])


def test_inexistente_pausada_y_ajena_dan_el_mismo_error_byte_a_byte() -> None:
    inexistente = _fails(_FakeSearchClient([]), [_OWN])
    pausada = _fails(_FakeSearchClient([_row(_OWN, status="PAUSED")]), [_OWN])
    ajena = _fails(_FakeSearchClient([_row(_OTHER_ACCOUNT)]), [_OTHER_ACCOUNT])

    expected = "campaign_creation_conversion_goal_unverified"
    assert str(inexistente) == str(pausada) == str(ajena) == expected


def _fails(client: _FakeSearchClient, resource_names: list[str]) -> Exception:
    with pytest.raises(ConversionGoalVerificationError) as excinfo:
        verify_conversion_goals(client, "1112223333", resource_names)
    return excinfo.value


def test_el_prefijo_se_compara_contra_el_customer_id_del_destino_no_contra_el_del_payload() -> None:
    """El nombre de recurso trae su propio `customer_id` (`_OWN` empieza
    por `customers/1112223333`); pasar ESE MISMO valor como
    `customer_id` de destino no basta si Google, en la relectura, dice
    que la fila pertenece a otra cuenta -- lo que cuenta es el parametro
    explicito, nunca una cadena derivada del propio nombre de recurso."""
    client = _FakeSearchClient([_row(_OWN)])

    with pytest.raises(ConversionGoalVerificationError):
        verify_conversion_goals(client, "9998887777", [_OWN])

    assert client.queries == []


def test_mas_de_diez_metas_no_verifica() -> None:
    names = [f"customers/1112223333/conversionActions/{n}" for n in range(11)]

    with pytest.raises(ConversionGoalVerificationError):
        verify_conversion_goals(_FakeSearchClient([]), "1112223333", names)


def test_lista_vacia_no_verifica() -> None:
    with pytest.raises(ConversionGoalVerificationError):
        verify_conversion_goals(_FakeSearchClient([]), "1112223333", [])


def test_duplicados_no_verifican() -> None:
    with pytest.raises(ConversionGoalVerificationError):
        verify_conversion_goals(_FakeSearchClient([_row(_OWN)]), "1112223333", [_OWN, _OWN])


def test_nombre_de_recurso_malformado_no_verifica() -> None:
    with pytest.raises(ConversionGoalVerificationError):
        verify_conversion_goals(_FakeSearchClient([]), "1112223333", ["not-a-resource-name"])
