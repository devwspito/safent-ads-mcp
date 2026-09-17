"""`reference_data_tools.py` (R3/R4, historia 18): registro de las nueve
herramientas, nombres verbo-primero validos, y que cada arista pide solo
sus campos de la lista blanca -- una cuenta ajena da `ENTITY_NOT_FOUND`
(la comprobacion IDOR vive en `broker_reference_data_port.py`, aqui se
prueba con un doble que la reproduce)."""

from __future__ import annotations

import pytest

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.application.reference_data_port import (
    GoogleConstant,
    GoogleConstantKind,
    GoogleConversionAction,
    GoogleKeywordIdea,
    MetaAudience,
    MetaCatalog,
    MetaPage,
    MetaPixel,
    MetaReachEstimate,
    MetaTargetingKind,
    MetaTargetingSuggestion,
)
from safent_ads.mcp.presentation.reference_data_tools import (
    GetGoogleKeywordIdeasArgs,
    GetMetaReachEstimateArgs,
    ListMetaPagesArgs,
    MetaOptimizationGoal,
    ReferenceDataToolServices,
    SearchGoogleConstantsArgs,
    SearchMetaTargetingArgs,
    build_reference_data_tool_definitions,
)
from safent_ads.mcp.presentation.registry import ToolClass, ToolRegistry

_EXPECTED_NAMES = frozenset(
    {
        "list_meta_pages",
        "list_meta_pixels",
        "list_meta_audiences",
        "list_meta_catalogs",
        "search_meta_targeting",
        "get_meta_reach_estimate",
        "list_google_conversion_actions",
        "search_google_constants",
        "get_google_keyword_ideas",
    }
)
_BUSINESS_ID = "9d9b8b1a-6b8e-4f0a-9d1e-8f2c6b7a5e10"
_OWN_ACCOUNT_REF = "meta:act_111"
_OTHER_ACCOUNT_REF = "meta:act_999"


def _caller_scope() -> CallerScope:
    return CallerScope(
        caller_id="test",
        allowed_business_ids=frozenset({_BUSINESS_ID}),
        permission=Permission.VIEW,
        person_label="Agente de prueba",
    )


