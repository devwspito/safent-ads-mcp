"""`GetCapabilities`: caso de uso detras de la herramienta MCP
`get_capabilities` (tool-surface.md §0: "la capacidad se declara, nunca se
finge"). El agente lo llama antes de prometer nada que el entorno no pueda
cumplir hoy -- ninguna comprobacion aqui lanza por credencial ausente,
ausencia es un resultado valido que se reporta."""

from __future__ import annotations

from safent_ads.mcp.application.dto import CapabilitiesReport
from safent_ads.mcp.domain.capabilities import (
    generation_backend_capabilities,
    missing_byok_keys,
    missing_platform_accounts,
    platform_write_capabilities,
    resolve_autonomy_status,
)
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.shared.clock import Clock


class GetCapabilities:
    def __init__(self, ports: ReadModelPorts, clock: Clock) -> None:
        self._ports = ports
        self._clock = clock

    async def execute(self, business_id: str) -> CapabilitiesReport:
        platform_accounts = await self._ports.portfolio.list_platform_accounts(business_id)
        kill_switch = await self._ports.rule.get_kill_switch_status(business_id)
        enabled_rules = await self._ports.rule.list_rules(business_id, platform=None, enabled=True)
        configured_keys = await self._ports.capability.configured_byok_keys()

        missing_env_keys = missing_byok_keys(configured_keys)
        missing_credentials = missing_platform_accounts(platform_accounts)
        return CapabilitiesReport(
            business_id=business_id,
            platform_writes=platform_write_capabilities(),
            generation_backends=generation_backend_capabilities(configured_keys),
            autonomy=resolve_autonomy_status(kill_switch, enabled_rules),
            missing_credentials=missing_credentials,
            generated_at=self._clock.now(),
            optional_missing_credentials=tuple(missing_env_keys),
        )
