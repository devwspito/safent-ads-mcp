"""`google_create` para PERFORMANCE_MAX (tipos de campana de
Google, tasks.md T034): el grupo de recursos entero en UNA mutacion, con
protobuf real y transporte falso -- mismo patron que
`test_native_campaign_creation.py`."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from safent_ads.broker.platforms.native_ad_child import google_create

_PARENT = "customers/123/campaigns/456"


class _EnumField:
    def __init__(self, name: str) -> None:
        self.name = name


_SIGNED_NAME = "Grupo de recursos Reservas"


def _asset_group_plan(
    *,
    name: str | None = _SIGNED_NAME,
    headlines: tuple[str, ...] = ("H1", "H2", "H3"),
    long_headlines: tuple[str, ...] = ("LH1",),
    descriptions: tuple[str, ...] = ("D1", "D2"),
    business_name: str = "Negocio",
    logo: str = "customers/123/assets/900",
    marketing_image: str = "customers/123/assets/901",
    square_image: str = "customers/123/assets/902",
) -> dict[str, Any]:
    assets: dict[str, Any] = {
        "headlines": list(headlines),
        "long_headlines": list(long_headlines),
        "descriptions": list(descriptions),
        "business_name": business_name,
        "logo": logo,
        "marketing_image": marketing_image,
        "square_image": square_image,
    }
    if name is not None:
        assets["name"] = name
    return {
        "schema_version": 1,
        "platform": "google",
        "kind": "ad_set",
        "status": "PAUSED",
        "native": {
            "kind": "ASSET_GROUP",
            "final_url": "https://example.com/landing",
            "assets": assets,
        },
    }


_EXPECTED_LINKS = {
    ("customers/123/assets/500", "HEADLINE"),
    ("customers/123/assets/501", "HEADLINE"),
    ("customers/123/assets/502", "HEADLINE"),
    ("customers/123/assets/503", "LONG_HEADLINE"),
    ("customers/123/assets/504", "DESCRIPTION"),
    ("customers/123/assets/505", "DESCRIPTION"),
    ("customers/123/assets/506", "BUSINESS_NAME"),
    ("customers/123/assets/900", "LOGO"),
    ("customers/123/assets/901", "MARKETING_IMAGE"),
    ("customers/123/assets/902", "SQUARE_MARKETING_IMAGE"),
}


def _mutate_ok(sdk: Any) -> tuple[Any, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def mutate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        operations = kwargs["mutate_operations"]
        response = sdk.get_type("MutateGoogleAdsResponse")
        results = []
        r0 = sdk.get_type("MutateOperationResponse")
        r0.asset_group_result.resource_name = "customers/123/assetGroups/999"
        results.append(r0)
        text_count = 7
        for i in range(text_count):
            r = sdk.get_type("MutateOperationResponse")
            r.asset_result.resource_name = f"customers/123/assets/{500 + i}"
            results.append(r)
        link_count = len(operations) - 1 - text_count
        for i in range(link_count):
            r = sdk.get_type("MutateOperationResponse")
            r.asset_group_asset_result.resource_name = f"customers/123/assetGroupAssets/999~{i}~2"
            results.append(r)
        response.mutate_operation_responses.extend(results)
        return response

    return mutate, calls


def _rows_for(links: set[tuple[str, str]]) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            asset_group_asset=SimpleNamespace(asset=asset, field_type=_EnumField(field_type))
        )
        for asset, field_type in links
    ]


def _sdk_with(monkeypatch: pytest.MonkeyPatch, *, mutate: Any, links: set[tuple[str, str]]) -> Any:
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)

    def search_stream(*, customer_id: str, query: str) -> list[SimpleNamespace]:  # noqa: ARG001
        return [SimpleNamespace(results=_rows_for(links))]

    monkeypatch.setattr(
        sdk, "get_service", lambda _: SimpleNamespace(mutate=mutate, search_stream=search_stream)
    )
    return sdk


def test_una_sola_mutacion_con_temporales(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    mutate, calls = _mutate_ok(sdk)
    sdk = _sdk_with(monkeypatch, mutate=mutate, links=_EXPECTED_LINKS)

    result = google_create(sdk, _PARENT, _asset_group_plan())

    assert result == {
        "child_resource": "customers/123/assetGroups/999",
        "parent_resource": _PARENT,
        "status": "PAUSED",
    }
    assert len(calls) == 1
    operations = calls[0]["mutate_operations"]
    assert len(operations) == 1 + 7 + 10
    asset_group = operations[0].asset_group_operation.create
    assert asset_group.resource_name == "customers/123/assetGroups/-1"
    assert asset_group.campaign == _PARENT
    assert list(asset_group.final_urls) == ["https://example.com/landing"]
    assert asset_group.name == _SIGNED_NAME
    text_resource_names = [op.asset_operation.create.resource_name for op in operations[1:8]]
    assert text_resource_names == [f"customers/123/assets/-{n}" for n in range(2, 9)]


def test_partial_failure_sigue_en_false(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    mutate, calls = _mutate_ok(sdk)
    sdk = _sdk_with(monkeypatch, mutate=mutate, links=_EXPECTED_LINKS)

    google_create(sdk, _PARENT, _asset_group_plan())

    assert calls[0]["partial_failure"] is False
    assert calls[0]["response_content_type"] == "MUTABLE_RESOURCE"
    assert calls[0]["retry"] is None


def test_supera_el_tope_de_operaciones_deniega_sin_escribir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-1/AL-6: un plan hostil con mas titulares de los que el dominio
    admitiria nunca llega a `GoogleAdsService.mutate` -- defensa en
    profundidad, aunque la validacion de dominio ya lo hubiera rechazado
    antes de firmar."""
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    calls: list[dict[str, Any]] = []

    def forbidden_mutate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise AssertionError("must not reach the SDK")

    monkeypatch.setattr(sdk, "get_service", lambda _: SimpleNamespace(mutate=forbidden_mutate))
    oversized_plan = _asset_group_plan(headlines=tuple(f"H{n}" for n in range(30)))

    with pytest.raises(ValueError, match="asset_group_operation_limit"):
        google_create(sdk, _PARENT, oversized_plan)

    assert calls == []


