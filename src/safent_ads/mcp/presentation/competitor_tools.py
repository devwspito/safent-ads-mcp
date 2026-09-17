"""004 tasks-2.md R7 (historias 21-23): `get_competitor_links` (puro, sin
red) y `search_competitor_ads` (API oficial de la Biblioteca de Anuncios de
Meta via el bróker; Google sin API, enlaces igualmente). Modulo autonomo
sobre `mcp.domain.competitor_urls`/`mcp.application.competitor_research_port`,
no toca `args.py` compartido."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field, model_validator

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.competitor_research_port import (
    CompetitorAdsResult,
    CompetitorPlatform,
    CompetitorResearchPort,
    MetaAdLibraryUnavailableError,
    MetaAdLibraryUnavailableReason,
)
from safent_ads.mcp.application.errors import ToolValidationError
from safent_ads.mcp.domain.competitor_urls import (
    CompetitorLinks,
    InvalidCountryCodeError,
    InvalidDomainError,
    MissingSearchTermError,
    build_competitor_links,
)
from safent_ads.mcp.presentation.args import BusinessId, ToolArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition

__all__ = ["CompetitorToolServices", "build_competitor_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_COUNTRY_PATTERN = r"^[A-Z]{2}$"
_DEFAULT_COUNTRY = "ES"
_MAX_TERM_LENGTH = 200

CountryCode = Annotated[str, Field(pattern=_COUNTRY_PATTERN)]
_SearchTerm = Annotated[str, Field(min_length=1, max_length=_MAX_TERM_LENGTH)]

_GOOGLE_UNAVAILABLE_REASON = "google_no_ofrece_api"

# Owner requirement (fix/ad-library-over-composio, addendum): honest fallback
# while the API no esta disponible -- nunca "Meta exige una verificacion
# adicional" fijo (esa frase asumia una causa sin comprobarla, incidente que
# esta lane cierra). `_FALLBACK_MESSAGE` es la misma linea para las dos
# plataformas: la pagina publica sigue abierta en el navegador aunque la API
# no responda.
_FALLBACK_MESSAGE = (
    "Puedes abrir la página pública de la Biblioteca de anuncios o del Centro de "
    "Transparencia en el navegador sin iniciar sesión; no es igual que los datos de "
    "la API (sin cifras de alcance, sin exportar), pero sigue siendo útil."
)
_NEXT_STEPS_BY_REASON: dict[MetaAdLibraryUnavailableReason, str] = {
    MetaAdLibraryUnavailableReason.NOT_CONNECTED: "Conecta una cuenta de Meta en el panel.",
    MetaAdLibraryUnavailableReason.IDENTITY_CONFIRMATION_REQUIRED: (
        "Con la cuenta de Facebook que autorizó Meta, confirma tu identidad en "
        "https://www.facebook.com/ID (tarda unos días)."
    ),
    MetaAdLibraryUnavailableReason.PROVIDER_ERROR: (
        "Vuelve a intentarlo en unos minutos; si el problema persiste, revisa la "
        "conexión de Meta en el panel."
    ),
}


@dataclass(frozen=True, slots=True)
class CompetitorToolServices:
    competitor_research: CompetitorResearchPort


class GetCompetitorLinksArgs(ToolArgs):
    business_id: BusinessId
    domain: _SearchTerm | None = None
    name: _SearchTerm | None = None
    country: CountryCode = _DEFAULT_COUNTRY

    @model_validator(mode="after")
    def _exactly_one_term(self) -> GetCompetitorLinksArgs:
        if (self.domain is None) == (self.name is None):
            raise ValueError("get_competitor_links: usar `domain` o `name`, no ambos ni ninguno")
        return self


class SearchCompetitorAdsArgs(ToolArgs):
    business_id: BusinessId
    platform: CompetitorPlatform
    query: _SearchTerm | None = None
    page_id: _SearchTerm | None = None
    domain: _SearchTerm | None = None
    country: CountryCode = _DEFAULT_COUNTRY
    active_only: bool = True

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> SearchCompetitorAdsArgs:
        selectors = (self.query, self.page_id, self.domain)
        if sum(selector is not None for selector in selectors) != 1:
            raise ValueError(
                "search_competitor_ads: usar exactamente uno de `query`, `page_id` o `domain`"
            )
        return self


def build_competitor_tool_definitions(
    services: CompetitorToolServices,
) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="get_competitor_links",
            description=(
                "Construye los enlaces a la Biblioteca de anuncios de Meta y al Centro de "
                "Transparencia de Google para un dominio o nombre, en un pais. No hace ninguna "
                "llamada de red: solo el formato correcto del enlace."
            ),
            args_model=GetCompetitorLinksArgs,
            tool_class=ToolClass.READ,
            handler=_get_competitor_links(),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="search_competitor_ads",
            description=(
                "Anuncios recientes de un competidor en Meta (API oficial de la Biblioteca de "
                "Anuncios) o enlaces sin red para Google (sin API oficial). Devuelve siempre los "
                "enlaces junto a los resultados; parte (a) de la revision previa obligatoria."
            ),
            args_model=SearchCompetitorAdsArgs,
            tool_class=ToolClass.READ,
            handler=_search_competitor_ads(services.competitor_research),
            business_id_of=_by_business_id,
        ),
    ]


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)


def _get_competitor_links() -> Handler[GetCompetitorLinksArgs, CompetitorLinks]:
    async def handler(
        args: GetCompetitorLinksArgs, _caller_scope: CallerScope
    ) -> CompetitorLinks:
        try:
            return build_competitor_links(domain=args.domain, name=args.name, country=args.country)
        except (InvalidDomainError, InvalidCountryCodeError, MissingSearchTermError) as exc:
            raise ToolValidationError(str(exc)) from exc

    return handler


def _search_competitor_ads(
    port: CompetitorResearchPort,
) -> Handler[SearchCompetitorAdsArgs, CompetitorAdsResult]:
    async def handler(
        args: SearchCompetitorAdsArgs, _caller_scope: CallerScope
    ) -> CompetitorAdsResult:
        links = build_competitor_links(
            domain=args.domain, name=args.query or args.page_id, country=args.country
        )
        if args.platform is CompetitorPlatform.GOOGLE:
            return CompetitorAdsResult(
                platform=CompetitorPlatform.GOOGLE,
                available=False,
                reason=_GOOGLE_UNAVAILABLE_REASON,
                ads=(),
                links=links,
                next_steps=None,
                fallback=_FALLBACK_MESSAGE,
            )
        try:
            ads = await port.search_meta_ads(
                args.business_id,
                query=args.query,
                page_id=args.page_id,
                domain=args.domain,
                country=args.country,
                active_only=args.active_only,
            )
        except MetaAdLibraryUnavailableError as exc:
            # Nunca se relata el error del proveedor: puede llevar tokens o
            # URL internas (R7, regla de seguridad) -- solo el codigo
            # estable y la linea accionable que le corresponde.
            return CompetitorAdsResult(
                platform=CompetitorPlatform.META,
                available=False,
                reason=exc.reason.value,
                ads=(),
                links=links,
                next_steps=_NEXT_STEPS_BY_REASON[exc.reason],
                fallback=_FALLBACK_MESSAGE,
            )
        return CompetitorAdsResult(
            platform=CompetitorPlatform.META,
            available=True,
            reason=None,
            ads=ads,
            links=links,
            next_steps=None,
            fallback=None,
        )

    return handler
