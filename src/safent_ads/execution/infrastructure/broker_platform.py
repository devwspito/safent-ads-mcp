"""Adaptadores de `PlatformReaderPort` y `AdsPlatformWritePort` sobre
`AdsPlatformPort` (contracts/platform-port.md), es decir, sobre el socket del
broker (`accounts/infrastructure/broker_client.py`).

Estos dos son el unico punto de `execution` que habla con una plataforma
(plan.md §6, paso 5 y 6). No conocen ningun SDK: solo el puerto.

Denegar por defecto, literal (threat-model.md C-1): el broker devuelve un
`WriteOutcome` con veredicto, y **solo** `SUCCEEDED` se traduce a
`WriteResult`. `DENIED` — que es lo que responde hoy, mientras la ruta de
escritura del broker no exista — sale como `PlatformWriteDeniedError`, un
tipo propio; nunca como un exito con valores a medias. Un `SUCCEEDED` sin
estado remoto confirmado tampoco cuela: FR-21 exige verificar el estado real
tras aplicar, y sin ese hash no hay verificacion.

El `WriteIntent` se arma con los valores que el broker va a recomputar
(comprobacion 3 de contracts/platform-port.md): `valor_actual` y
`valor_propuesto` se proyectan con `proposals.domain.diff_hash.to_jsonable`,
la MISMA funcion con la que se calculo el `diff_hash` firmado. Cualquier otra
representacion haria fallar la recomputacion en el broker."""

from __future__ import annotations

from typing import Final, Literal, cast

from safent_ads.accounts.application.ports import (
    AdsPlatformPort,
    SignedAuthorization,
    WriteIntent,
    WriteOperation,
)
from safent_ads.accounts.domain.json_value import JsonValue
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.domain.refs import IdempotencyKey
from safent_ads.execution.application.ports import (
    WriteCommand,
    WriteResult,
)
from safent_ads.execution.infrastructure.errors import (
    ConfirmedPlatformWriteRejectedError,
    NoOpWriteCommandError,
    PlatformWriteDeniedError,
    PlatformWriteRejectedError,
    UnsupportedWriteParameterError,
    WriteContextMissingError,
)
from safent_ads.proposals.application.ports import ProposalRepository
from safent_ads.proposals.domain.ad_child_creation import child_parameter, validate_child_payload
from safent_ads.proposals.domain.authorization import Authorization, AuthorizationKind
from safent_ads.proposals.domain.campaign_creation import creation_budget
from safent_ads.proposals.domain.diff_hash import to_jsonable
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import Proposal
from safent_ads.shared.ids import EntityRef

__all__ = ["BrokerPlatformReader", "BrokerPlatformWriter"]

_AUTHORIZATION_KIND: Final[
    dict[AuthorizationKind, Literal["human_approval", "rule_authorization", "package_step"]]
] = {
    AuthorizationKind.HUMAN_APPROVAL: "human_approval",
    AuthorizationKind.RULE_AUTHORIZATION: "rule_authorization",
    # `003-paquete-de-campana` contracts/api.md §R2.E (BL-3): el bróker de
    # hoy sigue denegando `package_step` via `OWNER_APPROVAL_REQUIRED`
    # (`require_owner_approval=True`) -- admitirlo de verdad es T107/T108,
    # fuera de esta entrega. Este valor solo evita el `KeyError` que
    # `_signed()` lanzaria en cuanto `chokepoint_step_executor` firme una
    # `Authorization` de este `kind` y la ruta llegue al bróker real.
    AuthorizationKind.PACKAGE_STEP: "package_step",
}

