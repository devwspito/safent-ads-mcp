"""`AutonomyLevel` y `ActionKind` (data-model.md §Rule; spec.md FR-11/FR-12).

`increases_spend()` es el unico lugar que decide si una accion "sube gasto":
lo usa el invariante de carga del catalogo para rechazar una regla `AUTO`
que aumente gasto (FR-11: 'autonomo SOLO defensivo'; FR-12: 'aprobacion para
... todo lo que aumente gasto'). `UNPAUSE` queda fuera a proposito: data-
model.md nombra 'reanudar por conversion tardia' como accion AUTO permitida
(M11), distinta de subir un presupuesto ya activo."""

from __future__ import annotations

from enum import StrEnum


class AutonomyLevel(StrEnum):
    NOTIFY = "notify"
    AUTO = "auto"
    APPROVAL = "approval"


class ActionKind(StrEnum):
    BUY = "buy"
    SELL = "sell"
    EXIT = "exit"
    UNPAUSE = "unpause"
    HOLD_ALL = "hold_all"
    NOTIFY_ONLY = "notify_only"
    ADD_NEGATIVE_KEYWORD = "add_negative_keyword"
    CREATIVE_KILL = "creative_kill"
    CREATIVE_SCALE = "creative_scale"
    TIGHTEN_TARGET = "tighten_target"
    LOOSEN_TARGET = "loosen_target"
    REPLACE_ASSET = "replace_asset"


_SPEND_INCREASING_ACTIONS = frozenset(
    {ActionKind.BUY, ActionKind.CREATIVE_SCALE, ActionKind.LOOSEN_TARGET}
)


def increases_spend(action: ActionKind) -> bool:
    return action in _SPEND_INCREASING_ACTIONS
