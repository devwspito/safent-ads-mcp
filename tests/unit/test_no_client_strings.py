"""Detector de cadenas de cliente: el motor publicable no nombra a ningun
despliegue concreto (contracts/ci-guard.md §1, plan.md §6). Hermano de
`test_no_vendor_specific_names.py` (mismo patron: una definicion, varios
consumidores) -- los tres tests que afirman que el catalogo MCP no nombra a
nadie (`test_mcp_instructions_and_descriptions.py`, `test_kit_tools.py`,
`test_catalog_registries_by_permission.py`) usan `has_client_string` de
aqui en vez de escribir el literal.

**Este modulo no contiene las cadenas.** Guarda sus `sha256`: un detector
que viaja al repo publico con la lista de clientes dentro publica
exactamente lo que existe para no publicar (la ironia la senalo la
revision de seguridad de T044). Con digests, el arbol publico se lleva la
capacidad de comprobar y no la lista.

Que eso funcione exige tokenizar en vez de buscar subcadenas: una palabra
metida dentro de un dominio (`ads.<algo>.center`) no es una subcadena que
se pueda digerir, pero si es un token de esa linea. Se generan, por linea:
cada tirada de `[a-z0-9._-]`, todos sus trozos contiguos de piezas, esos
mismos trozos con los separadores convertidos en espacios, los pares de
palabras adyacentes y -- para no perder lo que un `search()` sobre texto si
veia -- cada rodaja de caracteres de la tirada cuya LONGITUD coincida con
la de alguna cadena buscada. Esa ultima es la que recupera la semantica de
subcadena: una cadena pegada a lo que tiene al lado (`<algo>2`,
`mi<algo>`, un identificador en camelCase) esta dentro de UNA sola tirada,
sin separador que la delate.

El conjunto de longitudes es publico (`CLIENT_STRING_LENGTHS`) porque no
dice nada: cuantos caracteres mide cada cadena, no cuales.

El texto llano vive SOLO en el modulo de politica del repo privado, que no
viaja, con dos pruebas que impiden que las dos definiciones se separen: los
digests de aqui son los de esa lista, y cada cadena de esa lista sigue
disparando este detector.

Este modulo es solo el DETECTOR y viaja al repo publico con el motor. La
politica del repo privado -- que rutas se perdonan, que borra un export,
cuantos ofensores quedan pendientes -- vive aparte, en su propio modulo,
que NO viaja: en el arbol publico no existe la relacion privado↔estandar
que esa politica describe (T042)."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SELF_PATH = Path(__file__).resolve()
# La misma ruta, relativa a la raiz que se escanee: al barrer un arbol
# EXPORTADO (`CLIENT_STRINGS_ROOT`) el fichero que se encuentra ahi es una
# copia de este, no este -- `path == SELF_PATH` no la reconoceria y el
# propio detector contaria como ofensor (mismo criterio que
# `test_no_vendor_specific_names.py::KNOWN_DETECTION_PATTERNS`: el
# detector no es una fuga).
SELF_RELATIVE = SELF_PATH.relative_to(ROOT)


@lru_cache(maxsize=1 << 16)
def digest_of(token: str) -> str:
    """El `sha256` de un token ya normalizado. Publica a proposito: la usa
    la prueba del repo privado que comprueba que los digests de abajo son
    los de su lista en texto llano.

    Con cache porque un barrido del arbol entero repite los mismos tokens
    miles de veces (`self`, `def`, `import`): sin ella, el barrido paga un
    sha256 por aparicion en vez de uno por token distinto."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# `sha256` de cada cadena de cliente. Nadie puede leer aqui a quien
