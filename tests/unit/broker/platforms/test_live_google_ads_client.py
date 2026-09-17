"""`LiveGoogleAdsSearchClient`: solo lo que no toca el SDK real -- extraccion
de campos de una consulta GAQL, aplanado de una fila proto-plus, y el borde
fail-closed cuando `CredentialStorePort` no tiene la cuenta conectada. El
resto (llamada real a `GoogleAdsService.search_stream`) no se prueba aqui,
igual que `GoogleAdsAdapter` sustituye el SDK por un doble
(`test_google_ads_adapter.py`).

`create_paused_campaign` por canal (tipos de campana de
Google, tasks.md T031) SI se prueba contra los tipos reales de protobuf
(`google-ads-python`) con un transporte falso -- mismo patron que
`test_native_campaign_creation.py`: si un nombre de campo estuviera mal,
protobuf lo rechaza en vez de aceptarlo en silencio."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from safent_ads.broker.application.ports import PlatformCredential
from safent_ads.broker.infrastructure.in_memory_credential_store import InMemoryCredentialStore
from safent_ads.broker.platforms import live_google_ads_client
from safent_ads.broker.platforms.errors import CredentialNotConnectedError, GoogleAssetUploadError
from safent_ads.broker.platforms.live_google_ads_client import (
    LiveGoogleAdsSearchClient,
    LiveGoogleAssetUploadClient,
    LiveGoogleKeywordIdeaClient,
    extract_field,
    select_fields,
)
from safent_ads.proposals.domain.campaign_creation import CampaignCreationError
from safent_ads.shared.ids import PlatformCode
from tests.unit.execution.test_campaign_creation_budget import creation_payload


class _EnumLike:
    def __init__(self, name: str) -> None:
        self.name = name


class _Row:
    def __init__(self, status: _EnumLike, resource_name: str) -> None:
        class _Campaign:
            pass

        self.campaign = _Campaign()
        self.campaign.status = status  # type: ignore[attr-defined]
        self.campaign.resource_name = resource_name  # type: ignore[attr-defined]


def test_select_fields_parses_select_clause() -> None:
    query = "SELECT campaign.resource_name, campaign.status FROM campaign WHERE x != 'REMOVED'"

    assert select_fields(query) == ["campaign.resource_name", "campaign.status"]


def test_select_fields_rejects_query_without_select() -> None:
    with pytest.raises(ValueError, match="SELECT"):
        select_fields("UPDATE campaign SET status = 'PAUSED'")


def test_extract_field_unwraps_proto_plus_enum_name() -> None:
    row = _Row(status=_EnumLike("ENABLED"), resource_name="customers/1/campaigns/2")

    assert extract_field(row, "campaign.status") == "ENABLED"
    assert extract_field(row, "campaign.resource_name") == "customers/1/campaigns/2"


def test_search_stream_fails_closed_without_connected_credential() -> None:
    client = LiveGoogleAdsSearchClient(
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106
        credential_store=InMemoryCredentialStore(),
    )

    with pytest.raises(CredentialNotConnectedError):
        next(client.search_stream("1234567890", "SELECT campaign.id FROM campaign"))


def test_search_stream_fails_closed_with_incomplete_credential() -> None:
    """Un refresh token es obligatorio; la MCC es opcional."""
    incomplete = PlatformCredential(
        platform=PlatformCode.GOOGLE,
        external_account_id="1234567890",
        refresh_token=None,
        login_customer_id=None,
    )
    client = LiveGoogleAdsSearchClient(
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106
        credential_store=InMemoryCredentialStore({(PlatformCode.GOOGLE, "1234567890"): incomplete}),
    )

    with pytest.raises(CredentialNotConnectedError):
        next(client.search_stream("1234567890", "SELECT campaign.id FROM campaign"))


def test_sdk_client_uses_cloud_project_access_without_sending_the_retired_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    sdk_client = object()

    def fake_load_from_dict(config: dict[str, object], *, version: str) -> object:
        captured.update(config)
        captured["version"] = version
        return sdk_client

    monkeypatch.setattr(
        live_google_ads_client.GoogleAdsClient, "load_from_dict", fake_load_from_dict
    )
    credential = PlatformCredential(
        platform=PlatformCode.GOOGLE,
        external_account_id="1234567890",
        refresh_token="refresh-token",
        login_customer_id="1112223333",
    )
    client = LiveGoogleAdsSearchClient(
        client_id="client-id.apps.googleusercontent.com",
        client_secret="client-secret",
        credential_store=InMemoryCredentialStore(),
    )

    result = client._build_sdk_client(credential)

    assert result is sdk_client
    assert "developer_token" not in captured
    assert "use_cloud_org_for_api_access" not in captured
    assert captured["version"] == "v25"


def test_direct_account_does_not_require_manager() -> None:
    credential = PlatformCredential(
        platform=PlatformCode.GOOGLE,
        external_account_id="1234567890",
        refresh_token="test-refresh",
        login_customer_id=None,
    )
    client = LiveGoogleAdsSearchClient(
        client_id="test",
        client_secret="test",
        credential_store=InMemoryCredentialStore({(PlatformCode.GOOGLE, "1234567890"): credential}),
    )
    assert client._resolve_credential("1234567890") == credential


def test_build_sdk_client_reuses_the_same_credential_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`build_sdk_client` (composed by `LiveGoogleKeywordIdeaClient`) is
    just the existing `_resolve_credential` + `_build_sdk_client` pair,
    exposed publicly -- never a second copy of the refresh token lookup."""
    sdk_client = object()
    monkeypatch.setattr(
        live_google_ads_client.GoogleAdsClient, "load_from_dict", lambda *_a, **_k: sdk_client
    )
    credential = PlatformCredential(
        platform=PlatformCode.GOOGLE,
        external_account_id="1234567890",
        refresh_token="refresh-token",
        login_customer_id=None,
    )
    client = LiveGoogleAdsSearchClient(
        client_id="client-id",
        client_secret="client-secret",
        credential_store=InMemoryCredentialStore({(PlatformCode.GOOGLE, "1234567890"): credential}),
    )

    assert client.build_sdk_client("1234567890") is sdk_client