# Parametros que el catalogo de acciones sabe escribir hoy. Uno que no este
# aqui no se traduce "a lo que mas se parezca": se rechaza (FR-41, palanca no
# controlable).
_OPERATION_BY_PARAMETER: Final[dict[str, WriteOperation]] = {
    "bid_target": WriteOperation.SET_BID_TARGET,
    "targeting": WriteOperation.SET_TARGETING,
    "negative_keywords": WriteOperation.ADD_NEGATIVE_KEYWORD,
    "creative": WriteOperation.ROTATE_OUT_CREATIVE,
}
_BUDGET_PARAMETERS: Final = frozenset({"daily_budget", "lifetime_budget", "budget"})
_STATUS_PARAMETER: Final = "status"
_PAUSED_VALUES: Final = frozenset({"PAUSED", "paused"})
_DELETED_VALUE: Final = "DELETED"
# 004 tasks-2.md W3: `propose_native_write` firma `parameter =
# f"native:{platform}:{operation}"` (mcp/domain/native_write_payload.py ya
# rechazo presupuesto/puja/estado/token en dominio puro) -- una unica
# operacion de puerto para cualquier plataforma/operacion nativa, nunca un
# `WriteOperation` por combinacion.
_NATIVE_WRITE_PARAMETER_PREFIX: Final = "native:"


class BrokerPlatformReader:
    """Implementa `PlatformReaderPort`. La politica del hash de estado es de
    `accounts` (`PlatformStateHash.compute`): aqui no se elige que campos
    cuentan para la deriva, solo se pide el estado y se calcula."""

    def __init__(self, platform: AdsPlatformPort) -> None:
        self._platform = platform

    async def fetch_state_hash(self, entity_ref: EntityRef) -> str:
        snapshot = await self._platform.read_entity_state(entity_ref)
        return PlatformStateHash.compute(snapshot.canonical_state).value


class BrokerPlatformWriter:
    """Implementa `AdsPlatformWritePort`. Necesita la propuesta que respalda
    la autorizacion solo por `expected_state_hash` (la precondicion de
    deriva del `WriteIntent`) -- el valor ANTERIOR y el NUEVO ya viajan los
    dos en `WriteCommand.before`/`.value`, el diff EFECTIVO que el
    chokepoint verifico y firmo; este adaptador nunca vuelve a leer
    `proposal.diff` para clasificar la operacion (BUG corregido)."""

    def __init__(self, platform: AdsPlatformPort, proposals: ProposalRepository) -> None:
        self._platform = platform
        self._proposals = proposals

    async def execute_write(
        self, command: WriteCommand, authorization: Authorization, idempotency_key: str
    ) -> WriteResult:
        _reject_noop(command)
        proposal_id = _require_proposal_id(authorization)
        proposal = await self._proposals.get(proposal_id)
        if proposal is None:
            raise WriteContextMissingError(
                f"la propuesta {proposal_id} no existe: no hay intento que firmar"
            )
        outcome = await self._platform.execute_write(
            _intent(command, proposal, authorization),
            _signed(authorization),
            IdempotencyKey(idempotency_key),
        )
        if outcome.outcome != "SUCCEEDED":
            raise _rejection(outcome.outcome, outcome.error_code)
        if outcome.state_hash_after is None:
            raise PlatformWriteRejectedError("SUCCEEDED", "missing_state_hash_after")
        return WriteResult(
            applied_value=outcome.applied_value,
            confirmed_state_hash=outcome.state_hash_after,
            created_external_id=outcome.platform_request_id,
        )

    async def read_receipt(
        self, command: WriteCommand, authorization: Authorization, idempotency_key: str
    ) -> WriteResult | None:
        proposal = await self._proposals.get(_require_proposal_id(authorization))
        if proposal is None:
            raise WriteContextMissingError("receipt_proposal_missing")
        outcome = await self._platform.read_write_receipt(
            _intent(command, proposal, authorization),
            _signed(authorization),
            IdempotencyKey(idempotency_key),
        )
        if outcome is None or outcome.outcome in {"UNKNOWN", "FAILED"}:
            return None
        if outcome.outcome in {"DENIED", "BLOCKED_HARD_CAP", "SKIPPED_DRIFT"}:
            raise _rejection(outcome.outcome, outcome.error_code)
        if outcome.outcome != "SUCCEEDED" or not outcome.state_hash_after:
            raise PlatformWriteRejectedError("UNKNOWN", "unverified_receipt")
        return WriteResult(
            outcome.applied_value,
            outcome.state_hash_after,
            created_external_id=outcome.platform_request_id,
        )


