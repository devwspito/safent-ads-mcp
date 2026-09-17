"""T084 (002b threat-model.md C-72/MENOR-3): el numero de sitios que
construyen un cliente httpx SIN pasar por el guard unico de egreso
(`shared/net/safe_egress.build_pinned_async_client`) queda fijado en 11.
La revision de seguridad de 17-sep encontro que la cifra anotada en el
documento (10) llevaba desactualizada desde 0.2.40 -- el numero real, hoy y
entonces, es 11. Este test fija ESE numero: crecer exige tocar este test a
proposito, nunca en silencio.

El canje OIDC contra Google (`iam/infrastructure/google_oidc_provider.py`)
es el unico egreso nuevo que 002b añade -- y es, precisamente, el que NO
debe sumar a la cuenta: sale por `build_pinned_async_client`, no por un
`httpx.AsyncClient(` desnudo.

Code review 17-sep (nit): la version anterior contaba coincidencias de
TEXTO (`"httpx.AsyncClient(" in linea`) -- un comentario o una cadena que
solo MENCIONE esa forma (por ejemplo, explicando por que no usarla, como
hace el docstring de este mismo fichero) habria forzado una subida
artificial de `_EXPECTED_COUNT` sin que exista ninguna llamada nueva de
verdad. Cuenta ahora la LLAMADA real via AST (`ast.Call` con
`func.attr == "AsyncClient"` sobre el nombre `httpx`)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "safent_ads"
_EXPECTED_COUNT: Final[int] = 11
_OIDC_PROVIDER_FILE: Final[str] = "iam/infrastructure/google_oidc_provider.py"


def _is_httpx_async_client_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "AsyncClient"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "httpx"
    )


def _naked_httpx_async_client_calls(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sum(1 for node in ast.walk(tree) if _is_httpx_async_client_call(node))


def _count_naked_httpx_async_client() -> int:
    return sum(_naked_httpx_async_client_calls(path) for path in sorted(_SRC_ROOT.rglob("*.py")))


def _imports_build_pinned_async_client(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "build_pinned_async_client" for alias in node.names
        ):
            return True
    return False


def test_the_count_is_pinned_at_eleven() -> None:
    count = _count_naked_httpx_async_client()
    assert count == _EXPECTED_COUNT, (
        f"httpx.AsyncClient( desnudo aparece {count} veces en src/, se esperaban "
        f"{_EXPECTED_COUNT} -- si es un sitio nuevo legitimo, actualiza este test Y "
        "threat-model.md C-72 a proposito; si no, usa build_pinned_async_client."
    )


def test_the_only_oidc_exchange_path_uses_the_safe_egress_guard() -> None:
    oidc_provider = _SRC_ROOT / _OIDC_PROVIDER_FILE
    assert oidc_provider.is_file(), f"no existe {_OIDC_PROVIDER_FILE}"

    assert _naked_httpx_async_client_calls(oidc_provider) == 0, (
        "el canje OIDC construye un httpx.AsyncClient( desnudo -- debe salir "
        "unicamente por shared.net.safe_egress.build_pinned_async_client"
    )
    assert _imports_build_pinned_async_client(oidc_provider), (
        "el canje OIDC ya no importa build_pinned_async_client -- "
        "el guard unico de egreso dejo de aplicarse al canje contra Google"
    )


def test_a_comment_mentioning_the_pattern_never_inflates_the_count(tmp_path: Path) -> None:
    """Nit (code review 17-sep): un comentario o una cadena que solo
    MENCIONE `httpx.AsyncClient(` -- explicando, por ejemplo, por que no
    usarlo -- no es una llamada real y no debe contarse."""
    source = tmp_path / "commented_only.py"
    source.write_text(
        "# nunca uses httpx.AsyncClient( aqui -- pasa por build_pinned_async_client\n"
        'MESSAGE = "se detecto un httpx.AsyncClient( desnudo"\n'
    )

    assert _naked_httpx_async_client_calls(source) == 0


def test_a_real_call_is_still_counted(tmp_path: Path) -> None:
    source = tmp_path / "real_call.py"
    source.write_text("import httpx\n\nclient = httpx.AsyncClient()\n")

    assert _naked_httpx_async_client_calls(source) == 1
