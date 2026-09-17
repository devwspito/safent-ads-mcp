"""Safent Ads es un producto estandar: ninguna referencia al vocabulario de
un vertical concreto (formacion/oposiciones) puede aparecer en el codigo,
la configuracion, el bundle, los tests ni los documentos de diseno de
`specs/` -- ni siquiera dentro de listas de palabras prohibidas, regex o
nombres de test (vocabulary.md §7).
`tests/unit/bundle/test_skills_pack.py` ya vigila `safent-bundle/skills/`
con su propio patron de vocabulario vertical (`VERTICAL_VOCABULARY_RE`,
confinado a `calendar-event-launch`); este test cubre el resto del arbol
(y ese fichero, salvo por su propio patron de deteccion, que contiene las
palabras como cadena de regex, no como una fuga real)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# `panel/src/` queda fuera a proposito: esa guarda la anade el carril del
# panel en su propio commit (vocabulary.md §8, orden Backend/Panel).
SCANNED_DIRS = ("src", "config", "safent-bundle", "tests", "specs")
VERTICAL_VOCABULARY_RE = re.compile(
    r"convocatorias?"
    r"|oposici(?:ón|on|ones)"
    r"|matr[ií]culas?"
    r"|(?<!\ben )\bcursos?\b"
    r"|\bcourses?\b"
    r"|enrolments?"
    r"|academia"
    r"|temario"
    r"|especialidad"
    r"|denominacion"
    r"|fecha_(?:inicio|fin)_plazo"
    r"|fecha_examen"
    r"|ventana_abierta"
    r"|\bformaci[oó]n\b"
    r"|\bex[aá]men(?:es)?\b"
    r"|\balumn[oa]s?\b"
    r"|\bopositor(?:es|a|as)?\b"
    r"|\bprofesor(?:es|a|as)?\b"
    r"|\bdocentes?\b"
    r"|\bacad[eé]mic[oa]s?\b",
    re.IGNORECASE,
)
SELF_PATH = Path(__file__).resolve()
# `test_skills_pack.py` declara su propio guardian (`VERTICAL_VOCABULARY_RE`)
# con las mismas palabras como cadena de deteccion, no como una fuga del
# vocabulario -- igual que este fichero es su propia excepcion.
KNOWN_DETECTION_PATTERNS = frozenset(
    {SELF_PATH, ROOT / "tests/unit/bundle/test_skills_pack.py"}
)
# `test_vocabulary.py` siembra el esquema PRE-`0024` (`courses`/
# `convocatorias`/`conversion_kind='enrolment'`) a proposito para probar el
# propio renombre -- mismo motivo que excluye `alembic/versions/`: es
# historia inmutable del esquema, no vocabulario del producto en vivo.
ALLOWED_MIGRATION_HISTORY_TESTS = frozenset(
    {ROOT / "tests/integration/migrations/test_vocabulary.py"}
)
# Unico sitio del producto donde el vocabulario del vertical puede vivir,
# como ejemplo (vocabulary.md §6/§7).
ALLOWED_VERTICAL_SKILL = ROOT / "safent-bundle/skills/calendar-event-launch"
# `config/brand/*.yaml` es contenido de ejemplo del kit de marca del
# cliente: puede ilustrar un vertical sin que eso sea una fuga del producto.
ALLOWED_BRAND_EXAMPLES = ROOT / "config/brand"
# `vocabulary.md` es el mapa vinculante del renombre: contiene las palabras
# del vertical en la columna «Antes», que es justo su razon de ser. Se
# reconoce por NOMBRE dentro de `specs/`, no por una ruta escrita a mano:
# los specs son diseno del repo privado y el export los borra (T042,
# plan.md §7.1), asi que escribir aqui la ruta de uno seria una referencia
# colgante en un fichero que si viaja -- lo que persigue la verificacion
# (e) de `export-standard-cli.md`.
SPECS_DIR = ROOT / "specs"
ALLOWED_SPEC_FILENAME = "vocabulary.md"


def _is_allowed(path: Path) -> bool:
    if path in KNOWN_DETECTION_PATTERNS or path in ALLOWED_MIGRATION_HISTORY_TESTS:
        return True
    if path.name == ALLOWED_SPEC_FILENAME and SPECS_DIR in path.parents:
        return True
    if ALLOWED_VERTICAL_SKILL in path.parents:
        return True
    return path.suffix == ".yaml" and ALLOWED_BRAND_EXAMPLES in path.parents


def _files_to_scan() -> list[Path]:
    files: list[Path] = []
    for directory in SCANNED_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            continue
        files.extend(p for p in base.rglob("*") if p.is_file())
    return files


def test_no_vertical_vocabulary_anywhere_in_the_product() -> None:
    offenders = []
    for path in _files_to_scan():
        if _is_allowed(path):
            continue
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if VERTICAL_VOCABULARY_RE.search(text):
            offenders.append(str(path.relative_to(ROOT)))

    assert not offenders, f"vocabulario del vertical filtrado en: {offenders}"