# nombran; cualquiera puede comprobar si un texto las contiene.
CLIENT_STRING_DIGESTS: frozenset[str] = frozenset(
    {
        "d84c4718f4cc2ce031b3ba2b73ece0e3b898aec04b133f693141b74cfc1d7eb1",
        "b7fafd6d1d3437d5a7061d3cc1b2654c604d6526ac477791e052e565b7b30b37",
        "c64fce8a5be1dff04215b506f77b4470022731e9f441363a59ad2ba0d54fc529",
        "6448ae6f87b17bd7a571ffea88f0ec6aada8385ac023c1af4f925879a138825d",
        "fc3841261e0feecc6a60545ecb4b1862f28a23ffd3ff7bde364ada3bf90bfa1b",
        "f0e9b63d0c7739a3b4f1eb7aee16b10ec05dbe1e9cf4079598aebc53bcd42ff2",
        "9ec742508aac968da9349a2979f8dd01d48e5e218a75b6ea34a81fbc52b0b8f7",
        "79e2a3b6774d8b077073f655c661456f35ed0c05c874fdaed44ce9828aed71e6",
        "4b2b8ba40fe90cac54984b4c747898d7474edb789f0839f21a812d9148c6bded",
    }
)

# Lo que NO es un nombre, sino una FORMA: un hostname con una IP dentro
# (`<a>-<b>-<c>-<d>.sslip.io` y sus primos `nip.io`), que es como se sirve
# una instancia antes de que el DNS del dominio propio apunte. No nombra a
# nadie, asi que se queda como expresion regular y cubre cualquier
# despliegue, no solo uno.
STRUCTURAL_CLIENT_RE = re.compile(
    r"\b\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}\.(?:sslip|nip)\.io\b", re.IGNORECASE
)

# Las LONGITUDES de las cadenas buscadas. Publicas a proposito: un numero
# no nombra a nadie, y sin ellas no se puede recuperar la semantica de
# subcadena sin probar todas las rodajas posibles de cada linea.
CLIENT_STRING_LENGTHS: frozenset[int] = frozenset({8, 10, 11, 12, 16, 20, 22})

_RAW_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_SEPARATORS_RE = re.compile(r"[._-]+")


# Cuantas piezas separadas por `.`/`_`/`-` puede tener un token. La cadena
# mas larga que se persigue hoy tiene cuatro; seis deja margen sin convertir
# el barrido en cuadratico sobre una ruta larga. Si una cadena nueva tuviera
# mas, la prueba del modulo de politica lo dice.
_MAX_TOKEN_PIECES = 6


def _tokens(line: str) -> set[str]:
    """Los tokens de una linea, en las formas en las que una cadena de
    cliente puede aparecer escrita.

    De cada tirada se sacan TODOS los trozos contiguos de sus piezas, no
    solo la tirada entera y sus piezas sueltas: en
    `un-id-de-proyecto.iam.gserviceaccount.com` la cadena que importa es un
    trozo de en medio, y en `ads.<algo>.center` es una pieza suelta. Cada
    trozo se emite dos veces: como esta escrito y con los separadores
    convertidos en espacios, para que `dos.palabras` y `dos_palabras` valgan
    tanto como `Dos Palabras`."""
    tokens: set[str] = set()
    palabras: list[str] = []
    for bruta in _RAW_TOKEN_RE.findall(line.lower()):
        tirada = bruta.strip("._-")
        if not tirada:
            continue
        palabras.append(tirada)
        piezas = _SEPARATORS_RE.split(tirada)
        separadores = _SEPARATORS_RE.findall(tirada)
        for inicio in range(len(piezas)):
            for fin in range(inicio + 1, min(inicio + _MAX_TOKEN_PIECES, len(piezas)) + 1):
                trozo = piezas[inicio:fin]
                cola = [*separadores[inicio : fin - 1], ""]
                tokens.add("".join(pieza + sep for pieza, sep in zip(trozo, cola, strict=True)))
                tokens.add(" ".join(trozo))
    tokens.update(
        f"{izquierda} {derecha}"
        for izquierda, derecha in zip(palabras, palabras[1:], strict=False)
    )
    tokens.update(_rodajas(palabras))
    return tokens


