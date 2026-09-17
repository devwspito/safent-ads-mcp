"""`make check-secrets` (T066, threat-model.md C-24): el marcador de
plantilla se busca SOLO en líneas `CLAVE=valor` sin comentar. Bug real
(walkthrough en limpio T049, spec 008): la propia CABECERA de
`secrets/*.env.example` nombraba el marcador para explicar qué hace este
target, y ese comentario viaja tal cual a `secrets/*.env` al copiarla --
con un `grep` sin filtrar, el fichero no pasaba NUNCA, ni con todo
relleno de verdad.

`.env` entra en la misma lista de ficheros comprobados (revisión de
seguridad PR 45): lleva `POSTGRES_PASSWORD` y el DSN con esa contraseña
dentro, y el camino manual (`cp .env.example .env`) lo dejaba en 0644
con un `change-me` sin tocar -- exactamente lo que este target existe
para impedir en `secrets/*.env`, pero nunca lo miraba aquí.

Corre el target REAL vía `make -C <fixture> -f <Makefile>`, no una copia
de su lógica: lo que se prueba es el comportamiento del propio Makefile."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MAKEFILE = _REPO_ROOT / "Makefile"
_MAKE_BIN = shutil.which("make")

# Sin `make` en el PATH, `subprocess.run([_MAKE_BIN, ...])` con `_MAKE_BIN
# is None` reventaría con un `TypeError` que no dice nada -- salta el
# módulo entero con un motivo legible (revisión de PR 45).
pytestmark = pytest.mark.skipif(_MAKE_BIN is None, reason="'make' no está en el PATH")

# Cabecera real de un `*.env.example` (previa a T049): nombra el marcador
# de plantilla en un comentario para explicar qué hace `make check-secrets`.
# Sigue siendo una entrada de prueba válida aunque los ejemplos de hoy ya
# no la usen -- es la forma exacta de comentario que rompía el target.
_HEADER_THAT_NAMES_THE_MARKER = (
    "# make check-secrets verifica permisos 0600 y que no quede ningun\n"
    "# placeholder `change-me` antes de `make up`.\n"
)
_FILLED_VALUE_LINES = (
    "ADS_APPROVAL_SIGNING_KEY=un-valor-real-que-no-es-una-plantilla\n"
    "ADS_SESSION_SECRET=otro-valor-real-de-32-bytes-en-base64\n"
)
_PLACEHOLDER_VALUE_LINES = (
    "ADS_APPROVAL_SIGNING_KEY=change-me-ed25519-seed-base64\n"
    "ADS_SESSION_SECRET=otro-valor-real-de-32-bytes-en-base64\n"
)
_DOTENV_FILLED = (
    "POSTGRES_USER=ads\n"
    "POSTGRES_PASSWORD=un-valor-real-que-no-es-una-plantilla\n"
    "POSTGRES_DB=ads\n"
)
_DOTENV_PLACEHOLDER = "POSTGRES_PASSWORD=change-me-strong-password\n"


def _write_secret(path: Path, body: str) -> None:
    path.write_text(_HEADER_THAT_NAMES_THE_MARKER + body, encoding="utf-8")
    path.chmod(0o600)


def _write_dotenv(directory: Path, body: str = _DOTENV_FILLED) -> None:
    dotenv = directory / ".env"
    dotenv.write_text(_HEADER_THAT_NAMES_THE_MARKER + body, encoding="utf-8")
    dotenv.chmod(0o600)


def _write_secrets_dir(directory: Path, *, api: str = _FILLED_VALUE_LINES) -> None:
    secrets_dir = directory / "secrets"
    secrets_dir.mkdir(exist_ok=True)
    _write_secret(secrets_dir / "api.env", api)
    _write_secret(secrets_dir / "broker.env", _FILLED_VALUE_LINES)


def _write_caps(directory: Path) -> None:
    caps = directory / "config/caps.yaml"
    caps.parent.mkdir(parents=True, exist_ok=True)
    caps.write_text("accounts: {}\n", encoding="utf-8")
    caps.chmod(0o644)


def _write_a_fully_correct_fixture(directory: Path) -> None:
    _write_secrets_dir(directory)
    _write_dotenv(directory)
    _write_caps(directory)


def _run_check_secrets(fixture: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- ruta resuelta con shutil.which, sin shell
        [_MAKE_BIN, "-C", str(fixture), "-f", str(_MAKEFILE), "check-secrets"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


class TestCheckSecretsOnlyLooksAtValues:
    def test_a_correctly_filled_file_passes_even_with_the_marker_in_a_comment(
        self, tmp_path: Path
    ) -> None:
        """Regresión T049: la cabecera nombra el marcador, los valores no
        son plantilla -- el target tiene que pasar."""
        _write_a_fully_correct_fixture(tmp_path)

        result = _run_check_secrets(tmp_path)

        assert result.returncode == 0, result.stderr
        assert "check-secrets: OK" in result.stdout

    def test_a_placeholder_value_still_fails(self, tmp_path: Path) -> None:
        """El detector no queda ciego: un valor sin sustituir de verdad
        sigue bloqueando `make up`."""
        _write_a_fully_correct_fixture(tmp_path)
        _write_secrets_dir(tmp_path, api=_PLACEHOLDER_VALUE_LINES)

        result = _run_check_secrets(tmp_path)

        assert result.returncode != 0
        assert "secrets/api.env todavia tiene placeholders" in result.stderr

    def test_a_commented_out_placeholder_does_not_fail(self, tmp_path: Path) -> None:
        """Una integración opcional sin activar (comentada, como
        `#GOOGLE_ADS_CLIENT_ID=change-me-...` en broker.env.example) no es
        un fichero a medias."""
        _write_a_fully_correct_fixture(tmp_path)
        _write_secret(
            tmp_path / "secrets/broker.env",
            _FILLED_VALUE_LINES + "#GOOGLE_ADS_CLIENT_ID=change-me-google-oauth-client-id\n",
        )

        result = _run_check_secrets(tmp_path)

        assert result.returncode == 0, result.stderr

    def test_a_missing_secrets_file_still_fails(self, tmp_path: Path) -> None:
        """El target sigue exigiendo que los tres ficheros existan -- este
        fix no afloja esa otra comprobación."""
        _write_a_fully_correct_fixture(tmp_path)
        (tmp_path / "secrets/broker.env").unlink()

        result = _run_check_secrets(tmp_path)

        assert result.returncode != 0
        assert "FALTA secrets/broker.env" in result.stderr

    @pytest.mark.parametrize("mode", [0o640, 0o664])
    def test_permissions_looser_than_0600_still_fail(self, tmp_path: Path, mode: int) -> None:
        _write_a_fully_correct_fixture(tmp_path)
        (tmp_path / "secrets/api.env").chmod(mode)

        result = _run_check_secrets(tmp_path)

        assert result.returncode != 0
        assert stat.S_IMODE((tmp_path / "secrets/api.env").stat().st_mode) == mode


class TestCheckSecretsAlsoInspectsDotenv:
    """Revisión de seguridad PR 45: `.env` lleva `POSTGRES_PASSWORD` y el
    DSN con esa contraseña dentro (mismo criterio que `secrets/*.env`),
    pero el target nunca lo miraba -- el camino manual (`cp .env.example
    .env`) podía quedarse en 0644 con `change-me-strong-password` y
    `make check-secrets` lo dejaba pasar igual."""

    def test_a_missing_dotenv_fails(self, tmp_path: Path) -> None:
        _write_a_fully_correct_fixture(tmp_path)
        (tmp_path / ".env").unlink()

        result = _run_check_secrets(tmp_path)

        assert result.returncode != 0
        assert "FALTA .env" in result.stderr

    def test_a_placeholder_password_in_dotenv_fails(self, tmp_path: Path) -> None:
        _write_a_fully_correct_fixture(tmp_path)
        _write_dotenv(tmp_path, _DOTENV_PLACEHOLDER)

        result = _run_check_secrets(tmp_path)

        assert result.returncode != 0
        assert ".env todavia tiene placeholders" in result.stderr

    @pytest.mark.parametrize("mode", [0o644, 0o640])
    def test_dotenv_looser_than_0600_fails(self, tmp_path: Path, mode: int) -> None:
        _write_a_fully_correct_fixture(tmp_path)
        (tmp_path / ".env").chmod(mode)

        result = _run_check_secrets(tmp_path)

        assert result.returncode != 0
        assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == mode
