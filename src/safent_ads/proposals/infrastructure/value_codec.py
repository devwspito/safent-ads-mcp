"""Codec de los valores de un `ProposedDiff` hacia/desde JSONB.

`ProposedDiff.before`/`after` son `object`: hoy `Money` (presupuesto, puja) o
un escalar JSON (`status`, una lista de negativas). La columna es JSONB, asi
que hace falta un envoltorio *autodescriptivo*: sin el, `Money.of("100")`
vuelve de la base como `dict` y `money_pair_from_diff` (guardrails.py) lo
trata como `Money.zero()` -- el guardarraíl evaluaria un cambio de 0 a 0 y
dejaria pasar cualquier subida de gasto.

El envoltorio no toca el `diff_hash`: ese se calcula sobre el JSON canonico
de `diff_hash.py` (`Money.to_canonical()`), nunca sobre esta representacion
de almacenamiento."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from safent_ads.proposals.domain.campaign_creation import creation_budget
from safent_ads.proposals.domain.money import Money

_MONEY = "money"
_JSON = "json"


class ValueCodecError(ValueError):
    """El JSONB almacenado no respeta el envoltorio de `encode_value`."""


def encode_value(value: object) -> str:
    """Serializa a texto JSON listo para `CAST(:param AS JSONB)`."""
    if isinstance(value, Money):
        payload: dict[str, Any] = {
            "type": _MONEY,
            "amount": str(value.amount),
            "currency": value.currency,
        }
    else:
        payload = {"type": _JSON, "value": _to_jsonable(value)}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def decode_value(raw: str | None) -> object:
    """Inverso exacto de `encode_value`. `None` (columna nullable vacia) se
    devuelve tal cual: `applied_value` no existe hasta que la escritura se
    confirma."""
    if raw is None:
        return None
    payload = json.loads(raw)
    if not isinstance(payload, dict) or "type" not in payload:
        raise ValueCodecError(f"valor JSONB sin envoltorio reconocible: {raw!r}")
    kind = payload["type"]
    if kind == _MONEY:
        return _decode_money(payload, raw)
    if kind == _JSON:
        return payload.get("value")
    raise ValueCodecError(f"tipo de valor desconocido: {kind!r}")


def _decode_money(payload: dict[str, Any], raw: str) -> Money:
    try:
        return Money(amount=Decimal(str(payload["amount"])), currency=str(payload["currency"]))
    except (KeyError, InvalidOperation) as exc:
        raise ValueCodecError(f"importe invalido en {raw!r}") from exc


def _to_jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


_MINOR_UNITS_PER_UNIT = 100


def money_to_minor(value: object) -> int:
    """Unidades menores enteras para `spend_ledger` (la suma del tope es
    exacta y no depende del redondeo de cada plataforma). Un valor no
    monetario no consume presupuesto: 0."""
    if isinstance(value, dict) and "creation_plan" in value:
        value = creation_budget(value)
    if not isinstance(value, Money):
        return 0
    return int((value.amount * _MINOR_UNITS_PER_UNIT).to_integral_value())


def money_from_minor(minor_units: int, currency: str) -> Money:
    """Inverso exacto de `money_to_minor`: lo que `spend_ledger` guarda en
    unidades menores vuelve a ser `Money` para el evaluador de
    guardarrailes. El factor vive en este modulo y solo en el, para que
    ninguna consulta lo repita a mano."""
    return Money(amount=Decimal(minor_units) / Decimal(_MINOR_UNITS_PER_UNIT), currency=currency)
