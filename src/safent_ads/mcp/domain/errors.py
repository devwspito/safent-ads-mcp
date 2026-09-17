"""Errores de construccion del catalogo (no de peticion: estos son bugs de
configuracion, se lanzan una vez, al montar `ToolRegistry`)."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class ForbiddenToolNameError(DomainError):
    """Un nombre que no respeta `contracts/mcp-tools.md` regla 2 (verbo
    primero) o que es un verbo de decision prohibido (INV-2) nunca llega a
    registrarse: el catalogo se niega a construirse, no solo el test lo
    detecta."""


class DuplicateToolNameError(DomainError):
    """Dos `ToolDefinition` con el mismo nombre: bug de configuracion."""


class ToolClassMismatchError(DomainError):
    """Nit de la revision de seguridad (16-sep): un verbo de propuesta
    (`propose_*`/`withdraw_*`/`generate_*`/`apply_*`) registrado con
    `tool_class` distinto de `PROPOSAL` dejaria a `ToolDispatcher`
    (`mcp/presentation/dispatcher.py`) sin exigir `ads:propose` para una
    herramienta que, por su nombre, escribe -- un caller con solo
    `ads:read` podria invocarla."""
