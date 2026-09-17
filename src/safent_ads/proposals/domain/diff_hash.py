"""`diff_hash`: SHA-256 determinista sobre el JSON canonico del cambio
propuesto (data-model.md invariante 1 de `PropuestaDeAccion`: "el diff_hash
se recalcula desde el payload vivo, nunca se confia en el almacenado").

Puerto de `oposads/hitl/domain/propuesta.py::compute_diff_hash`, reescrito
para trabajar sobre `EntityRef` + `Money`-aware en vez de ids sueltos."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import TYPE_CHECKING

from safent_ads.proposals.domain.money import Money

if TYPE_CHECKING:
    from safent_ads.shared.ids import EntityRef
    from safent_ads.shared.managed_ads import ManagedAdsBinding


def compute_diff_hash(
    entity_ref: EntityRef,
    parameter: str,
    before: object,
    after: object,
    managed_binding: ManagedAdsBinding | None = None,
) -> str:
    """SHA-256 sobre `{entity_ref, parameter, before, after}` en JSON canonico
    (claves ordenadas, sin espacios). Recomputable desde cero por cualquier
    parte que tenga el payload vivo — nunca se confia en un hash almacenado."""
    payload = {
        "entity_ref": str(entity_ref),
        "parameter": parameter,
        "before": to_jsonable(before),
        "after": to_jsonable(after),
    }
    if managed_binding is not None:
        managed_binding.validate_entity(entity_ref)
        payload["managed_binding"] = managed_binding.as_claims()
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def canonical_json_bytes(payload: dict[str, object]) -> bytes:
    """JSON canonico reutilizado para firmar/verificar (`authorization.py`)."""
    return json.dumps(
        {key: to_jsonable(value) for key, value in payload.items()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def to_jsonable(value: object) -> object:
    """Proyeccion canonica de un valor del diff a JSON. Publica porque el
    adaptador que arma el `WriteIntent` para el broker tiene que mandar
    EXACTAMENTE estos valores: el broker recomputa el `diff_hash` desde los
    campos vivos (contracts/platform-port.md, comprobacion 3) y cualquier
    otra representacion lo haria fallar."""
    if isinstance(value, Money):
        return value.to_canonical()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)