def test_build_sdk_client_fails_closed_without_connected_credential() -> None:
    client = LiveGoogleAdsSearchClient(
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106
        credential_store=InMemoryCredentialStore(),
    )

    with pytest.raises(CredentialNotConnectedError):
        client.build_sdk_client("1234567890")


class _FakeKeywordSeed:
    def __init__(self) -> None:
        self.keywords: list[str] = []


class _FakeKeywordIdeasRequest:
    def __init__(self) -> None:
        self.customer_id: str | None = None
        self.language: str | None = None
        self.geo_target_constants: list[str] = []
        self.keyword_plan_network: str | None = None
        self.keyword_seed = _FakeKeywordSeed()


class _FakeCompetition:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeKeywordIdeaMetrics:
    def __init__(self, *, avg_monthly_searches: int | None, competition: str) -> None:
        self.avg_monthly_searches = avg_monthly_searches
        self.competition = _FakeCompetition(competition)
        self._pb = _FakePb(avg_monthly_searches is not None)


class _FakePb:
    def __init__(self, has_avg_monthly_searches: bool) -> None:
        self._has_avg_monthly_searches = has_avg_monthly_searches

    def HasField(self, name: str) -> bool:  # noqa: N802 - mirrors the real protobuf API
        assert name == "avg_monthly_searches"
        return self._has_avg_monthly_searches


class _FakeKeywordIdea:
    def __init__(self, text: str, metrics: _FakeKeywordIdeaMetrics) -> None:
        self.text = text
        self.keyword_idea_metrics = metrics


class _FakeKeywordPlanIdeaService:
    def __init__(self, results: list[_FakeKeywordIdea]) -> None:
        self._results = results
        self.received_request: _FakeKeywordIdeasRequest | None = None

    def generate_keyword_ideas(
        self, *, request: _FakeKeywordIdeasRequest
    ) -> list[_FakeKeywordIdea]:
        self.received_request = request
        return self._results


class _FakeSdkClient:
    def __init__(self, service: _FakeKeywordPlanIdeaService) -> None:
        self._service = service

    def get_type(self, name: str) -> _FakeKeywordIdeasRequest:
        assert name == "GenerateKeywordIdeasRequest"
        return _FakeKeywordIdeasRequest()

    def get_service(self, name: str) -> _FakeKeywordPlanIdeaService:
        assert name == "KeywordPlanIdeaService"
        return self._service


class _FakeSearchClient:
    def __init__(self, sdk_client: _FakeSdkClient) -> None:
        self._sdk_client = sdk_client
        self.requested_customer_id: str | None = None

    def build_sdk_client(self, customer_id: str) -> _FakeSdkClient:
        self.requested_customer_id = customer_id
        return self._sdk_client


