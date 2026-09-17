#!/usr/bin/env python3
"""Doble de los CLI `claude` y `codex` para los tests del instalador
(008-mcp-ads-estandar T025). Se instala en el `PATH` del test con dos
enlaces (`claude`, `codex`): el nombre con el que se le invoca decide a
quien imita.

Fidelidad que importa para el contrato:
- `claude mcp add` con un nombre ya registrado FALLA (por eso el
  instalador retira antes en ambos ambitos). El doble falla igual: si el
  instalador dejara de retirar, la prueba de idempotencia lo diria.
- `codex mcp add` SUSTITUYE la entrada (su registro es un mapa por
  nombre), nunca duplica.
- `claude mcp get` responde «Connected» solo si el servidor esta
  registrado: es lo que el instalador busca en el modo de token.

Cada invocacion queda en `calls.log` con su argv completo -- de ahi sale
la comprobacion de que ningun token viaja por argumento a Codex."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_STATE = Path(os.environ["FAKE_AGENT_STATE"])


def _first_positional(arguments: list[str]) -> str:
    """Primer argumento que no es un flag ni el valor de un flag."""
    index = 0
    while index < len(arguments):
        if arguments[index].startswith("-"):
            index += 2
            continue
        return arguments[index]
    return ""


def _entries(registry: Path) -> list[str]:
    return registry.read_text(encoding="utf-8").splitlines() if registry.is_file() else []


def _save(registry: Path, entries: list[str]) -> None:
    registry.write_text("".join(f"{entry}\n" for entry in entries), encoding="utf-8")


def _handle(agent: str, arguments: list[str]) -> int:
    registry = _STATE / f"{agent}.registry"
    entries = _entries(registry)
    action = arguments[1] if len(arguments) > 1 else ""
    name = _first_positional(arguments[2:])
    if action == "add" and agent == "claude":
        if name in entries:
            print(f"MCP server {name} already exists", file=sys.stderr)
            return 1
        _save(registry, [*entries, name])
    elif action == "add":
        _save(registry, [*[entry for entry in entries if entry != name], name])
    elif action == "remove":
        if name not in entries:
            return 1
        _save(registry, [entry for entry in entries if entry != name])
    elif action in {"get", "list"}:
        if name not in entries:
            return 1
        print(f"{name}: Connected")
    return 0


def main() -> int:
    agent = Path(sys.argv[0]).name
    arguments = sys.argv[1:]
    with (_STATE / "calls.log").open("a", encoding="utf-8") as log:
        log.write(f"{agent} {' '.join(arguments)}\n")
    if not arguments or arguments[0] != "mcp":
        return 0
    return _handle(agent, arguments)


if __name__ == "__main__":
    raise SystemExit(main())
