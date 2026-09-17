"""Excepciones de infraestructura de `execution` (shared/errors.py: "Fallo de
un adaptador: base de datos, socket del broker, SDK externo").

Todas denegan por defecto: el chokepoint las captura y deja el intento en
`FAILED`, nunca en `EXECUTED` (plan.md §6, threat-model.md C-1)."""

from __future__ import annotations

from safent_ads.execution.application.ports import ConfirmedWriteRejection
from safent_ads.shared.errors import InfrastructureError


class DuplicateExecutionError(InfrastructureError):
    """Otra fila ya tiene esa `idempotency_key` (UNIQUE de `0009_executions`).
    Reintentar el mismo cambio no lo aplica dos veces (threat-model.md C-8):
    el adaptador lo dice en voz alta en vez de insertar un duplicado."""


class ExecutionRowRejectedError(InfrastructureError):
    """La fila viola un CHECK de `executions`: un exito sin valor aplicado o
    sin estado remoto posterior, un estado terminal sin `finished_at`, un
    `platform_state_hash_before` ausente. El esquema es la ultima frontera
    del invariante y no se le da la vuelta desde el codigo."""


class UnknownProposalError(InfrastructureError):
    """La propuesta del intento no existe en `proposals`. Sin ella no hay
    `entity_ref` que poner en la fila (FK compuesta con el negocio, C-27)."""


class UnknownEntityRefError(InfrastructureError):
    """El `entity_ref` no esta en `ad_entities`: no se puede resolver ni su
    negocio ni su cuenta de plataforma, que son la clave de los topes."""


class MissingGuardrailSetError(LookupError, InfrastructureError):
    """No hay ningun `guardrails` que alcance a ese ambito. Ejecutar sin
    limites es exactamente lo que FR-13 prohibe: se deniega.

    Hereda de `LookupError` porque el doble en memoria falla con `KeyError`
    ante el mismo caso: el banco de contrato exige el mismo tipo de fallo a
    las dos implementaciones."""


class IncompleteGuardrailSetError(InfrastructureError):
    """La fila de `guardrails` deja algun limite a NULL. Un `GuardrailSet` a
    medias dejaria pasar cambios que nadie acoto."""


class LedgerContextMissingError(InfrastructureError):
    """No hay intento reclamado al que atribuir el apunte del ledger. Un
    cambio aplicado sin ejecucion asociada rompe "un apunte por ejecucion"
    (indice unico de `0009_executions`) y con el, el tope diario (C-17)."""


class PlatformWriteRejectedError(InfrastructureError):
    """El broker no aplico la escritura. `outcome` es el veredicto literal
    del puerto de plataforma (`DENIED`, `FAILED`, `SKIPPED_DRIFT`,
    `BLOCKED_HARD_CAP`): nunca se traduce a exito (threat-model.md C-1)."""

    def __init__(self, outcome: str, error_code: str | None = None) -> None:
        super().__init__(f"{outcome}: {error_code}" if error_code else outcome)
        self.outcome = outcome
        self.error_code = error_code


class ConfirmedPlatformWriteRejectedError(PlatformWriteRejectedError, ConfirmedWriteRejection):
    """The broker conclusively rejected this request before dispatch."""


class PlatformWriteDeniedError(ConfirmedPlatformWriteRejectedError):
    """`outcome = DENIED`: el broker rechaza la escritura de plano (hoy
    siempre, hasta que otra rama abra la ruta de escritura). Tipo propio
    para que quien la reciba no la confunda con un fallo transitorio."""

    def __init__(self, error_code: str | None = None) -> None:
        super().__init__("DENIED", error_code)


class WriteContextMissingError(InfrastructureError):
    """No se pudo reconstruir el `WriteIntent`: la propuesta que respalda la
    autorizacion no existe. Sin `diff_hash` ni estado esperado no hay
    escritura verificable (contracts/platform-port.md)."""


class LedgerCurrencyMismatchError(InfrastructureError):
    """El cambio aplicado viene en una divisa distinta de la de la cuenta.
    Sumarlo al tope seria comparar peras con manzanas: se rechaza."""


class UnsupportedWriteParameterError(InfrastructureError):
    """El parametro de la propuesta no tiene operacion en el puerto de
    plataforma. No se traduce "a lo que mas se parezca": una palanca que el
    puerto no expone es una palanca que no se toca (FR-41)."""


class NoOpWriteCommandError(InfrastructureError):
    """Defensa en profundidad (threat-model.md C-15/C-17): un `WriteCommand`
    sin cambio real (`before == value`) nunca deberia llegar hasta aqui --
    el chokepoint ya lo corta antes de construirlo
    (`ExecutionChokepoint._skip_guardrail_noop`). Si algun otro llamante se
    saltase ese paso, pedirle al broker una operacion (RAISE/LOWER_BUDGET,
    PAUSE/RESUME...) para un valor que no cambia es, en el mejor caso,
    ruido, y en el peor, una clasificacion de direccion sin sentido -- se
    rechaza aqui, en el propio limite con el broker."""
