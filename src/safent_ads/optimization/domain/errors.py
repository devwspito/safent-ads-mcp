"""Errores de dominio de `optimization` (profitability-engine.md §3-§7).

Nombrados por invariante violado, nunca genericos (mismo criterio que
`economics/domain/errors.py`)."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class PairedWindowShapeError(DomainError):
    """Las ventanas emparejadas no tienen exactamente 7 pares diarios
    (profitability-engine.md §3: 'Ventanas de 7 dias emparejadas')."""


class NonPositiveContributionMarginError(DomainError):
    """`contribution_margin` <= 0: no hay margen que repartir."""


class InvalidChannelMappingError(DomainError):
    """Un registro de gasto no encaja en ningun canal ni en el residual."""


class MinViableSpendViolationError(DomainError):
    """Un `AllocationPlan` propone bajar por debajo de `min_viable_spend`
    (profitability-engine.md §3: 'nunca se baja de min_viable_spend')."""


class UnbalancedAllocationStepError(DomainError):
    """El paso propuesto no conserva el gasto total (build/sell deben
    compensarse)."""


class ImpossibleExperimentDesignError(DomainError):
    """El diseno de experimento supera 28 dias o no alcanza el volumen
    necesario (profitability-engine.md §4: 'rechaza el diseno, no lo
    arranca')."""


class InvalidExperimentTransitionError(DomainError):
    """Transicion de estado invalida en el agregado `Experiment`."""


class CalibrationDirectionViolationError(DomainError):
    """Guardarrail innegociable: un ajuste de calibracion sobre una regla
    `AUTO` intenta moverse en direccion agresiva (profitability-engine.md
    §6: 'la calibracion es monotona conservadora... el agregado rechaza el
    ajuste, no lo recorta en silencio')."""


class InsufficientCalibrationSampleError(DomainError):
    """`n < 20`: no se calibra, solo se muestra el dato."""


class ResponseCurveNotConvergedError(DomainError):
    """La curva Hill/potencia no tiene observaciones suficientes o los
    parametros estimados no son validos (`b` fuera de (0,1), `k`<=0)."""


class OutOfSupportForecastError(DomainError):
    """El escenario solicitado excede el +-50% del rango observado
    (profitability-engine.md §7: 'fuera, out_of_support y no hay numero')."""
