"""INV-17 (tasks.md T013, plan.md T024;
data-model.md §"Claves de dinero (regla transversal)"; threat-model.md
T-6/ME-7, casilla 10): una sola forma de dinero en todo el arbol declarado.

Las claves de dinero del plan son exactamente `daily_budget`, `cpc_bid`,
`target_cpa`, `cpc_bid_ceiling` -- lista cerrada. `target_roas` es un
ratio (Decimal) y nunca pasa por `Money`; la separacion es de TIPO
(campos distintos en dataclasses distintas: `GoogleBidding` variantes en
`proposals/domain/google_bidding.py`), no de valor. Micros (`x
1_000_000`) y `float` no viven en el arbol firmado: la multiplicacion a
micros es una conversion de salida hacia el broker/adaptador de Google,
nunca del dominio.

Dos tecnicas, igual que `tests/architecture/test_no_channel_literals_
outside_spec.py`: introspeccion de AST sobre el arbol declarado
(`packages/domain/planned_tree.py`) y las pujas (`proposals/domain/
google_bidding.py`) para la lista cerrada; comportamiento real de
`google_bidding.py` para las dos direcciones dinero/ratio.

Deuda explicita (`_PENDIENTES_CONOCIDAS`): vacia. `MaximizeClicks.cpc_bid_ceiling`
ya exige positivo en `__post_init__`, igual que `MaximizeConversions.target_cpa`;
el techo (`_PENDIENTES_CEILING`) queda a cero para que ninguna deuda nueva entre
en silencio."""

from __future__ import annotations

import ast
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from safent_ads.proposals.domain.google_bidding import (
    GoogleBiddingError,
    MaximizeClicks,
    MaximizeConversions,
    MaximizeConversionValue,
)
from safent_ads.proposals.domain.money import Money

_SRC_ROOT = Path(__file__).resolve().parents[4] / "src" / "safent_ads"

# data-model.md INV-17: la lista cerrada de claves de dinero "del arbol".
_MONEY_KEYS: Final[frozenset[str]] = frozenset(
    {"daily_budget", "cpc_bid", "target_cpa", "cpc_bid_ceiling"}
)

# Los dos unicos ficheros que hoy declaran el arbol firmado y sus pujas
# tipadas -- ni `google_channel_spec.py` (su `min_daily_budget` es un
# suelo de configuracion por canal, nunca una clave del plan) ni
# `packages/domain/values.py` (`PackageBudget` es contexto de aprobacion,
# no el nativo que se firma y se envia a Google) entran aqui.
_PLAN_TREE_MODULES: Final[tuple[Path, ...]] = (
    _SRC_ROOT / "packages" / "domain" / "planned_tree.py",
    _SRC_ROOT / "proposals" / "domain" / "google_bidding.py",
)

# El resto de la tuberia del plan firmado: construccion, codec de
# persistencia y los dos validadores nativos de campaign_creation.py /
# ad_child_creation.py (T014/T015 los migran a la tabla, pero ya hoy no
# deben tener micros ni float).
_PLAN_PIPELINE_MODULES: Final[tuple[Path, ...]] = (
    *_PLAN_TREE_MODULES,
    _SRC_ROOT / "packages" / "domain" / "package_hash.py",
    _SRC_ROOT / "packages" / "infrastructure" / "package_codec.py",
    _SRC_ROOT / "proposals" / "domain" / "money.py",
    _SRC_ROOT / "proposals" / "domain" / "google_channel_spec.py",
    _SRC_ROOT / "proposals" / "domain" / "campaign_creation.py",
    _SRC_ROOT / "proposals" / "domain" / "ad_child_creation.py",
)

_MICROS_FACTOR: Final[int] = 1_000_000

# Deuda conocida y congelada (ver docstring del modulo). Ninguna prueba de
# este fichero exige cerrarla; el techo evita que crezca sin que alguien
# lo note.
_PENDIENTES_CONOCIDAS: Final[frozenset[str]] = frozenset()
_PENDIENTES_CEILING: Final[int] = 0


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _annotation_is_money(annotation: ast.expr) -> bool:
    if isinstance(annotation, ast.Name):
        return annotation.id == "Money"
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_is_money(annotation.left) or _annotation_is_money(annotation.right)
    return False


