"""`LiveGoogleAdsSearchClient`: unica implementacion de produccion de
`GoogleAdsSearchClient` (`google_ads_adapter.py`) sobre `google-ads-python`.
Resuelve la credencial de CLIENTE (refresh token + `login_customer_id` de
acceso) por `customer_id` contra `CredentialStorePort`
(`broker/application/ports.py`) en cada llamada -- nunca una unica
credencial estatica para todas las cuentas conectadas de Google, y nunca
leida de `BrokerSettings`/entorno (esa capa solo guarda las credenciales
OAuth de VENDOR, `client_id`/`client_secret`, comunes a
toda cuenta). Sin credencial conectada, falla cerrado con
`CredentialNotConnectedError` -- `GoogleAdsAdapter._run_gaql` la redacta y
la propaga como `GoogleAdsAdapterError`, que el dispatcher del broker
convierte en `{"ok": false, "error_code": "FAILED"}` (nunca inventa una
respuesta vacia)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

from google.ads.googleads.client import GoogleAdsClient
from google.api_core import protobuf_helpers

from safent_ads.broker.application.ports import CredentialStorePort, PlatformCredential
from safent_ads.broker.platforms.error_sanitizer import redact_sdk_error
from safent_ads.broker.platforms.errors import CredentialNotConnectedError, GoogleAssetUploadError
from safent_ads.broker.platforms.google_conversion_goal_reader import (
    VerifiedConversionGoal,
    verify_conversion_goals,
)
from safent_ads.broker.platforms.native_ad_child import google_create, google_prepare
from safent_ads.broker.platforms.native_google_targeting import (
    confirm_geography,
    geography_operations,
)
from safent_ads.proposals.domain.campaign_creation import creation_budget
from safent_ads.proposals.domain.google_channel_spec import GoogleBiddingStrategy
from safent_ads.shared.ids import EntityLevel, PlatformCode

_GOOGLE_ADS_API_VERSION: Final = "v25"
_SELECT_FIELDS_PATTERN: Final = re.compile(r"^SELECT\s+(.*?)\s+FROM\s", re.IGNORECASE | re.DOTALL)

_SERVICE_AND_OPERATION_BY_LEVEL: Final[dict[EntityLevel, tuple[str, str, str]]] = {
    EntityLevel.CAMPAIGN: ("CampaignService", "CampaignOperation", "mutate_campaigns"),
    EntityLevel.AD_SET: ("AdGroupService", "AdGroupOperation", "mutate_ad_groups"),
    EntityLevel.AD: ("AdGroupAdService", "AdGroupAdOperation", "mutate_ad_group_ads"),
}
# Coincidencia deliberada con `KeywordMatchTypeEnum.BROAD`: una negativa
# amplia bloquea mas trafico que EXACT/PHRASE para el mismo termino, que es
# lo que se espera de una accion defensiva (add_negative_keyword).
_NEGATIVE_KEYWORD_MATCH_TYPE: Final = "BROAD"
# `get_google_keyword_ideas` (R4): mismo criterio de "solo red de Busqueda"
# que el plan de creacion por defecto (`campaign_draft.py::
# _GOOGLE_SEARCH_ONLY_NETWORKS`) -- nunca Display/Partners sin que el
# llamante lo pida.
_KEYWORD_PLAN_NETWORK: Final = "GOOGLE_SEARCH"
_UNSET_COMPETITION_LEVELS: Final = frozenset({"UNSPECIFIED", "UNKNOWN"})

# Puja como `oneof` (tasks.md T031; threat-model.md T-5): el campo del
# `oneof` que corresponde a cada `kind` firmado -- nunca `manual_cpc` a
# ciegas. `ManualCpc` no lleva objetivo; los demas si. Claves por el enum
# de `google_channel_spec.py` (INV-15): ningun literal de puja fuera de
# esa tabla, ni siquiera como clave de un diccionario de este fichero.
_BIDDING_FIELD_BY_KIND: Final[Mapping[GoogleBiddingStrategy, str]] = {
    GoogleBiddingStrategy.MANUAL_CPC: "manual_cpc",
    GoogleBiddingStrategy.MAXIMIZE_CLICKS: "maximize_clicks",
    GoogleBiddingStrategy.MAXIMIZE_CONVERSIONS: "maximize_conversions",
    GoogleBiddingStrategy.MAXIMIZE_CONVERSION_VALUE: "maximize_conversion_value",
}
_BIDDING_TYPE_BY_FIELD: Final[Mapping[str, str]] = {
    "manual_cpc": "ManualCpc",
    "maximize_clicks": "MaximizeClicks",
    "maximize_conversions": "MaximizeConversions",
    "maximize_conversion_value": "MaximizeConversionValue",
}

# BL-1 capa 4 (threat-model.md T-1/T-2): los literales forzados del plan
# FIRMADO se reafirman aqui, nunca desde una constante local. En la API
# v25 el campo booleano independiente `url_expansion_opt_out` de versiones
# anteriores del SDK ya no existe -- Google unifico la expansion de URL
# dentro de `asset_automation_settings`
# (`AssetAutomationType.FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION`: "there
# is no way to opt out of text asset automation and still use final URL
# expansion"). Los dos literales del plan viven en la MISMA lista.
_ASSET_AUTOMATION_TYPE_BY_FORCED_LITERAL: Final[Mapping[str, str]] = {
    "url_expansion_opt_out": "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION",
    "text_asset_automation_enabled": "TEXT_ASSET_AUTOMATION",
}
_OPTED_OUT: Final = "OPTED_OUT"

# `GoogleAssetUploadClient.mutate_image_asset` (google_asset_upload.py): el
# unico par que `ALLOWED_IMAGE_MIME_TYPES` deja pasar, mapeado al nombre del
# enum `MimeTypeEnum.MimeType` de la API (v25) -- nunca un literal fuera de
# esa lista blanca llega hasta aqui (el adaptador ya lo rechazo antes).
_IMAGE_ASSET_MIME_TYPE_BY_VALUE: Final[Mapping[str, str]] = {
    "image/jpeg": "IMAGE_JPEG",
    "image/png": "IMAGE_PNG",
}


def _money_micros(value: Mapping[str, Any]) -> int:
    return int(Decimal(str(value["amount"])) * 1_000_000)


def _apply_bidding(client: GoogleAdsClient, campaign: Any, bidding: object) -> str:
    """Builds the `oneof` from the SIGNED plan (threat-model.md T-5:
    `create_paused_campaign` stopped fixing `manual_cpc` unconditionally).
    Returns the field name that was set, so confirmation can compare
    `WhichOneof` against it instead of a single hard-coded field."""
    kind, cpc_bid_ceiling, target_cpa, target_roas = _bidding_shape(bidding)
    field = _BIDDING_FIELD_BY_KIND[kind]
    client.copy_from(getattr(campaign, field), client.get_type(_BIDDING_TYPE_BY_FIELD[field]))
    if field == "maximize_clicks" and cpc_bid_ceiling is not None:
        campaign.maximize_clicks.cpc_bid_ceiling_micros = _money_micros(cpc_bid_ceiling)
    if field == "maximize_conversions" and target_cpa is not None:
        campaign.maximize_conversions.target_cpa_micros = _money_micros(target_cpa)
    if field == "maximize_conversion_value" and target_roas is not None:
        campaign.maximize_conversion_value.target_roas = float(target_roas)
    return field


def _bidding_shape(
    bidding: object,
) -> tuple[GoogleBiddingStrategy, Mapping[str, Any] | None, Mapping[str, Any] | None, str | None]:
    """`campaign_creation._google` already validated this exact shape
    before the plan was ever signed -- this reads it, it does not
    re-validate it (the broker trusts its own signed diff). The bare
    string form is legacy compatibility for SEARCH only (already stored
    `creation_plan`s, `campaign_creation._resolve_bidding`)."""
    if isinstance(bidding, str):
        return GoogleBiddingStrategy(bidding), None, None, None
    assert isinstance(bidding, Mapping)  # noqa: S101 - internal invariant, signed plan
    kind = GoogleBiddingStrategy(bidding["kind"])
    return kind, bidding.get("cpc_bid_ceiling"), bidding.get("target_cpa"), bidding.get(
        "target_roas"
    )


def _apply_forced_literals(
    client: GoogleAdsClient, campaign: Any, native: Mapping[str, Any]
) -> None:
    for literal_key, automation_type in _ASSET_AUTOMATION_TYPE_BY_FORCED_LITERAL.items():
        if literal_key not in native:
            continue
        setting = client.get_type("Campaign.AssetAutomationSetting")
        setting.asset_automation_type = automation_type
        setting.asset_automation_status = _OPTED_OUT
        campaign.asset_automation_settings.append(setting)


def _distinct_categories_and_origins(
    goals: Sequence[VerifiedConversionGoal],
) -> tuple[tuple[str, str], ...]:
    """Two signed conversion actions sharing a (category, origin) pair
    collapse to one `campaign_conversion_goal`: that resource is keyed by
    category+origin, not by an individual conversion action."""
    seen: dict[tuple[str, str], None] = {}
    for goal in goals:
        seen.setdefault((goal.category, goal.origin), None)
    return tuple(seen)


def _campaign_conversion_goal_resource_name(
    customer_id: str, campaign_id: str, category: str, origin: str
) -> str:
    return f"customers/{customer_id}/campaignConversionGoals/{campaign_id}~{category}~{origin}"


def _conversion_goal_operations(
    client: GoogleAdsClient,
    customer_id: str,
    temporary_campaign_id: str,
    goals: Sequence[VerifiedConversionGoal],
) -> list[Any]:
    """One `campaign_conversion_goal` update per distinct (category,
    origin) pair (data-model.md `ConversionGoal`). `CampaignConversionGoal`
    only supports `update` -- the resource always exists implicitly per
    account, this just opts the campaign's category/origin into bidding."""
    operations: list[Any] = []
    for category, origin in _distinct_categories_and_origins(goals):
        operation = client.get_type("MutateOperation")
        target = operation.campaign_conversion_goal_operation
        target.update.resource_name = _campaign_conversion_goal_resource_name(
            customer_id, temporary_campaign_id, category, origin
        )
        target.update.biddable = True
        mask = protobuf_helpers.field_mask(None, target.update._pb)  # type: ignore[no-untyped-call]
        client.copy_from(target.update_mask, mask)
        operations.append(operation)
    return operations


