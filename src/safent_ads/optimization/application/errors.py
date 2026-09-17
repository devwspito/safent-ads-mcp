"""Errores de aplicacion de `optimization`: entidad no encontrada o
precondicion de puerto no cumplida (mismo criterio que
`economics/application/errors.py`)."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class MarginalEstimateNotFoundError(ApplicationError):
    """No hay `MarginalEstimate` materializada para esta entidad."""


class DiagnosisMetricsNotFoundError(ApplicationError):
    """No hay metricas suficientes para diagnosticar esta entidad."""


class ResponseCurveNotFoundError(ApplicationError):
    """No hay `ResponseCurve` materializada para esta entidad."""


class ContributionMarginNotFoundError(ApplicationError):
    """No hay perfil de economia unitaria para traducir conversiones de negocio a euros."""


class NoReallocationCandidatesError(ApplicationError):
    """Menos de dos candidatos elegibles: no hay donante/receptor que comparar."""


class ReallocationVetoedError(ApplicationError):
    """`build_equimarginal_plan` veta el unico par disponible (aprendizaje,
    IC solapado, ya equimarginal)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ExperimentNotFoundError(ApplicationError):
    """No hay `Experiment` con ese `experiment_id`."""
