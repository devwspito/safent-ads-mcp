"""Contrato de delegacion a herramientas nativas del agente. Vive en
`application/` — no en `infrastructure/hermes_tool_renderer.py`, que es
donde se justifica esta forma con detalle — porque es parte del contrato
del puerto: cualquier adaptador de `RendererTier.DELEGATED` lo usa para
senalar que no puede completar un render de forma sincrona, y
`GenerateCreativeAssets` necesita reconocerlo sin importar un adaptador
concreto (DIP: la aplicacion depende de la abstraccion, la infraestructura
depende de la aplicacion, nunca al reves)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from safent_ads.shared.errors import InfrastructureError

_DEFAULT_NEXT_STEP = (
    "Cuando la herramienta devuelva el activo, tráelo con import_creative_asset "
    "usando este mismo brief_id y la URL que te haya dado la herramienta. Si la "
    "herramienta declina o no está disponible en el modelo configurado, dilo "
    "explícitamente: no inventes un resultado."
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DelegationInstruction:
    """Que herramienta nativa debe invocar el agente y con que argumentos.
    Nunca lleva un tamano en pixeles fijo ni datos personales — solo el
    prompt/aspecto/duracion ya sanitizados antes de llegar aqui."""

    native_tool: str
    arguments: Mapping[str, object]
    next_step: str = _DEFAULT_NEXT_STEP


class RendererDelegationRequiredError(InfrastructureError):
    """Un adaptador `RendererTier.DELEGATED` nunca completa un render por
    si solo (research/creative-via-codex.md §(b): nuestro servicio no
    puede invocar las herramientas in-process de Hermes). `instruction` es
    lo que el llamante debe registrar para trazabilidad (FR-34) antes de
    pasar al siguiente candidato de `RendererSelector.candidates_for`."""

    def __init__(self, instruction: DelegationInstruction) -> None:
        super().__init__(
            f"requiere que el agente invoque '{instruction.native_tool}' y traiga "
            "el resultado con import_creative_asset"
        )
        self.instruction = instruction