def test_falta_el_nombre_firmado_deniega_sin_escribir(monkeypatch: pytest.MonkeyPatch) -> None:
    """El nombre que el dueño aprueba (`assets["name"]`,
    `platform_completeness._google_asset_group_wire`) nunca se sustituye
    por uno derivado aqui -- si no llega, se falla cerrado antes de tocar
    el SDK, en vez de inventar un nombre distinto al firmado."""
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    calls: list[dict[str, Any]] = []

    def forbidden_mutate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise AssertionError("must not reach the SDK")

    monkeypatch.setattr(sdk, "get_service", lambda _: SimpleNamespace(mutate=forbidden_mutate))

    with pytest.raises(ValueError, match="asset_group_name_missing"):
        google_create(sdk, _PARENT, _asset_group_plan(name=None))

    assert calls == []


def test_confirmacion_exige_el_conjunto_exacto_de_recursos(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    mutate, _calls = _mutate_ok(sdk)
    extra_link = {("customers/123/assets/999", "HEADLINE")}
    sdk = _sdk_with(monkeypatch, mutate=mutate, links=_EXPECTED_LINKS | extra_link)

    with pytest.raises(ValueError, match="asset_group_confirmation_mismatch"):
        google_create(sdk, _PARENT, _asset_group_plan())


def test_field_type_cruzado_en_la_lectura_de_vuelta_falla_el_paso(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El logo enlazado como imagen de marketing (mismos bytes aprobados,
    papel cruzado) no confirma."""
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    mutate, _calls = _mutate_ok(sdk)
    swapped = (_EXPECTED_LINKS - {("customers/123/assets/900", "LOGO")}) | {
        ("customers/123/assets/900", "MARKETING_IMAGE")
    }
    sdk = _sdk_with(monkeypatch, mutate=mutate, links=swapped)

    with pytest.raises(ValueError, match="asset_group_confirmation_mismatch"):
        google_create(sdk, _PARENT, _asset_group_plan())