def _require_proposal_id(authorization: Authorization) -> ProposalId:
    """`BrokerPlatformReader`/`BrokerPlatformWriter` solo ejecutan escrituras
    respaldadas por una `Proposal` (plan.md §6, pasos 5/6): una
    `Authorization` de paquete (`subject`, no `proposal_id` --
    `AuthorizationSubject`, T104, `proposals/domain/authorization.py`)
    todavia no tiene saga de publicacion propia y nunca deberia llegar
    aqui. Falla explicito con `WriteContextMissingError` en vez de dejar
    que `ProposalRepository.get(None)` llegue a la capa SQL con un
    `str(None)` que no es un `ProposalId` valido (mypy: `proposal_id`
    paso a ser `ProposalId | None` -- este es el unico punto que asume
    que existe, narrowing explicito en vez de `# type: ignore`)."""
    if authorization.proposal_id is None:
        raise WriteContextMissingError(
            "la autorizacion no tiene proposal_id (es una autorizacion de "
            "paquete): no hay propuesta que respalde la escritura"
        )
    return authorization.proposal_id


def _rejection(outcome: str, error_code: str | None) -> PlatformWriteRejectedError:
    if outcome == "DENIED":
        return PlatformWriteDeniedError(error_code)
    if outcome in {"BLOCKED_HARD_CAP", "SKIPPED_DRIFT"}:
        return ConfirmedPlatformWriteRejectedError(outcome, error_code)
    return PlatformWriteRejectedError(outcome, error_code)


def _intent(command: WriteCommand, proposal: Proposal, authorization: Authorization) -> WriteIntent:
    if not (
        command.managed_binding == proposal.diff.managed_binding == authorization.managed_binding
    ):
        raise PlatformWriteDeniedError("managed_binding_mismatch")
    return WriteIntent(
        entity_ref=command.entity_ref,
        operation=_operation(command),
        parametro=command.parameter,
        # `command.before`, no `proposal.diff.before`: el `WriteCommand` ya
        # trae el diff EFECTIVO que el chokepoint verifico y firmo -- releer
        # la propuesta aqui es exactamente la recomputacion que este fix
        # elimina (ver docstring de `WriteCommand`).
        valor_actual=_json(command.before),
        valor_propuesto=_json(command.value),
        diff_hash=authorization.diff_hash,
        # Una propuesta sin estado previo capturado (crear una campana no lo
        # tiene) viaja con la precondicion vacia; el broker decide si su
        # operacion la admite.
        expected_state_hash=proposal.expected_state_hash or "",
        business_id=str(proposal.business_id),
        managed_binding=command.managed_binding,
        # `003-paquete-de-campana` (BL-2/BL-3, T106): forma canonica ya
        # serializada por `packages.domain.step_binding.PackageStepBinding.
        # to_canonical()` -- `None` para cualquier autorizacion que no sea
        # `package_step` (INV-13, verificado por el bróker en R1).
        package_binding=authorization.package_binding,
    )


def _signed(authorization: Authorization) -> SignedAuthorization:
    return SignedAuthorization(
        authorization_id=str(authorization.authorization_id),
        proposal_id=str(authorization.proposal_id),
        kind=_AUTHORIZATION_KIND[authorization.kind],
        diff_hash=authorization.diff_hash,
        guardrail_verdict_hash=authorization.guardrail_verdict_hash,
        # Mismo campo que `Authorization.signing_payload()` firmo: el
        # broker lo incluye en su propio payload verificado
        # (`broker.domain.write_authorization.authorization_signing_payload`)
        # -- omitirlo aqui haria que la firma nunca verificase.
        issued_by=authorization.issued_by,
        expires_at=authorization.expires_at,
        # Hexadecimal, como el resto de digests del sistema. El verificador
        # del broker lo lee igual.
        signature=authorization.signature.hex(),
        managed_binding=authorization.managed_binding,
        # `003-paquete-de-campana` (BL-2/BL-3, T106): el sobre humano
        # completo, tal cual lo construyo `chokepoint_step_executor.py`
        # (`PackageApprovalProof.as_claims()`) -- `None` para
        # `human_approval`/`rule_authorization` (INV-13).
        package_approval=(
            authorization.package_approval.as_claims()
            if authorization.package_approval is not None
            else None
        ),
    )