def test_generate_keyword_ideas_builds_the_request_and_maps_every_row() -> None:
    results = [
        _FakeKeywordIdea(
            "zapatillas running",
            _FakeKeywordIdeaMetrics(avg_monthly_searches=1000, competition="HIGH"),
        ),
        _FakeKeywordIdea(
            "zapatillas trail",
            _FakeKeywordIdeaMetrics(avg_monthly_searches=None, competition="UNSPECIFIED"),
        ),
    ]
    service = _FakeKeywordPlanIdeaService(results)
    search_client = _FakeSearchClient(_FakeSdkClient(service))
    client = LiveGoogleKeywordIdeaClient(search_client)  # type: ignore[arg-type]

    rows = client.generate_keyword_ideas(
        "1234567890",
        seed_keywords=("zapatillas",),
        geo_target_constant="geoTargetConstants/2724",
        language_constant="languageConstants/1003",
        limit=200,
    )

    assert search_client.requested_customer_id == "1234567890"
    request = service.received_request
    assert request is not None
    assert request.customer_id == "1234567890"
    assert request.language == "languageConstants/1003"
    assert request.geo_target_constants == ["geoTargetConstants/2724"]
    assert request.keyword_plan_network == "GOOGLE_SEARCH"
    assert request.keyword_seed.keywords == ["zapatillas"]
    assert rows == [
        {"text": "zapatillas running", "avg_monthly_searches": 1000, "competition": "HIGH"},
        {"text": "zapatillas trail", "avg_monthly_searches": None, "competition": None},
    ]


def test_generate_keyword_ideas_stops_at_the_requested_limit() -> None:
    results = [
        _FakeKeywordIdea(
            f"idea-{i}", _FakeKeywordIdeaMetrics(avg_monthly_searches=i, competition="LOW")
        )
        for i in range(5)
    ]
    service = _FakeKeywordPlanIdeaService(results)
    search_client = _FakeSearchClient(_FakeSdkClient(service))
    client = LiveGoogleKeywordIdeaClient(search_client)  # type: ignore[arg-type]

    rows = client.generate_keyword_ideas(
        "1234567890",
        seed_keywords=("idea",),
        geo_target_constant="geoTargetConstants/2724",
        language_constant="languageConstants/1003",
        limit=2,
    )

    assert len(rows) == 2
    assert [row["text"] for row in rows] == ["idea-0", "idea-1"]


# ---------------------------------------------------------------------------
# create_paused_campaign por canal (T031) -- protobuf real, transporte falso
# ---------------------------------------------------------------------------

_CUSTOMER_ID = "123"


def _pmax_payload(
    *,
    conversion_goals: tuple[str, ...] = ("customers/123/conversionActions/1",),
    bidding: dict[str, Any] | None = None,
    channel_type: str = "PERFORMANCE_MAX",
    url_expansion_opt_out: bool = True,
    text_asset_automation_enabled: bool = False,
    omit_literal: str | None = None,
) -> dict[str, Any]:
    native: dict[str, Any] = {
        "advertising_channel_type": channel_type,
        "bidding_strategy": bidding
        or {
            "kind": "MAXIMIZE_CONVERSIONS",
            "target_cpa": {"amount": "8.00", "currency": "EUR"},
        },
        "contains_eu_political_advertising": ("DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"),
        "conversion_goals": [{"resource_name": name} for name in conversion_goals],
        "url_expansion_opt_out": url_expansion_opt_out,
        "text_asset_automation_enabled": text_asset_automation_enabled,
    }
    if omit_literal is not None:
        del native[omit_literal]
    return {
        "creation_plan": {
            "schema_version": 1,
            "platform": "google",
            "name": "Maximo rendimiento",
            "status": "PAUSED",
            "daily_budget": {"amount": "20.00", "currency": "EUR"},
            "native": native,
        }
    }


class _EnumField:
    def __init__(self, name: str) -> None:
        self.name = name


def _conversion_action_row(
    resource_name: str,
    *,
    status: str = "ENABLED",
    category: str = "PURCHASE",
    origin: str = "WEBSITE",
) -> SimpleNamespace:
    return SimpleNamespace(
        conversion_action=SimpleNamespace(
            resource_name=resource_name,
            status=_EnumField(status),
            category=_EnumField(category),
            origin=_EnumField(origin),
        )
    )