def _network_settings_mismatch(
    network_settings: Mapping[str, Any] | None, actual_campaign: Any, campaign: Any
) -> bool:
    """`network_settings` is `FORBIDDEN` for DISPLAY/DEMAND_GEN/PERFORMANCE_MAX
    (`GoogleChannelSpec`): there is nothing signed to compare against for
    those rows, so the confirmation is skipped rather than asserting a
    zero-value default Google may or may not echo back."""
    if network_settings is None:
        return False
    return bool(actual_campaign.network_settings != campaign.network_settings)


def _confirm_conversion_goals(
    results: Sequence[Any],
    customer_id: str,
    campaign_id: str,
    goals: Sequence[VerifiedConversionGoal],
) -> None:
    expected = {
        _campaign_conversion_goal_resource_name(customer_id, campaign_id, category, origin)
        for category, origin in _distinct_categories_and_origins(goals)
    }
    actual = {str(response.campaign_conversion_goal_result.resource_name) for response in results}
    if actual != expected:
        raise ValueError("campaign_creation_confirmation_mismatch")


def select_fields(query: str) -> Sequence[str]:
    """Extrae la lista de campos de una consulta GAQL bien formada
    (`google_ads_adapter._build_select`/`_build_metrics_query`): la unica
    forma que este cliente necesita entender, no un parser GAQL general."""
    match = _SELECT_FIELDS_PATTERN.match(query.strip())
    if match is None:
        raise ValueError(f"consulta GAQL sin clausula SELECT reconocible: {query!r}")
    return [field.strip() for field in match.group(1).split(",")]


