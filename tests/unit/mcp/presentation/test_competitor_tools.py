"""`competitor_tools.py` (R7, historias 21-23; fix/ad-library-over-composio):
`get_competitor_links` no hace red y siempre nombra el mismo dominio en
ambos enlaces; Google nunca intenta una descarga y Meta no disponible
nunca relata el error real del proveedor -- solo el codigo estable de
`MetaAdLibraryUnavailableReason`, la linea `next_steps` que le corresponde
y el `fallback` a la pagina publica."""

from __future__ import annotations

import pytest

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.competitor_research_port import (
    CompetitorAd,
    CompetitorPlatform,
    MetaAdLibraryUnavailableError,
    MetaAdLibraryUnavailableReason,
)
from safent_ads.mcp.presentation.competitor_tools import (
    CompetitorToolServices,
    GetCompetitorLinksArgs,
    SearchCompetitorAdsArgs,
    build_competitor_tool_definitions,
)
from safent_ads.mcp.presentation.registry import ToolClass, ToolRegistry

_EXPECTED_NAMES = frozenset({"get_competitor_links", "search_competitor_ads"})
_BUSINESS_ID = "9d9b8b1a-6b8e-4f0a-9d1e-8f2c6b7a5e10"


def _caller_scope() -> CallerScope:
    return CallerScope(
        caller_id="test",
        allowed_business_ids=frozenset({_BUSINESS_ID}),
        permission=Permission.VIEW,
        person_label="Agente de prueba",
    )


class _FakeCompetitorResearchPort:
    def __init__(
        self,
        *,
        ads: tuple[CompetitorAd, ...] | None = None,
        unavailable: MetaAdLibraryUnavailableReason | None = None,
    ):
        self._ads = ads or ()
        self._unavailable = unavailable
        self.calls: list[tuple[str | None, str | None, str | None, str, bool]] = []

    async def search_meta_ads(
        self,
        business_id: str,  # noqa: ARG002 - firma del puerto, no usado en el doble
        *,
        query: str | None,
        page_id: str | None,
        domain: str | None,
        country: str,
        active_only: bool,
    ) -> tuple[CompetitorAd, ...]:
        self.calls.append((query, page_id, domain, country, active_only))
        if self._unavailable is not None:
            raise MetaAdLibraryUnavailableError(self._unavailable)
        return self._ads


def _services(port: _FakeCompetitorResearchPort | None = None) -> CompetitorToolServices:
    return CompetitorToolServices(competitor_research=port or _FakeCompetitorResearchPort())


def test_build_competitor_tool_definitions_registers_the_two_verbs() -> None:
    definitions = build_competitor_tool_definitions(_services())

    assert {d.name for d in definitions} == _EXPECTED_NAMES


def test_both_tools_are_read_class() -> None:
    for definition in build_competitor_tool_definitions(_services()):
        assert definition.tool_class is ToolClass.READ


def test_definitions_pass_the_registry_naming_guard() -> None:
    ToolRegistry(build_competitor_tool_definitions(_services()))


async def test_get_competitor_links_never_calls_the_port() -> None:
    port = _FakeCompetitorResearchPort()
    definitions = {d.name: d for d in build_competitor_tool_definitions(_services(port))}
    args = GetCompetitorLinksArgs(business_id=_BUSINESS_ID, domain="example.com", country="ES")

    links = await definitions["get_competitor_links"].handler(args, _caller_scope())

    assert "example.com" in links.meta_ad_library_url
    assert port.calls == []


def test_get_competitor_links_rejects_both_domain_and_name() -> None:
    with pytest.raises(ValueError, match="domain.*name"):
        GetCompetitorLinksArgs(
            business_id=_BUSINESS_ID, domain="example.com", name="Ejemplo", country="ES"
        )


def test_get_competitor_links_rejects_neither_domain_nor_name() -> None:
    with pytest.raises(ValueError, match="domain.*name"):
        GetCompetitorLinksArgs(business_id=_BUSINESS_ID, country="ES")


