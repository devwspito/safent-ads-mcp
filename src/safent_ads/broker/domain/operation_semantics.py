"""An operation must mean exactly the parameter/value transition being signed."""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from safent_ads.accounts.application.ports import WriteIntent, WriteOperation
from safent_ads.accounts.domain.json_value import JsonValue
from safent_ads.mcp.domain.native_write_payload import (
    NativeWritePayloadError,
    validate_native_write_payload,
)
from safent_ads.proposals.domain.ad_child_creation import (
    AdChildCreationError,
    child_parameter,
    validate_child_diff,
)
from safent_ads.proposals.domain.campaign_creation import CampaignCreationError, creation_budget
from safent_ads.shared.ids import EntityLevel

_BUDGETS = frozenset({"budget", "daily_budget", "lifetime_budget"})
_CURRENCY_CODE_LENGTH = 3
# 004 tasks-2.md W3: `propose_native_write` firma `parameter =
# f"native:{platform}:{operation}"` -- mismo prefijo que
# `execution/infrastructure/broker_platform.py::_NATIVE_WRITE_PARAMETER_PREFIX`.
_NATIVE_WRITE_PARAMETER_PREFIX = "native:"
_CREATIVE_PUBLICATION_KEYS = frozenset({"creative_asset_ids", "ad_copy"})
_BY_PARAMETER = {
    "bid_target": WriteOperation.SET_BID_TARGET,
    "targeting": WriteOperation.SET_TARGETING,
    "negative_keywords": WriteOperation.ADD_NEGATIVE_KEYWORD,
    "creative": WriteOperation.ROTATE_OUT_CREATIVE,
}
DEFENSIVE_OPERATIONS = frozenset(
    {
        WriteOperation.LOWER_BUDGET,
        WriteOperation.PAUSE,
        WriteOperation.ADD_NEGATIVE_KEYWORD,
        WriteOperation.ROTATE_OUT_CREATIVE,
    }
)


def matches_signed_transition(intent: WriteIntent) -> bool:  # noqa: PLR0911 - explicit fail-closed operation cases
    if child_parameter(intent.parametro):
        try:
            plan = validate_child_diff(
                intent.parametro,
                intent.valor_actual,
                intent.valor_propuesto,
                intent.entity_ref,
                intent.expected_state_hash,
            )
        except AdChildCreationError:
            return False
        return intent.operation == (
            WriteOperation.CREATE_AD_SET if plan["kind"] == "ad_set" else WriteOperation.CREATE_AD
        )
    if intent.parametro.startswith("new_campaign:"):
        if (
            intent.operation != WriteOperation.CREATE_CAMPAIGN
            or intent.valor_actual is not None
            or intent.expected_state_hash
        ):
            return False
        try:
            creation_budget(intent.valor_propuesto, intent.entity_ref)
        except (CampaignCreationError, TypeError):
            return False
        return True
    if intent.parametro.startswith(_NATIVE_WRITE_PARAMETER_PREFIX):
        return _native_write_matches(intent)
    if intent.valor_actual == intent.valor_propuesto:
        return False
    if intent.parametro in _BUDGETS:
        return _budget_matches(intent)
    if intent.parametro == "status":
        after = str(intent.valor_propuesto).upper()
        expected = {
            "PAUSED": WriteOperation.PAUSE,
            "ACTIVE": WriteOperation.RESUME,
            "ENABLED": WriteOperation.RESUME,
            # Vocabulario canonico de dominio, no el de ninguna plataforma
            # (Meta escribe `DELETED`, Google `REMOVED` -- ver
            # `_MUTATE_FIELDS_BY_OPERATION`/`_STATUS_BY_OPERATION`, que
            # ignoran `valor_propuesto` para operaciones de status igual
            # que ya hacen PAUSE/RESUME): un solo valor firmado por diseno.
            "DELETED": WriteOperation.DELETE,
        }.get(after)
        return expected is not None and intent.operation == expected
    if intent.parametro == "creative":
        if intent.entity_ref.level == EntityLevel.AD:
            return (
                str(intent.valor_propuesto).upper() == "PAUSED"
                and intent.operation == WriteOperation.ROTATE_OUT_CREATIVE
            )
        # H-1 (004 tasks-2.md §1, composition/mcp_write_adapter.py::
        # _CREATIVE_PARAMETER): `propose_creative_publication` firma el
        # MISMO parametro a nivel de ad set, con el payload de
        # `creative_asset_ids`/`ad_copy` -- sin esta rama, una propuesta de
        # publicacion aprobada y firmada nunca pasaba de aqui: el bróker
        # real la rechazaba en silencio un nivel mas adentro que el
        # `UnsupportedWriteParameterError` que motivo H-1.
        return (
            intent.entity_ref.level == EntityLevel.AD_SET
            and intent.valor_actual is None
            and _is_creative_publication_payload(intent.valor_propuesto)
            and intent.operation == WriteOperation.ROTATE_OUT_CREATIVE
        )
    return _BY_PARAMETER.get(intent.parametro) == intent.operation


def _native_write_matches(intent: WriteIntent) -> bool:
    """A-3: la lista negra de presupuesto/puja/estado/token ya se aplico en
    dominio puro (mcp/domain/native_write_payload.py) antes de firmar, pero
    eso ocurre en el borde MCP -- el bróker (otro proceso) vuelve a
    aplicarla AQUI, antes de conceder que la transicion firmada es la que
    dice ser, en vez de fiarse de que nadie firmo nunca un payload que no
    debia."""
    if intent.operation != WriteOperation.NATIVE_WRITE:
        return False
    if not isinstance(intent.valor_propuesto, Mapping):
        return False
    try:
        validate_native_write_payload(intent.valor_propuesto)
    except NativeWritePayloadError:
        return False
    return True


def _is_creative_publication_payload(value: JsonValue) -> bool:
    return isinstance(value, Mapping) and _CREATIVE_PUBLICATION_KEYS.issubset(value.keys())


def _amount(value: JsonValue) -> Decimal | None:
    if not isinstance(value, Mapping) or not isinstance(value.get("amount"), str):
        return None
    try:
        amount = Decimal(str(value["amount"]))
        if (
            not amount.is_finite()
            or amount < 0
            or amount * 100 != (amount * 100).to_integral_value()
        ):
            return None
        return amount
    except InvalidOperation:
        return None


def _budget_matches(intent: WriteIntent) -> bool:
    before, after = _amount(intent.valor_actual), _amount(intent.valor_propuesto)
    if before is None or after is None or before == after:
        return False
    if not isinstance(intent.valor_actual, Mapping) or not isinstance(
        intent.valor_propuesto, Mapping
    ):
        return False
    currency = intent.valor_actual.get("currency")
    if (
        not isinstance(currency, str)
        or len(currency) != _CURRENCY_CODE_LENGTH
        or intent.valor_propuesto.get("currency") != currency
    ):
        return False
    expected = WriteOperation.RAISE_BUDGET if after > before else WriteOperation.LOWER_BUDGET
    return intent.operation == expected
