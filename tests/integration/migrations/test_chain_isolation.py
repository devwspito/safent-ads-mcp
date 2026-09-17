"""Guardia de aislamiento (004 tasks-2.md Carril Q, Q4): ningun fichero de
`tests/integration/migrations/` escribe filas a traves del codigo de
aplicacion. Los repositorios y casos de uso solo compilan contra `head`
-- una revision congelada los rompe: el caso real fue 0043, que anadio
`proposals.proposed_by` y tumbo seis tests que sembraban con
`use_case(session).execute(...)` -> `SqlProposalRepository`, cuyo INSERT
nombra esa columna explicitamente (`tests/integration/migrations/
conftest.py`, `insert_managed_proposal`, ahora en SQL crudo).

Analiza el AST de cada fichero, no importa los paquetes (mismo criterio
que `tests/unit/test_application_and_domain_do_not_import_infrastructure.
py`): un `import` dentro de una funcion nunca ejecutada tambien cuenta.

`_ALLOWED_IMPORTS` documenta, por fichero, las unicas excepciones
auditadas hoy -- ninguna toca un repositorio ni un caso de uso, asi que
ninguna reproduce la rotura de 0043:
- `value_codec.encode_value` (`conftest.py`, `test_physical_metric_views.
  py`): serializa `Money` byte a byte como lo haria el dominio, sin tabla
  ni revision de por medio.
- `scoped_pair`/`managed_command` (`test_managed_signed_context.py`,
  `test_managed_provider_account.py`, ambos importando de
  `tests/integration/proposals/test_managed_signed_context.py`): solo
  construyen el `AccountRef`/`ManagedAdsBinding` de dominio y hacen un
  UPDATE de SQL crudo -- nunca llaman a `use_case(session).execute()` ni a
  `SqlProposalRepository`, que es lo que este guardia prohibe de verdad.
Una entrada nueva aqui exige la misma justificacion: por que NO es el
patron que rompio con la columna 0043."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

_MIGRATIONS_ROOT = Path(__file__).resolve().parent
_FORBIDDEN_PACKAGE_PARTS = ("application", "infrastructure")
_FORBIDDEN_PROPOSALS_TESTS_PREFIX = "tests.integration.proposals"

_ALLOWED_IMPORTS: Final[dict[str, frozenset[str]]] = {
    "conftest.py": frozenset({"safent_ads.proposals.infrastructure.value_codec"}),
    "test_physical_metric_views.py": frozenset(
        {"safent_ads.proposals.infrastructure.value_codec"}
    ),
    "test_managed_signed_context.py": frozenset(
        {"tests.integration.proposals.test_managed_signed_context"}
    ),
    "test_managed_provider_account.py": frozenset(
        {"tests.integration.proposals.test_managed_signed_context"}
    ),
}


def _imported_module_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _is_forbidden(name: str) -> bool:
    if name.startswith(_FORBIDDEN_PROPOSALS_TESTS_PREFIX):
        return True
    return name.startswith("safent_ads.") and any(
        part in name.split(".") for part in _FORBIDDEN_PACKAGE_PARTS
    )


def _forbidden_imports(path: Path) -> set[str]:
    imported = _imported_module_names(path.read_text())
    return {name for name in imported if _is_forbidden(name)}


def test_ningun_test_de_migracion_importa_codigo_de_aplicacion() -> None:
    offenders: dict[str, set[str]] = {}
    for path in sorted(_MIGRATIONS_ROOT.glob("*.py")):
        found = _forbidden_imports(path)
        unjustified = found - _ALLOWED_IMPORTS.get(path.name, frozenset())
        if unjustified:
            offenders[path.name] = unjustified
    assert offenders == {}, (
        "tests de migracion importan codigo de aplicacion/infraestructura o de "
        f"tests/integration/proposals sin justificar en _ALLOWED_IMPORTS: {offenders}"
    )
