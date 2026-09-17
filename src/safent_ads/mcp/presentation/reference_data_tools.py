"""004 tasks-2.md R3/R4 (historia 18): nueve lecturas de referencia,
`business_id` + `account_ref` obligatorios en todas, `resolve_owned_account_ref`
siempre antes de la llamada (lo aplica el puerto de infraestructura, no
aqui). Modulo autonomo: declara sus propios `Args`, no toca `args.py`
compartido (mismo criterio que `search_terms_tools.py`)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.reference_data_port import (
    GoogleConstantKind,
    GoogleConversionAction,
    GoogleKeywordIdea,
    GoogleReferenceDataPort,
    MetaAudience,
    MetaCatalog,
    MetaPage,
    MetaPixel,
    MetaReachEstimate,
    MetaReferenceDataPort,
    MetaTargetingKind,
    MetaTargetingSuggestion,
)
from safent_ads.mcp.presentation.args import BusinessId, OpaqueId, ToolArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition

__all__ = ["ReferenceDataToolServices", "build_reference_data_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_COUNTRY_PATTERN = r"^[A-Z]{2}$"
_MAX_COUNTRIES = 10
_MAX_SEED_KEYWORDS = 20
_MAX_KEYWORD_LENGTH = 80
_MAX_QUERY_LENGTH = 200

CountryCode = Annotated[str, Field(pattern=_COUNTRY_PATTERN)]
_SeedKeyword = Annotated[str, Field(min_length=1, max_length=_MAX_KEYWORD_LENGTH)]
_SearchQuery = Annotated[str, Field(min_length=1, max_length=_MAX_QUERY_LENGTH)]


class MetaOptimizationGoal(StrEnum):
    """Subconjunto cerrado suficiente para una estimacion de alcance
    (`ad_child_args.py` ya fija `LINK_CLICKS` como unico objetivo de
    creacion; aqui se admite el resto de objetivos de solo lectura)."""

    LINK_CLICKS = "LINK_CLICKS"
    REACH = "REACH"
    CONVERSIONS = "CONVERSIONS"
    IMPRESSIONS = "IMPRESSIONS"
    LANDING_PAGE_VIEWS = "LANDING_PAGE_VIEWS"


@dataclass(frozen=True, slots=True)
class ReferenceDataToolServices:
    meta: MetaReferenceDataPort
    google: GoogleReferenceDataPort


class _AccountScopedArgs(ToolArgs):
    business_id: BusinessId
    account_ref: OpaqueId


class ListMetaPagesArgs(_AccountScopedArgs):
    pass


class ListMetaPixelsArgs(_AccountScopedArgs):
    pass


class ListMetaAudiencesArgs(_AccountScopedArgs):
    pass


class ListMetaCatalogsArgs(_AccountScopedArgs):
    pass


class SearchMetaTargetingArgs(_AccountScopedArgs):
    kind: MetaTargetingKind
    query: _SearchQuery


class GetMetaReachEstimateArgs(_AccountScopedArgs):
    optimization_goal: MetaOptimizationGoal
    countries: Annotated[list[CountryCode], Field(min_length=1, max_length=_MAX_COUNTRIES)]


class ListGoogleConversionActionsArgs(_AccountScopedArgs):
    pass


class SearchGoogleConstantsArgs(_AccountScopedArgs):
    kind: GoogleConstantKind
    query: _SearchQuery
    country: CountryCode | None = None


class GetGoogleKeywordIdeasArgs(_AccountScopedArgs):
    seed_keywords: Annotated[list[_SeedKeyword], Field(min_length=1, max_length=_MAX_SEED_KEYWORDS)]
    geo_target: OpaqueId
    language: OpaqueId


def build_reference_data_tool_definitions(
    services: ReferenceDataToolServices,
) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="list_meta_pages",
            description=(
                "Paginas de Facebook que la credencial alcanza, con la cuenta de Instagram "
                "vinculada si la hay. El id de pagina que suele faltar antes de crear anuncios."
            ),
            args_model=ListMetaPagesArgs,
            tool_class=ToolClass.READ,
            handler=_list_meta_pages(services.meta),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="list_meta_pixels",
            description="Pixeles de Meta de la cuenta, con los eventos que han disparado.",
            args_model=ListMetaPixelsArgs,
            tool_class=ToolClass.READ,
            handler=_list_meta_pixels(services.meta),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="list_meta_audiences",
            description="Audiencias personalizadas y guardadas de la cuenta, con su tamano.",
            args_model=ListMetaAudiencesArgs,
            tool_class=ToolClass.READ,
            handler=_list_meta_audiences(services.meta),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="list_meta_catalogs",
            description="Catalogos de producto de la cuenta, con el numero de productos.",
            args_model=ListMetaCatalogsArgs,
            tool_class=ToolClass.READ,
            handler=_list_meta_catalogs(services.meta),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="search_meta_targeting",
            description=(
                "Busca intereses, comportamientos o categorias demograficas de Meta por "
                "`query`, para preparar una segmentacion antes de proponerla."
            ),
            args_model=SearchMetaTargetingArgs,
            tool_class=ToolClass.READ,
            handler=_search_meta_targeting(services.meta),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="get_meta_reach_estimate",
            description="Estimacion de alcance de Meta para un objetivo y una lista de paises.",
            args_model=GetMetaReachEstimateArgs,
            tool_class=ToolClass.READ,
            handler=_get_meta_reach_estimate(services.meta),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="list_google_conversion_actions",
            description="Acciones de conversion configuradas en la cuenta de Google Ads.",
            args_model=ListGoogleConversionActionsArgs,
            tool_class=ToolClass.READ,
            handler=_list_google_conversion_actions(services.google),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="search_google_constants",
            description=(
                "Busca constantes de Google Ads (ubicaciones geograficas o idiomas) por "
                "`query`, para resolver el id que necesita una segmentacion."
            ),
            args_model=SearchGoogleConstantsArgs,
            tool_class=ToolClass.READ,
            handler=_search_google_constants(services.google),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="get_google_keyword_ideas",
            description=(
                "Ideas de palabras clave de Google Ads a partir de hasta 20 semillas, con "
                "busquedas mensuales medias y nivel de competencia."
            ),
            args_model=GetGoogleKeywordIdeasArgs,
            tool_class=ToolClass.READ,
            handler=_get_google_keyword_ideas(services.google),
            business_id_of=_by_business_id,
        ),
    ]


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)


def _list_meta_pages(
    port: MetaReferenceDataPort,
) -> Handler[ListMetaPagesArgs, tuple[MetaPage, ...]]:
    async def handler(
        args: ListMetaPagesArgs, _caller_scope: CallerScope
    ) -> tuple[MetaPage, ...]:
        return await port.list_meta_pages(args.business_id, args.account_ref)

    return handler


def _list_meta_pixels(
    port: MetaReferenceDataPort,
) -> Handler[ListMetaPixelsArgs, tuple[MetaPixel, ...]]:
    async def handler(
        args: ListMetaPixelsArgs, _caller_scope: CallerScope
    ) -> tuple[MetaPixel, ...]:
        return await port.list_meta_pixels(args.business_id, args.account_ref)

    return handler


def _list_meta_audiences(
    port: MetaReferenceDataPort,
) -> Handler[ListMetaAudiencesArgs, tuple[MetaAudience, ...]]:
    async def handler(
        args: ListMetaAudiencesArgs, _caller_scope: CallerScope
    ) -> tuple[MetaAudience, ...]:
        return await port.list_meta_audiences(args.business_id, args.account_ref)

    return handler


def _list_meta_catalogs(
    port: MetaReferenceDataPort,
) -> Handler[ListMetaCatalogsArgs, tuple[MetaCatalog, ...]]:
    async def handler(
        args: ListMetaCatalogsArgs, _caller_scope: CallerScope
    ) -> tuple[MetaCatalog, ...]:
        return await port.list_meta_catalogs(args.business_id, args.account_ref)

    return handler


def _search_meta_targeting(
    port: MetaReferenceDataPort,
) -> Handler[SearchMetaTargetingArgs, tuple[MetaTargetingSuggestion, ...]]:
    async def handler(
        args: SearchMetaTargetingArgs, _caller_scope: CallerScope
    ) -> tuple[MetaTargetingSuggestion, ...]:
        return await port.search_meta_targeting(
            args.business_id, args.account_ref, kind=args.kind, query=args.query
        )

    return handler


def _get_meta_reach_estimate(
    port: MetaReferenceDataPort,
) -> Handler[GetMetaReachEstimateArgs, MetaReachEstimate]:
    async def handler(
        args: GetMetaReachEstimateArgs, _caller_scope: CallerScope
    ) -> MetaReachEstimate:
        return await port.get_meta_reach_estimate(
            args.business_id,
            args.account_ref,
            optimization_goal=args.optimization_goal.value,
            countries=tuple(args.countries),
        )

    return handler


def _list_google_conversion_actions(
    port: GoogleReferenceDataPort,
) -> Handler[ListGoogleConversionActionsArgs, tuple[GoogleConversionAction, ...]]:
    async def handler(
        args: ListGoogleConversionActionsArgs, _caller_scope: CallerScope
    ) -> tuple[GoogleConversionAction, ...]:
        return await port.list_google_conversion_actions(args.business_id, args.account_ref)

    return handler


def _search_google_constants(
    port: GoogleReferenceDataPort,
) -> Handler[SearchGoogleConstantsArgs, tuple[Any, ...]]:
    async def handler(
        args: SearchGoogleConstantsArgs, _caller_scope: CallerScope
    ) -> tuple[Any, ...]:
        return await port.search_google_constants(
            args.business_id,
            args.account_ref,
            kind=args.kind,
            query=args.query,
            country=args.country,
        )

    return handler


def _get_google_keyword_ideas(
    port: GoogleReferenceDataPort,
) -> Handler[GetGoogleKeywordIdeasArgs, tuple[GoogleKeywordIdea, ...]]:
    async def handler(
        args: GetGoogleKeywordIdeasArgs, _caller_scope: CallerScope
    ) -> tuple[GoogleKeywordIdea, ...]:
        return await port.get_google_keyword_ideas(
            args.business_id,
            args.account_ref,
            seed_keywords=tuple(args.seed_keywords),
            geo_target=args.geo_target,
            language=args.language,
        )

    return handler