def extract_field(row: Any, dotted_path: str) -> Any:  # noqa: ANN401 - fila heterogenea del SDK
    """Recorre `row.campaign.status` a partir de `"campaign.status"`. Con
    `use_proto_plus=True` los campos enumerados llegan como el envoltorio
    proto-plus del enum: `.name` da la cadena ("ENABLED") que espera
    `google_ads_adapter._map_generic_status`; un escalar corriente (str,
    int, Decimal) no tiene `.name` y se devuelve tal cual."""
    value: Any = row
    for part in dotted_path.split("."):
        value = getattr(value, part)
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else value


def _fill_update_mask(client: GoogleAdsClient, operation: Any) -> None:  # noqa: ANN401 - tipo proto-plus
    """GAQL no tiene bind parameters, pero `mutate_*` si tiene `update_mask`:
    field mask con los campos realmente tocados en `operation.update`, no un
    `*` que sobrescribiria todo lo demas con el valor por defecto."""
    mask = protobuf_helpers.field_mask(  # type: ignore[no-untyped-call]
        None,
        operation.update._pb,  # noqa: SLF001
    )
    client.copy_from(operation.update_mask, mask)


class LiveGoogleAdsSearchClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        credential_store: CredentialStorePort,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._credential_store = credential_store

    def build_sdk_client(self, customer_id: str) -> GoogleAdsClient:
        """La MISMA resolucion de credencial de cliente (`_resolve_credential`)
        y `GoogleAdsClient` que todo metodo de esta clase usa, expuesta para
        que otro cliente de la misma cuenta (`LiveGoogleKeywordIdeaClient`,
        `KeywordPlanIdeaService`) no mantenga una segunda copia del refresh
        token ni de la resolucion contra `CredentialStorePort`."""
        return self._build_sdk_client(self._resolve_credential(customer_id))

    def search_stream(self, customer_id: str, query: str) -> Iterator[Mapping[str, Any]]:
        credential = self._resolve_credential(customer_id)
        client = self._build_sdk_client(credential)
        fields = select_fields(query)
        service = client.get_service("GoogleAdsService")
        for batch in service.search_stream(customer_id=customer_id, query=query):
            for row in batch.results:
                yield {field: extract_field(row, field) for field in fields}

    def campaign_creation_currency(self, customer_id: str) -> str:
        rows = list(self.search_stream(customer_id, "SELECT customer.currency_code FROM customer"))
        if len(rows) != 1:
            raise ValueError("campaign_creation_currency_unverified")
        return str(rows[0]["customer.currency_code"])

    def verify_conversion_goals(self, customer_id: str, resource_names: Sequence[str]) -> None:
        """Wiring point for `broker/platforms/campaign_creation.py`'s S-1
        gate (tasks.md T032): raises `ConversionGoalVerificationError`
        without ever calling `GoogleAdsService.mutate`. The returned
        `category`/`origin` pairs are discarded here -- the gate only
        needs pass/fail; `create_paused_campaign` re-reads them itself to
        build the `campaign_conversion_goal` operations."""
        verify_conversion_goals(self, customer_id, resource_names)

    def prepare_child(self, parent: str, plan: Mapping[str, Any]) -> None:
        google_prepare(self, parent, plan)

    def create_paused_child(self, parent: str, plan: Mapping[str, Any]) -> Mapping[str, Any]:
        customer = parent.split("/")[1]
        client = self._build_sdk_client(self._resolve_credential(customer))
        return google_create(client, parent, plan)

    def create_paused_campaign(  # noqa: PLR0914 - one mutation built and confirmed field by field
        self, customer_id: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        money = creation_budget(payload)
        plan = payload["creation_plan"]
        native = plan["native"]
        client = self._build_sdk_client(self._resolve_credential(customer_id))
        budget_op = client.get_type("MutateOperation")
        budget = budget_op.campaign_budget_operation.create
        budget.resource_name = f"customers/{customer_id}/campaignBudgets/-1"
        budget.name = plan["name"]
        budget.amount_micros = int(money.amount * 1_000_000)
        budget.delivery_method = "STANDARD"
        budget.explicitly_shared = False
        campaign_op = client.get_type("MutateOperation")
        campaign = campaign_op.campaign_operation.create
        campaign.resource_name = f"customers/{customer_id}/campaigns/-2"
        campaign.name = plan["name"]
        campaign.status = "PAUSED"
        campaign.advertising_channel_type = native["advertising_channel_type"]
        campaign.campaign_budget = budget.resource_name
        bidding_field = _apply_bidding(client, campaign, native["bidding_strategy"])
        campaign.contains_eu_political_advertising = native["contains_eu_political_advertising"]
        network_settings = native.get("network_settings")
        if network_settings is not None:
            for field, enabled in network_settings.items():
                setattr(campaign.network_settings, field, enabled)
        _apply_forced_literals(client, campaign, native)
        operations = [budget_op, campaign_op]
        signed_goals = tuple(goal["resource_name"] for goal in native.get("conversion_goals", ()))
        verified_goals = (
            verify_conversion_goals(self, customer_id, signed_goals) if signed_goals else ()
        )
        conversion_goal_ops = _conversion_goal_operations(client, customer_id, "-2", verified_goals)
        operations.extend(conversion_goal_ops)
        geography = native.get("geographic_targeting")
        if geography is not None:
            operations.extend(
                geography_operations(
                    client,
                    campaign,
                    geography,
                    f"customers/{customer_id}/campaigns/-2",
                )
            )
        response = client.get_service("GoogleAdsService").mutate(
            customer_id=customer_id,
            mutate_operations=operations,
            partial_failure=False,
            response_content_type="MUTABLE_RESOURCE",
            retry=None,
        )
        if response.partial_failure_error.code or len(response.mutate_operation_responses) != len(
            operations
        ):
            raise ValueError("campaign_creation_partial_response")
        results = response.mutate_operation_responses
        actual_budget = results[0].campaign_budget_result.campaign_budget
        actual_campaign = results[1].campaign_result.campaign
        campaign_ref = str(results[1].campaign_result.resource_name)
        budget_ref = str(results[0].campaign_budget_result.resource_name)
        if (
            not re.fullmatch(rf"customers/{customer_id}/campaigns/[0-9]+", campaign_ref)
            or not re.fullmatch(rf"customers/{customer_id}/campaignBudgets/[0-9]+", budget_ref)
            or actual_budget.amount_micros != budget.amount_micros
            or actual_campaign.status.name != "PAUSED"
            or actual_campaign.name != plan["name"]
            or actual_campaign.campaign_budget != budget_ref
            or actual_campaign.advertising_channel_type != campaign.advertising_channel_type
            or actual_campaign.contains_eu_political_advertising
            != campaign.contains_eu_political_advertising
            or actual_campaign.asset_automation_settings != campaign.asset_automation_settings
            or actual_campaign._pb.WhichOneof("campaign_bidding_strategy") != bidding_field
            or getattr(actual_campaign, bidding_field) != getattr(campaign, bidding_field)
            or _network_settings_mismatch(network_settings, actual_campaign, campaign)
        ):
            raise ValueError("campaign_creation_confirmation_mismatch")
        conversion_goal_results = results[2 : 2 + len(conversion_goal_ops)]
        if verified_goals:
            campaign_id = campaign_ref.rsplit("/", 1)[1]
            _confirm_conversion_goals(
                conversion_goal_results, customer_id, campaign_id, verified_goals
            )
        if geography is not None:
            geography_results = results[2 + len(conversion_goal_ops) :]
            confirm_geography(list(geography_results), geography, campaign_ref, actual_campaign)
        return {
            "campaign_resource": campaign_ref,
            "budget_resource": budget_ref,
            "status": "PAUSED",
            "daily_budget_minor": int(money.amount * 100),
        }

    def mutate_campaign_budget(
        self, customer_id: str, budget_resource_name: str, amount_micros: int
    ) -> str:
        client = self._build_sdk_client(self._resolve_credential(customer_id))
        service = client.get_service("CampaignBudgetService")
        operation = client.get_type("CampaignBudgetOperation")
        operation.update.resource_name = budget_resource_name
        operation.update.amount_micros = amount_micros
        _fill_update_mask(client, operation)
        response = service.mutate_campaign_budgets(customer_id=customer_id, operations=[operation])
        return str(response.results[0].resource_name)

    def mutate_status(
        self, customer_id: str, resource_name: str, level: EntityLevel, status: str
    ) -> str:
        client = self._build_sdk_client(self._resolve_credential(customer_id))
        service_name, operation_name, mutate_method = _SERVICE_AND_OPERATION_BY_LEVEL[level]
        service = client.get_service(service_name)
        operation = client.get_type(operation_name)
        operation.update.resource_name = resource_name
        operation.update.status = status
        _fill_update_mask(client, operation)
        response = getattr(service, mutate_method)(customer_id=customer_id, operations=[operation])
        return str(response.results[0].resource_name)

    def mutate_negative_keyword(
        self, customer_id: str, ad_group_resource_name: str, keyword_text: str
    ) -> str:
        client = self._build_sdk_client(self._resolve_credential(customer_id))
        service = client.get_service("AdGroupCriterionService")
        operation = client.get_type("AdGroupCriterionOperation")
        operation.create.ad_group = ad_group_resource_name
        operation.create.negative = True
        operation.create.keyword.text = keyword_text
        operation.create.keyword.match_type = _NEGATIVE_KEYWORD_MATCH_TYPE
        response = service.mutate_ad_group_criteria(customer_id=customer_id, operations=[operation])
        return str(response.results[0].resource_name)

    def _build_sdk_client(self, credential: PlatformCredential) -> GoogleAdsClient:
        # SDK 32 admite acceso por proyecto OAuth sin developer token ni
        # interruptores de compatibilidad del SDK anterior.
        return GoogleAdsClient.load_from_dict(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": credential.refresh_token,
                "login_customer_id": credential.login_customer_id,
                "use_proto_plus": True,
            },
            version=_GOOGLE_ADS_API_VERSION,
        )

    def _resolve_credential(self, customer_id: str) -> PlatformCredential:
        # `search_stream` corre en el hilo de `asyncio.to_thread` que abre
        # `GoogleAdsAdapter._run_gaql` (contracts/platform-port.md): sin
        # bucle de eventos activo ahi, `asyncio.run` es la forma segura de
        # llamar al puerto async de credenciales desde codigo sincrono.
        credential = asyncio.run(
            self._credential_store.get_credential(PlatformCode.GOOGLE, customer_id)
        )
        if credential is None or not credential.refresh_token:
            raise CredentialNotConnectedError(
                f"sin credencial de cliente de Google conectada para {customer_id}"
            )
        return credential


