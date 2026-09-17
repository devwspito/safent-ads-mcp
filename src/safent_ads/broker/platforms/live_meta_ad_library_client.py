"""`LiveMetaAdLibraryClient`: unica implementacion de produccion de
`MetaAdLibraryClient` (`meta_ad_library.py`) sobre `GET /ads_archive`.

A diferencia de `LiveMetaGraphClient` (una cuenta publicitaria conectada
por llamada), la Biblioteca de Anuncios es transparencia publica: no
cuelga de ninguna cuenta ni requiere el token de sistema de un cliente. El
unico token que necesita es el de la APP de Meta ya conectada por el
propietario (`app_id`/`app_secret`, el mismo par que `LiveMetaGraphClient`
recibe) -- ningun secreto nuevo. `search_ads_archive` acepta
`external_account_id` solo para cumplir el `Protocol` (`meta_ad_library.py`)
que tambien implementa `ComposioMetaAdLibraryClient` (fix/ad-library-over-
composio): esta implementacion nativa lo ignora, nunca lo necesita."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from facebook_business.api import FacebookAdsApi
from facebook_business.session import FacebookSession

from safent_ads.broker.platforms.errors import CredentialNotConnectedError

_GRAPH_API_VERSION = "v26.0"
_ADS_ARCHIVE_NODE = "ads_archive"


class LiveMetaAdLibraryClient:
    def __init__(self, *, app_id: str, app_secret: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret

    def search_ads_archive(
        self,
        *,
        country: str,
        search_terms: str | None,
        search_page_ids: str | None,
        active_status: str,
        fields: Sequence[str],
        limit: int,
        external_account_id: str | None = None,  # noqa: ARG002 - solo para el Protocol, ver docstring
    ) -> Sequence[Mapping[str, Any]]:
        if not self._app_id or not self._app_secret:
            # Mismo criterio que `ComposioMetaGraphClient._api_for`: modo
            # gestionado sin app nativa de Meta configurada -- fail closed,
            # nunca un token de app vacio contra el Graph API real.
            raise CredentialNotConnectedError("meta_native_app_not_configured")
        params: dict[str, Any] = {
            "ad_reached_countries": [country],
            "ad_active_status": active_status,
            "fields": ",".join(fields),
            "limit": limit,
        }
        if search_terms:
            params["search_terms"] = search_terms
        if search_page_ids:
            params["search_page_ids"] = [search_page_ids]
        body = self._api().call("GET", [_ADS_ARCHIVE_NODE], params=params).json()
        data = body.get("data")
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError("respuesta Meta sin lista data valida")
        return data

    def _api(self) -> FacebookAdsApi:
        # Token de APP (`app_id|app_secret`), nunca el token de sistema de
        # ninguna cuenta de cliente -- la Biblioteca de Anuncios es
        # transparencia publica, ver docstring del modulo.
        session = FacebookSession(
            app_id=self._app_id,
            app_secret=self._app_secret,
            access_token=f"{self._app_id}|{self._app_secret}",
        )
        return FacebookAdsApi(session, api_version=_GRAPH_API_VERSION)