async def test_google_devuelve_enlaces_sin_red_con_fallback_pero_sin_next_steps() -> None:
    port = _FakeCompetitorResearchPort(unavailable=MetaAdLibraryUnavailableReason.PROVIDER_ERROR)
    definitions = {d.name: d for d in build_competitor_tool_definitions(_services(port))}

    google_args = SearchCompetitorAdsArgs(
        business_id=_BUSINESS_ID, platform=CompetitorPlatform.GOOGLE, domain="example.com"
    )
    google_result = await definitions["search_competitor_ads"].handler(
        google_args, _caller_scope()
    )

    assert google_result.available is False
    assert google_result.reason == "google_no_ofrece_api"
    assert google_result.ads == ()
    assert google_result.next_steps is None
    assert google_result.fallback
    assert "example.com" in google_result.links.meta_ad_library_url
    assert port.calls == []  # Google nunca intenta una descarga.


@pytest.mark.parametrize(
    "reason",
    [
        MetaAdLibraryUnavailableReason.NOT_CONNECTED,
        MetaAdLibraryUnavailableReason.IDENTITY_CONFIRMATION_REQUIRED,
        MetaAdLibraryUnavailableReason.PROVIDER_ERROR,
    ],
)
async def test_meta_no_disponible_expone_un_codigo_estable_next_steps_y_fallback_sin_filtrar_el_error(  # noqa: E501
    reason: MetaAdLibraryUnavailableReason,
) -> None:
    port = _FakeCompetitorResearchPort(unavailable=reason)
    definitions = {d.name: d for d in build_competitor_tool_definitions(_services(port))}
    meta_args = SearchCompetitorAdsArgs(
        business_id=_BUSINESS_ID, platform=CompetitorPlatform.META, domain="example.com"
    )

    meta_result = await definitions["search_competitor_ads"].handler(meta_args, _caller_scope())

    assert meta_result.available is False
    assert meta_result.reason == reason.value
    assert meta_result.ads == ()
    assert meta_result.next_steps
    assert meta_result.fallback
    assert "example.com" in meta_result.links.meta_ad_library_url


async def test_identity_confirmation_required_uses_the_owner_mandated_copy() -> None:
    """fix/ad-library-identity-reason: this exact string is the contract --
    the owner specified it verbatim (facebook.com/ID, "tarda unos días")."""
    port = _FakeCompetitorResearchPort(
        unavailable=MetaAdLibraryUnavailableReason.IDENTITY_CONFIRMATION_REQUIRED
    )
    definitions = {d.name: d for d in build_competitor_tool_definitions(_services(port))}
    meta_args = SearchCompetitorAdsArgs(
        business_id=_BUSINESS_ID, platform=CompetitorPlatform.META, domain="example.com"
    )

    meta_result = await definitions["search_competitor_ads"].handler(meta_args, _caller_scope())

    assert meta_result.reason == "identity_confirmation_required"
    assert meta_result.next_steps == (
        "Con la cuenta de Facebook que autorizó Meta, confirma tu identidad en "
        "https://www.facebook.com/ID (tarda unos días)."
    )


async def test_search_competitor_ads_returns_meta_results_when_available() -> None:
    ad = CompetitorAd(
        advertiser_name="Competidor SL",
        ad_text="Anuncio de ejemplo",
        image_url=None,
        start_date=None,
        stop_date=None,
        platforms=("facebook",),
        reach_by_country=None,
    )
    port = _FakeCompetitorResearchPort(ads=(ad,))
    definitions = {d.name: d for d in build_competitor_tool_definitions(_services(port))}
    args = SearchCompetitorAdsArgs(
        business_id=_BUSINESS_ID, platform=CompetitorPlatform.META, query="zapatillas"
    )

    result = await definitions["search_competitor_ads"].handler(args, _caller_scope())

    assert result.available is True
    assert result.ads == (ad,)
    assert result.reason is None
    assert result.next_steps is None
    assert result.fallback is None
    assert port.calls == [("zapatillas", None, None, "ES", True)]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": "a", "domain": "example.com"},
        {"query": "a", "page_id": "123"},
        {},
    ],
)
def test_search_competitor_ads_requires_exactly_one_selector(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="exactamente uno"):
        SearchCompetitorAdsArgs(
            business_id=_BUSINESS_ID, platform=CompetitorPlatform.META, **kwargs
        )