def _rodajas(tiradas: list[str]) -> set[str]:
    """Cada rodaja de caracteres de cada tirada cuya longitud coincida con
    la de alguna cadena buscada.

    Es lo que recupera la semantica del `search()` de antes: una cadena
    pegada a lo que tiene al lado vive dentro de UNA tirada y no hay
    separador que la separe en piezas. Cuesta `len(tirada)` rodajas por
    longitud buscada -- hoy siete --, no las `n**2` de probarlas todas."""
    rodajas: set[str] = set()
    for tirada in tiradas:
        for longitud in CLIENT_STRING_LENGTHS:
            if longitud > len(tirada):
                continue
            rodajas.update(
                tirada[inicio : inicio + longitud]
                for inicio in range(len(tirada) - longitud + 1)
            )
    return rodajas


def has_client_string(text: str) -> bool:
    """`True` si el texto nombra a algun cliente. Es la pregunta que hacen
    los tres tests del catalogo MCP y el del instalador."""
    return any(_line_has_client_string(linea) for linea in text.splitlines() or [text])


def _line_has_client_string(line: str) -> bool:
    if STRUCTURAL_CLIENT_RE.search(line):
        return True
    return any(digest_of(token) in CLIENT_STRING_DIGESTS for token in _tokens(line))


# Directorios que no son el arbol: dependencias, artefactos de build y
# caches de herramientas (`.pytest_cache` guarda los IDs de las pruebas,
# que no son contenido del repo y ensucian el barrido).
_SKIP_DIR_NAMES = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".git",
        "dist",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "htmlcov",
    }
)


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIR_NAMES]
        files.extend(Path(dirpath) / name for name in filenames)
    return files


def _is_allowed(relative_path: Path, allowed_paths: Sequence[str]) -> bool:
    text = relative_path.as_posix()
    return any(text == allowed or text.startswith(f"{allowed}/") for allowed in allowed_paths)


def find_client_string_offenders(
    root: Path, *, allowed_paths: Sequence[str] = ()
) -> list[str]:
    """`ruta:linea` de cada coincidencia bajo `root`, ordenado.

    `allowed_paths` es la lista permitida del barrido: el repo privado
    pasa la suya (contracts/ci-guard.md §1) y el modo exportacion
    (`CLIENT_STRINGS_ROOT`) no pasa ninguna -- "en modo exportacion no hay
    lista permitida". La unica excepcion que no se puede desactivar es
    este propio fichero: el detector no es una fuga.
    """
    offenders: list[str] = []
    for path in _iter_files(root):
        relative = path.relative_to(root)
        if path == SELF_PATH or relative == SELF_RELATIVE:
            continue
        if _is_allowed(relative, allowed_paths):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if _line_has_client_string(line):
                offenders.append(f"{relative.as_posix()}:{line_number}")
    return sorted(offenders)


# ── casos de prueba del detector (contracts/ci-guard.md §1) ─────────────


# Ninguna cadena de cliente de verdad, aqui tampoco: las pruebas del
# detector siembran UNA suya y la dan de alta en el conjunto de digests.
# Lo que comprueban es el mecanismo -- que encuentra lo que tiene que
# encontrar, que la lista permitida se respeta y que sin ella no se
# perdona nada --, no quien esta en la lista.
_SEMILLA = "cliente-de-prueba"


@pytest.fixture
def con_semilla(monkeypatch: pytest.MonkeyPatch) -> None:
    modulo = sys.modules[__name__]
    monkeypatch.setattr(
        modulo, "CLIENT_STRING_DIGESTS", CLIENT_STRING_DIGESTS | {digest_of(_SEMILLA)}
    )
    # Tambien la LONGITUD: sin ella no se generan las rodajas de ese tamano
    # y la semilla pegada a otra cosa no se detectaria.
    monkeypatch.setattr(
        modulo, "CLIENT_STRING_LENGTHS", CLIENT_STRING_LENGTHS | {len(_SEMILLA)}
    )


