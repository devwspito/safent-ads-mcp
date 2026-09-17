"""`IngestBrandFromWebsite`: valida la URL antes de rastrear, guarda el
borrador y deja una vista previa `is_confirmed=False` en `BrandKit` para
que el resto del producto vea de inmediato que la marca esta a medias."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.brand.application.errors import BrandWebsiteUnreachableError
from safent_ads.brand.application.ingest_brand_from_website import (
    IngestBrandFromWebsite,
    IngestBrandFromWebsiteRequest,
)
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft
from safent_ads.brand.domain.errors import InvalidDiscoveryUrlError
from safent_ads.brand.testing.in_memory_brand_discovery_draft_repository import (
    InMemoryBrandDiscoveryDraftRepository,
)
from safent_ads.brand.testing.in_memory_brand_kit_repository import InMemoryBrandKitRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.ids import BusinessId

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class _FakeWebsiteBrandDiscovery:
    def __init__(self, draft: BrandDiscoveryDraft) -> None:
        self._draft = draft
        self.calls: list[tuple[BusinessId, str]] = []

    async def discover(self, business_id: BusinessId, url: str) -> BrandDiscoveryDraft:
        self.calls.append((business_id, url))
        return self._draft


def _use_case(
    draft: BrandDiscoveryDraft,
) -> tuple[
    IngestBrandFromWebsite,
    _FakeWebsiteBrandDiscovery,
    InMemoryBrandDiscoveryDraftRepository,
    InMemoryBrandKitRepository,
]:
    discovery = _FakeWebsiteBrandDiscovery(draft)
    drafts = InMemoryBrandDiscoveryDraftRepository()
    brand_kits = InMemoryBrandKitRepository()
    use_case = IngestBrandFromWebsite(
        discovery=discovery, drafts=drafts, brand_kits=brand_kits, clock=FixedClock(_NOW)
    )
    return use_case, discovery, drafts, brand_kits


async def test_rejects_non_http_url_before_discovering() -> None:
    business_id = BusinessId.new()
    draft = BrandDiscoveryDraft(business_id=business_id, source_url=None, discovered_at=_NOW)
    use_case, discovery, _drafts, _kits = _use_case(draft)

    with pytest.raises(InvalidDiscoveryUrlError):
        await use_case.execute(
            IngestBrandFromWebsiteRequest(business_id=business_id, url="ftp://example.test")
        )
    assert discovery.calls == []


async def test_saves_the_draft_and_calls_discover_with_the_url() -> None:
    business_id = BusinessId.new()
    draft = BrandDiscoveryDraft(
        business_id=business_id, source_url="https://example-business.test", discovered_at=_NOW
    )
    use_case, discovery, drafts, _kits = _use_case(draft)

    result = await use_case.execute(
        IngestBrandFromWebsiteRequest(business_id=business_id, url="https://example-business.test")
    )

    assert result == draft
    assert await drafts.get_by_business(business_id) == draft
    assert discovery.calls == [(business_id, "https://example-business.test")]


async def test_saves_an_unconfirmed_preview_brand_kit() -> None:
    business_id = BusinessId.new()
    draft = BrandDiscoveryDraft(
        business_id=business_id, source_url="https://example-business.test", discovered_at=_NOW
    )
    use_case, _discovery, _drafts, brand_kits = _use_case(draft)

    await use_case.execute(
        IngestBrandFromWebsiteRequest(business_id=business_id, url="https://example-business.test")
    )

    preview = await brand_kits.get_by_business(business_id)
    assert preview is not None
    assert preview.is_confirmed is False
    assert preview.is_complete() is False


async def test_confirms_the_website_host_of_a_successful_discovery() -> None:
    """F-8: cada `execute()` exitoso fija `confirmed_website_host` al
    host de la URL rastreada -- para REST (propietario autenticado
    tecleando la URL) esto ES la confirmacion que la herramienta MCP
    luego solo puede releer, nunca sobreescribir con una URL propia."""
    business_id = BusinessId.new()
    draft = BrandDiscoveryDraft(
        business_id=business_id, source_url="https://example-business.test", discovered_at=_NOW
    )
    use_case, _discovery, _drafts, brand_kits = _use_case(draft)

    await use_case.execute(
        IngestBrandFromWebsiteRequest(business_id=business_id, url="https://example-business.test")
    )

    preview = await brand_kits.get_by_business(business_id)
    assert preview is not None
    assert preview.confirmed_website_host == "example-business.test"


async def test_a_later_rest_discovery_reconfirms_the_host_it_was_given() -> None:
    """Cada rastreo exitoso por REST viene de un propietario autenticado
    tecleando la URL: el ultimo host rastreado es, por diseno, el host
    confirmado -- no queda "fijado" tras la primera vez."""
    business_id = BusinessId.new()
    draft = BrandDiscoveryDraft(
        business_id=business_id, source_url="https://example-business.test", discovered_at=_NOW
    )
    use_case, discovery, _drafts, brand_kits = _use_case(draft)
    await use_case.execute(
        IngestBrandFromWebsiteRequest(business_id=business_id, url="https://example-business.test")
    )

    discovery._draft = BrandDiscoveryDraft(  # noqa: SLF001
        business_id=business_id, source_url="https://other-business.test", discovered_at=_NOW
    )
    await use_case.execute(
        IngestBrandFromWebsiteRequest(business_id=business_id, url="https://other-business.test")
    )

    preview = await brand_kits.get_by_business(business_id)
    assert preview is not None
    assert preview.confirmed_website_host == "other-business.test"


class _FailingWebsiteBrandDiscovery:
    """Simula el adaptador real fallando (SSRF bloqueado, robots.txt,
    tamano excedido, red caida): cualquiera de esas causas concretas llega
    aqui como `InfrastructureError`."""

    async def discover(self, business_id: BusinessId, url: str) -> BrandDiscoveryDraft:  # noqa: ARG002
        raise InfrastructureError("no se pudo descargar la pagina de inicio")


async def test_wraps_infrastructure_failures_as_a_brand_website_unreachable_error() -> None:
    business_id = BusinessId.new()
    use_case = IngestBrandFromWebsite(
        discovery=_FailingWebsiteBrandDiscovery(),
        drafts=InMemoryBrandDiscoveryDraftRepository(),
        brand_kits=InMemoryBrandKitRepository(),
        clock=FixedClock(_NOW),
    )

    with pytest.raises(BrandWebsiteUnreachableError) as excinfo:
        await use_case.execute(
            IngestBrandFromWebsiteRequest(
                business_id=business_id, url="https://example-business.test"
            )
        )
    assert isinstance(excinfo.value.__cause__, InfrastructureError)
