"""`scripts/primer-arranque.sh` -- `--password-stdin` en un solo comando no
interactivo (T049, spec 008): el envoltorio invoca `docker compose run`
DOS veces (mas `ads-migrate` en medio) sobre la misma stdin real del
script. `docker compose run` reenvía esa stdin al contenedor entera, hasta
EOF, la lea la app de dentro o no -- con las tres invocaciones heredando
sin tocar la MISMA tubería, la primera que arranca (el paso
`--skip-start`, que nunca pide la contraseña) se la bebía entera y el paso
que de verdad la pedía se encontraba "stdin no traía ningún valor".

El doble de `docker` de abajo modela justo ese comportamiento: CUALQUIER
`compose run` drena su propia stdin hasta EOF, la use o no -- como el
`docker` real. Contra un envoltorio que reenvíe la tubería del host sin
tocar a las tres invocaciones, este doble reproduce el fallo tal cual;
contra el envoltorio corregido (que lee la contraseña UNA vez en el propio
shell y solo la vuelve a servir, por una tubería propia, al paso que la
pide), el alta del dueño la recibe intacta."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WRAPPER = _REPO_ROOT / "scripts/primer-arranque.sh"
_WRAPPER_TOOLS = ("bash", "env", "dirname", "id", "cat")
_PASSWORD = "una-contrasena-larga-de-verdad"  # noqa: S105 -- valor del test, no un secreto

_FAKE_DOCKER = """#!/usr/bin/env bash
set -u
log() { printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"; }

if [ "${1:-}" = "compose" ]; then
  shift
  sub="${1:-}"
  shift || true
  case "$sub" in
    version) exit 0 ;;
    config) exit 0 ;;
    # Un id no vacío: "ya es nuestra pila" -- el preflight de puerto
    # ocupado no debe depender de qué haya realmente escuchando en 8410 en
    # la máquina que corre este test.
    ps) echo "fake-container-id"; exit 0 ;;
    build) log "BUILD $*"; exit 0 ;;
    pull) log "PULL $*"; exit 0 ;;
    up) log "UP $*"; exit 0 ;;
    run)
      if [[ "$*" == *"safent_ads.tools.first_run"* ]]; then
        # Como el `docker` real: drena esta invocación de `compose run`
        # entera, hasta EOF, se use o no dentro.
        IFS= read -r -d '' drenado || true
        if [[ "$*" == *"--skip-start"* ]]; then
          log "FILES-STEP drenado_len=${#drenado}"
          exit 0
        fi
        if [[ "$*" == *"--password-stdin"* ]]; then
          if [ -z "$drenado" ]; then
            echo "ERROR: --password-stdin: stdin no traía ningún valor" >&2
            log "OWNER-STEP FAILED"
            exit 1
          fi
          printf '%s' "$drenado" > "$FAKE_DOCKER_RESULT_DIR/password.txt"
          log "OWNER-STEP OK drenado_len=${#drenado}"
          exit 0
        fi
        log "OWNER-STEP NOPASS"
        exit 0
      fi
      # `ads-migrate` u otro `run`: el `docker` real igual la reenvía y la
      # drena, la use o no el contenido.
      IFS= read -r -d '' _otro || true
      log "RUN-OTHER $*"
      exit 0
      ;;
    *) log "COMPOSE-OTHER $sub $*"; exit 0 ;;
  esac
elif [ "${1:-}" = "info" ]; then
  exit 0
elif [ "${1:-}" = "image" ]; then
  exit 0
else
  log "OTHER $*"
  exit 0
