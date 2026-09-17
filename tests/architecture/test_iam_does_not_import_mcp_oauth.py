"""Guarda arquitectonica de plan.md ("mcp_oauth -> iam, nunca al reves").
T085 (revision de seguridad, 17-sep): `iam/presentation/federated_router.py`
importaba `mcp_oauth.domain.authorization` y `mcp_oauth.infrastructure.
sql_authorization_repository` para comprobar que un `txn_id` de
consentimiento seguia abierto -- la direccion equivocada. Ese dato ahora
llega por `iam.application.ports.OpenConsentTransactions`, un puerto que
`composition/federated_routes.py` (el UNICO punto autorizado a conocer los
dos modulos) implementa e inyecta.

Analiza el AST de cada fichero FUENTE de `src/safent_ads/iam` (nunca los
tests: `tests/` importa `iam` y `mcp_oauth` a proposito para montar routers
juntos -- `test_federated_login.py`, `test_consent_router.py`) en vez de
importar el paquete: una comprobacion en tiempo de import no detectaria un
`import` puesto dentro de una funcion y nunca ejecutado en el proceso de
test (mismo criterio que `test_mcp_does_not_import_panel.py`).

Code review 17-sep (item 5): la primera version solo miraba `ast.
ImportFrom.module` con el prefijo absoluto `safent_ads.mcp_oauth` --
`node.level > 0` (un `from ...mcp_oauth import X` relativo) entrega
`module="mcp_oauth..."` SIN ese prefijo, asi que la comprobacion original
no lo habria detectado. Se compara por SEGMENTO del path (`"mcp_oauth" in
modulo.split(".")`), que casa igual con la forma absoluta y con
cualquier nivel de import relativo."""

from __future__ import annotations

import ast
from pathlib import Path

_IAM_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "safent_ads" / "iam"
_FORBIDDEN_SEGMENT = "mcp_oauth"


def _imported_module_names(source: str) -> set[str]:
    """Nombres de modulo tal y como los escribio el `import`: para uno
    relativo (`node.level > 0`) es el sufijo SIN el prefijo del paquete
    desde el que se resuelve -- nunca se reconstruye el nombre absoluto
    (no hace falta: comparar por segmento basta y no puede acertar mal la
    reconstruccion)."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _mentions_mcp_oauth(module_name: str) -> bool:
    return _FORBIDDEN_SEGMENT in module_name.split(".")


def test_no_iam_source_file_imports_mcp_oauth() -> None:
    offenders: dict[str, set[str]] = {}
    for path in _IAM_SRC_ROOT.rglob("*.py"):
        imported = _imported_module_names(path.read_text(encoding="utf-8"))
        forbidden = {name for name in imported if _mentions_mcp_oauth(name)}
        if forbidden:
            offenders[str(path.relative_to(_IAM_SRC_ROOT))] = forbidden
    assert offenders == {}, (
        "ficheros de src/safent_ads/iam/ que importan mcp_oauth (plan.md "
        f"'mcp_oauth -> iam, nunca al reves'): {offenders}"
    )


def test_the_guard_catches_a_relative_import_of_mcp_oauth() -> None:
    """Code review 17-sep (item 5): un `from ...mcp_oauth.domain.client
    import OAuthClient` (relativo, `node.level=3`) entrega `module=
    "mcp_oauth.domain.client"` -- SIN el prefijo `safent_ads.` -- y la
    version anterior de este guard, que solo comparaba por ese prefijo, lo
    habria dejado pasar."""
    relative_import_source = "from ...mcp_oauth.domain.client import OAuthClient\n"

    imported = _imported_module_names(relative_import_source)

    assert any(_mentions_mcp_oauth(name) for name in imported)


def test_the_guard_does_not_flag_an_unrelated_relative_import() -> None:
    unrelated_source = "from ...iam.domain.email import Email\n"

    imported = _imported_module_names(unrelated_source)

    assert not any(_mentions_mcp_oauth(name) for name in imported)
