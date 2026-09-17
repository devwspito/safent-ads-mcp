"""`ToolRegistry` (T044): lista blanca propia del dispatch (INV-2,
contracts/mcp-tools.md regla 1). Construir el registro con una herramienta
mal nombrada falla en el acto, no solo en un test."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.domain.errors import (
    DuplicateToolNameError,
    ForbiddenToolNameError,
    ToolClassMismatchError,
)
from safent_ads.mcp.domain.tool_naming import (
    is_forbidden_decision_verb,
    is_proposal_verb,
    is_read_verb,
)
from safent_ads.mcp.presentation.args import ToolArgs


class ToolClass(StrEnum):
    """Clase de lectura/propuesta (contracts/mcp-tools.md regla 2).

    `CONNECTION_WRITE` es la unica clase visible solo bajo el permiso
    `aprobar` (Anadido del dueno, 15-sep: conectar una cuenta de plataforma
    es una decision de empresa, no una lectura ni una propuesta). Distinta
    de `CATALOG_WRITE`, que `proponer` y `aprobar` comparten sin cambios.

    `CREATIVE_WRITE` (004 tasks-2.md D-3): subir un activo binario tiene un
    coste (disco, politica, GPU aguas abajo) y una cuota propia (Q1,
    3/min) que ninguna otra clase distingue -- mezclarla en `CATALOG_WRITE`
    perderia esa senal en la auditoria y en el cubo de cuota. Visible en
    `proponer` y `aprobar`, igual que `CATALOG_WRITE`; nunca en `ver`."""

    READ = "read"
    PROPOSAL = "proposal"
    CATALOG_WRITE = "catalog_write"
    CONNECTION_WRITE = "connection_write"
    CREATIVE_WRITE = "creative_write"


BusinessIdExtractor = Callable[[Any], str]

# Listas cerradas de nombres por clase de escritura (INV-2): ninguna se
# infiere de un prefijo de verbo, se enumera explicitamente. `ToolRegistry`
# rechaza al construir cualquier otro nombre en esa clase.
# `upsert_dns_record`/`delete_dns_record` (Anadido del dueno, 14-sep:
# integrations/cloudflare) se suman aqui, no a `tool_naming.py`: no son
# lecturas ni propuestas, son escritura directa auditada, mismo trato que
# `create_offering`.
_CATALOG_WRITE_NAMES = frozenset({"create_offering", "upsert_dns_record", "delete_dns_record"})
_CONNECTION_WRITE_NAMES = frozenset({"connect_platform_account", "get_connection_status"})
# 004 tasks-2.md I1 punto 1: lista cerrada de una unica herramienta, mismo
# mecanismo que las dos de arriba -- `upload_` no necesita entrar en
# `tool_naming.py`, ese fichero no se toca.
_CREATIVE_WRITE_NAMES = frozenset({"upload_creative_asset"})


@dataclass(frozen=True, slots=True)
class ToolDefinition[ArgsT: ToolArgs]:
    """`business_id_of=None` es la unica excepcion legitima: `list_businesses`,
    que no recibe `business_id` porque enumera los negocios que la propia
    `CallerScope` ya autoriza (documentado en `presentation/args.py`).

    Generica sobre `ArgsT` para que cada `ToolDefinition` concreta (en
    `catalog.py`) se construya con el modelo de argumentos y el handler ya
    emparejados por el checker, sin `cast()` disperso por los 32 registros."""

    name: str
    description: str
    args_model: type[ArgsT]
    tool_class: ToolClass
    handler: Callable[[ArgsT, CallerScope], Awaitable[object]]
    business_id_of: BusinessIdExtractor | None


class ToolRegistry:
    """Lista blanca (INV-2): solo lo que esta aqui puede ejecutarse, sin
    importar lo que el cliente MCP pida (contracts/mcp-tools.md regla 1)."""

    def __init__(self, definitions: Iterable[ToolDefinition[Any]]) -> None:
        self._by_name: dict[str, ToolDefinition[Any]] = {}
        for definition in definitions:
            self._register(definition)

    def _register(self, definition: ToolDefinition[Any]) -> None:
        self._reject_forbidden_name(definition.name, definition.tool_class)
        self._reject_tool_class_mismatch(definition)
        if definition.name in self._by_name:
            raise DuplicateToolNameError(f"herramienta duplicada: {definition.name}")
        self._by_name[definition.name] = definition

    def _reject_forbidden_name(self, name: str, tool_class: ToolClass) -> None:
        if tool_class is ToolClass.CATALOG_WRITE:
            if name not in _CATALOG_WRITE_NAMES:
                raise ForbiddenToolNameError("escritura de catálogo no permitida")
            return
        if tool_class is ToolClass.CONNECTION_WRITE:
            if name not in _CONNECTION_WRITE_NAMES:
                raise ForbiddenToolNameError("escritura de conexión no permitida")
            return
        if tool_class is ToolClass.CREATIVE_WRITE:
            if name not in _CREATIVE_WRITE_NAMES:
                raise ForbiddenToolNameError("escritura de creatividad no permitida")
            return
        if is_forbidden_decision_verb(name):
            raise ForbiddenToolNameError(f"verbo de decision prohibido en MCP: {name}")
        if not (is_read_verb(name) or is_proposal_verb(name)):
            raise ForbiddenToolNameError(f"nombre fuera de convencion verbo-primero: {name}")

    @staticmethod
    def _reject_tool_class_mismatch(definition: ToolDefinition[Any]) -> None:
        # Nit de la revision de seguridad (16-sep): un verbo de propuesta
        # SIEMPRE tiene que declararse `ToolClass.PROPOSAL`, o
        # `ToolDispatcher` (T012, `dispatcher.py:131-133`) nunca exigiria
        # `ads:propose` para invocarla.
        if is_proposal_verb(definition.name) and definition.tool_class is not ToolClass.PROPOSAL:
            raise ToolClassMismatchError(
                f"«{definition.name}» tiene verbo de propuesta pero "
                f"tool_class={definition.tool_class}, no PROPOSAL"
            )

    def get(self, name: str) -> ToolDefinition[Any] | None:
        return self._by_name.get(name)

    def __iter__(self) -> Iterator[ToolDefinition[Any]]:
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)