_GOAL_ROWS = [_conversion_action_row("customers/123/conversionActions/1")]


def _pmax_client(
    monkeypatch: pytest.MonkeyPatch, *, mutate: Any, rows: list[SimpleNamespace]
) -> tuple[LiveGoogleAdsSearchClient, Any]:
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)

    def search_stream(*, customer_id: str, query: str) -> list[SimpleNamespace]:  # noqa: ARG001
        return [SimpleNamespace(results=rows)]

    monkeypatch.setattr(
        sdk, "get_service", lambda _: SimpleNamespace(mutate=mutate, search_stream=search_stream)
    )
    client = LiveGoogleAdsSearchClient(
        client_id="test", client_secret="test", credential_store=InMemoryCredentialStore()
    )
    monkeypatch.setattr(client, "_resolve_credential", lambda _: None)
    monkeypatch.setattr(client, "_build_sdk_client", lambda _: sdk)
    return client, sdk


def _mutate_ok(sdk: Any, *, conversion_goal_resource: str | None) -> Any:
    calls: list[dict[str, Any]] = []

    def mutate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        operations = kwargs["mutate_operations"]
        budget_op, campaign_op, *rest = operations
        budget = budget_op.campaign_budget_operation.create
        campaign = campaign_op.campaign_operation.create
        response = sdk.get_type("MutateGoogleAdsResponse")
        b = sdk.get_type("MutateOperationResponse")
        b.campaign_budget_result.resource_name = "customers/123/campaignBudgets/456"
        b.campaign_budget_result.campaign_budget = budget
        c = sdk.get_type("MutateOperationResponse")
        c.campaign_result.resource_name = "customers/123/campaigns/789"
        c.campaign_result.campaign = campaign
        c.campaign_result.campaign.campaign_budget = b.campaign_budget_result.resource_name
        results = [b, c]
        if conversion_goal_resource is not None:
            (goal_op,) = rest
            g = sdk.get_type("MutateOperationResponse")
            g.campaign_conversion_goal_result.resource_name = conversion_goal_resource
            results.append(g)
        response.mutate_operation_responses.extend(results)
        return response

    return mutate, calls


def test_pmax_no_manda_network_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    goal_resource = "customers/123/campaignConversionGoals/789~PURCHASE~WEBSITE"

    def build(sdk: Any) -> Any:
        return _mutate_ok(sdk, conversion_goal_resource=goal_resource)

    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    mutate, calls = build(sdk)
    client, sdk = _pmax_client(
        monkeypatch, mutate=mutate, rows=_GOAL_ROWS
    )

    client.create_paused_campaign(_CUSTOMER_ID, _pmax_payload())

    campaign = calls[0]["mutate_operations"][1].campaign_operation.create
    assert not campaign._pb.HasField("network_settings")


def test_pmax_manda_url_expansion_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    goal_resource = "customers/123/campaignConversionGoals/789~PURCHASE~WEBSITE"
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    mutate, calls = _mutate_ok(sdk, conversion_goal_resource=goal_resource)
    client, sdk = _pmax_client(
        monkeypatch, mutate=mutate, rows=_GOAL_ROWS
    )

    client.create_paused_campaign(_CUSTOMER_ID, _pmax_payload())

    campaign = calls[0]["mutate_operations"][1].campaign_operation.create
    automation = {
        (setting.asset_automation_type.name, setting.asset_automation_status.name)
        for setting in campaign.asset_automation_settings
    }
    assert ("FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION", "OPTED_OUT") in automation
    assert ("TEXT_ASSET_AUTOMATION", "OPTED_OUT") in automation


def test_demand_gen_manda_automatizacion_apagada(monkeypatch: pytest.MonkeyPatch) -> None:
    """BL-1 (T-1/T-2): la misma tabla de literales forzados (`_AUTOMATION_OFF`
    de `GoogleChannelSpec`) rige para Demand Gen, no solo Maximo Rendimiento
    -- `_apply_forced_literals` no distingue canal, lee el nativo firmado."""
    goal_resource = "customers/123/campaignConversionGoals/789~PURCHASE~WEBSITE"
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    mutate, calls = _mutate_ok(sdk, conversion_goal_resource=goal_resource)
    client, sdk = _pmax_client(monkeypatch, mutate=mutate, rows=_GOAL_ROWS)

    client.create_paused_campaign(_CUSTOMER_ID, _pmax_payload(channel_type="DEMAND_GEN"))

    campaign = calls[0]["mutate_operations"][1].campaign_operation.create
    assert campaign.advertising_channel_type.name == "DEMAND_GEN"
    automation = {
        (setting.asset_automation_type.name, setting.asset_automation_status.name)
        for setting in campaign.asset_automation_settings
    }
    assert ("FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION", "OPTED_OUT") in automation
    assert ("TEXT_ASSET_AUTOMATION", "OPTED_OUT") in automation
    assert not campaign._pb.HasField("network_settings")


