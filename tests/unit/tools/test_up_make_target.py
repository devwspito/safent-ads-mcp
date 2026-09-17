"""`make up` (revisión de PR 45, T049): con `ADS_IMAGE` puesta (entorno o
`.env`, la escribe `make first-run`) tiene que DESCARGAR esa imagen,
nunca compilarla. Sin este target, `docker compose up` con `build:`
presente y la imagen ausente en local intentaba un `pull`, y si fallaba
(la referencia no existe todavía en el registro, o el operador no tiene
sesión) CONSTRUÍA de fuente en silencio y etiquetaba ese build local con
el nombre de la referencia firmada -- sustituía la imagen verificada por
una que no lo es, sin avisar.

Corre el target REAL vía `make -C <fixture> -f <Makefile> up`, con un
doble de `docker` que solo registra la llamada (nunca arranca nada de
verdad)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MAKEFILE = _REPO_ROOT / "Makefile"
_MAKE_BIN = shutil.which("make")
_IMAGE = "ghcr.io/devwspito/safent-ads-mcp@sha256:" + "0" * 64

# Sin `make` en el PATH, `subprocess.run([_MAKE_BIN, ...])` con `_MAKE_BIN
# is None` reventaría con un `TypeError` que no dice nada -- salta el
# módulo entero con un motivo legible (revisión de PR 45).
pytestmark = pytest.mark.skipif(_MAKE_BIN is None, reason="'make' no está en el PATH")

_FAKE_DOCKER_OK = """#!/usr/bin/env bash
echo "CALL $*" >> "$FAKE_DOCKER_LOG"
exit 0
"""

_FAKE_DOCKER_PULL_FAILS = """#!/usr/bin/env bash
echo "CALL $*" >> "$FAKE_DOCKER_LOG"
if [ "${1:-}" = "compose" ] && [ "${2:-}" = "pull" ]; then
  echo "authorization failed" >&2
  exit 1
fi
exit 0
"""


def _fake_docker_bin(directory: Path, log: Path, *, script: str = _FAKE_DOCKER_OK) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fake = directory / "docker"
    fake.write_text(script, encoding="utf-8")
    fake.chmod(0o755)
    log.write_text("", encoding="utf-8")
    return directory


def _run_make_up(
    fixture: Path, path_dir: Path, log: Path, *, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- ruta resuelta con shutil.which, sin shell
        [_MAKE_BIN, "-C", str(fixture), "-f", str(_MAKEFILE), "up"],
        capture_output=True,
        text=True,
        env={
            # `path_dir` primero: intercepta `docker`; el resto del PATH real
            # (sed, tail, sh, bash) sigue disponible para la receta de `make`.
            "PATH": f"{path_dir}:{os.environ['PATH']}",
            "FAKE_DOCKER_LOG": str(log),
            **(extra_env or {}),
        },
        check=False,
        timeout=30,
    )


def _calls(log: Path) -> list[str]:
    return [
        line.removeprefix("CALL ")
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith("CALL ")
    ]


class TestMakeUpNeverBuildsOverAPublishedImage:
    def test_without_ads_image_the_old_behaviour_is_untouched(self, tmp_path: Path) -> None:
        log = tmp_path / "docker.log"
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        result = _run_make_up(tmp_path, path_dir, log)

        assert result.returncode == 0, result.stderr
        assert _calls(log) == [
            "compose up -d ads-db",
            "compose run --rm ads-migrate",
            "compose up -d ads-broker ads-api ads-worker",
        ]

    def test_ads_image_from_the_environment_pulls_then_forbids_building(
        self, tmp_path: Path
    ) -> None:
        log = tmp_path / "docker.log"
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        result = _run_make_up(tmp_path, path_dir, log, extra_env={"ADS_IMAGE": _IMAGE})

        assert result.returncode == 0, result.stderr
        calls = _calls(log)
        assert calls[0] == "compose pull ads-migrate ads-api ads-worker ads-broker"
        assert calls[-1] == "compose up -d --no-build ads-broker ads-api ads-worker"

    def test_ads_image_from_dotenv_pulls_then_forbids_building(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(f"ADS_IMAGE={_IMAGE}\n", encoding="utf-8")
        log = tmp_path / "docker.log"
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        result = _run_make_up(tmp_path, path_dir, log)

        assert result.returncode == 0, result.stderr
        calls = _calls(log)
        assert calls[0] == "compose pull ads-migrate ads-api ads-worker ads-broker"
        assert calls[-1] == "compose up -d --no-build ads-broker ads-api ads-worker"

    def test_a_failed_pull_aborts_instead_of_falling_back_to_build(self, tmp_path: Path) -> None:
        log = tmp_path / "docker.log"
        path_dir = _fake_docker_bin(tmp_path / "bin", log, script=_FAKE_DOCKER_PULL_FAILS)

        result = _run_make_up(tmp_path, path_dir, log, extra_env={"ADS_IMAGE": _IMAGE})

        assert result.returncode != 0
        assert "no se pudo descargar" in result.stdout + result.stderr
        calls = _calls(log)
        assert calls == ["compose pull ads-migrate ads-api ads-worker ads-broker"]
        assert not any("build" in call for call in calls)
        assert not any("ads-db" in call for call in calls)
