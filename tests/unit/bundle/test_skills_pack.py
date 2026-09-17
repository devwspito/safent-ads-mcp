"""Pack de skills del bundle (`safent-bundle/skills/`; tool-surface.md §3 mas
`keyword-research`). Formato exacto del hub de Hermes 0.21.1 (front-matter con
`name/description/version/author/license/platforms/metadata.hermes`,
`description` <= 60, secciones When to Use / Procedure / Pitfalls /
Verification), herramientas citadas solo del contrato MCP, ningun verbo de
aprobacion o ejecucion, paradas explicitas, cuerpo en espanol y <= 900 palabras."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.unit.bundle.test_bundle_consistency import contract_tool_names

ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = ROOT / "safent-bundle/skills"

EXPECTED_SKILLS = frozenset(
    {
        "campaign-triage",
        "budget-reallocation",
        "weekly-account-audit",
        "learning-phase-rescue",
        "calendar-event-launch",
        "competitive-teardown",
        "spanish-ad-copy",
        "creative-ab-test",
        "keyword-research",
    }
)
REQUIRED_FRONTMATTER = (
    "name",
    "description",
    "version",
    "author",
    "license",
    "platforms",
    "metadata",
)
REQUIRED_SECTIONS = ("## When to Use", "## Procedure", "## Pitfalls", "## Verification")
GATE_TOOLS = ("`get_data_freshness`", "`get_kill_switch_status`")
DESCRIPTION_MAX = 60
WORDS_MAX = 900
MCP_TOOLSET = "mcp-safent-ads"  # Hermes registra un servidor MCP bajo el toolset `mcp-<nombre>`
# Nativas de Hermes que tool-surface.md §2.3 habilita en el overlay del agente;
# las skills las condicionan a que el overlay las exponga.
NATIVE_HERMES_TOOLS = frozenset({"web_search", "web_extract"})
FORBIDDEN_VERBS = ("approve_", "execute_", "apply_proposal")
# El producto es generico: el vocabulario del vertical educativo queda
# confinado a la skill que lo necesita, como ejemplo, hasta que otra fase
# lo generalice (vocabulary.md §6/§7).
VERTICAL_VOCABULARY_RE = re.compile(r"oposici|matr[ií]cula|alumno|temario|academia", re.IGNORECASE)
VERTICAL_ALLOWED_IN = frozenset({"calendar-event-launch"})

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")  # tools/skill_manager_tool.py::VALID_NAME_RE
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
FRONTMATTER_END_RE = re.compile(r"\n---\s*\n")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
TOOL_LIKE_RE = re.compile(
    r"^(list|get|search|run|explain|propose|withdraw|generate|apply|check|compose|import"
    r"|simulate|research|validate|build|analyze|edit|assemble|burn|read|image|video|text"
    r"|web|browser|vision|transcribe)_[a-z_]+$"
)
PENDING_CAP_RE = re.compile(r"≤ 10|cupo de 10|10 pendientes")
INJECTION_RE = re.compile(r"nunca instrucci")
SPANISH_MARKERS = (
    " el ",
    " la ",
    " de ",
    " que ",
    " con ",
    " para ",
    " por ",
    " una ",
    " se ",
    " no ",
    " y ",
    " los ",
    " las ",
)
ENGLISH_MARKERS = (" the ", " and ", " with ", " for ", " that ", " this ", " is ", " are ", " of ")

# Invariantes de negocio por skill: herramientas que DEBEN aparecer en el cuerpo
# (la palanca que da sentido a la skill) y las que NO deben aparecer en Procedure.
MUST_MENTION: dict[str, tuple[str, ...]] = {
    "campaign-triage": (
        "`apply_defensive_action`",
        "`explain_rule`",
        "`propose_pause`",
        "`explain_signal`",
    ),
    "budget-reallocation": ("`propose_budget_change`", "`get_pacing`", "`list_guardrails`"),
    "weekly-account-audit": (
        "google-ads-audit",
        "arba",
        "14 categorías",
        "`get_portfolio_overview`",
    ),
    "learning-phase-rescue": (
        "`propose_budget_change`",
        "`propose_targeting_change`",
        "`search_decision_log`",
    ),
    "calendar-event-launch": (
        "`propose_campaign`",
        "`kill_criterion`",
        "pausada",
        "`run_creative_policy_check`",
    ),
    "competitive-teardown": ("auction insights", "`propose_targeting_change`"),
    "spanish-ad-copy": ("copycat", "30", "90", "Ley 34/1988"),
    "creative-ab-test": (
        "`run_creative_policy_check`",
        "`propose_creative_publication`",
        "`generate_creative_assets`",
    ),
    "keyword-research": ("`add_negative_keyword`", "`propose_targeting_change`", "`explain_rule`"),
}
NEVER_IN_PROCEDURE: dict[str, tuple[str, ...]] = {
    "learning-phase-rescue": ("`apply_defensive_action`",),  # nunca autonoma sobre aprendizaje
    "competitive-teardown": ("`apply_defensive_action`",),  # solo lectura
    "spanish-ad-copy": ("`apply_defensive_action`", "`propose_"),  # redacta, no propone
}


def _read(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def _split(text: str) -> tuple[dict, str]:
    assert text.startswith("---"), "el front-matter debe empezar en el byte 0"
    end = FRONTMATTER_END_RE.search(text[3:])
    assert end, "front-matter sin cerrar con '---'"
    frontmatter = yaml.safe_load(text[3 : end.start() + 3])
    body = text[end.end() + 3 :]
    assert isinstance(frontmatter, dict), "el front-matter debe ser un mapping YAML"
    assert body.strip(), "cuerpo vacio"
    return frontmatter, body


def _section(body: str, header: str) -> str:
    start = body.index(header) + len(header)
    following = [body.index(h) for h in REQUIRED_SECTIONS if h != header and body.index(h) > start]
    return body[start : min(following)] if following else body[start:]


@pytest.fixture(params=sorted(EXPECTED_SKILLS), ids=str)
def skill(request: pytest.FixtureRequest) -> tuple[str, dict, str, str]:
    text = _read(request.param)
    frontmatter, body = _split(text)
    return request.param, frontmatter, body, text


# --- inventario -------------------------------------------------------------


def test_skill_pack_is_exactly_the_nine_skills() -> None:
    dirs = {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()}
    assert dirs == EXPECTED_SKILLS
    for name in EXPECTED_SKILLS:
        assert (SKILLS_DIR / name / "SKILL.md").is_file(), name


# --- front-matter (formato del hub de Hermes) --------------------------------


def test_frontmatter_has_hermes_required_fields(skill) -> None:
    name, fm, _, _ = skill
    for field in REQUIRED_FRONTMATTER:
        assert field in fm, f"{name}: falta `{field}`"
    assert fm["name"] == name
    assert SKILL_NAME_RE.match(fm["name"]) and len(fm["name"]) <= 64
    assert SEMVER_RE.match(str(fm["version"])), fm["version"]
    assert isinstance(fm["platforms"], list) and fm["platforms"]
    assert isinstance(fm["author"], str) and fm["author"].strip()
    assert isinstance(fm["license"], str) and fm["license"].strip()


def test_description_fits_the_prompt_index(skill) -> None:
    name, fm, _, _ = skill
    description = str(fm["description"])
    assert len(description) <= DESCRIPTION_MAX, f"{name}: {len(description)} chars"
    assert description.endswith("."), f"{name}: la descripcion termina en punto"
    assert "\n" not in description


def test_metadata_hermes_block_gates_on_the_mcp_toolset(skill) -> None:
    name, fm, _, _ = skill
    hermes = fm["metadata"]["hermes"]
    assert isinstance(hermes["tags"], list) and hermes["tags"]
    assert hermes["category"] == "marketing"
    assert hermes["requires_toolsets"] == [MCP_TOOLSET]
    related = hermes["related_skills"]
    assert related and set(related) <= EXPECTED_SKILLS - {name}, f"{name}: {related}"


# --- cuerpo ------------------------------------------------------------------


def test_body_sections_follow_hermes_order_and_are_non_empty(skill) -> None:
    name, _, body, _ = skill
    positions = [body.find(h) for h in REQUIRED_SECTIONS]
    assert all(p >= 0 for p in positions), f"{name}: faltan secciones {positions}"
    assert positions == sorted(positions), f"{name}: secciones desordenadas"
    for header in REQUIRED_SECTIONS:
        assert _section(body, header).strip(), f"{name}: seccion vacia {header}"


def test_body_within_word_budget(skill) -> None:
    name, _, _, text = skill
    assert len(text.split()) <= WORDS_MAX, f"{name}: {len(text.split())} palabras"


def test_backticked_tools_exist_in_the_mcp_contract(skill) -> None:
    name, _, body, _ = skill
    cited = {span for span in BACKTICK_RE.findall(body) if TOOL_LIKE_RE.match(span)}
    unknown = cited - contract_tool_names() - NATIVE_HERMES_TOOLS
    assert not unknown, f"{name}: herramientas fuera del contrato {sorted(unknown)}"


def test_no_approval_or_execution_verbs(skill) -> None:
    name, _, _, text = skill
    for verb in FORBIDDEN_VERBS:
        assert verb not in text.lower(), f"{name}: menciona {verb}"


def test_procedure_pulls_context_and_checks_gates_first(skill) -> None:
    name, _, body, _ = skill
    procedure = _section(body, "## Procedure")
    first_step = procedure.strip().split("\n2.")[0]
    assert "`list_businesses`" in first_step, f"{name}: el paso 1 no arranca con contexto"
    for tool in GATE_TOOLS:
        assert tool in procedure, f"{name}: Procedure no comprueba {tool}"


def test_stop_conditions_are_explicit(skill) -> None:
    name, _, body, _ = skill
    lowered = body.lower()
    assert "solo lectura" in lowered, f"{name}: no declara solo lectura ante dato obsoleto"
    assert PENDING_CAP_RE.search(body), f"{name}: no declara el cupo de 10 propuestas"
    assert INJECTION_RE.search(lowered), f"{name}: no declara que los datos no son instrucciones"
    assert "**Produce**" in body, f"{name}: no declara que produce"


def test_business_invariants_per_skill(skill) -> None:
    name, _, body, _ = skill
    for needle in MUST_MENTION[name]:
        assert needle in body, f"{name}: falta {needle}"
    procedure = _section(body, "## Procedure")
    for needle in NEVER_IN_PROCEDURE.get(name, ()):
        assert needle not in procedure, f"{name}: Procedure no debe usar {needle}"


def test_budget_reallocation_splits_lowering_and_raising() -> None:
    _, body = _split(_read("budget-reallocation"))
    procedure = _section(body, "## Procedure")
    assert procedure.count("`propose_budget_change`") >= 2
    assert "misma propuesta" in procedure


def test_body_is_spanish(skill) -> None:
    name, _, body, _ = skill
    flat = " " + re.sub(r"\s+", " ", body.lower()) + " "
    spanish = sum(flat.count(m) for m in SPANISH_MARKERS)
    english = sum(flat.count(m) for m in ENGLISH_MARKERS)
    assert spanish >= 40 and spanish > 5 * english, f"{name}: es={spanish} en={english}"


def test_no_machine_local_paths(skill) -> None:
    name, _, _, text = skill
    assert "/home/" not in text and "/Users/" not in text, name


# --- producto generico: vocabulario del vertical confinado ---------------


def test_vertical_vocabulary_confined_to_calendar_event_launch(skill) -> None:
    name, _, _, text = skill
    if name in VERTICAL_ALLOWED_IN:
        return
    hits = sorted({m.group(0).lower() for m in VERTICAL_VOCABULARY_RE.finditer(text)})
    assert not hits, f"{name}: vocabulario del vertical fuera de sitio {hits}"