def _money_typed_field_names(path: Path) -> set[str]:
    return {
        node.target.id
        for node in ast.walk(_parse(path))
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and _annotation_is_money(node.annotation)
    }


def test_la_lista_de_claves_de_dinero_es_cerrada() -> None:
    assert len(_PENDIENTES_CONOCIDAS) <= _PENDIENTES_CEILING, (
        "la deuda de INV-17 solo puede encoger, nunca crecer sin revisar el techo: "
        f"{sorted(_PENDIENTES_CONOCIDAS)}"
    )

    found: set[str] = set()
    for path in _PLAN_TREE_MODULES:
        found |= _money_typed_field_names(path)

    assert found == _MONEY_KEYS, (
        f"campos tipados como Money en el arbol declarado: {sorted(found)}; "
        f"la lista cerrada de data-model.md INV-17 es: {sorted(_MONEY_KEYS)}"
    )


def test_ningun_validador_de_dinero_acepta_target_roas() -> None:
    """`MaximizeConversions` y `MaximizeClicks` son los unicos tipos de
    puja que llevan dinero (`target_cpa`, `cpc_bid_ceiling`). Ninguno de
    los dos declara un campo `target_roas`: el `__init__` generado por
    `dataclass` lo rechaza antes de leer nada, sin ambiguedad de valor."""
    for money_bearing_bidding in (MaximizeConversions, MaximizeClicks):
        with pytest.raises(TypeError):
            money_bearing_bidding(target_roas=Decimal("4.0"))  # type: ignore[call-arg]


def test_ningun_validador_de_ratio_acepta_amount_currency() -> None:
    with pytest.raises(GoogleBiddingError):
        MaximizeConversionValue(target_roas={"amount": "10.00", "currency": "EUR"})  # type: ignore[arg-type]


def _has_float_literal(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Constant) and isinstance(node.value, float) for node in ast.walk(tree)
    )


def _has_float_cast(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float"
        for node in ast.walk(tree)
    )


def _has_micros_multiplication(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult)):
            continue
        for operand in (node.left, node.right):
            if isinstance(operand, ast.Constant) and operand.value == _MICROS_FACTOR:
                return True
    return False


def test_no_hay_micros_ni_float_en_el_plan_firmado() -> None:
    offenders: dict[str, str] = {}
    for path in _PLAN_PIPELINE_MODULES:
        tree = _parse(path)
        if _has_float_literal(tree):
            offenders[str(path)] = "float literal"
        elif _has_float_cast(tree):
            offenders[str(path)] = "float(...)"
        elif _has_micros_multiplication(tree):
            offenders[str(path)] = "x 1_000_000 (micros)"

    assert offenders == {}, f"micros/float encontrados en el plan firmado: {offenders}"


@pytest.mark.parametrize(
    ("build_bidding", "expected_exception"),
    [
        pytest.param(
            lambda: MaximizeConversionValue(target_roas=Money.of("10.00")),
            GoogleBiddingError,
            id="target_roas_rechaza_una_instancia_money",
        ),
        pytest.param(
            lambda: MaximizeConversionValue(
                target_roas={"amount": "10.00", "currency": "EUR"}
            ),
            GoogleBiddingError,
            id="target_roas_rechaza_forma_amount_currency",
        ),
        pytest.param(
            lambda: MaximizeConversions(target_cpa=Decimal("4.0000")),
            AttributeError,
            id="target_cpa_rechaza_un_decimal_desnudo_con_forma_de_ratio",
        ),
        pytest.param(
            lambda: MaximizeConversions(target_cpa="4.0000"),
            AttributeError,
            id="target_cpa_rechaza_la_cadena_de_ratio_del_cable",
        ),
    ],
)
def test_target_roas_no_es_dinero_y_target_cpa_no_es_ratio(
    build_bidding: Callable[[], object],
    expected_exception: type[BaseException],
) -> None:
    with pytest.raises(expected_exception):
        build_bidding()