class _FakeMetaReferenceDataPort:
    """Reproduce la comprobacion IDOR de `resolve_owned_account_ref`: solo
    `_OWN_ACCOUNT_REF` responde, cualquier otra cuenta da `ENTITY_NOT_FOUND`
    (mismo codigo para inexistente o ajena, nunca se distingue)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def _check(self, account_ref: str) -> None:
        if account_ref != _OWN_ACCOUNT_REF:
            raise EntityNotFoundError(f"{account_ref} aun no disponible")

    async def list_meta_pages(self, business_id: str, account_ref: str) -> tuple[MetaPage, ...]:
        self._check(account_ref)
        self.calls.append(("list_meta_pages", business_id, account_ref))
        return (MetaPage(page_id="1", name="Pagina", instagram_business_account_id=None),)

    async def list_meta_pixels(self, business_id: str, account_ref: str) -> tuple[MetaPixel, ...]:
        self._check(account_ref)
        self.calls.append(("list_meta_pixels", business_id, account_ref))
        return (MetaPixel(pixel_id="1", name="Pixel", last_fired_event_names=()),)

    async def list_meta_audiences(
        self, business_id: str, account_ref: str  # noqa: ARG002 - firma del puerto
    ) -> tuple[MetaAudience, ...]:
        self._check(account_ref)
        return (
            MetaAudience(
                audience_id="1", name="Audiencia", kind="custom", approximate_count_upper_bound=100
            ),
        )

    async def list_meta_catalogs(
        self, business_id: str, account_ref: str  # noqa: ARG002 - firma del puerto
    ) -> tuple[MetaCatalog, ...]:
        self._check(account_ref)
        return (MetaCatalog(catalog_id="1", name="Catalogo", product_count=10),)

    async def search_meta_targeting(
        self, business_id: str, account_ref: str, *, kind: MetaTargetingKind, query: str
    ) -> tuple[MetaTargetingSuggestion, ...]:
        self._check(account_ref)
        self.calls.append(("search_meta_targeting", business_id, account_ref, kind.value, query))
        return (
            MetaTargetingSuggestion(
                targeting_id="1",
                name=query,
                audience_size_lower_bound=1000,
                audience_size_upper_bound=2000,
            ),
        )

    async def get_meta_reach_estimate(
        self,
        business_id: str,
        account_ref: str,
        *,
        optimization_goal: str,
        countries: tuple[str, ...],
    ) -> MetaReachEstimate:
        self._check(account_ref)
        self.calls.append(
            ("get_meta_reach_estimate", business_id, account_ref, optimization_goal, *countries)
        )
        return MetaReachEstimate(
            users_lower_bound=1000, users_upper_bound=5000, estimate_ready=True
        )


class _FakeGoogleReferenceDataPort:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def list_google_conversion_actions(
        self, business_id: str, account_ref: str
    ) -> tuple[GoogleConversionAction, ...]:
        self.calls.append(("list_google_conversion_actions", business_id, account_ref))
        return (
            GoogleConversionAction(
                resource_name="customers/1/conversionActions/1",
                name="Compra",
                category="PURCHASE",
                status="ENABLED",
            ),
        )

    async def search_google_constants(
        self,
        business_id: str,
        account_ref: str,
        *,
        kind: GoogleConstantKind,
        query: str,
        country: str | None,
    ) -> tuple[GoogleConstant, ...]:
        self.calls.append(
            ("search_google_constants", business_id, account_ref, kind.value, query, country)
        )
        return (GoogleConstant(resource_name="geoTargetConstants/1", name=query, code="ES"),)

    async def get_google_keyword_ideas(
        self,
        business_id: str,
        account_ref: str,
        *,
        seed_keywords: tuple[str, ...],
        geo_target: str,
        language: str,
    ) -> tuple[GoogleKeywordIdea, ...]:
        self.calls.append(
            (
                "get_google_keyword_ideas",
                business_id,
                account_ref,
                seed_keywords,
                geo_target,
                language,
            )
        )
        return (
            GoogleKeywordIdea(text=seed_keywords[0], avg_monthly_searches=100, competition="LOW"),
        )


def _services() -> ReferenceDataToolServices:
    return ReferenceDataToolServices(
        meta=_FakeMetaReferenceDataPort(), google=_FakeGoogleReferenceDataPort()
    )


def test_build_reference_data_tool_definitions_registers_the_nine_verbs() -> None:
    definitions = build_reference_data_tool_definitions(_services())

    assert {d.name for d in definitions} == _EXPECTED_NAMES


def test_all_nine_tools_are_read_class() -> None:
    for definition in build_reference_data_tool_definitions(_services()):
        assert definition.tool_class is ToolClass.READ


def test_definitions_pass_the_registry_naming_guard() -> None:
    ToolRegistry(build_reference_data_tool_definitions(_services()))


async def test_cada_arista_pide_solo_campos_de_su_lista_blanca_y_una_cuenta_ajena_da_entity_not_found() -> None:  # noqa: E501
    services = _services()
    definitions = {d.name: d for d in build_reference_data_tool_definitions(services)}
    args = ListMetaPagesArgs(business_id=_BUSINESS_ID, account_ref=_OWN_ACCOUNT_REF)

    pages = await definitions["list_meta_pages"].handler(args, _caller_scope())

    assert pages == (MetaPage(page_id="1", name="Pagina", instagram_business_account_id=None),)

    other_args = ListMetaPagesArgs(business_id=_BUSINESS_ID, account_ref=_OTHER_ACCOUNT_REF)
    with pytest.raises(EntityNotFoundError):
        await definitions["list_meta_pages"].handler(other_args, _caller_scope())


async def test_search_meta_targeting_delegates_kind_and_query() -> None:
    meta_port = _FakeMetaReferenceDataPort()
    services = ReferenceDataToolServices(meta=meta_port, google=_FakeGoogleReferenceDataPort())
    definitions = {d.name: d for d in build_reference_data_tool_definitions(services)}
    args = SearchMetaTargetingArgs(
        business_id=_BUSINESS_ID,
        account_ref=_OWN_ACCOUNT_REF,
        kind=MetaTargetingKind.INTEREST,
        query="senderismo",
    )

    result = await definitions["search_meta_targeting"].handler(args, _caller_scope())

    assert result[0].name == "senderismo"
    assert meta_port.calls == [
        ("search_meta_targeting", _BUSINESS_ID, _OWN_ACCOUNT_REF, "adinterest", "senderismo")
    ]


async def test_get_meta_reach_estimate_passes_countries_and_goal() -> None:
    meta_port = _FakeMetaReferenceDataPort()
    services = ReferenceDataToolServices(meta=meta_port, google=_FakeGoogleReferenceDataPort())
    definitions = {d.name: d for d in build_reference_data_tool_definitions(services)}
    args = GetMetaReachEstimateArgs(
        business_id=_BUSINESS_ID,
        account_ref=_OWN_ACCOUNT_REF,
        optimization_goal=MetaOptimizationGoal.REACH,
        countries=["ES", "PT"],
    )

    estimate = await definitions["get_meta_reach_estimate"].handler(args, _caller_scope())

    assert estimate.estimate_ready is True
    assert meta_port.calls == [
        ("get_meta_reach_estimate", _BUSINESS_ID, _OWN_ACCOUNT_REF, "REACH", "ES", "PT")
    ]


def test_get_meta_reach_estimate_rejects_more_than_10_countries() -> None:
    with pytest.raises(ValueError, match="countries"):
        GetMetaReachEstimateArgs(
            business_id=_BUSINESS_ID,
            account_ref=_OWN_ACCOUNT_REF,
            optimization_goal=MetaOptimizationGoal.REACH,
            countries=[f"C{i}" for i in range(11)],
        )


async def test_ideas_de_palabras_clave_acotan_semillas_y_filas_y_no_aceptan_gaql_libre() -> None:
    google_port = _FakeGoogleReferenceDataPort()
    services = ReferenceDataToolServices(meta=_FakeMetaReferenceDataPort(), google=google_port)
    definitions = {d.name: d for d in build_reference_data_tool_definitions(services)}
    args = GetGoogleKeywordIdeasArgs(
        business_id=_BUSINESS_ID,
        account_ref=_OWN_ACCOUNT_REF,
        seed_keywords=["zapatillas running"],
        geo_target="2724",
        language="1003",
    )

    ideas = await definitions["get_google_keyword_ideas"].handler(args, _caller_scope())

    assert ideas[0].text == "zapatillas running"
    # `GetGoogleKeywordIdeasArgs` no tiene ningun campo `query`/`gaql`: no
    # hay forma de que el llamante pida una consulta libre a este servicio.
    assert not hasattr(args, "query")
    assert not hasattr(args, "gaql")


def test_get_google_keyword_ideas_rejects_more_than_20_seed_keywords() -> None:
    with pytest.raises(ValueError, match="seed_keywords"):
        GetGoogleKeywordIdeasArgs(
            business_id=_BUSINESS_ID,
            account_ref=_OWN_ACCOUNT_REF,
            seed_keywords=[f"kw{i}" for i in range(21)],
            geo_target="2724",
            language="1003",
        )


def test_get_google_keyword_ideas_rejects_seed_keywords_over_80_chars() -> None:
    with pytest.raises(ValueError, match="80"):
        GetGoogleKeywordIdeasArgs(
            business_id=_BUSINESS_ID,
            account_ref=_OWN_ACCOUNT_REF,
            seed_keywords=["x" * 81],
            geo_target="2724",
            language="1003",
        )


async def test_search_google_constants_delegates_kind_query_and_country() -> None:
    google_port = _FakeGoogleReferenceDataPort()
    services = ReferenceDataToolServices(meta=_FakeMetaReferenceDataPort(), google=google_port)
    definitions = {d.name: d for d in build_reference_data_tool_definitions(services)}
    args = SearchGoogleConstantsArgs(
        business_id=_BUSINESS_ID,
        account_ref=_OWN_ACCOUNT_REF,
        kind=GoogleConstantKind.GEO_TARGET,
        query="Madrid",
        country="ES",
    )

    result = await definitions["search_google_constants"].handler(args, _caller_scope())

    assert result[0].name == "Madrid"
    assert google_port.calls == [
        (
            "search_google_constants",
            _BUSINESS_ID,
            _OWN_ACCOUNT_REF,
            "geo_target",
            "Madrid",
            "ES",
        )
    ]
