"""`BrokerSocketClient`: implementa `AdsPlatformPort` serializando la
llamada sobre `$ADS_BROKER_SOCKET` (contracts/platform-port.md). Vive en
`ads-api`/`ads-worker`; no conoce ningun SDK de plataforma ni credenciales.

El framing (uint32 big-endian + JSON) se repite aqui en vez de importarlo
de `broker/presentation/` a proposito: ese paquete es del proceso
`ads-broker` y no debe aparecer en la ruta de importacion de `ads-api`/
`ads-worker` (contracts/platform-port.md: "Los procesos api/worker no
tienen ese codigo en su ruta de importacion"). Son ~15 lineas; duplicarlas
es mas barato que acoplar los dos procesos por un modulo compartido."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.application.ports import (
    AccountRef,
    AdEntitySnapshot,
    AdsPlatformPort,
    AssetUploadRequest,
    EntityStateSnapshot,
    IdempotencyKey,
    MetricFactSnapshot,
    MetricsRequest,
    PlatformAssetHandle,
    SignedAuthorization,
    WriteIntent,
    WriteOutcome,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.budget import Budget, BudgetKind
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.money import Money
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.shared.ids import EntityRef

_LENGTH_PREFIX_FORMAT: Final = ">I"
_LENGTH_PREFIX_SIZE: Final = struct.calcsize(_LENGTH_PREFIX_FORMAT)
_DEFAULT_MAX_FRAME_BYTES: Final = 512 * 1024
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0


class BrokerSocketClient(AdsPlatformPort):
    def __init__(
        self,
        socket_path: Path,
        *,
        max_frame_bytes: int = _DEFAULT_MAX_FRAME_BYTES,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._socket_path = socket_path
        self._max_frame_bytes = max_frame_bytes
        self._timeout_seconds = timeout_seconds

    async def fetch_account_inventory(self, account_ref: AccountRef) -> Sequence[AdEntitySnapshot]:
        result = await self._request(
            {
                "op": "fetch_account_inventory",
                "business_id": str(account_ref.business_id) if account_ref.business_id else None,
                "connection_id": str(account_ref.connection_id)
                if account_ref.connection_id
                else None,
                "platform": account_ref.platform.value,
                "external_account_id": account_ref.external_account_id,
            },
            retryable=True,
        )
        return [_parse_ad_entity_snapshot(item) for item in result]

    async def fetch_metrics(self, request: MetricsRequest) -> Sequence[MetricFactSnapshot]:
        result = await self._request(
            {
                "op": "fetch_metrics",
                "business_id": str(request.account_ref.business_id)
                if request.account_ref.business_id
                else None,
                "connection_id": str(request.account_ref.connection_id)
                if request.account_ref.connection_id
                else None,
                "platform": request.account_ref.platform.value,
                "external_account_id": request.account_ref.external_account_id,
                "window_start": request.window.start.isoformat(),
                "window_end": request.window.end.isoformat(),
                "granularity": request.granularity.value,
                "entity_refs": (
                    [str(ref) for ref in request.entity_refs] if request.entity_refs else None
                ),
            },
            retryable=True,
        )
        return [_parse_metric_fact_snapshot(item) for item in result]

    async def read_entity_state(self, entity_ref: EntityRef) -> EntityStateSnapshot:
        result = await self._request(
            {"op": "read_entity_state", "entity_ref": str(entity_ref)}, retryable=True
        )
        return _parse_entity_state_snapshot(result)

    async def run_gaql(
        self, account_ref: AccountRef, query: str, *, max_rows: int
    ) -> Sequence[Mapping[str, Any]]:
        result = await self._request(
            {
                "op": "run_gaql",
                "business_id": str(account_ref.business_id) if account_ref.business_id else None,
                "connection_id": str(account_ref.connection_id)
                if account_ref.connection_id
                else None,
                "platform": account_ref.platform.value,
                "external_account_id": account_ref.external_account_id,
                "query": query,
                "max_rows": max_rows,
            },
            retryable=True,
        )
        return list(result["rows"])

    async def upload_asset(self, request: AssetUploadRequest) -> PlatformAssetHandle:
        """`op: upload_asset` (003-paquete-de-campana BL-6): serializa
        `AssetUploadRequest` sobre el socket -- `media` viaja en base64
        porque JSON no lleva bytes crudos. H1 (revision de seguridad 0.2.23):
        `width`/`height`/`package_binding`/`package_approval` viajan
        OMITIDOS -- nunca `null` -- cuando `request` no es un paso de
        paquete, mismo criterio que `_package_binding_claim` de
        `execute_write`."""
        result = await self._request(
            {
                "op": "upload_asset",
                "business_id": str(request.account_ref.business_id)
                if request.account_ref.business_id
                else None,
                "connection_id": str(request.account_ref.connection_id)
                if request.account_ref.connection_id
                else None,
                "platform": request.account_ref.platform.value,
                "external_account_id": request.account_ref.external_account_id,
                "file_name": request.file_name,
                "mime_type": request.mime_type,
                "media_base64": base64.b64encode(request.media).decode("ascii"),
                **_upload_dimensions_claim(request),
                **_upload_package_claim(request),
            }
        )
        return _parse_platform_asset_handle(result)

    async def execute_write(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome:
        """Serializa `WriteIntent`+`SignedAuthorization`+`idempotency_key`
        sobre el wire (contracts/platform-port.md `execute_write`) y
        devuelve el `WriteOutcome` tipado tal cual lo decidio el broker.
        `DENIED`/`SKIPPED_DRIFT`/`BLOCKED_HARD_CAP`/`FAILED` viajan como
        veredictos `ok: true` -- nunca una excepcion (esa via es solo para
        un fallo realmente inesperado del broker, `_unwrap`)."""
        result = await self._request(
            {
                "op": "execute_write",
                **_package_binding_claim(intent),
                **(
                    {"managed_binding": intent.managed_binding.as_claims()}
                    if intent.managed_binding
                    else {}
                ),
                "entity_ref": str(intent.entity_ref),
                "operation": intent.operation.value,
                "parametro": intent.parametro,
                "valor_actual": intent.valor_actual,
                "valor_propuesto": intent.valor_propuesto,
                "diff_hash": intent.diff_hash,
                "expected_state_hash": intent.expected_state_hash,
                "business_id": intent.business_id,
                "authorization": _serialize_authorization(authorization),
                "idempotency_key": str(idempotency_key),
            }
        )
        return _parse_write_outcome(result)

    async def read_write_receipt(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome | None:
        result = await self._request(
            {
                "op": "read_write_receipt",
                **_package_binding_claim(intent),
                **(
                    {"managed_binding": intent.managed_binding.as_claims()}
                    if intent.managed_binding
                    else {}
                ),
                "entity_ref": str(intent.entity_ref),
                "business_id": intent.business_id,
                "operation": intent.operation.value,
                "parametro": intent.parametro,
                "valor_actual": intent.valor_actual,
                "valor_propuesto": intent.valor_propuesto,
                "diff_hash": intent.diff_hash,
                "expected_state_hash": intent.expected_state_hash,
                "authorization": _serialize_authorization(authorization),
                "idempotency_key": str(idempotency_key),
            }
        )
        return None if result is None else _parse_write_outcome(result)

    async def _request(self, payload: dict[str, Any], *, retryable: bool = False) -> Any:  # noqa: ANN401 - JSON heterogeneo
        """`retryable=True` (lecturas idempotentes: `fetch_account_inventory`/
        `fetch_metrics`/`read_entity_state`/`run_gaql`, y los `op` de solo
        lectura de `mcp/infrastructure/broker_reference_data_port.py`) repite
        UNA vez, con una conexion nueva, cuando `_request_once` falla por
        `BrokerConnectionError` -- nunca cuando el broker ya respondio (una
        `BrokerRequestDeniedError` no pasa por aqui, `_unwrap` solo se llama
        tras una trama completa). Cubre el primer arranque en frio tras
        `docker compose up -d`: la primera lectura de cada plataforma
        construye en el bróker el adaptador/cliente SDK todavia sin usar
        (`DynamicPlatformAdapterRegistry`, cacheado despues) y puede tardar
        mas que `timeout_seconds`; el bróker sigue trabajando aunque el
        cliente ya haya cerrado el socket (`BrokenPipeError` en su log), y
        sin reintento esa primera lectura devolvia `BROKER_UNAVAILABLE` en
        vez de dejar que la segunda conexion, ya en caliente, respondiera.
        Nunca para `upload_asset`/`execute_write`/`read_write_receipt`: no
        son idempotentes de la misma forma (crean o firman algo del lado del
        proveedor), asi que ninguno pasa `retryable=True`."""
        try:
            return await self._request_once(payload)
        except BrokerConnectionError:
            if not retryable:
                raise
            return await self._request_once(payload)

    async def _request_once(self, payload: dict[str, Any]) -> Any:  # noqa: ANN401 - JSON heterogeneo
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._socket_path)), timeout=self._timeout_seconds
            )
        except (OSError, TimeoutError) as exc:
            raise BrokerConnectionError(f"no se pudo conectar a {self._socket_path}") from exc

        try:
            await self._write_frame(writer, json.dumps(payload).encode("utf-8"))
            raw_response = await asyncio.wait_for(
                self._read_frame(reader), timeout=self._timeout_seconds
            )
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise BrokerConnectionError("fallo de E/S hablando con el broker") from exc
        finally:
            writer.close()
            await writer.wait_closed()

        return self._unwrap(json.loads(raw_response))

    def _unwrap(self, response: dict[str, Any]) -> Any:  # noqa: ANN401 - JSON heterogeneo
        if not response.get("ok"):
            raise BrokerRequestDeniedError(
                str(response.get("error_code", "UNKNOWN")), response.get("reason")
            )
        return response["result"]

    async def _write_frame(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        if len(payload) > self._max_frame_bytes:
            raise BrokerConnectionError("peticion mayor que el limite de trama")
        writer.write(struct.pack(_LENGTH_PREFIX_FORMAT, len(payload)) + payload)
        await writer.drain()

    async def _read_frame(self, reader: asyncio.StreamReader) -> bytes:
        header = await reader.readexactly(_LENGTH_PREFIX_SIZE)
        (length,) = struct.unpack(_LENGTH_PREFIX_FORMAT, header)
        if length > self._max_frame_bytes:
            raise BrokerConnectionError("respuesta mayor que el limite de trama")
        return await reader.readexactly(length)


def _upload_dimensions_claim(request: AssetUploadRequest) -> dict[str, Any]:
    """H1 (revision de seguridad 0.2.23): `width`/`height` solo viajan
    cuando el llamante los conoce (paso `UPLOAD_CREATIVE` de un paquete) --
    un `upload_creative_asset` independiente los deja fuera del payload."""
    if request.width is None or request.height is None:
        return {}
    return {"width": request.width, "height": request.height}


def _upload_package_claim(request: AssetUploadRequest) -> dict[str, Any]:
    """H1 (revision de seguridad 0.2.23): forma exacta de `_package_binding_
    claim`/`_serialize_authorization` de mas abajo -- omitido, nunca `null`,
    para una subida sin paquete."""
    claim: dict[str, Any] = {}
    if request.package_binding is not None:
        claim["package_binding"] = dict(request.package_binding)
    if request.package_approval is not None:
        claim["package_approval"] = dict(request.package_approval)
    return claim


def _package_binding_claim(intent: WriteIntent) -> dict[str, Any]:
    """`003-paquete-de-campana` contracts/api.md §R2.E (BL-2/BL-3):
    obligatorio en el cable si y solo si `authorization.kind ==
    "package_step"` (R1) -- omitido, nunca `null`, para los otros dos
    `kind`, igual que `managed_binding`."""
    if intent.package_binding is None:
        return {}
    return {"package_binding": dict(intent.package_binding)}


def _serialize_authorization(authorization: SignedAuthorization) -> dict[str, Any]:
    return {
        **(
            {"managed_binding": authorization.managed_binding.as_claims()}
            if authorization.managed_binding
            else {}
        ),
        **(
            {"package_approval": dict(authorization.package_approval)}
            if authorization.package_approval is not None
            else {}
        ),
        "authorization_id": authorization.authorization_id,
        "proposal_id": authorization.proposal_id,
        "kind": authorization.kind,
        "diff_hash": authorization.diff_hash,
        "guardrail_verdict_hash": authorization.guardrail_verdict_hash,
        "issued_by": authorization.issued_by,
        "expires_at": authorization.expires_at.isoformat(),
        "signature": authorization.signature,
    }


def _parse_platform_asset_handle(raw: Mapping[str, Any]) -> PlatformAssetHandle:
    return PlatformAssetHandle(
        platform_asset_id=str(raw["platform_asset_id"]), preview_url=raw.get("preview_url")
    )


def _parse_write_outcome(raw: Mapping[str, Any]) -> WriteOutcome:
    return WriteOutcome(
        outcome=raw["outcome"],
        applied_value=raw["applied_value"],
        state_hash_after=raw["state_hash_after"],
        error_code=raw["error_code"],
        platform_request_id=raw["platform_request_id"],
    )


def _parse_money(raw: Mapping[str, Any] | None) -> Money | None:
    if raw is None:
        return None
    return Money(int(raw["minor_units"]), str(raw["currency"]))


def _parse_budget(raw: Mapping[str, Any] | None) -> Budget | None:
    if raw is None:
        return None
    amount = _parse_money(raw["amount"])
    assert amount is not None  # noqa: S101 - invariante del propio servidor: amount nunca falta
    return Budget(amount, BudgetKind(raw["kind"]))


def _parse_ad_entity_snapshot(raw: Mapping[str, Any]) -> AdEntitySnapshot:
    return AdEntitySnapshot(
        entity_ref=EntityRef.parse(raw["entity_ref"]),
        parent_ref=EntityRef.parse(raw["parent_ref"]),
        name=raw["name"],
        status=AdEntityStatus(raw["status"]),
        is_controllable=raw["is_controllable"],
        learning_state=LearningState(raw["learning_state"]),
        budget=_parse_budget(raw.get("budget")),
        bid_target=_parse_money(raw.get("bid_target")),
        shared_budget_ref=(
            EntityRef.parse(raw["shared_budget_ref"]) if raw.get("shared_budget_ref") else None
        ),
        canonical_state=raw["canonical_state"],
        fetched_at=_parse_datetime(raw["fetched_at"]),
    )


def _parse_entity_state_snapshot(raw: Mapping[str, Any]) -> EntityStateSnapshot:
    return EntityStateSnapshot(
        entity_ref=EntityRef.parse(raw["entity_ref"]),
        status=AdEntityStatus(raw["status"]),
        is_controllable=raw["is_controllable"],
        canonical_state=raw["canonical_state"],
        fetched_at=_parse_datetime(raw["fetched_at"]),
    )


def _parse_metric_fact_snapshot(raw: Mapping[str, Any]) -> MetricFactSnapshot:
    spend = _parse_money(raw["spend"])
    assert spend is not None  # noqa: S101 - invariante del propio servidor: spend nunca falta
    return MetricFactSnapshot(
        entity_ref=EntityRef.parse(raw["entity_ref"]),
        stat_date=_parse_date(raw["stat_date"]),
        stat_hour=raw["stat_hour"],
        currency=raw["currency"],
        spend=spend,
        impressions=raw["impressions"],
        clicks=raw["clicks"],
        reach=raw["reach"],
        frequency=raw["frequency"],
        conversions_by_kind=raw["conversions_by_kind"],
        conversion_value=_parse_money(raw.get("conversion_value")),
        video_views_3s=raw["video_views_3s"],
        video_views_75pct=raw["video_views_75pct"],
        search_lost_is_budget=raw["search_lost_is_budget"],
        search_lost_is_rank=raw["search_lost_is_rank"],
    )


def _parse_datetime(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw)