def _operation(command: WriteCommand) -> WriteOperation:  # noqa: PLR0911 - explicit fail-closed operation cases
    """Del parametro y la direccion del cambio a la operacion del puerto. La
    direccion importa: subir y bajar presupuesto no son la misma operacion
    para el tope duro del broker.

    BUG corregido: clasificaba desde `proposal.diff.before/after` (el diff
    CRUDO) en vez de `command.before`/`command.value` (el diff EFECTIVO, ya
    recortado por guardarrailes). Si un ambito ya estaba fuera de
    suelo/techo por deriva externa, el recorte podia dejar el valor
    EFECTIVO al otro lado de `before` -- una subida nominal que en realidad
    aplica una bajada, o al reves -- y el broker recibia la operacion
    contraria a lo que de verdad se iba a escribir."""
    parameter = command.parameter
    if child_parameter(parameter):
        plan = validate_child_payload(command.value, command.entity_ref)
        if command.before is not None or not parameter.startswith(f"new_{plan['kind']}:"):
            raise UnsupportedWriteParameterError(parameter)
        return (
            WriteOperation.CREATE_AD_SET if plan["kind"] == "ad_set" else WriteOperation.CREATE_AD
        )
    if parameter.startswith("new_campaign:"):
        creation_budget(command.value, command.entity_ref)
        if command.before is not None:
            raise UnsupportedWriteParameterError("campaign_creation_before_must_be_absent")
        return WriteOperation.CREATE_CAMPAIGN
    if parameter.startswith(_NATIVE_WRITE_PARAMETER_PREFIX):
        return WriteOperation.NATIVE_WRITE
    if parameter in _BUDGET_PARAMETERS:
        return _budget_operation(command)
    if parameter == _STATUS_PARAMETER:
        value = str(command.value)
        if value == _DELETED_VALUE:
            return WriteOperation.DELETE
        return WriteOperation.PAUSE if value in _PAUSED_VALUES else WriteOperation.RESUME
    operation = _OPERATION_BY_PARAMETER.get(parameter)
    if operation is None:
        raise UnsupportedWriteParameterError(
            f"{parameter!r} no tiene operacion en el puerto de plataforma"
        )
    return operation


def _budget_operation(command: WriteCommand) -> WriteOperation:
    before, after = command.before, command.value
    if isinstance(before, Money) and isinstance(after, Money) and after > before:
        return WriteOperation.RAISE_BUDGET
    return WriteOperation.LOWER_BUDGET


def _reject_noop(command: WriteCommand) -> None:
    """Ultima frontera antes del broker (defensa en profundidad,
    threat-model.md C-15/C-17): un diff efectivo sin cambio real ya deberia
    haberse cortado en `ExecutionChokepoint._skip_guardrail_noop` -- si
    algo se salta ese paso, no se le pide al broker que clasifique una
    direccion (RAISE/LOWER, PAUSE/RESUME...) para un valor que no cambia."""
    if command.before == command.value:
        raise NoOpWriteCommandError(
            f"{command.entity_ref}/{command.parameter}: before == value ({command.before!r}), "
            "nada que escribir"
        )


def _json(value: object) -> JsonValue:
    """`to_jsonable` es la proyeccion con la que se calculo el `diff_hash`
    firmado; el broker la recomputa desde estos mismos campos."""
    return cast(JsonValue, to_jsonable(value))
