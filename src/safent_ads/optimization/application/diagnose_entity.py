"""`DiagnoseEntity` (contracts/mcp-tools.md P1 `diagnose_entity`): camino
completo de los 9 nodos, no solo el primero (profitability-engine.md §5)."""

from __future__ import annotations

from safent_ads.optimization.application.dto import DiagnosisNodeView, DiagnosisView
from safent_ads.optimization.application.errors import DiagnosisMetricsNotFoundError
from safent_ads.optimization.application.ports import DiagnosisMetricsPort
from safent_ads.optimization.domain.diagnosis import diagnose
from safent_ads.shared.ids import BusinessId, EntityRef


class DiagnoseEntity:
    def __init__(self, metrics_port: DiagnosisMetricsPort) -> None:
        self._metrics_port = metrics_port

    async def execute(self, *, business_id: BusinessId, entity_ref: EntityRef) -> DiagnosisView:
        metrics = await self._metrics_port.get_metrics(
            business_id=business_id, entity_ref=entity_ref
        )
        if metrics is None:
            raise DiagnosisMetricsNotFoundError(str(entity_ref))
        path = diagnose(metrics)
        return DiagnosisView(
            entity_ref=str(entity_ref),
            action=path.action,
            nodes=tuple(
                DiagnosisNodeView(
                    order=node.order,
                    name=node.name,
                    matched=node.matched,
                    action=node.action,
                    reason=node.reason,
                    reference_check_ids=node.reference_check_ids,
                )
                for node in path.nodes
            ),
        )
