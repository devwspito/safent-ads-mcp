"""Definiciones y helpers de metricas Prometheus (T127, plan.md SS7/NFR-1..4).

Un `CollectorRegistry` propio (`REGISTRY` de este modulo), no el global de
`prometheus_client`: crear la app dos veces en el mismo proceso (patron
`TestClient(create_app(...))` repetido en `tests/unit/`) no debe reventar
con `Duplicated timeseries` -- las metricas se registran UNA vez, al
importar este modulo, nunca dentro de `create_app()`/`run()`.

Cardinalidad (tasks.md T127: "no high-cardinality labels: never entity
ids, never business ids beyond a bounded count"): toda etiqueta de este
modulo es un valor de un enum de dominio cerrado (nombre de ciclo/paso,
`ProposalState`, `ExecutionStatus`, `WriteDenialCode`, nombre de
herramienta MCP registrada) o una de las dos plataformas soportadas --
nunca un identificador de negocio, cuenta, propuesta o entidad. Los
llamantes pasan `str` primitivos (no los dataclasses/enums de cada
dominio) para que este paquete transversal no dependa de
`orchestration`/`proposals`/`broker`/`mcp` (evita el ciclo de import
inverso; solo `composition` y esos paquetes dependen de este)."""

from __future__ import annotations

from collections.abc import Mapping

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import generate_latest as _generate_latest

REGISTRY = CollectorRegistry()

_NAMESPACE = "ads"

cycle_duration_seconds = Histogram(
    f"{_NAMESPACE}_orchestration_cycle_duration_seconds",
    "Duracion de una vuelta completa de un ciclo de orquestacion.",
    labelnames=("cycle",),
    registry=REGISTRY,
)
cycles_total = Counter(
    f"{_NAMESPACE}_orchestration_cycles_total",
    "Vueltas de ciclo de orquestacion terminadas, por desenlace.",
    labelnames=("cycle", "outcome"),
    registry=REGISTRY,
)
steps_total = Counter(
    f"{_NAMESPACE}_orchestration_steps_total",
    "Pasos de negocio dentro de un ciclo, por desenlace tecnico.",
    labelnames=("step", "outcome"),
    registry=REGISTRY,
)
execution_outcomes_total = Counter(
    f"{_NAMESPACE}_execution_outcomes_total",
    "Intentos de ExecutionCycle reclamados, por desenlace de negocio.",
    labelnames=("outcome",),
    registry=REGISTRY,
)
proposals_saved_total = Counter(
    f"{_NAMESPACE}_proposals_saved_total",
    "Propuestas persistidas (alta o transicion), por estado resultante.",
    labelnames=("state",),
    registry=REGISTRY,
)
broker_write_denials_total = Counter(
    f"{_NAMESPACE}_broker_write_denials_total",
    "Escrituras denegadas por el broker, por control que la deniega.",
    labelnames=("control",),
    registry=REGISTRY,
)
credential_health = Gauge(
    f"{_NAMESPACE}_credential_health",
    "Credenciales de plataforma por estado de salud (recuento, no PII).",
    labelnames=("platform", "status"),
    registry=REGISTRY,
)
mcp_tool_calls_total = Counter(
    f"{_NAMESPACE}_mcp_tool_calls_total",
    "Llamadas a herramientas MCP, por nombre y desenlace.",
    labelnames=("tool", "outcome"),
    registry=REGISTRY,
)
mcp_tool_call_duration_seconds = Histogram(
    f"{_NAMESPACE}_mcp_tool_call_duration_seconds",
    "Latencia de una llamada a herramienta MCP.",
    labelnames=("tool",),
    registry=REGISTRY,
)


def record_cycle(cycle: str, *, outcome: str, duration_seconds: float) -> None:
    cycle_duration_seconds.labels(cycle=cycle).observe(duration_seconds)
    cycles_total.labels(cycle=cycle, outcome=outcome).inc()


def record_step(step: str, *, outcome: str) -> None:
    steps_total.labels(step=step, outcome=outcome).inc()


def record_execution_outcome(outcome: str) -> None:
    execution_outcomes_total.labels(outcome=outcome).inc()


def record_proposal_saved(state: str) -> None:
    proposals_saved_total.labels(state=state).inc()


def record_write_denial(control: str) -> None:
    broker_write_denials_total.labels(control=control).inc()


def record_mcp_tool_call(tool: str, *, outcome: str, duration_seconds: float) -> None:
    mcp_tool_calls_total.labels(tool=tool, outcome=outcome).inc()
    mcp_tool_call_duration_seconds.labels(tool=tool).observe(duration_seconds)


def set_credential_health(
    counts: Mapping[tuple[str, str], int],
    *,
    known_platforms: tuple[str, ...],
    known_statuses: tuple[str, ...],
) -> None:
    """Fija el recuento completo de la rejilla `platform x status` en cada
    refresco (`composition/credential_health_metrics.py`): una combinacion
    ausente en `counts` vale 0 explicito, para que un estado que desaparece
    (p. ej. la ultima credencial `INVALID` se reconecta) deje de leerse
    en vez de quedar congelado con el ultimo valor visto."""
    for platform in known_platforms:
        for status in known_statuses:
            credential_health.labels(platform=platform, status=status).set(
                counts.get((platform, status), 0)
            )


def render_latest() -> bytes:
    return _generate_latest(REGISTRY)


__all__ = [
    "CONTENT_TYPE_LATEST",
    "REGISTRY",
    "record_cycle",
    "record_execution_outcome",
    "record_mcp_tool_call",
    "record_proposal_saved",
    "record_step",
    "record_write_denial",
    "render_latest",
    "set_credential_health",
]
