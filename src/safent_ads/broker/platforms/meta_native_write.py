"""`WriteOperation.NATIVE_WRITE` sobre Meta (004 tasks-2.md W3, historia 20):
el Graph API es uniforme (POST de campos sobre cualquier nodo), asi que
`propose_native_write` se traduce a `MetaGraphClient.update_node` sobre el
nodo ya resuelto y autorizado por `WriteAuthorizationPipeline` -- ningun
codigo nuevo por combinacion plataforma/operacion, a diferencia de Google
(`GoogleAdsAdapter`, sin mutacion generica: fail-closed via
`WriteDenialCode.OPERATION_NOT_SUPPORTED`, D-5).

`intent.valor_propuesto` ya es el `payload` validado por
`mcp/domain/native_write_payload.py` en el borde MCP (claves/profundidad/
tamano, sin presupuesto/puja/estado/token) -- este modulo no repite esa
validacion, solo la aplica: mismo criterio que `create_paused_child`, que
tampoco revalida el `child_plan` mas alla de lo que el dominio ya exige."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, cast

from safent_ads.accounts.application.ports import (
    EntityStateSnapshot,
    SignedAuthorization,
    WriteIntent,
    WriteOutcome,
)
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.shared.ids import EntityRef

__all__ = ["apply_native_write"]


async def apply_native_write(
    *,
    pipeline: WriteAuthorizationPipeline,
    intent: WriteIntent,
    authorization: SignedAuthorization,
    key: str,
    account: str,
    now: datetime,
    update_node: Callable[[str, Mapping[str, Any]], None],
    read_state: Callable[[EntityRef], Awaitable[EntityStateSnapshot]],
    consume_rate: Callable[[], bool],
) -> WriteOutcome:
    if not consume_rate():
        return WriteOutcome("DENIED", None, None, "rate_limited", None)
    replay = await pipeline.begin_admitted_write(key, intent, authorization, account, now)
    if replay is not None:
        return replay
    fields = cast(Mapping[str, Any], intent.valor_propuesto)
    try:
        update_node(intent.entity_ref.external_id, fields)
        confirmed = await read_state(intent.entity_ref)
        outcome = WriteOutcome(
            "SUCCEEDED",
            intent.valor_propuesto,
            PlatformStateHash.compute(confirmed.canonical_state).value,
            None,
            intent.entity_ref.external_id,
        )
    except Exception:  # noqa: BLE001 - el POST de Graph puede haberse aplicado igualmente
        outcome = WriteOutcome("UNKNOWN", None, None, "native_write_outcome_unknown", None)
    return pipeline.finalize(key, account, intent, outcome, now=now)
