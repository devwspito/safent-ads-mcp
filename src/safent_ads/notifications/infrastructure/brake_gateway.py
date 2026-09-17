"""`TelegramBrakeGateway` implementa `BrakeGatewayPort` (este branch,
`/freno on|off`): envuelve el MISMO `ToggleEmergencyBrake` que
`composition/execution_rest.py::post_kill_switch` usa para `POST
/kill-switch` (contracts/telegram.md: "Telegram debe llamar al mismo caso
de uso que REST, nunca uno paralelo"). `EmergencyBrakeEngaged`/`Released`
se escriben en `decision_log` por el propio caso de uso
(`ToggleEmergencyBrake` -> `SqlDecisionRecorder`), no aqui: no hay nada que
duplicar.

Ambito SIEMPRE global (`BrakeScope(kind=GLOBAL)`, modo `ALL`): el bot no
ofrece elegir negocio ni cuenta, solo el interruptor general -- mismo
alcance por defecto que `execution_rest.py::_parse_brake_scope` cuando el
body no trae `scope_kind`."""

from __future__ import annotations

from safent_ads.execution.application.toggle_emergency_brake import (
    BrakeAlreadyEngagedError,
    EngageBrakeCommand,
    NoBrakeRegisteredError,
    ReleaseBrakeCommand,
    ToggleEmergencyBrake,
)
from safent_ads.execution.domain.guardrails import BrakeMode, BrakeScope, BrakeScopeKind
from safent_ads.execution.infrastructure.sql_brake_state import SqlBrakeStatePort
from safent_ads.notifications.application.ports import (
    BrakeStatusView,
    BrakeToggleOutcomeKind,
    BrakeToggleResult,
)
from safent_ads.shared.ids import BusinessId

_GLOBAL_SCOPE = BrakeScope(kind=BrakeScopeKind.GLOBAL)
# El freno de Telegram no tiene negocio real al que atribuirse (mismo
# patron centinela que `execution_rest.py::_ZERO_BUSINESS_ID`, aqui para
# `EngageBrakeCommand.business_id`, que solo alimenta el evento de
# `decision_log`, nunca resuelve el ambito -- eso lo decide `scope`).
_UNSCOPED_BUSINESS_ID = BusinessId.parse("00000000-0000-0000-0000-000000000000")


class TelegramBrakeGateway:
    def __init__(self, *, toggle: ToggleEmergencyBrake, brakes: SqlBrakeStatePort) -> None:
        self._toggle = toggle
        self._brakes = brakes

    async def get_status(self) -> BrakeStatusView:
        brake = await self._brakes.get(_GLOBAL_SCOPE)
        if brake is None or not brake.engaged:
            return BrakeStatusView(engaged=False, mode=None, reason=None, since=None)
        return BrakeStatusView(
            engaged=True, mode=brake.mode.value, reason=brake.reason, since=brake.since
        )

    async def engage(self, *, reason: str, engaged_by: str) -> BrakeToggleResult:
        try:
            brake = await self._toggle.engage(
                EngageBrakeCommand(
                    business_id=_UNSCOPED_BUSINESS_ID,
                    scope=_GLOBAL_SCOPE,
                    mode=BrakeMode.ALL,
                    reason=reason,
                    engaged_by=engaged_by,
                )
            )
        except BrakeAlreadyEngagedError:
            return BrakeToggleResult(kind=BrakeToggleOutcomeKind.ALREADY_ENGAGED)
        return BrakeToggleResult(
            kind=BrakeToggleOutcomeKind.ENGAGED,
            status=BrakeStatusView(
                engaged=True, mode=brake.mode.value, reason=brake.reason, since=brake.since
            ),
        )

    async def release(self, *, released_by: str) -> BrakeToggleResult:
        try:
            await self._toggle.release(
                ReleaseBrakeCommand(
                    business_id=_UNSCOPED_BUSINESS_ID, scope=_GLOBAL_SCOPE, released_by=released_by
                )
            )
        except NoBrakeRegisteredError:
            return BrakeToggleResult(kind=BrakeToggleOutcomeKind.NOT_REGISTERED)
        return BrakeToggleResult(kind=BrakeToggleOutcomeKind.RELEASED)