@pytest.mark.parametrize(
    "overrides",
    [
        {"url_expansion_opt_out": False},
        {"text_asset_automation_enabled": True},
        {"omit_literal": "url_expansion_opt_out"},
    ],
)
def test_literal_forzado_manipulado_deniega_sin_mutar(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    """BL-1 (threat-model.md T-1/T-2): `creation_budget` -- la primera
    accion de `create_paused_campaign`, no una comprobacion posterior --
    relee el nativo firmado contra `GoogleChannelSpec.forced_literals` en
    CADA intento de mutacion, no solo al proponer. Un literal forzado que se
    aparta de lo firmado (manipulado o ausente) deniega antes de construir
    ninguna operacion: el doble del SDK ve cero llamadas a `mutate`, y el
    valor NUNCA se corrige en silencio a lo esperado."""

    def forbidden(**_kwargs: Any) -> Any:
        raise AssertionError("must deny before building any mutate operation")

    client, _sdk = _pmax_client(monkeypatch, mutate=forbidden, rows=_GOAL_ROWS)
    payload = _pmax_payload(**overrides)

    with pytest.raises(CampaignCreationError, match="campaign_creation_native_invalid"):
        client.create_paused_campaign(_CUSTOMER_ID, payload)


def test_confirmacion_compara_puja_y_metas(monkeypatch: pytest.MonkeyPatch) -> None:
    goal_resource = "customers/123/campaignConversionGoals/789~PURCHASE~WEBSITE"
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    mutate, calls = _mutate_ok(sdk, conversion_goal_resource=goal_resource)
    client, sdk = _pmax_client(
        monkeypatch, mutate=mutate, rows=_GOAL_ROWS
    )

    result = client.create_paused_campaign(_CUSTOMER_ID, _pmax_payload())

    assert result["campaign_resource"] == "customers/123/campaigns/789"
    campaign = calls[0]["mutate_operations"][1].campaign_operation.create
    assert campaign.maximize_conversions.target_cpa_micros == 8_000_000
    goal_op = calls[0]["mutate_operations"][2]
    assert goal_op.campaign_conversion_goal_operation.update.biddable is True
    assert (
        goal_op.campaign_conversion_goal_operation.update.resource_name
        == "customers/123/campaignConversionGoals/-2~PURCHASE~WEBSITE"
    )


def test_una_puja_distinta_de_la_firmada_no_confirma(monkeypatch: pytest.MonkeyPatch) -> None:
    """T-5: si la lectura de vuelta trae otra estrategia de puja (Google
    cambio el `oneof`), el paso no confirma -- nunca `SUCCEEDED`."""
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)

    def mutate(**kwargs: Any) -> Any:
        operations = kwargs["mutate_operations"]
        budget_op, campaign_op, goal_op = operations
        budget = budget_op.campaign_budget_operation.create
        signed_campaign = campaign_op.campaign_operation.create
        drifted_campaign = sdk.get_type("Campaign")
        drifted_campaign.name = signed_campaign.name
        drifted_campaign.status = signed_campaign.status
        drifted_campaign.advertising_channel_type = signed_campaign.advertising_channel_type
        drifted_campaign.contains_eu_political_advertising = (
            signed_campaign.contains_eu_political_advertising
        )
        drifted_campaign.campaign_budget = budget.resource_name
        drifted_campaign.asset_automation_settings.extend(
            signed_campaign.asset_automation_settings
        )
        # Google, no el dueño, cambio la estrategia: manual_cpc en vez de
        # la puja por conversiones firmada.
        sdk.copy_from(drifted_campaign.manual_cpc, sdk.get_type("ManualCpc"))
        response = sdk.get_type("MutateGoogleAdsResponse")
        b = sdk.get_type("MutateOperationResponse")
        b.campaign_budget_result.resource_name = "customers/123/campaignBudgets/456"
        b.campaign_budget_result.campaign_budget = budget
        c = sdk.get_type("MutateOperationResponse")
        c.campaign_result.resource_name = "customers/123/campaigns/789"
        c.campaign_result.campaign = drifted_campaign
        c.campaign_result.campaign.campaign_budget = b.campaign_budget_result.resource_name
        g = sdk.get_type("MutateOperationResponse")
        g.campaign_conversion_goal_result.resource_name = (
            "customers/123/campaignConversionGoals/789~PURCHASE~WEBSITE"
        )
        response.mutate_operation_responses.extend([b, c, g])
        return response

    client, sdk = _pmax_client(
        monkeypatch, mutate=mutate, rows=_GOAL_ROWS
    )

    with pytest.raises(ValueError, match="confirmation_mismatch"):
        client.create_paused_campaign(_CUSTOMER_ID, _pmax_payload())


