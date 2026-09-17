"""`scripts/instalar-mcp.sh` (008-mcp-ads-estandar T025): los cinco
invariantes de `contracts/instalar-mcp-cli.md`, con `claude` y `codex`
simulados (`fake_agent_cli.py`) y un HOME aislado por test -- ningun test
toca el `~/.claude.json`, el `~/.codex` ni los perfiles de quien lo corre.

Los nombres heredados que el instalador retira ya no viven en el script
(T042): los declara el `--config` del despliegue que los tuvo
(`LEGACY_ENV_VAR=`/`LEGACY_CONFIG_DIR=`). Estas pruebas usan un fichero de
configuracion propio con nombres neutros, asi que ni siembran la cadena de
cliente que la guarda de CI persigue (`tests/unit/test_no_client_strings.py`)
ni dependen de que el script conozca a nadie."""

from __future__ import annotations

import os
import pty
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.unit.test_no_client_strings import has_client_string

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INSTALLER = _REPO_ROOT / "scripts/instalar-mcp.sh"
_FAKE_CLI = Path(__file__).resolve().parent / "fake_agent_cli.py"
_SERVER_NAME = "mis-ads"
_SERVER_URL = "https://ads.example.com/mcp"
_TOKEN = "bearer-de-prueba-que-no-debe-filtrarse"  # noqa: S105 -- valor del test, no un secreto
_PROFILES = (".zshrc", ".bashrc", ".profile")
# Par heredado de un despliegue imaginario: nombres neutros, declarados en
# un `--config` de prueba. `$HOME` va sin expandir a proposito -- asi lo
# escribio en el perfil la version anterior del instalador y asi hay que
# encontrarlo para quitarlo.
_LEGACY_VARIABLE = "VIEJO_MCP_TOKEN"  # noqa: S105 -- nombre de variable, no un secreto
_LEGACY_CONFIG_DIR = "$HOME/.config/viejo-ads"
_LEGACY_SOURCE_LINE = (
    f'[ -f "{_LEGACY_CONFIG_DIR}/mcp.env" ] && . "{_LEGACY_CONFIG_DIR}/mcp.env"'
)


