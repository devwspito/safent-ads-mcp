"""`LiveMetaGraphClient`: unica implementacion de produccion de
`MetaGraphClient` (`meta_ads_adapter.py`) sobre `facebook-business`.
Resuelve el `access_token` de CLIENTE (system user, por cuenta publicitaria
conectada) contra `CredentialStorePort` -- nunca el `META_SYSTEM_USER_TOKEN`
de `BrokerSettings` (eso queda para `app_id`/`app_secret`, credenciales de
VENDOR compartidas por todo negocio).

Los nodos anidados usan `act_<cuenta>/<nodo>`; no se aceptan ids sin cuenta.
Cada llamada instancia su propia sesion SDK y comprueba la cuenta propietaria.
La paginacion usa cursores sobre el mismo endpoint, nunca URLs arbitrarias.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping, Sequence
from typing import Any

from facebook_business.api import FacebookAdsApi
from facebook_business.session import FacebookSession

from safent_ads.broker.application.ports import CredentialStorePort, PlatformCredential
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.broker.platforms.meta_scope import split_meta_scope
from safent_ads.broker.platforms.native_ad_child import meta_create, meta_prepare
from safent_ads.proposals.domain.campaign_creation import creation_budget
from safent_ads.shared.ids import PlatformCode

_GET = "GET"
_POST = "POST"
_GRAPH_API_VERSION = "v26.0"
# A-1: mismo tope que `mcp/domain/meta_graph_path.py::_MAX_ROWS` -- una
# unica pagina de `get_meta_graph` nunca pide mas de lo que ads-api va a
# quedarse de todos modos.
_NO_PAGINATE_LIMIT = 200


class LiveMetaGraphClient:
    def __init__(
        self, *, app_id: str, app_secret: str, credential_store: CredentialStorePort
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._credential_store = credential_store

    def get_node(self, node_id: str, fields: Sequence[str]) -> Mapping[str, Any]:
        api = self._api_for(node_id)
        account, node = split_meta_scope(node_id)
        requested = tuple(dict.fromkeys((*fields, "account_id"))) if node != account else fields
        response = api.call(_GET, [node], params={"fields": ",".join(requested)})
        result: Mapping[str, Any] = response.json()
        if node != account:
            self._check_owner(account, result)
        return result

    def get_edge(
        self,
        node_id: str,
        edge: str,
        fields: Sequence[str],
        params: Mapping[str, Any] | None = None,
        *,
        paginate: bool = True,
    ) -> Sequence[Mapping[str, Any]]:
        api = self._api_for(node_id)
        account, node = split_meta_scope(node_id)
        if node != account:
            self._check_owner(
                account, api.call(_GET, [node], params={"fields": "account_id"}).json()
            )
        # B-1: `fields` se aplica EL ULTIMO -- ninguna clave de `params`
        # puede pisar la lista blanca de campos, ni siquiera si el llamante
        # de mas arriba (bug o denegacion no aplicada) deja pasar una.
        request_params: dict[str, Any] = {**(params or {}), "fields": ",".join(fields)}
        if not paginate:
            # A-1 (R5, get_meta_graph): una unica pagina, tope fijado por el
            # servidor -- nunca sigue `paging.next`, `params` ya rechazo
            # `limit` (B-1) asi que esto es lo unico que decide el tamano.
            request_params["limit"] = _NO_PAGINATE_LIMIT
            body = api.call(_GET, [node, edge], params=request_params).json()
            return _graph_rows(body)
        rows: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        while True:
            body = api.call(_GET, [node, edge], params=request_params).json()
            rows.extend(_graph_rows(body))
            paging = body.get("paging", {})
            if not paging.get("next"):
                return rows
            cursor = paging.get("cursors", {}).get("after")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError("cursor de paginacion Meta ausente o repetido")
            seen.add(cursor)
            # Keep credentials on this edge, never follow arbitrary paging URLs.
            request_params["after"] = cursor

    def update_node(self, node_id: str, fields: Mapping[str, Any]) -> None:
        """Graph API uniforme: un `POST` de campos sobre cualquier nodo
        (campana, conjunto de anuncios, anuncio) lo actualiza -- a
        diferencia de Google no hace falta un servicio de mutacion por
        tipo de entidad."""
        api = self._api_for(node_id)
        account, node = split_meta_scope(node_id)
        if node == account:
            raise ValueError("actualizacion de cuenta Meta no permitida")
        self._check_owner(account, api.call(_GET, [node], params={"fields": "account_id"}).json())
        result = api.call(_POST, [node], params=dict(fields)).json()
        if result.get("success") is not True:
            raise ValueError("Meta no confirmo la actualizacion")

    def campaign_creation_currency(self, account_id: str) -> str:
        return str(self.get_node(account_id, ("currency",))["currency"])

    def prepare_child(self, parent: str, plan: Mapping[str, Any]) -> None:
        meta_prepare(self, parent, plan)

    def create_paused_child(self, parent: str, plan: Mapping[str, Any]) -> Mapping[str, Any]:
        return meta_create(self, self._api_for(parent), parent, plan)

    def create_paused_campaign(
        self, account_id: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        money = creation_budget(payload)
        account, node = split_meta_scope(account_id)
        if account != node:
            raise ValueError("campaign_creation_account_invalid")
        plan = payload["creation_plan"]
        fields = {
            "name": plan["name"],
            "status": "PAUSED",
            "daily_budget": int(money.amount * 100),
            **plan["native"],
        }
        api = self._api_for(account)
        # One request, no _call retry loop and no batch/partial operation.
        response = api.call(_POST, [account, "campaigns"], params=fields).json()
        campaign_id = response.get("id")
        if not isinstance(campaign_id, str) or not campaign_id.isdigit():
            raise ValueError("campaign_creation_partial_response")
        confirmed = api.call(
            _GET, [campaign_id], params={"fields": ",".join((*fields, "account_id"))}
        ).json()
        self._check_owner(account, confirmed)
        if any(
            str(confirmed.get(key)) != str(value)
            for key, value in fields.items()
            if key not in {"special_ad_categories", "special_ad_category_country"}
        ):
            raise ValueError("campaign_creation_confirmation_mismatch")
        categories = _confirmed_set(confirmed.get("special_ad_categories"))
        if categories == {"NONE"}:
            categories = set()
        if categories != set(fields["special_ad_categories"]):
            raise ValueError("campaign_creation_confirmation_mismatch")
        countries = confirmed.get("special_ad_category_country")
        if countries is None and not categories and not fields["special_ad_category_country"]:
            # Optional and inapplicable when the signed declaration is no
            # special category. Never infer missing countries for a category.
            countries = []
        if _confirmed_set(countries) != set(fields["special_ad_category_country"]):
            raise ValueError("campaign_creation_confirmation_mismatch")
        return {
            "campaign_resource": f"{account}/{campaign_id}",
            "status": "PAUSED",
            "daily_budget_minor": int(money.amount * 100),
        }

    def create_image(self, account_id: str, file_name: str, media: bytes) -> Mapping[str, Any]:
        """Deliverable 2 / BL-6: `POST /act_<id>/adimages` with the image
        inline as base64 (`bytes` field) instead of multipart (`files=`) --
        the same request shape works unchanged over both this native
        transport and the Composio JSON proxy (`ComposioMetaGraphClient`,
        `composio_sdk_clients.py::_MetaApiFacade`, which has no multipart
        support and only ever forwards a JSON body). `name` is the key Meta
        echoes the entry back under in `images` (same name in the request
        and the response, documented by the Graph API)."""
        account, node = split_meta_scope(account_id)
        if account != node:
            raise ValueError("creative_upload_account_invalid")
        api = self._api_for(account)
        encoded = base64.b64encode(media).decode("ascii")
        response = api.call(
            _POST, [account, "adimages"], params={"bytes": encoded, "name": file_name}
        ).json()
        images = response.get("images")
        if not isinstance(images, dict) or file_name not in images:
            raise ValueError("creative_upload_confirmation_missing")
        entry = images[file_name]
        image_hash = entry.get("hash")
        if not isinstance(image_hash, str) or not image_hash:
            raise ValueError("creative_upload_confirmation_missing")
        return {"hash": image_hash, "url": entry.get("url")}

    @staticmethod
    def _check_owner(account: str, body: Mapping[str, Any]) -> None:
        if f"act_{body.get('account_id')}" != account:
            raise CredentialNotConnectedError("la entidad Meta no pertenece a la cuenta solicitada")

    def _api_for(self, node_id: str) -> FacebookAdsApi:
        credential = self._resolve_credential(node_id)
        session = FacebookSession(
            app_id=self._app_id,
            app_secret=self._app_secret,
            access_token=credential.access_token,
        )
        return FacebookAdsApi(session, api_version=_GRAPH_API_VERSION)

    def _resolve_credential(self, node_id: str) -> PlatformCredential:
        account_id, _ = split_meta_scope(node_id)
        # `get_node`/`get_edge` son sincronos (protocolo `MetaGraphClient`,
        # llamados desde el hilo de `asyncio.to_thread` de `MetaAdsAdapter`):
        # sin bucle de eventos activo ahi, `asyncio.run` es seguro.
        credential = asyncio.run(
            self._credential_store.get_credential(PlatformCode.META, account_id)
        )
        if credential is None or not credential.access_token:
            raise CredentialNotConnectedError(
                f"sin credencial de cliente de Meta conectada para {account_id}"
            )
        return credential


def _graph_rows(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = body.get("data")
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError("respuesta Meta sin lista data valida")
    return data


def _confirmed_set(value: object) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("campaign_creation_confirmation_mismatch")
    return set(value)