def test_una_puja_con_objetivo_distinto_del_firmado_no_confirma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-5/AL-2: Google devuelve el MISMO `oneof` (`maximize_conversions`)
    pero con un `target_cpa_micros` distinto del firmado -- la confirmacion
    no puede limitarse a comparar el nombre del campo del `oneof`, tiene que
    comparar tambien el objetivo. Ningun paso confirma con un precio que el
    dueño no aprobo."""
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)

    def mutate(**kwargs: Any) -> Any:
        operations = kwargs["mutate_operations"]
        budget_op, campaign_op, goal_op = operations
        budget = budget_op.campaign_budget_operation.create
        signed_campaign = campaign_op.campaign_operation.create
        drifted_campaign = sdk.get_type("Campaign")
        drifted_campaign.name = signed_campaign.name
        drifted_campaign.status = signed_campaign.status
        drifted_campaign.advertising_channel_type = signed_campaign.advertising_channel_type
        drifted_campaign.contains_eu_political_advertising = (
            signed_campaign.contains_eu_political_advertising
        )
        drifted_campaign.campaign_budget = budget.resource_name
        drifted_campaign.asset_automation_settings.extend(
            signed_campaign.asset_automation_settings
        )
        # Mismo oneof que el firmado, pero con otro target_cpa: Google
        # ajusto el objetivo al aceptar la campaña sin que nadie lo pidiera.
        sdk.copy_from(drifted_campaign.maximize_conversions, sdk.get_type("MaximizeConversions"))
        drifted_campaign.maximize_conversions.target_cpa_micros = (
            signed_campaign.maximize_conversions.target_cpa_micros + 1_000_000
        )
        response = sdk.get_type("MutateGoogleAdsResponse")
        b = sdk.get_type("MutateOperationResponse")
        b.campaign_budget_result.resource_name = "customers/123/campaignBudgets/456"
        b.campaign_budget_result.campaign_budget = budget
        c = sdk.get_type("MutateOperationResponse")
        c.campaign_result.resource_name = "customers/123/campaigns/789"
        c.campaign_result.campaign = drifted_campaign
        c.campaign_result.campaign.campaign_budget = b.campaign_budget_result.resource_name
        g = sdk.get_type("MutateOperationResponse")
        g.campaign_conversion_goal_result.resource_name = (
            "customers/123/campaignConversionGoals/789~PURCHASE~WEBSITE"
        )
        response.mutate_operation_responses.extend([b, c, g])
        return response

    client, sdk = _pmax_client(monkeypatch, mutate=mutate, rows=_GOAL_ROWS)

    with pytest.raises(ValueError, match="confirmation_mismatch"):
        client.create_paused_campaign(_CUSTOMER_ID, _pmax_payload())


def test_search_manda_exactamente_las_mismas_operaciones_que_hoy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regresion (threat-model.md T-8, casilla 3): Busqueda no gana ni una
    operacion nueva -- sin `campaign_conversion_goal`, sin
    `asset_automation_settings`."""
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    calls: list[dict[str, Any]] = []

    def mutate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        operations = kwargs["mutate_operations"]
        budget_op, campaign_op = operations
        budget = budget_op.campaign_budget_operation.create
        campaign = campaign_op.campaign_operation.create
        response = sdk.get_type("MutateGoogleAdsResponse")
        b = sdk.get_type("MutateOperationResponse")
        b.campaign_budget_result.resource_name = "customers/123/campaignBudgets/456"
        b.campaign_budget_result.campaign_budget = budget
        c = sdk.get_type("MutateOperationResponse")
        c.campaign_result.resource_name = "customers/123/campaigns/789"
        c.campaign_result.campaign = campaign
        c.campaign_result.campaign.campaign_budget = b.campaign_budget_result.resource_name
        response.mutate_operation_responses.extend([b, c])
        return response

    client, sdk = _pmax_client(monkeypatch, mutate=mutate, rows=[])

    result = client.create_paused_campaign(_CUSTOMER_ID, creation_payload())

    assert len(calls[0]["mutate_operations"]) == 2
    assert result["campaign_resource"] == "customers/123/campaigns/789"
    campaign = calls[0]["mutate_operations"][1].campaign_operation.create
    assert not campaign.asset_automation_settings
    assert campaign._pb.HasField("manual_cpc")