@pytest.mark.usefixtures("con_semilla")
def test_a_seeded_offender_inside_scope_is_detected(tmp_path: Path) -> None:
    (tmp_path / "offender.py").write_text(f"# esto menciona {_SEMILLA} aqui\n")

    offenders = find_client_string_offenders(tmp_path)

    assert offenders == ["offender.py:1"]


@pytest.mark.usefixtures("con_semilla")
def test_a_seeded_offender_glued_to_its_neighbours_is_detected(tmp_path: Path) -> None:
    """Pegada a lo que tiene al lado, la cadena vive dentro de UNA tirada y
    no hay separador que la parta en piezas. Es lo que un `search()` sobre
    texto veia y las rodajas por longitud recuperan: sin ellas, un
    identificador en camelCase de `panel/src` se colaba entero."""
    (tmp_path / "offender.ts").write_text(f"const x = new x{_SEMILLA}yClient();\n")

    offenders = find_client_string_offenders(tmp_path)

    assert offenders == ["offender.ts:1"]


@pytest.mark.usefixtures("con_semilla")
def test_the_same_string_inside_an_allowed_path_is_not_detected(tmp_path: Path) -> None:
    allowed_dir = tmp_path / "despliegue" / "instancia"
    allowed_dir.mkdir(parents=True)
    (allowed_dir / "ads.env.example").write_text(f"ADS_BRAND_NAME={_SEMILLA}\n")

    offenders = find_client_string_offenders(
        tmp_path, allowed_paths=("despliegue/instancia",)
    )

    assert offenders == []


@pytest.mark.usefixtures("con_semilla")
def test_without_an_allowed_list_the_same_file_is_detected(tmp_path: Path) -> None:
    """La contraparte: sin lista permitida -- el modo exportacion -- la
    MISMA ruta que el repo privado perdona se detecta, porque el arbol
    exportado nunca deberia contenerla en absoluto."""
    allowed_dir = tmp_path / "despliegue" / "instancia"
    allowed_dir.mkdir(parents=True)
    (allowed_dir / "ads.env.example").write_text(f"ADS_BRAND_NAME={_SEMILLA}\n")

    offenders = find_client_string_offenders(tmp_path)

    assert offenders == ["despliegue/instancia/ads.env.example:1"]


# ── Modo exportacion como programa (contracts/ci-guard.md §1) ───────────


_EXIT_CLEAN = 0
_EXIT_OFFENDERS = 1
_EXIT_USAGE = 2


def _main() -> int:
    """`CLIENT_STRINGS_ROOT=<dir> python3 tests/unit/test_no_client_strings.py`:
    la verificacion (a) de `contracts/export-standard-cli.md` sobre un
    arbol ya exportado, sin lista permitida. Es la MISMA funcion que usan
    los tests -- el exportador no reimplementa la guarda, la invoca.

    Imprime un `ruta:linea` por ofensor (stdout, ordenado) y un resumen en
    stderr. `0` limpio · `1` hay coincidencias · `2` uso incorrecto."""
    root_text = os.environ.get("CLIENT_STRINGS_ROOT", "").strip()
    if not root_text:
        print("CLIENT_STRINGS_ROOT es obligatorio: la raiz del arbol a barrer", file=sys.stderr)
        return _EXIT_USAGE
    root = Path(root_text)
    if not root.is_dir():
        print(f"CLIENT_STRINGS_ROOT no es un directorio: {root}", file=sys.stderr)
        return _EXIT_USAGE
    offenders = find_client_string_offenders(root.resolve())
    for offender in offenders:
        print(offender)
    if offenders:
        print(f"{len(offenders)} cadenas de cliente bajo {root}", file=sys.stderr)
        return _EXIT_OFFENDERS
    return _EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(_main())
