"""`BrokerCompetitorResearchPort` real (R7): `search_meta_ads` pasa por el
bróker (`meta_ads_archive`, nueva op que cablea I1 en
`broker/presentation/dispatcher.py` + `broker/platforms/meta_ad_library.py`).
Estrecho a proposito: el enrutado por plataforma vive en
`presentation/competitor_tools.py`, no aqui.

Mismo patron que `native_ads_broker_client.py`: subclase de
`BrokerSocketClient` con su propia op, sin tocar el fichero compartido
(`accounts/infrastructure/broker_client.py`, fuera de este carril).

fix/ad-library-over-composio: el nodo `ads_archive` en si es transparencia
publica (no cuelga de ninguna cuenta conectada), pero en modo Composio
(companion, sin app nativa de Meta) el proxy SI necesita una cuenta ya
conectada para autenticar la llamada -- `_resolve_active_meta_account`
resuelve la primera cuenta de Meta ACTIVA del negocio, la misma cuenta que
autentica cualquier otra lectura de Meta (`broker_reference_data_port.py`),
salvo que aqui no la elige el llamante: `search_competitor_ads` no lleva
`account_ref` (el nodo es independiente de cuenta), asi que este puerto la
resuelve por su cuenta antes de hablar con el bróker."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.infrastructure.broker_client import BrokerSocketClient
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.accounts.infrastructure.sql_repositories import SqlAccountRepository
from safent_ads.mcp.application.competitor_research_port import (
    CompetitorAd,
    MetaAdLibraryUnavailableError,
    MetaAdLibraryUnavailableReason,
)
from safent_ads.shared.ids import BusinessId, PlatformCode

__all__ = ["BrokerCompetitorResearchPort", "MetaAdLibraryBrokerClient"]

# H-follow-up (companion 0.2.25/0.2.26): el dispatcher del bróker exige
# `connection_id` para resolver la credencial de produccion aunque la
# conexion de Composio este ACTIVA -- mismo criterio que `_scope_id` en
# `broker_reference_data_port.py`.
_NOT_CONNECTED_BROKER_ERROR_CODES = frozenset(
    {"CREDENTIAL_NOT_CONNECTED", "PLATFORM_APP_NOT_CONFIGURED"}
)
# fix/ad-library-identity-reason: unico codigo del bróker que se traduce a
# `IDENTITY_CONFIRMATION_REQUIRED` -- todos los demas (incluido cualquier
# codigo futuro que este puerto todavia no conozca) siguen cayendo en
# `PROVIDER_ERROR`, nunca una suposicion.
_IDENTITY_REQUIRED_BROKER_ERROR_CODE = "META_AD_LIBRARY_IDENTITY_REQUIRED"
# fix/broker-ad-library-typeerror: the bróker is a trust boundary, not a
# guarantee -- `_row_to_ad` shape-checks its `start_date`/`stop_date`
# before ever calling `date.fromisoformat`, so a malformed value never
# raises past this port (`competitor_tools.py` only catches
# `MetaAdLibraryUnavailableError`) with the offending string in the
# exception message.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class MetaAdLibraryBrokerClient(BrokerSocketClient):
    def __init__(self, socket_path: Path) -> None:
        super().__init__(socket_path)

    async def search_ads_archive(
        self,
        *,
        business_id: str,
        connection_id: str | None,
        external_account_id: str,
        country: str,
        search_terms: str | None,
        search_page_ids: str | None,
        active_only: bool,
    ) -> list[dict[str, Any]]:
        result = await self._request(
            {
                "op": "meta_ads_archive",
                "business_id": business_id,
                "connection_id": connection_id,
                "external_account_id": external_account_id,
                "country": country,
                "search_terms": search_terms,
                "search_page_ids": search_page_ids,
                "active_status": "ACTIVE" if active_only else "ALL",
            },
            # Biblioteca de Anuncios (R7): lectura publica sin cuenta
            # conectada, mismo arranque en frio que el resto de lecturas de
            # `BrokerSocketClient`.
            retryable=True,
        )
        return list(result["ads"])


class BrokerCompetitorResearchPort:
    def __init__(
        self,
        client: MetaAdLibraryBrokerClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._client = client
        self._session_factory = session_factory

    async def search_meta_ads(
        self,
        business_id: str,
        *,
        query: str | None,
        page_id: str | None,
        domain: str | None,
        country: str,
        active_only: bool,
    ) -> tuple[CompetitorAd, ...]:
        account = await _resolve_active_meta_account(self._session_factory, business_id)
        if account is None:
            raise MetaAdLibraryUnavailableError(MetaAdLibraryUnavailableReason.NOT_CONNECTED)
        try:
            raw_ads = await self._client.search_ads_archive(
                business_id=business_id,
                connection_id=account.connection_id,
                external_account_id=account.external_account_id,
                country=country,
                search_terms=domain or query,
                search_page_ids=page_id,
                active_only=active_only,
            )
        except BrokerRequestDeniedError as exc:
            raise MetaAdLibraryUnavailableError(_reason_for_denial(exc.error_code)) from exc
        except BrokerConnectionError as exc:
            raise MetaAdLibraryUnavailableError(
                MetaAdLibraryUnavailableReason.PROVIDER_ERROR
            ) from exc
        return tuple(_row_to_ad(row) for row in raw_ads)


@dataclass(frozen=True, slots=True)
class _ActiveMetaAccount:
    external_account_id: str
    connection_id: str | None


async def _resolve_active_meta_account(
    session_factory: async_sessionmaker[AsyncSession], business_id: str
) -> _ActiveMetaAccount | None:
    async with session_factory() as session:
        account = await SqlAccountRepository(session).find_first_active(
            BusinessId.parse(business_id), PlatformCode.META
        )
    if account is None:
        return None
    connection_id = account.account_ref.connection_id
    return _ActiveMetaAccount(
        external_account_id=account.account_ref.external_account_id,
        connection_id=str(connection_id) if connection_id is not None else None,
    )


def _reason_for_denial(error_code: str) -> MetaAdLibraryUnavailableReason:
    if error_code in _NOT_CONNECTED_BROKER_ERROR_CODES:
        return MetaAdLibraryUnavailableReason.NOT_CONNECTED
    if error_code == _IDENTITY_REQUIRED_BROKER_ERROR_CODE:
        return MetaAdLibraryUnavailableReason.IDENTITY_CONFIRMATION_REQUIRED
    return MetaAdLibraryUnavailableReason.PROVIDER_ERROR


def _row_to_ad(row: dict[str, Any]) -> CompetitorAd:
    return CompetitorAd(
        advertiser_name=str(row["advertiser_name"]),
        ad_text=row.get("ad_text"),
        image_url=row.get("image_url"),
        start_date=_parse_optional_date(row.get("start_date")),
        stop_date=_parse_optional_date(row.get("stop_date")),
        platforms=tuple(row.get("platforms") or ()),
        reach_by_country=row.get("reach_by_country"),
    )


def _parse_optional_date(value: Any) -> date | None:
    """`meta_ad_library.py::map_ads_archive_row` sends ISO `YYYY-MM-DD`
    strings over the wire (the broker response is JSON, no native date
    type) -- parse it back here, the one place this port turns the broker's
    wire shape into the domain's `CompetitorAd`. The bróker is a trust
    boundary: a non-string or non-ISO-shaped value (or a shape-matching but
    invalid calendar date, e.g. `2026-13-99`) returns `None` instead of
    raising `date.fromisoformat`'s `ValueError` -- that would carry the raw
    string in its message straight out to the MCP transport, since neither
    this function nor `_row_to_ad`'s caller here is wrapped for it."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