# ---------------------------------------------------------------------------
# LiveGoogleAssetUploadClient.mutate_image_asset (residual a04cf9f,
# threat-model.md #17) -- protobuf real, transporte falso.
# ---------------------------------------------------------------------------


def _asset_upload_client(
    monkeypatch: pytest.MonkeyPatch, *, mutate_assets: Any
) -> tuple[LiveGoogleAssetUploadClient, Any]:
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    monkeypatch.setattr(sdk, "get_service", lambda _: SimpleNamespace(mutate_assets=mutate_assets))
    search_client = SimpleNamespace(build_sdk_client=lambda customer_id: sdk)  # noqa: ARG005
    client = LiveGoogleAssetUploadClient(search_client)  # type: ignore[arg-type]
    return client, sdk


def _mutate_assets_ok(
    sdk: Any, resource_name: str, calls: list[dict[str, Any]]
) -> Any:
    def mutate_assets(**kwargs: Any) -> Any:
        calls.append(kwargs)
        response = sdk.get_type("MutateAssetsResponse")
        result = sdk.get_type("MutateAssetResult")
        result.resource_name = resource_name
        response.results.append(result)
        return response

    return mutate_assets


def test_mutate_image_asset_creates_the_asset_and_returns_its_resource_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_sdk = GoogleAdsClient(
        credentials=AnonymousCredentials(), version="v25", use_proto_plus=True
    )
    calls: list[dict[str, Any]] = []
    client, _sdk = _asset_upload_client(
        monkeypatch,
        mutate_assets=_mutate_assets_ok(response_sdk, "customers/123/assets/456", calls),
    )

    resource_name = client.mutate_image_asset(
        "123",
        name="checksum-abc",
        data=b"raw-bytes",
        mime_type="image/png",
        width=200,
        height=100,
    )

    assert resource_name == "customers/123/assets/456"
    (kwargs,) = calls
    assert kwargs["customer_id"] == "123"
    (operation,) = kwargs["operations"]
    asset = operation.create
    assert asset.name == "checksum-abc"
    assert asset.image_asset.data == b"raw-bytes"
    assert asset.image_asset.mime_type.name == "IMAGE_PNG"
    assert asset.image_asset.full_size.width_pixels == 200
    assert asset.image_asset.full_size.height_pixels == 100


def test_mutate_image_asset_maps_jpeg_to_the_sdk_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    response_sdk = GoogleAdsClient(
        credentials=AnonymousCredentials(), version="v25", use_proto_plus=True
    )
    calls: list[dict[str, Any]] = []
    client, _sdk = _asset_upload_client(
        monkeypatch,
        mutate_assets=_mutate_assets_ok(response_sdk, "customers/123/assets/789", calls),
    )

    client.mutate_image_asset(
        "123", name="checksum-jpeg", data=b"x", mime_type="image/jpeg", width=10, height=10
    )

    (kwargs,) = calls
    (operation,) = kwargs["operations"]
    assert operation.create.image_asset.mime_type.name == "IMAGE_JPEG"


def test_mutate_image_asset_translates_a_provider_error_without_leaking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutate_assets(**_kwargs: Any) -> Any:
        raise RuntimeError("refresh_token=super-secret-token rejected upstream")

    client, _sdk = _asset_upload_client(monkeypatch, mutate_assets=mutate_assets)

    with pytest.raises(GoogleAssetUploadError) as excinfo:
        client.mutate_image_asset(
            "123", name="checksum-abc", data=b"x", mime_type="image/jpeg", width=10, height=10
        )

    assert "super-secret-token" not in str(excinfo.value)