@dataclass(frozen=True)
class Installation:
    """Una maquina simulada: su HOME, su PATH y lo que los agentes vieron."""

    home: Path
    path_dir: Path
    state: Path

    def _environment(self) -> dict[str, str]:
        return {
            "PATH": f"{self.path_dir}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "SHELL": "/bin/bash",
            "FAKE_AGENT_STATE": str(self.state),
            "CODEX_HOME": str(self.home / ".codex"),
        }

    def run(self, *arguments: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 -- ruta fija del repo, sin shell
            [str(_INSTALLER), *arguments],
            capture_output=True,
            text=True,
            env=self._environment(),
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            check=False,
            timeout=60,
        )

    def run_with_a_terminal(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Igual, pero con un terminal de verdad en stdin: es la unica
        forma de ejercitar la rama `[ -t 0 ]` del instalador."""
        controller, terminal = pty.openpty()
        try:
            return subprocess.run(  # noqa: S603 -- ruta fija del repo, sin shell
                [str(_INSTALLER), *arguments],
                capture_output=True,
                text=True,
                env=self._environment(),
                stdin=terminal,
                check=False,
                timeout=60,
            )
        finally:
            os.close(terminal)
            os.close(controller)

    def registry(self, agent: str) -> list[str]:
        path = self.state / f"{agent}.registry"
        return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []

    def calls(self) -> str:
        path = self.state / "calls.log"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def profiles_text(self) -> str:
        return "".join(
            (self.home / profile).read_text(encoding="utf-8")
            for profile in _PROFILES
            if (self.home / profile).is_file()
        )

    def token_file(self, name: str = _SERVER_NAME) -> Path:
        return self.home / ".config" / name / "mcp.env"

    def legacy_token_file(self) -> Path:
        return Path(_LEGACY_CONFIG_DIR.replace("$HOME", str(self.home))) / "mcp.env"

    def config_with(self, *lines: str) -> Path:
        """Un `--config` cualquiera, con URL y NOMBRE validos y lo que se
        le anada."""
        path = self.home.parent / "instalar-mcp.env"
        path.write_text(
            f"URL={_SERVER_URL}\nNOMBRE={_SERVER_NAME}\n" + "".join(f"{line}\n" for line in lines),
            encoding="utf-8",
        )
        return path

    def config_with_the_legacy_pair(self) -> Path:
        """El `--config` de un despliegue que viene del instalador
        anterior: el par heredado se declara AQUI, nunca en el script."""
        path = self.home.parent / "instalar-mcp.env"
        path.write_text(
            f"URL={_SERVER_URL}\n"
            f"NOMBRE={_SERVER_NAME}\n"
            f"LEGACY_ENV_VAR={_LEGACY_VARIABLE}\n"
            f"LEGACY_CONFIG_DIR={_LEGACY_CONFIG_DIR}\n",
            encoding="utf-8",
        )
        return path


@pytest.fixture
def installation(tmp_path: Path) -> Installation:
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    for profile in _PROFILES:
        (home / profile).write_text("# perfil de prueba\n", encoding="utf-8")
    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    for agent in ("claude", "codex"):
        link = path_dir / agent
        link.symlink_to(_FAKE_CLI)
        # Sin bit de ejecucion, `command -v claude` del instalador se iria
        # al CLI REAL de la maquina y el test dejaria de probar nada.
        assert os.access(link, os.X_OK), link
    state = tmp_path / "state"
    state.mkdir()
    return Installation(home=home, path_dir=path_dir, state=state)


class TestInvariant1UrlIsRequired:
    def test_without_url_it_exits_1_naming_the_flag(self, installation: Installation) -> None:
        result = installation.run()

        assert result.returncode == 1
        assert "--url" in result.stderr

    def test_an_url_that_is_not_https_exits_1(self, installation: Installation) -> None:
        result = installation.run("--url", "http://ads.example.com/mcp")

        assert result.returncode == 1
        assert "--url" in result.stderr

    def test_an_invalid_name_exits_1(self, installation: Installation) -> None:
        result = installation.run("--url", _SERVER_URL, "--nombre", "Mis Ads")

        assert result.returncode == 1
        assert "--nombre" in result.stderr


class TestInvariant2NoClientStringsInTheScript:
    def test_the_script_names_no_client_at_all(self) -> None:
        """Ni una: el par heredado que el instalador retira lo declara el
        `--config` del despliegue que lo tuvo, no el script (T042)."""
        offenders = [
            line
            for line in _INSTALLER.read_text(encoding="utf-8").splitlines()
            if has_client_string(line)
        ]

        assert offenders == []


class TestInvariant3OAuthLeavesNoSecretBehind:
    def test_a_previous_token_of_this_server_is_removed(self, installation: Installation) -> None:
        token_file = installation.token_file()
        token_file.parent.mkdir(parents=True)
        token_file.write_text(f"export ADS_MCP_TOKEN_MIS_ADS={_TOKEN}\n", encoding="utf-8")
        source_line = f'[ -f "{token_file}" ] && . "{token_file}"\n'
        (installation.home / ".bashrc").write_text(source_line, encoding="utf-8")

        result = installation.run("--url", _SERVER_URL, "--nombre", _SERVER_NAME)

        assert result.returncode == 0, result.stderr
        assert not token_file.exists()
        assert _TOKEN not in installation.profiles_text()
        assert str(token_file) not in installation.profiles_text()

    def test_the_inherited_pair_declared_in_the_config_is_removed_too(
        self, installation: Installation
    ) -> None:
        legacy_file = installation.legacy_token_file()
        legacy_file.parent.mkdir(parents=True)
        legacy_file.write_text(f"export {_LEGACY_VARIABLE}={_TOKEN}\n", encoding="utf-8")
        (installation.home / ".bashrc").write_text(
            f"export {_LEGACY_VARIABLE}={_TOKEN}\n{_LEGACY_SOURCE_LINE}\n", encoding="utf-8"
        )

        result = installation.run("--config", str(installation.config_with_the_legacy_pair()))

        assert result.returncode == 0, result.stderr
        assert not legacy_file.exists()
        assert _LEGACY_VARIABLE not in installation.profiles_text()
        assert _LEGACY_SOURCE_LINE not in installation.profiles_text()
        assert _TOKEN not in installation.profiles_text()

    def test_a_hostile_legacy_variable_name_is_refused_before_deleting_anything(
        self, installation: Installation
    ) -> None:
        """`LEGACY_ENV_VAR` sale de un fichero y acaba en una busqueda
        sobre los perfiles de shell. Con `.*` interpretado como expresion
        regular, `^export .*=` casa con TODAS las lineas de export del
        perfil y las borra. Ni se interpreta ni se llega a borrar nada: el
        valor se valida antes."""
        profile = installation.home / ".bashrc"
        profile.write_text("export OTRA_COSA=no-tocar\n", encoding="utf-8")

        result = installation.run("--config", str(installation.config_with("LEGACY_ENV_VAR=.*")))

        assert result.returncode == 1
        assert "LEGACY_ENV_VAR" in result.stderr
        assert profile.read_text(encoding="utf-8") == "export OTRA_COSA=no-tocar\n"

    @pytest.mark.parametrize(
        "clave_y_valor",
        [
            pytest.param("LEGACY_CONFIG_DIR=/etc", id="ruta-absoluta-fuera-del-home"),
            pytest.param("LEGACY_CONFIG_DIR=$HOME/../../etc", id="con-dos-puntos"),
            pytest.param("LEGACY_CONFIG_DIR=$HOME/.config/*", id="con-comodin"),
            pytest.param("LEGACY_CONFIG_DIR=$HOME/.config/a b", id="con-espacio"),
        ],
    )
    def test_a_legacy_directory_outside_the_home_is_refused(
        self, installation: Installation, clave_y_valor: str
    ) -> None:
        """`LEGACY_CONFIG_DIR` acaba en un `rm`: se acota al HOME de quien
        ejecuta. El par heredado que este instalador retira siempre vivio
        ahi, y ampliarlo es un permiso que nadie ha pedido."""
        result = installation.run("--config", str(installation.config_with(clave_y_valor)))

        assert result.returncode == 1
        assert "LEGACY_CONFIG_DIR" in result.stderr

    def test_a_name_with_a_newline_is_refused(self, installation: Installation) -> None:
        """La validacion mira la cadena ENTERA: linea a linea, un valor con
        un salto dentro pasaria por la primera linea y colaria el resto."""
        result = installation.run("--url", _SERVER_URL, "--nombre", "mis-ads\nrm -rf /")

        assert result.returncode == 1
        assert "--nombre" in result.stderr

    def test_without_that_config_the_script_knows_no_legacy_name(
        self, installation: Installation
    ) -> None:
        """La contraparte de la prueba de arriba: el instalador estandar no
        lleva ningun nombre heredado dentro, asi que sin un `--config` que
        lo declare no borra nada que no sea suyo."""
        legacy_file = installation.legacy_token_file()
        legacy_file.parent.mkdir(parents=True)
        legacy_file.write_text(f"export {_LEGACY_VARIABLE}={_TOKEN}\n", encoding="utf-8")

        result = installation.run("--url", _SERVER_URL, "--nombre", _SERVER_NAME)

        assert result.returncode == 0, result.stderr
        assert legacy_file.exists()

    def test_a_profile_that_cannot_be_rewritten_keeps_what_was_there(
        self, installation: Installation
    ) -> None:
        """`cat > perfil` trunca ANTES de escribir: una escritura que falla
        dejaba un fichero de arranque cortado y sin copia. Ahora la copia se
        hace antes de truncar y solo se retira si todo fue bien."""
        profile = installation.home / ".bashrc"
        original = f"export ADS_MCP_TOKEN_MIS_ADS={_TOKEN}\n"
        profile.write_text(original, encoding="utf-8")
        profile.chmod(0o444)
        try:
            result = installation.run("--url", _SERVER_URL, "--nombre", _SERVER_NAME)
        finally:
            profile.chmod(0o644)

        assert result.returncode != 0
        copias = sorted(installation.home.glob(".bashrc.bak.*"))
        assert copias, "sin copia, lo que hubiera en el perfil se habria perdido"
        assert [c.read_text(encoding="utf-8") for c in copias] == [original]
        assert str(copias[0]) in result.stderr

    def test_a_successful_rewrite_leaves_no_copy_behind(
        self, installation: Installation
    ) -> None:
        """La contraparte: en el camino normal no queda ninguna copia con el
        token dentro rondando por el HOME."""
        profile = installation.home / ".bashrc"
        profile.write_text(f"export ADS_MCP_TOKEN_MIS_ADS={_TOKEN}\n", encoding="utf-8")

        result = installation.run("--url", _SERVER_URL, "--nombre", _SERVER_NAME)

        assert result.returncode == 0, result.stderr
        assert _TOKEN not in profile.read_text(encoding="utf-8")
        assert list(installation.home.glob(".bashrc.bak*")) == []

    def test_oauth_registers_both_agents_without_any_token(
        self, installation: Installation
    ) -> None:
        result = installation.run("--url", _SERVER_URL, "--nombre", _SERVER_NAME)

        assert result.returncode == 0, result.stderr
        assert installation.registry("claude") == [_SERVER_NAME]
        assert installation.registry("codex") == [_SERVER_NAME]
        assert "Bearer" not in installation.calls()
        assert not installation.token_file().exists()


class TestInvariant4TheTokenNeverLeaks:
    def test_a_tty_less_token_lands_in_a_0600_file_and_nowhere_else(
        self, installation: Installation
    ) -> None:
        result = installation.run(
            "--url", _SERVER_URL, "--nombre", _SERVER_NAME, "--token-stdin", stdin=_TOKEN
        )

        assert result.returncode == 0, result.stderr
        token_file = installation.token_file()
        assert token_file.stat().st_mode & 0o777 == 0o600
        assert _TOKEN in token_file.read_text(encoding="utf-8")
        assert _TOKEN not in result.stdout
        assert _TOKEN not in result.stderr
        assert _TOKEN not in installation.profiles_text()
        assert str(token_file) in installation.profiles_text()

    def test_codex_receives_the_token_by_environment_variable_never_by_argument(
        self, installation: Installation
    ) -> None:
        """El riesgo documentado del contrato es SOLO de Claude Code
        (`--header` es su unica via); Codex nunca ve el token en argv."""
        installation.run(
            "--url", _SERVER_URL, "--nombre", _SERVER_NAME, "--token-stdin", stdin=_TOKEN
        )

        codex_calls = [
            line for line in installation.calls().splitlines() if line.startswith("codex")
        ]
        assert codex_calls
        assert all(_TOKEN not in line for line in codex_calls)
        assert any("--bearer-token-env-var ADS_MCP_TOKEN_MIS_ADS" in line for line in codex_calls)

    def test_a_token_from_a_terminal_is_refused(self, installation: Installation) -> None:
        """Tecleado en un terminal quedaria visible: el contrato lo prohibe."""
        result = installation.run_with_a_terminal("--url", _SERVER_URL, "--token-stdin")

        assert result.returncode == 1
        assert "--token-stdin" in result.stderr
        assert not installation.token_file().exists()

    def test_an_empty_stdin_is_refused(self, installation: Installation) -> None:
        result = installation.run("--url", _SERVER_URL, "--token-stdin")

        assert result.returncode == 1
        assert "token" in result.stderr
        assert not installation.token_file().exists()


class TestInvariant5TwiceLeavesOneRegistrationPerAgent:
    def test_two_runs_in_a_row(self, installation: Installation) -> None:
        first = installation.run("--url", _SERVER_URL, "--nombre", _SERVER_NAME)
        second = installation.run("--url", _SERVER_URL, "--nombre", _SERVER_NAME)

        assert (first.returncode, second.returncode) == (0, 0), second.stderr
        assert installation.registry("claude") == [_SERVER_NAME]
        assert installation.registry("codex") == [_SERVER_NAME]

    def test_switching_from_token_to_oauth_retires_the_token(
        self, installation: Installation
    ) -> None:
        installation.run(
            "--url", _SERVER_URL, "--nombre", _SERVER_NAME, "--token-stdin", stdin=_TOKEN
        )

        result = installation.run("--url", _SERVER_URL, "--nombre", _SERVER_NAME)

        assert result.returncode == 0, result.stderr
        assert not installation.token_file().exists()
        assert _TOKEN not in installation.profiles_text()
        assert installation.registry("claude") == [_SERVER_NAME]