fi
"""


def _fake_docker_bin(directory: Path, log: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for tool in _WRAPPER_TOOLS:
        resolved = shutil.which(tool)
        assert resolved is not None, tool
        (directory / tool).symlink_to(resolved)
    fake = directory / "docker"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)
    log.write_text("", encoding="utf-8")
    return directory


def _run_wrapper(
    path_dir: Path,
    result_dir: Path,
    log: Path,
    *,
    stdin_text: str | None,
    extra: tuple[str, ...],
    trace: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = ["bash", "-x", str(_WRAPPER)] if trace else [str(_WRAPPER)]
    return subprocess.run(  # noqa: S603 -- ruta fija del repo, sin shell
        [*command, *extra],
        capture_output=True,
        text=True,
        env={
            "PATH": str(path_dir),
            "HOME": str(path_dir.parent),
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_DOCKER_RESULT_DIR": str(result_dir),
        },
        input=stdin_text,
        stdin=None if stdin_text is not None else subprocess.DEVNULL,
        check=False,
        timeout=30,
    )


class TestPasswordStdinSurvivesTheTwoInvocations:
    def test_the_owner_step_receives_the_password_intact(self, tmp_path: Path) -> None:
        log = tmp_path / "docker.log"
        result_dir = tmp_path / "result"
        result_dir.mkdir()
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        result = _run_wrapper(
            path_dir,
            result_dir,
            log,
            stdin_text=_PASSWORD,
            extra=("--password-stdin", "--no-composio"),
        )

        assert result.returncode == 0, result.stderr
        assert (result_dir / "password.txt").read_text(encoding="utf-8") == _PASSWORD

    def test_the_files_step_never_drains_the_password(self, tmp_path: Path) -> None:
        """La contraseña es para el alta del dueño, no para el paso de
        ficheros (`--skip-start` nunca la pide): ese paso tiene que recibir
        `/dev/null`, nunca la tubería real, o se la bebería él."""
        log = tmp_path / "docker.log"
        result_dir = tmp_path / "result"
        result_dir.mkdir()
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        _run_wrapper(
            path_dir,
            result_dir,
            log,
            stdin_text=_PASSWORD,
            extra=("--password-stdin", "--no-composio"),
        )

        calls = log.read_text(encoding="utf-8")
        assert "FILES-STEP drenado_len=0" in calls
        assert "OWNER-STEP OK" in calls

    def test_an_actually_empty_stdin_still_fails_clearly(self, tmp_path: Path) -> None:
        """El doble no queda ciego: sin ningún valor de verdad, el alta
        sigue fallando -- lo que cambia es que ya no depende de qué paso
        drenó la tubería primero."""
        log = tmp_path / "docker.log"
        result_dir = tmp_path / "result"
        result_dir.mkdir()
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        result = _run_wrapper(
            path_dir,
            result_dir,
            log,
            stdin_text="",
            extra=("--password-stdin", "--no-composio"),
        )

        assert result.returncode != 0
        assert not (result_dir / "password.txt").exists()


class TestPasswordStdinPreservesWhitespace:
    """Revisión de seguridad PR 45: recortar cualquier espacio de los
    extremos (en vez de solo el salto de línea que añade la propia
    tubería) muta en silencio una contraseña que lo llevara a propósito."""

    def test_leading_and_trailing_spaces_survive(self, tmp_path: Path) -> None:
        password = " una-contraseña-con-espacios "  # noqa: S105 -- valor del test
        log = tmp_path / "docker.log"
        result_dir = tmp_path / "result"
        result_dir.mkdir()
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        result = _run_wrapper(
            path_dir,
            result_dir,
            log,
            stdin_text=password,
            extra=("--password-stdin", "--no-composio"),
        )

        assert result.returncode == 0, result.stderr
        assert (result_dir / "password.txt").read_text(encoding="utf-8") == password

    def test_a_trailing_newline_from_echo_is_removed_but_spaces_are_not(
        self, tmp_path: Path
    ) -> None:
        """`echo "$p" | ...` en vez de `printf '%s' "$p" | ...`: el `\\n`
        que añade `echo` es un artefacto de la tubería, no contenido."""
        password = " una-contraseña-con-espacios "  # noqa: S105 -- valor del test
        log = tmp_path / "docker.log"
        result_dir = tmp_path / "result"
        result_dir.mkdir()
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        result = _run_wrapper(
            path_dir,
            result_dir,
            log,
            stdin_text=password + "\n",
            extra=("--password-stdin", "--no-composio"),
        )

        assert result.returncode == 0, result.stderr
        assert (result_dir / "password.txt").read_text(encoding="utf-8") == password


class TestEmptyStdinFailsBeforeTheStack:
    """Revisión de seguridad PR 45: fallar DESPUÉS de `docker compose up
    -d ads-db` habría malgastado el arranque entero de la pila por una
    tubería vacía que se podía haber rechazado antes de tocar nada."""

    def test_it_never_reaches_the_stack_step(self, tmp_path: Path) -> None:
        log = tmp_path / "docker.log"
        result_dir = tmp_path / "result"
        result_dir.mkdir()
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        result = _run_wrapper(
            path_dir,
            result_dir,
            log,
            stdin_text="",
            extra=("--password-stdin", "--no-composio"),
        )

        assert result.returncode == 1
        assert not log.exists() or "UP " not in log.read_text(encoding="utf-8")


class TestSecretNeverAppearsInAnXtrace:
    """Revisión de seguridad PR 45: `bash -x` (depuración manual, o
    `SHELLOPTS` heredado en algún CI) imprime cada orden con sus
    argumentos ya expandidos -- sin apagarlo alrededor de la línea que
    usa `$SECRETO_STDIN`, la contraseña quedaría en la traza."""

    def test_the_password_never_shows_up_in_the_trace(self, tmp_path: Path) -> None:
        password = "contrasena-que-jamas-debe-salir-en-la-traza"  # noqa: S105
        log = tmp_path / "docker.log"
        result_dir = tmp_path / "result"
        result_dir.mkdir()
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        result = _run_wrapper(
            path_dir,
            result_dir,
            log,
            stdin_text=password,
            extra=("--password-stdin", "--no-composio"),
            trace=True,
        )

        assert result.returncode == 0, result.stderr
        assert password not in result.stderr
