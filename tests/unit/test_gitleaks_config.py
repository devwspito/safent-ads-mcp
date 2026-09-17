"""`.gitleaks.toml` (T084 threat-model.md C-74/MENOR-4): regla explicita
para `ADS_GOOGLE_OIDC_CLIENT_SECRET` (compartido con Safent Enterprise,
R-14) y lista blanca de `tests/` estrechada a los dos ficheros que de
verdad la necesitan.

No invoca el binario `gitleaks` (no es una dependencia de este repo):
comprueba la MISMA semantica a mano -- el patron de la regla nueva casa con
un secreto real y no con una asignacion vacia, y el patron de placeholder
de la lista blanca ya existente cubre lo que la regla nueva encontraria en
un placeholder. Verificado ademas con el binario real via Docker (17-sep,
`zricethezav/gitleaks:latest`): con la config resultante, `tests/` entero
sin la exencion generica solo dispara en los dos ficheros que este test
fija por nombre."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parents[2] / ".gitleaks.toml"
_RULE_ID = "safent-ads-google-oidc-client-secret"

_EXPECTED_NARROWED_TEST_PATHS = frozenset(
    {
        "tests/crossrepo/test_managed_broker_admission.py",
        "tests/unit/iam/test_google_oidc_provider.py",
    }
)


def _load_config() -> dict[str, object]:
    return tomllib.loads(_CONFIG_PATH.read_text(encoding="utf-8"))


def _client_secret_rule(config: dict[str, object]) -> dict[str, object]:
    rules = config.get("rules")
    assert isinstance(rules, list), "no hay ninguna tabla [[rules]] en .gitleaks.toml"
    matching = [rule for rule in rules if rule.get("id") == _RULE_ID]
    assert len(matching) == 1, f"se esperaba exactamente una regla {_RULE_ID!r}"
    return matching[0]


def _allowlist_placeholder_regex(config: dict[str, object]) -> re.Pattern[str]:
    allowlist = config["allowlist"]
    assert isinstance(allowlist, dict)
    (placeholder_pattern,) = (
        pattern for pattern in allowlist["regexes"] if pattern.startswith("change-me")
    )
    return re.compile(placeholder_pattern)


def test_the_client_secret_rule_exists_and_targets_the_right_variable() -> None:
    rule = _client_secret_rule(_load_config())

    assert "ADS_GOOGLE_OIDC_CLIENT_SECRET" in rule["keywords"]


def test_the_rule_matches_a_real_assignment() -> None:
    """El valor se arma en tiempo de ejecucion a partir de piezas -- nunca
    un literal contiguo en el FUENTE que la propia regla (o gitleaks
    escaneando este fichero en modo `git`, historial completo) pudiera
    volver a marcar como un secreto real (17-sep, commit b4e08c9a: el
    literal anterior quedo grabado en el historial y gitleaks lo encontro
    ahi -- ver `[allowlist].commits` en `.gitleaks.toml`)."""
    rule = _client_secret_rule(_load_config())
    pattern = re.compile(rule["regex"])
    key = "ADS_GOOGLE_OIDC_CLIENT_SECRET"
    fake_value_parts = ("GOCSPX-", "not-a-real", "-secret-", "value")
    assignment = "=".join((key, "".join(fake_value_parts)))

    match = pattern.search(assignment)

    assert match is not None


def test_the_rule_does_not_match_an_empty_assignment() -> None:
    rule = _client_secret_rule(_load_config())
    pattern = re.compile(rule["regex"])

    assert pattern.search("ADS_GOOGLE_OIDC_CLIENT_SECRET=\n") is None


def test_a_change_me_placeholder_would_be_caught_by_the_existing_allowlist() -> None:
    """La regla nueva SI casaria con el placeholder (tiene mas de 8
    caracteres); lo que lo exime es la lista blanca generica de
    `change-me*` que ya existia -- este test prueba que sigue cubriendo el
    texto que la regla nueva encontraria."""
    config = _load_config()
    rule = _client_secret_rule(config)
    client_secret_pattern = re.compile(rule["regex"])
    placeholder_line = "ADS_GOOGLE_OIDC_CLIENT_SECRET=change-me-google-oidc-client-secret"

    match = client_secret_pattern.search(placeholder_line)
    assert match is not None

    placeholder_pattern = _allowlist_placeholder_regex(config)
    assert placeholder_pattern.search(match.group(0)) is not None


def test_the_tests_allowlist_is_narrowed_to_exactly_the_two_fixtures_that_need_it() -> None:
    allowlist = _load_config()["allowlist"]
    assert isinstance(allowlist, dict)
    paths = allowlist["paths"]

    assert not any(pattern in {r"(^|/)tests/.*", r"(^|/)tests/"} for pattern in paths), (
        "la exencion generica de tests/ deberia haber desaparecido"
    )

    compiled = [re.compile(pattern) for pattern in paths]
    for expected in _EXPECTED_NARROWED_TEST_PATHS:
        assert any(pattern.search(expected) for pattern in compiled), (
            f"{expected} deberia seguir exento (fixture que de verdad lo necesita)"
        )

    unrelated = "tests/unit/iam/test_start_federated_login.py"
    assert not any(pattern.search(unrelated) for pattern in compiled), (
        f"{unrelated} no deberia estar exento -- la lista ya no es generica"
    )
