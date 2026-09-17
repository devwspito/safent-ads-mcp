"""The `platform == "google" and kind == "ad_set"` block dispatches by node
type -- ad group vs. asset group -- instead of hardcoding `"SEARCH_STANDARD"`
(tasks.md T015). `keywords`/`cpc_bid` are
enforced absent where the row forbids them; a channel that still allows
their omission (e.g. Search, kept optional-by-presence here for backward
compatibility with `tests/unit/broker/platforms/test_ad_child_creation.py`'s
existing fixture) is validated when present.
"""

from __future__ import annotations

import pytest

from safent_ads.proposals.domain.ad_child_creation import (
    AdChildCreationError,
    validate_child_payload,
)

_VALID_KEYWORD = {"text": "zapatos", "match_type": "BROAD"}
_VALID_CPC_BID = {"amount": "1.25", "currency": "EUR"}


def _child_plan(native: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "platform": "google",
        "kind": "ad_set",
        "status": "PAUSED",
        "native": native,
    }


def _search_ad_group(**overrides: object) -> dict[str, object]:
    native: dict[str, object] = {
        "name": "Grupo de anuncios",
        "type": "SEARCH_STANDARD",
        "bidding_strategy": "MANUAL_CPC",
        "cpc_bid": _VALID_CPC_BID,
        "targeting_mode": "INHERIT_CAMPAIGN",
    }
    native.update(overrides)
    return native


def _display_ad_group(**overrides: object) -> dict[str, object]:
    native: dict[str, object] = {
        "name": "Grupo de Display",
        "type": "DISPLAY_STANDARD",
        # Ad-group-level bidding beyond the "MANUAL_CPC" compatibility form
        # is out of T015's scope (deferred to the Display/Demand Gen ad
        # branches, US2/US3); this only exercises keywords/cpc_bid dispatch.
        "bidding_strategy": "MANUAL_CPC",
        "targeting_mode": "INHERIT_CAMPAIGN",
    }
    native.update(overrides)
    return native


def _demand_gen_ad_group(**overrides: object) -> dict[str, object]:
    native: dict[str, object] = {
        "name": "Grupo de Demand Gen",
        "type": "DEMAND_GEN_STANDARD",
        "bidding_strategy": "MANUAL_CPC",
        "targeting_mode": "INHERIT_CAMPAIGN",
    }
    native.update(overrides)
    return native


def _asset_group(**overrides: object) -> dict[str, object]:
    native: dict[str, object] = {
        "kind": "ASSET_GROUP",
        "final_url": "https://clinicax.es/reserva",
        "assets": {
            "headlines": ["Reserva hoy"],
            "long_headlines": ["Reserva tu cita en clinicax.es"],
            "descriptions": ["Atencion cercana", "Sin listas de espera"],
            "business_name": "ClinicaX",
            "name": "Grupo de recursos 1",
            "logo": "customers/123/assets/1",
            "marketing_image": "customers/123/assets/2",
            "square_image": "customers/123/assets/3",
        },
    }
    native.update(overrides)
    return native


def test_grupo_de_recursos_valido() -> None:
    validate_child_payload({"child_plan": _child_plan(_asset_group())})


def test_grupo_de_recursos_no_admite_palabras_clave() -> None:
    native = _asset_group(keywords=[_VALID_KEYWORD])
    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": _child_plan(native)})


def test_pmax_con_cpc_bid_falla() -> None:
    native = _asset_group(cpc_bid=_VALID_CPC_BID)
    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": _child_plan(native)})


def test_grupo_de_recursos_con_clave_extra_en_assets_falla() -> None:
    """T035 finding 3 (threat-model.md #156/#203): `assets` admite el
    conjunto EXACTO de `AssetGroupAssetPlan`, igual que cualquier native
    hermano -- una clave de mas se rechaza, no se ignora."""
    native = _asset_group()
    native["assets"] = {**native["assets"], "audience_signal": None}
    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": _child_plan(native)})


def test_grupo_de_recursos_con_clave_faltante_en_assets_falla() -> None:
    native = _asset_group()
    del native["assets"]["square_image"]
    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": _child_plan(native)})


def test_search_sin_palabras_clave_falla() -> None:
    """`keywords` presente pero vacia: la lista de palabras clave de
    Busqueda nunca puede estar vacia (`valid_keywords`, 1..50). La total
    ausencia de la clave se mantiene aceptada en este validador generico
    (compatibilidad con planes ya firmados sin la clave; el conjunto
    firmado la exige aguas arriba, en `packages/domain`)."""
    native = _search_ad_group(keywords=[])
    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": _child_plan(native)})


def test_search_con_palabras_clave_validas() -> None:
    validate_child_payload(
        {"child_plan": _child_plan(_search_ad_group(keywords=[_VALID_KEYWORD]))}
    )


def test_search_sin_clave_de_palabras_clave_sigue_validando() -> None:
    validate_child_payload({"child_plan": _child_plan(_search_ad_group())})


def test_display_con_palabras_clave_falla() -> None:
    native = _display_ad_group(keywords=[_VALID_KEYWORD])
    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": _child_plan(native)})


def test_display_valido_sin_cpc_bid() -> None:
    validate_child_payload({"child_plan": _child_plan(_display_ad_group())})


def test_display_valido_con_cpc_bid() -> None:
    native = _display_ad_group(cpc_bid=_VALID_CPC_BID)
    validate_child_payload({"child_plan": _child_plan(native)})


def test_demand_gen_con_cpc_bid_falla() -> None:
    native = _demand_gen_ad_group(cpc_bid=_VALID_CPC_BID)
    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": _child_plan(native)})


def test_demand_gen_con_palabras_clave_falla() -> None:
    native = _demand_gen_ad_group(keywords=[_VALID_KEYWORD])
    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": _child_plan(native)})


def test_demand_gen_valido() -> None:
    validate_child_payload({"child_plan": _child_plan(_demand_gen_ad_group())})


def test_tipo_de_nodo_desconocido_falla() -> None:
    native = _search_ad_group(type="TIKTOK_STANDARD")
    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": _child_plan(native)})