class LiveGoogleKeywordIdeaClient:
    """`GoogleKeywordIdeaClient` real (`google_reference_reader.py`) sobre
    `KeywordPlanIdeaService.generate_keyword_ideas` -- un servicio de
    generacion, sin plan de palabras clave persistido. Compone
    `LiveGoogleAdsSearchClient.build_sdk_client`: misma credencial de
    cliente por `customer_id`, nunca una segunda resolucion contra
    `CredentialStorePort`."""

    def __init__(self, search_client: LiveGoogleAdsSearchClient) -> None:
        self._search_client = search_client

    def generate_keyword_ideas(
        self,
        customer_id: str,
        *,
        seed_keywords: Sequence[str],
        geo_target_constant: str,
        language_constant: str,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        client = self._search_client.build_sdk_client(customer_id)
        request = client.get_type("GenerateKeywordIdeasRequest")
        request.customer_id = customer_id
        # Google exige nombres de recurso; el MCP recibe ids pelados ("1003",
        # "2724") o nombres completos. Con el id pelado Google responde
        # RESOURCE_NAME_MALFORMED (verificado en vivo 2026-09-15).
        request.language = _resource_name("languageConstants", language_constant)
        request.geo_target_constants.append(
            _resource_name("geoTargetConstants", geo_target_constant)
        )
        request.keyword_plan_network = _KEYWORD_PLAN_NETWORK
        request.keyword_seed.keywords.extend(seed_keywords)
        # Sin page_size Google devuelve miles de ideas (2 MB medidos en vivo);
        # el tope es el mismo que el corte de abajo.
        request.page_size = limit
        response = client.get_service("KeywordPlanIdeaService").generate_keyword_ideas(
            request=request
        )
        rows = []
        for result in response:
            rows.append(_keyword_idea_row(result))
            if len(rows) >= limit:
                break
        return rows


def _resource_name(collection: str, value: str) -> str:
    """`1003` -> `languageConstants/1003`; un nombre ya completo se respeta."""
    value = value.strip()
    prefix = f"{collection}/"
    return value if value.startswith(prefix) else f"{prefix}{value}"


def _keyword_idea_row(result: Any) -> dict[str, Any]:  # noqa: ANN401 - fila proto-plus del SDK
    metrics = result.keyword_idea_metrics
    avg_monthly_searches = (
        int(metrics.avg_monthly_searches) if metrics._pb.HasField("avg_monthly_searches") else None  # noqa: SLF001
    )
    competition = metrics.competition.name
    return {
        "text": str(result.text),
        "avg_monthly_searches": avg_monthly_searches,
        "competition": None if competition in _UNSET_COMPETITION_LEVELS else competition,
    }


class LiveGoogleAssetUploadClient:
    """`GoogleAssetUploadClient` real (`google_asset_upload.py`, threat-model.md
    #17) sobre `AssetService.mutate_assets`. Compone
    `LiveGoogleAdsSearchClient.build_sdk_client`: misma credencial de cliente
    por `customer_id`, nunca una segunda resolucion contra
    `CredentialStorePort` (mismo criterio que `LiveGoogleKeywordIdeaClient`)."""

    def __init__(self, search_client: LiveGoogleAdsSearchClient) -> None:
        self._search_client = search_client

    def mutate_image_asset(
        self,
        customer_id: str,
        *,
        name: str,
        data: bytes,
        mime_type: str,
        width: int,
        height: int,
    ) -> str:
        client = self._search_client.build_sdk_client(customer_id)
        operation = client.get_type("AssetOperation")
        asset = operation.create
        asset.name = name
        # `type_` es de solo lectura (Google lo infiere del miembro del
        # `oneof asset_data` que se rellena, `image_asset` aqui) -- fijarlo a
        # mano no es necesario y no forma parte del contrato de creacion.
        asset.image_asset.data = data
        asset.image_asset.mime_type = _IMAGE_ASSET_MIME_TYPE_BY_VALUE[mime_type]
        asset.image_asset.full_size.width_pixels = width
        asset.image_asset.full_size.height_pixels = height
        try:
            response = client.get_service("AssetService").mutate_assets(
                customer_id=customer_id, operations=[operation]
            )
        except Exception as exc:  # noqa: BLE001 - frontera con el SDK, nunca fail-open
            raise GoogleAssetUploadError(redact_sdk_error(exc)) from None
        return str(response.results[0].resource_name)
