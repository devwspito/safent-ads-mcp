"""Guarda arquitectonica de las reglas globales ("el dominio y la aplicacion
no mezclan infraestructura"): ningun modulo bajo `*/application/` de
`safent_ads` puede importar de `*/infrastructure/`. `application` depende de
puertos (`Protocol`) declarados en su propio `ports.py`; quien implementa el
puerto y se lo pasa al caso de uso es siempre `infrastructure` (o el
composition root), nunca al reves -- el mismo criterio que ya vigila
`test_mcp_does_not_import_panel.py` para el limite `mcp`/`panel`.

Analiza el AST de cada fichero en vez de importar los paquetes: una
comprobacion en tiempo de import no detectaria un `import` puesto dentro de
una funcion y nunca ejecutado en el proceso de test.

Sin excepciones: las tres violaciones encontradas al extender esta guarda
(`accounts.application.complete_oauth_connect`,
`accounts.application.register_meta_system_user_token`,
`broker.application.oauth_connect_flow`) ya estan corregidas -- el
puerto/excepcion que necesitaban vive en su propio `application/ports.py`
o `application/errors.py`.

I-1 (revision final T130): la guarda original solo miraba
`safent_ads.*.infrastructure`, asi que tres modulos de `rules.application
.read_models` abrian SQL directamente importando `sqlalchemy` -- un paquete
de terceros que la guarda no vigilaba. `test_no_forbidden_third_party_
infrastructure_import` cierra ese punto ciego: ni `application` ni `domain`
pueden importar frameworks de infraestructura (`sqlalchemy`, `asyncpg`,
`httpx`, `aiogram`, `fastapi`), en ninguna parte del arbol, con lista de
permitidos vacia por defecto -- cualquier entrada exigiria una justificacion
propia, linea a linea, nunca en bloque."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "safent_ads"

_FORBIDDEN_THIRD_PARTY_INFRASTRUCTURE: Final = frozenset(
    {"sqlalchemy", "asyncpg", "httpx", "aiogram", "fastapi"}
)

# Ruta (relativa a `_SRC_ROOT`) -> paquetes de terceros permitidos ahi, con
# la razon por la que ese caso concreto SI es aplicacion/dominio puro.
# Vacia a proposito (I-1, revision final T130): la unica excepcion
# documentada del informe, `rules/domain/rule_catalog_schema.py` con
# `pydantic` para validar el catalogo como dato, no esta en la lista
# prohibida de arriba y por eso no necesita entrada aqui.
_ALLOWED_THIRD_PARTY_INFRASTRUCTURE_IMPORTS: Final[dict[str, frozenset[str]]] = {}


def _imported_module_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _infrastructure_imports(path: Path) -> set[str]:
    imported = _imported_module_names(path.read_text())
    return {
        name
        for name in imported
        if name.startswith("safent_ads.") and "infrastructure" in name.split(".")
    }


def _third_party_infrastructure_imports(path: Path) -> set[str]:
    imported = _imported_module_names(path.read_text())
    top_level_packages = {name.split(".")[0] for name in imported}
    return top_level_packages & _FORBIDDEN_THIRD_PARTY_INFRASTRUCTURE


def _layer_source_files(layer: str) -> list[Path]:
    return [
        path for path in _SRC_ROOT.rglob("*.py") if layer in path.relative_to(_SRC_ROOT).parts
    ]


def _application_source_files() -> list[Path]:
    return _layer_source_files("application")


def _domain_source_files() -> list[Path]:
    return _layer_source_files("domain")


def test_no_application_source_file_imports_infrastructure() -> None:
    offenders = {
        str(path.relative_to(_SRC_ROOT)): forbidden
        for path in _application_source_files()
        if (forbidden := _infrastructure_imports(path))
    }
    assert offenders == {}, f"application importa infrastructure: {offenders}"


def test_no_forbidden_third_party_infrastructure_import() -> None:
    offenders: dict[str, set[str]] = {}
    for path in [*_application_source_files(), *_domain_source_files()]:
        rel = str(path.relative_to(_SRC_ROOT))
        found = _third_party_infrastructure_imports(path)
        allowed = _ALLOWED_THIRD_PARTY_INFRASTRUCTURE_IMPORTS.get(rel, frozenset())
        unjustified = found - allowed
        if unjustified:
            offenders[rel] = unjustified
    assert offenders == {}, (
        f"application/domain importan infraestructura de terceros sin puerto "
        f"de por medio (anadir a _ALLOWED_THIRD_PARTY_INFRASTRUCTURE_IMPORTS "
        f"solo con justificacion): {offenders}"
    )
