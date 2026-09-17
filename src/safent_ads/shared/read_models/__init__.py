"""DTOs y proyecciones de lectura compartidas entre `panel` y `mcp` (N7,
plan.md §4: ninguno de los dos importa al otro). Formas de lectura sin I/O
y sin comportamiento de negocio -- vivir en `shared` (N0) no viola la regla
de "los contextos no se importan entre si" (simplification-audit.md #9)."""

from __future__ import annotations
