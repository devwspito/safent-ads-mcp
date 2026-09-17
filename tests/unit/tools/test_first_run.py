"""`tools.first_run` (008-mcp-ads-estandar T021): los siete invariantes
verificables de `contracts/first-run-cli.md`, mas los codigos de salida y
la rama federada del alta (T018).

Todo corre con `--skip-start`: los pasos que necesitan la pila arriba
(alta del dueno contra Postgres y verificacion HTTP) tienen sus propias
pruebas de pieza; aqui se prueba lo que escribe ficheros y lo que
pregunta, que es donde viven los invariantes de secretos."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import yaml

from safent_ads.broker.infrastructure.caps_config import CapsConfigError, parse_caps_config
from safent_ads.tools import first_run
from safent_ads.tools.gen_keys import public_key_for

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OWNER_EMAIL = "owner@example.com"
_PUBLIC_BASE_URL = "https://ads.example.com"
_SECRET_KEYS = (
    "POSTGRES_PASSWORD",
    "ADS_APPROVAL_SIGNING_KEY",
    "ADS_SESSION_SECRET",
    "ADS_TOTP_ENC_KEY",
    "ADS_MCP_TOKEN",
    "ADS_APPROVAL_PUBLIC_KEY",
    "ADS_CREDENTIAL_MASTER_KEY",
)
_GENERATED_FILES = (".env", "secrets/api.env", "secrets/broker.env", "config/caps.yaml")
_MIN_GENERATED_SECRET_CHARS = 32
# El bearer que ya estaba escrito en un despliegue con la via abierta.
_BEARER_ALREADY_THERE = "el-de-siempre"  # noqa: S105 -- valor del test
_TYPED_PASSWORD = "una-contrasena-larga"  # noqa: S105 -- lo que teclearia el dueno, no un secreto
_RUNNING_AS_ROOT = os.getuid() == 0


class RecordingPrompter:
    """Cuenta y ordena las preguntas: el invariante 4 es «cuantas», no «cuales»."""

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.asked: list[str] = []
        self._answers = answers or {}

    def _answer(self, message: str) -> str:
        self.asked.append(message)
        return self._answers.get(message, "")

    def as_prompter(self) -> first_run.Prompter:
        return first_run.Prompter(visible=self._answer, secret=self._answer)


def _run(workspace: Path, *extra: str, prompter: first_run.Prompter | None = None) -> int:
    argv = ["--workspace", str(workspace), "--skip-start", *extra]
    return first_run.main(argv, prompter=prompter or RecordingPrompter().as_prompter())


def _run_with_answers(workspace: Path, *extra: str) -> int:
    return _run(
        workspace,
        "--public-base-url",
        _PUBLIC_BASE_URL,
        "--owner-email",
        _OWNER_EMAIL,
        "--no-composio",
        *extra,
    )


def _snapshot(workspace: Path) -> dict[str, str]:
    return {
        relative: (workspace / relative).read_text(encoding="utf-8")
        for relative in _GENERATED_FILES
        if (workspace / relative).is_file()
    }


def _env_values(path: Path) -> dict[str, str]:
    return first_run._parse_env_text(path.read_text(encoding="utf-8"))


def _generated_secrets(workspace: Path) -> list[str]:
    values: list[str] = []
    for relative in (".env", "secrets/api.env", "secrets/broker.env"):
        parsed = _env_values(workspace / relative)
        values.extend(parsed[key] for key in _SECRET_KEYS if key in parsed)
    return values


class TestInvariant1SecondPassChangesNothing:
    def test_a_second_pass_exits_zero_and_rewrites_nothing(self, tmp_path: Path) -> None:
        assert _run_with_answers(tmp_path) == first_run.EXIT_OK
        before = _snapshot(tmp_path)

        assert _run_with_answers(tmp_path) == first_run.EXIT_OK

        assert _snapshot(tmp_path) == before

    def test_a_second_pass_asks_nothing(self, tmp_path: Path) -> None:
        _run_with_answers(tmp_path)
        prompter = RecordingPrompter()

        assert _run(tmp_path, prompter=prompter.as_prompter()) == first_run.EXIT_OK

        assert prompter.asked == []


class TestInvariant2NoSecretReachesTheOutput:
    def test_no_generated_secret_is_printed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run_with_answers(tmp_path)
        captured = capsys.readouterr()
        printed = captured.out + captured.err

        leaked = [value for value in _generated_secrets(tmp_path) if value in printed]

        assert leaked == []
        assert _generated_secrets(tmp_path)  # la prueba seria vacua sin secretos que filtrar

    def test_a_secret_from_stdin_never_reaches_the_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        composio_key = "composio-key-que-no-debe-aparecer"
        monkeypatch.setattr("sys.stdin", _PipedStdin(composio_key))

        _run(
            tmp_path,
            "--public-base-url",
            _PUBLIC_BASE_URL,
            "--owner-email",
            _OWNER_EMAIL,
            "--composio-api-key-stdin",
        )
        captured = capsys.readouterr()

        assert composio_key not in captured.out + captured.err
        assert _env_values(tmp_path / "secrets/broker.env")["ADS_COMPOSIO_API_KEY"] == composio_key


class TestTheThirdQuestionNeverEatsAnotherSecret:
    """Revisión de código B1 y revisión de seguridad B2: la pregunta de
    Composio es la única sin eco del flujo normal, y estaba pegada a la
    misma tubería por la que llega la contraseña del dueño."""

    def test_without_a_terminal_and_without_a_composio_flag_it_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`primer-arranque.sh --owner-email … --password-stdin < pass.txt`:
        sin esta guarda, `getpass` caía a stdin, se comía la contraseña del
        dueño y la guardaba como `ADS_COMPOSIO_API_KEY` -- un secreto del
        panel persistido como bearer de un tercero."""
        monkeypatch.setattr("sys.stdin", _PipedStdin(_TYPED_PASSWORD))
        argv = [
            "--workspace",
            str(tmp_path),
            "--skip-start",
            "--public-base-url",
            _PUBLIC_BASE_URL,
            "--owner-email",
            _OWNER_EMAIL,
            "--password-stdin",
        ]

        assert first_run.main(argv) == first_run.EXIT_USAGE

        assert list(tmp_path.rglob("*")) == []

    def test_the_stdin_flag_survives_the_second_invocation_of_the_wrapper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El envoltorio invoca dos veces con los mismos argumentos: en la
        segunda la tubería ya está vacía. Leerla allí mataba el arranque
        con la pila levantada y el dueño sin dar de alta."""
        composio_key = "clave-gestionada-de-prueba"
        monkeypatch.setattr("sys.stdin", _PipedStdin(composio_key))
        first = _run(
            tmp_path,
            "--public-base-url",
            _PUBLIC_BASE_URL,
            "--owner-email",
            _OWNER_EMAIL,
            "--composio-api-key-stdin",
        )

        monkeypatch.setattr("sys.stdin", _PipedStdin(""))
        second = _run(tmp_path, "--composio-api-key-stdin")

        assert (first, second) == (first_run.EXIT_OK, first_run.EXIT_OK)
        broker = _env_values(tmp_path / "secrets/broker.env")
        assert broker["ADS_COMPOSIO_API_KEY"] == composio_key

    def test_an_existing_key_is_never_rotated_in_silence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin", _PipedStdin("primera-clave"))
        _run(
            tmp_path,
            "--public-base-url",
            _PUBLIC_BASE_URL,
            "--owner-email",
            _OWNER_EMAIL,
            "--composio-api-key-stdin",
        )
        capsys.readouterr()

        monkeypatch.setattr("sys.stdin", _PipedStdin("segunda-clave"))
        _run(tmp_path, "--composio-api-key-stdin")

        assert "no se rota" in capsys.readouterr().out
        assert _env_values(tmp_path / "secrets/broker.env")["ADS_COMPOSIO_API_KEY"] == (
            "primera-clave"
        )


class _PipedStdin:
    """stdin de tuberia: `isatty()` falso y una sola lectura."""

    def __init__(self, content: str) -> None:
        self._content = content

    def isatty(self) -> bool:
        return False

    def read(self) -> str:
        return self._content


class _TerminalStdin:
    """stdin de terminal: lo que ve el operador que teclea."""

    def isatty(self) -> bool:
        return True


class TestNothingReachesTheLoggingSystem:
    def test_the_first_run_never_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Los secretos no pueden filtrarse por un canal que no existe:
        esta herramienta imprime y nada más. Si alguien le añade
        `logging`, este test obliga a mirar qué escribe."""
        with caplog.at_level(0):
            _run_with_answers(tmp_path)

        assert caplog.records == []
        leaked = [value for value in _generated_secrets(tmp_path) if value in caplog.text]
        assert leaked == []


class TestInvariant3SecretFilePermissions:
    def test_secret_files_are_0600_and_owned_by_the_caller(self, tmp_path: Path) -> None:
        _run_with_answers(tmp_path)

        for relative in (".env", "secrets/api.env", "secrets/broker.env"):
            status = (tmp_path / relative).stat()
            assert status.st_mode & 0o777 == 0o600, relative
            assert status.st_uid == os.getuid(), relative

    def test_lax_permissions_on_an_existing_secret_file_are_corrected(
        self, tmp_path: Path
    ) -> None:
        _run_with_answers(tmp_path)
        (tmp_path / "secrets/api.env").chmod(0o644)

        assert _run_with_answers(tmp_path) == first_run.EXIT_OK

        assert (tmp_path / "secrets/api.env").stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(_RUNNING_AS_ROOT, reason="root escribe en directorios sin permiso")
    def test_a_secret_that_cannot_be_written_aborts_with_exit_6(self, tmp_path: Path) -> None:
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        secrets_dir.chmod(0o500)
        try:
            assert _run_with_answers(tmp_path) == first_run.EXIT_PERMISSIONS
        finally:
            secrets_dir.chmod(0o700)

        assert not (secrets_dir / "api.env").exists()


class TestAtomicWrites:
    def test_a_failure_mid_write_leaves_no_temporary_with_secrets_inside(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*_args: object, **_kwargs: object) -> None:
            raise OSError("disco lleno")

        monkeypatch.setattr("os.chmod", explode)

        assert _run_with_answers(tmp_path) == first_run.EXIT_PREFLIGHT

        assert list(tmp_path.glob("**/*first-run*")) == []

    def test_a_disk_error_is_not_reported_as_a_permissions_problem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Salida 6 queda para lo que de verdad es un permiso, que es
        cuando hay un secreto expuesto en juego."""

        def explode(*_args: object, **_kwargs: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("os.chmod", explode)

        assert _run_with_answers(tmp_path) == first_run.EXIT_PREFLIGHT

        error = capsys.readouterr().err
        assert "permisos" not in error
        assert "No space left on device" in error

    def test_the_final_file_never_exists_with_lax_permissions(self, tmp_path: Path) -> None:
        """`mkstemp` crea el temporal con 0600 y el `replace` es atomico:
        el fichero final no pasa por un estado intermedio legible."""
        _run_with_answers(tmp_path)

        assert (tmp_path / "secrets/api.env").stat().st_mode & 0o777 == 0o600


class TestManagedFilesAreNeverReachedThroughALink:
    """Revisión de seguridad I1: un fichero gestionado plantado como
    enlace se adoptaría como secreto propio, y el `chmod` de los permisos
    le pondría 0600 a la víctima."""

    def _planted_link(self, tmp_path: Path, relative: str) -> Path:
        victim = tmp_path / "victima.txt"
        victim.write_text("contenido ajeno\n", encoding="utf-8")
        victim.chmod(0o644)
        link = tmp_path / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(victim)
        return victim

    def test_a_planted_link_in_secrets_exits_6_and_leaves_the_victim_alone(
        self, tmp_path: Path
    ) -> None:
        victim = self._planted_link(tmp_path, "secrets/api.env")

        assert _run_with_answers(tmp_path) == first_run.EXIT_PERMISSIONS

        assert victim.read_text(encoding="utf-8") == "contenido ajeno\n"
        assert victim.stat().st_mode & 0o777 == 0o644

    def test_a_planted_link_is_never_adopted_as_configuration(self, tmp_path: Path) -> None:
        self._planted_link(tmp_path, ".env")

        assert _run_with_answers(tmp_path) == first_run.EXIT_PERMISSIONS

        assert not (tmp_path / "secrets/api.env").exists()

    def test_the_error_names_the_file_and_the_link(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._planted_link(tmp_path, "secrets/broker.env")

        _run_with_answers(tmp_path)

        error = capsys.readouterr().err
        assert "secrets/broker.env" in error
        assert "enlace simbólico" in error


class TestTheSecretsDirectory:
    def test_it_is_created_0700(self, tmp_path: Path) -> None:
        _run_with_answers(tmp_path)

        assert (tmp_path / "secrets").stat().st_mode & 0o777 == 0o700

    @pytest.mark.skipif(_RUNNING_AS_ROOT, reason="root corrige cualquier permiso")
    def test_a_group_writable_directory_is_corrected_not_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Un `git clone` con `umask 002` deja `secrets/` en 0775: eso no
        puede tumbar el arranque de un comando, pero tampoco quedarse."""
        (tmp_path / "secrets").mkdir()
        # `mkdir(mode=...)` respeta el umask (en CI es 022 y dejaria 0755, que no es
        # escribible por grupo): el modo que reproduce el `git clone` con umask 002 se
        # fija con chmod, que no lo respeta.
        (tmp_path / "secrets").chmod(0o775)

        assert _run_with_answers(tmp_path) == first_run.EXIT_OK

        assert (tmp_path / "secrets").stat().st_mode & 0o777 == 0o700
        assert "corregido" in capsys.readouterr().out


class TestInvariant4AtMostThreeQuestions:
    def test_a_clean_install_asks_exactly_the_three_questions_in_order(
        self, tmp_path: Path
    ) -> None:
        prompter = RecordingPrompter(
            {
                first_run._URL_QUESTION: _PUBLIC_BASE_URL,
                first_run._EMAIL_QUESTION: _OWNER_EMAIL,
                first_run._COMPOSIO_QUESTION: "",
            }
        )

        assert _run(tmp_path, prompter=prompter.as_prompter()) == first_run.EXIT_OK

        assert prompter.asked == [
            first_run._URL_QUESTION,
            first_run._EMAIL_QUESTION,
            first_run._COMPOSIO_QUESTION,
        ]

    def test_the_password_is_the_fourth_input_and_is_asked_twice_without_echo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Por el alta entera (`_register_owner`), no por la función de
        preguntar: lo que el contrato fija es el orden del paso 8."""
        registered = self._registered_owners(tmp_path, monkeypatch)
        prompter = RecordingPrompter(
            {"Contraseña del dueño: ": _TYPED_PASSWORD, "Repite la contraseña: ": _TYPED_PASSWORD}
        )

        entry = first_run._register_owner(
            tmp_path, self._args(tmp_path), self._answers(), prompter.as_prompter()
        )

        assert prompter.asked == ["Contraseña del dueño: ", "Repite la contraseña: "]
        assert registered == [(_OWNER_EMAIL, _TYPED_PASSWORD)]
        assert entry == _OWNER_EMAIL

    def test_with_the_federated_path_configured_no_password_is_asked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """«4 entradas con la contraseña, y solo sin vía federada»."""
        registered = self._registered_owners(tmp_path, monkeypatch)
        api = tmp_path / "secrets/api.env"
        api.write_text(
            api.read_text(encoding="utf-8")
            + "ADS_FEDERATED_LOGIN_ENABLED=true\n"
            + "ADS_GOOGLE_OIDC_CLIENT_ID=c\n"
            + "ADS_GOOGLE_OIDC_CLIENT_SECRET=s\n",
            encoding="utf-8",
        )
        prompter = RecordingPrompter()

        entry = first_run._register_owner(
            tmp_path, self._args(tmp_path), self._answers(), prompter.as_prompter()
        )

        assert prompter.asked == []
        assert registered == []
        assert "Entrar con Google" in entry
        assert _OWNER_EMAIL in _env_values(api)["ADS_FEDERATED_ALLOWED_EMAILS"]

    def _args(self, workspace: Path) -> argparse.Namespace:
        return first_run._parse_args(["--workspace", str(workspace)])

    def _answers(self) -> first_run.Answers:
        return first_run.Answers(
            public_base_url=_PUBLIC_BASE_URL, owner_email=_OWNER_EMAIL, composio_api_key=None
        )

    def _registered_owners(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> list[tuple[str, str]]:
        """Alta del dueño sin base de datos: lo que se prueba es el orden
        de las preguntas y qué rama se toma, no el `INSERT`."""
        _run_with_answers(workspace)
        monkeypatch.setattr("sys.stdin", _TerminalStdin())
        monkeypatch.setattr(first_run, "_existing_owner_email", lambda _dsn: None)
        registered: list[tuple[str, str]] = []

        async def fake_upsert(*, dsn: str, email: str, password: str) -> None:
            assert dsn
            registered.append((email, password))

        monkeypatch.setattr(first_run, "upsert_owner_password", fake_upsert)
        return registered

    def test_a_mismatched_password_never_reaches_the_database(self) -> None:
        prompter = RecordingPrompter(
            {"Contraseña del dueño: ": _TYPED_PASSWORD, "Repite la contraseña: ": "otra-cosa-larga"}
        )

        with pytest.raises(first_run.OwnerBootstrapError):
            first_run._ask_password(prompter.as_prompter())


class TestInvariant5TheKeyPairIsSplit:
    def test_private_and_public_land_in_different_files(self, tmp_path: Path) -> None:
        _run_with_answers(tmp_path)
        api = _env_values(tmp_path / "secrets/api.env")
        broker = _env_values(tmp_path / "secrets/broker.env")

        assert "ADS_APPROVAL_PUBLIC_KEY" not in api
        assert "ADS_APPROVAL_SIGNING_KEY" not in broker
        assert public_key_for(api["ADS_APPROVAL_SIGNING_KEY"]) == broker["ADS_APPROVAL_PUBLIC_KEY"]

    def test_a_lost_public_key_is_derived_never_regenerated(self, tmp_path: Path) -> None:
        """Muerte del proceso entre los pasos 4 y 5 del contrato: la
        privada ya existe y el broker confia en su publica."""
        _run_with_answers(tmp_path)
        signing_key = _env_values(tmp_path / "secrets/api.env")["ADS_APPROVAL_SIGNING_KEY"]
        (tmp_path / "secrets/broker.env").unlink()

        assert _run_with_answers(tmp_path) == first_run.EXIT_OK

        broker = _env_values(tmp_path / "secrets/broker.env")
        assert broker["ADS_APPROVAL_PUBLIC_KEY"] == public_key_for(signing_key)
        assert _env_values(tmp_path / "secrets/api.env")["ADS_APPROVAL_SIGNING_KEY"] == signing_key


class TestInvariant6ACleanInstallDeniesEverything:
    def test_the_generated_caps_file_has_no_account_with_caps(self, tmp_path: Path) -> None:
        _run_with_answers(tmp_path)

        caps = parse_caps_config((tmp_path / "config/caps.yaml").read_text(encoding="utf-8"))

        assert caps.accounts == {}
        with pytest.raises(CapsConfigError):
            caps.resolve("cualquier-cuenta")

    def test_the_spend_envelope_is_commented_out(self, tmp_path: Path) -> None:
        """Sin `panel_managed`, el panel no puede fijar ningun tope."""
        _run_with_answers(tmp_path)

        caps = parse_caps_config((tmp_path / "config/caps.yaml").read_text(encoding="utf-8"))

        # Con la fase D el modelo conoce `panel_managed`; un arranque limpio lo deja
        # sin declarar (None): el panel no puede fijar ningun tope.
        assert getattr(caps, "panel_managed", None) is None
        assert "#panel_managed:" in (tmp_path / "config/caps.yaml").read_text(encoding="utf-8")


class TestInvariant7DryRunHasNoEffects:
    def test_dry_run_on_a_clean_workspace_writes_nothing(self, tmp_path: Path) -> None:
        assert _run_with_answers(tmp_path, "--dry-run") == first_run.EXIT_OK

        assert list(tmp_path.iterdir()) == []

    def test_dry_run_on_an_existing_workspace_changes_nothing(self, tmp_path: Path) -> None:
        _run_with_answers(tmp_path)
        before = _snapshot(tmp_path)
        (tmp_path / "secrets/api.env").chmod(0o644)

        assert _run_with_answers(tmp_path, "--dry-run") == first_run.EXIT_OK

        assert _snapshot(tmp_path) == before
        assert (tmp_path / "secrets/api.env").stat().st_mode & 0o777 == 0o644

    def test_dry_run_never_asks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Sin prompter inyectado: `--dry-run` construye el que se niega a
        preguntar, en vez de quedarse esperando una respuesta que nadie va
        a teclear."""
        argv = ["--workspace", str(tmp_path), "--skip-start", "--dry-run"]

        assert first_run.main(argv) == first_run.EXIT_USAGE

        assert "no pregunta" in capsys.readouterr().err
        assert list(tmp_path.iterdir()) == []


class TestSummary:
    def test_the_panel_keeps_the_scheme_of_the_instance(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Un desarrollo local es `http://127.0.0.1:8410`: un `https://`
        inventado manda al operador a una puerta que no existe."""
        answers = first_run.Answers(
            public_base_url="http://127.0.0.1:8410",
            owner_email=_OWNER_EMAIL,
            composio_api_key=None,
        )

        first_run._print_summary(answers, _OWNER_EMAIL)

        printed = capsys.readouterr().out
        assert "Panel:     http://127.0.0.1:8410/" in printed
        assert "https://127.0.0.1" not in printed

    def test_the_summary_names_the_installer_command_with_the_real_url(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        answers = first_run.Answers(
            public_base_url=_PUBLIC_BASE_URL, owner_email=_OWNER_EMAIL, composio_api_key=None
        )

        first_run._print_summary(answers, _OWNER_EMAIL)

        assert f"--url {_PUBLIC_BASE_URL}/mcp" in capsys.readouterr().out


class TestExitCodes:
    def test_an_unknown_flag_is_exit_1(self, tmp_path: Path) -> None:
        assert _run(tmp_path, "--que-es-esto") == first_run.EXIT_USAGE

    def test_two_flags_fighting_for_stdin_are_exit_1(self, tmp_path: Path) -> None:
        assert (
            _run(tmp_path, "--composio-api-key-stdin", "--password-stdin")
            == first_run.EXIT_USAGE
        )

    def test_a_missing_workspace_is_exit_2(self, tmp_path: Path) -> None:
        assert _run(tmp_path / "no-existe", "--dry-run") == first_run.EXIT_PREFLIGHT

    def test_a_database_url_that_contradicts_the_password_is_exit_3(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(
            "POSTGRES_PASSWORD=una-cosa\n"
            "ADS_DATABASE_URL=postgresql+asyncpg://ads:otra-cosa@ads-db:5432/ads\n",
            encoding="utf-8",
        )

        assert _run_with_answers(tmp_path) == first_run.EXIT_INCOHERENT_CONFIG

    def test_a_public_key_that_is_not_the_pair_is_exit_3(self, tmp_path: Path) -> None:
        _run_with_answers(tmp_path)
        broker = tmp_path / "secrets/broker.env"
        other_public_key = public_key_for(
            _env_values(tmp_path / "secrets/api.env")["ADS_SESSION_SECRET"]
        )
        broker.write_text(
            broker.read_text(encoding="utf-8").replace(
                _env_values(broker)["ADS_APPROVAL_PUBLIC_KEY"], other_public_key
            ),
            encoding="utf-8",
        )

        assert _run_with_answers(tmp_path) == first_run.EXIT_INCOHERENT_CONFIG

    def test_an_invalid_public_base_url_in_the_env_is_exit_3(self, tmp_path: Path) -> None:
        _run_with_answers(tmp_path)
        dotenv = tmp_path / ".env"
        dotenv.write_text(
            dotenv.read_text(encoding="utf-8").replace(_PUBLIC_BASE_URL, "ftp://ads.example.com"),
            encoding="utf-8",
        )

        assert _run(tmp_path) == first_run.EXIT_INCOHERENT_CONFIG

    def test_no_tty_and_no_password_flag_is_exit_5(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", _PipedStdin(""))
        args = first_run._parse_args(["--workspace", "."])

        with pytest.raises(first_run.OwnerBootstrapError, match="--password-stdin"):
            first_run._owner_password(args, RecordingPrompter().as_prompter())


class TestATruncatedSecretsFileNeverMintsNewKeys:
    """Revisión de seguridad B1: lo que deja un corte de luz a mitad de
    escritura no puede convertirse en claves nuevas en silencio."""

    def _truncate_keeping(self, path: Path, keys: tuple[str, ...]) -> None:
        kept = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if any(line.startswith(f"{key}=") for key in keys)
        ]
        path.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")

    def test_a_secrets_file_that_lost_a_critical_key_exits_3(self, tmp_path: Path) -> None:
        _run_with_answers(tmp_path)
        api = tmp_path / "secrets/api.env"
        broker_before = (tmp_path / "secrets/broker.env").read_text(encoding="utf-8")
        self._truncate_keeping(api, ("ADS_SESSION_SECRET", "ADS_TOTP_ENC_KEY"))
        api_before = api.read_text(encoding="utf-8")

        assert _run_with_answers(tmp_path) == first_run.EXIT_INCOHERENT_CONFIG

        assert api.read_text(encoding="utf-8") == api_before
        assert (tmp_path / "secrets/broker.env").read_text(encoding="utf-8") == broker_before

    def test_the_error_names_the_file_and_the_missing_keys(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run_with_answers(tmp_path)
        self._truncate_keeping(tmp_path / "secrets/api.env", ("ADS_APPROVAL_SIGNING_KEY",))
        capsys.readouterr()

        _run_with_answers(tmp_path)

        error = capsys.readouterr().err
        assert "secrets/api.env" in error
        assert "ADS_SESSION_SECRET" in error
        assert "ADS_TOTP_ENC_KEY" in error

    def test_a_broker_public_key_without_its_seed_exits_3(self, tmp_path: Path) -> None:
        """El broker ya confía en esa pública: una semilla nueva dejaría
        toda aprobación firmada sin verificar."""
        _run_with_answers(tmp_path)
        (tmp_path / "secrets/api.env").unlink()

        assert _run_with_answers(tmp_path) == first_run.EXIT_INCOHERENT_CONFIG

        assert not (tmp_path / "secrets/api.env").exists()

    def test_a_change_me_placeholder_is_still_filled(self, tmp_path: Path) -> None:
        """Un marcador del ejemplo dice «esto lo pones tú», no «me han
        mutilado»: se rellena, como manda el contrato."""
        (tmp_path / "secrets").mkdir()
        (tmp_path / "secrets/api.env").write_text(
            "ADS_APPROVAL_SIGNING_KEY=change-me-ed25519-seed-base64\n"
            "ADS_SESSION_SECRET=change-me-32-bytes-base64\n"
            "ADS_TOTP_ENC_KEY=change-me-32-bytes-base64\n",
            encoding="utf-8",
        )

        assert _run_with_answers(tmp_path) == first_run.EXIT_OK

        api = _env_values(tmp_path / "secrets/api.env")
        assert "change-me" not in api["ADS_SESSION_SECRET"]
        assert public_key_for(api["ADS_APPROVAL_SIGNING_KEY"])


class TestTheOwnerBearerIsNotBornDormant:
    """Seguimiento aceptado en la revision de seguridad T022 (spec 008
    fase E): `ADS_MCP_TOKEN` se generaba SIEMPRE, incluso con la via
    estatica apagada -- que es como nace el producto estandar --, asi que
    toda instalacion limpia estrenaba un bearer de dueño eterno, con
    permiso `aprobar`, dormido en `secrets/api.env` y sin rotar. Ahora el
    bearer solo aparece si su via esta abierta."""

    _SWITCH = "ADS_MCP_STATIC_TOKEN_ENABLED"

    def _rewrite_switch(self, workspace: Path, value: str) -> None:
        api = workspace / "secrets/api.env"
        api.write_text(
            api.read_text(encoding="utf-8").replace(
                f"{self._SWITCH}=false", f"{self._SWITCH}={value}"
            ),
            encoding="utf-8",
        )

    def _append_to_api_env(self, workspace: Path, content: str) -> None:
        """Lo que hace un operador de verdad: instalar primero y abrir la
        via de emergencia despues, editando `secrets/api.env`."""
        api = workspace / "secrets/api.env"
        api.write_text(api.read_text(encoding="utf-8") + content, encoding="utf-8")

    def test_a_clean_install_generates_no_owner_bearer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(self._SWITCH, raising=False)

        assert _run_with_answers(tmp_path) == first_run.EXIT_OK

        api = _env_values(tmp_path / "secrets/api.env")
        assert api[self._SWITCH] == "false"
        assert "ADS_MCP_TOKEN" not in api

    def test_with_the_static_path_already_open_the_bearer_is_generated(
        self, tmp_path: Path
    ) -> None:
        """Un operador que enciende la via de emergencia a proposito SI
        necesita bearer -- `ApiSettings` no arranca sin el."""
        assert _run_with_answers(tmp_path) == first_run.EXIT_OK
        self._rewrite_switch(tmp_path, "true")

        assert _run_with_answers(tmp_path) == first_run.EXIT_OK

        api = _env_values(tmp_path / "secrets/api.env")
        assert api[self._SWITCH] == "true"
        assert len(api["ADS_MCP_TOKEN"]) >= _MIN_GENERATED_SECRET_CHARS

    def test_an_existing_bearer_is_never_rotated(self, tmp_path: Path) -> None:
        """El despliegue que ya tiene su via abierta y su bearer escrito no
        cambia: una rotacion silenciosa dejaria fuera a quien lo usa."""
        assert _run_with_answers(tmp_path) == first_run.EXIT_OK
        self._rewrite_switch(tmp_path, "true")
        self._append_to_api_env(tmp_path, f"ADS_MCP_TOKEN={_BEARER_ALREADY_THERE}\n")

        assert _run_with_answers(tmp_path) == first_run.EXIT_OK

        api = _env_values(tmp_path / "secrets/api.env")
        assert api["ADS_MCP_TOKEN"] == _BEARER_ALREADY_THERE


class TestACriticalKeyThatIsNoLongerAKey:
    """Segunda vuelta de la revisión de seguridad: vacía o truncada a la
    mitad es tan inservible como ausente."""

    def _rewrite(self, path: Path, key: str, value: str) -> None:
        lines = [
            f"{key}={value}" if line.startswith(f"{key}=") else line
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    def test_an_empty_critical_value_exits_3(self, tmp_path: Path) -> None:
        _run_with_answers(tmp_path)
        api = tmp_path / "secrets/api.env"
        self._rewrite(api, "ADS_TOTP_ENC_KEY", "")
        before = api.read_text(encoding="utf-8")

        assert _run_with_answers(tmp_path) == first_run.EXIT_INCOHERENT_CONFIG

        assert api.read_text(encoding="utf-8") == before

    def test_a_half_truncated_value_exits_3(self, tmp_path: Path) -> None:
        _run_with_answers(tmp_path)
        api = tmp_path / "secrets/api.env"
        self._rewrite(api, "ADS_SESSION_SECRET", "MDAwMD")
        before = api.read_text(encoding="utf-8")

        assert _run_with_answers(tmp_path) == first_run.EXIT_INCOHERENT_CONFIG

        assert api.read_text(encoding="utf-8") == before

    def test_the_error_names_the_file_and_the_key(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run_with_answers(tmp_path)
        self._rewrite(tmp_path / "secrets/broker.env", "ADS_CREDENTIAL_MASTER_KEY", "corta")
        capsys.readouterr()

        _run_with_answers(tmp_path)

        error = capsys.readouterr().err
        assert "secrets/broker.env" in error
        assert "ADS_CREDENTIAL_MASTER_KEY" in error

    def test_a_database_password_is_not_measured_by_length(self, tmp_path: Path) -> None:
        """Una instalación heredada puede traer una contraseña de base de
        datos corta: no es este el sitio de discutirla."""
        _run_with_answers(tmp_path)
        dotenv = tmp_path / ".env"
        short = "corta"
        self._rewrite(dotenv, "POSTGRES_PASSWORD", short)
        self._rewrite(
            dotenv, "ADS_DATABASE_URL", f"postgresql+asyncpg://ads:{short}@ads-db:5432/ads"
        )

        assert _run_with_answers(tmp_path) == first_run.EXIT_OK


class TestALostBrokerFile:
    def test_a_new_master_key_is_announced_never_minted_in_silence(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run_with_answers(tmp_path)
        old_key = _env_values(tmp_path / "secrets/broker.env")["ADS_CREDENTIAL_MASTER_KEY"]
        (tmp_path / "secrets/broker.env").unlink()
        capsys.readouterr()

        assert _run_with_answers(tmp_path) == first_run.EXIT_OK

        printed = capsys.readouterr().out
        assert "almacén cifrado quedará ilegible" in printed
        assert _env_values(tmp_path / "secrets/broker.env")["ADS_CREDENTIAL_MASTER_KEY"] != old_key

    def test_a_clean_install_says_nothing_about_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _run_with_answers(tmp_path) == first_run.EXIT_OK

        assert "almacén cifrado" not in capsys.readouterr().out


class TestFederatedOwnerBranch:
    def _api_env(self, workspace: Path, content: str) -> None:
        (workspace / "secrets").mkdir(parents=True, exist_ok=True)
        (workspace / "secrets/api.env").write_text(content, encoding="utf-8")

    def test_detects_the_branch_only_with_switch_and_full_oidc_client(
        self, tmp_path: Path
    ) -> None:
        self._api_env(tmp_path, "ADS_FEDERATED_LOGIN_ENABLED=true\nADS_GOOGLE_OIDC_CLIENT_ID=c\n")

        assert not first_run._federated_login_configured(first_run._load_workspace_env(tmp_path))

        self._api_env(
            tmp_path,
            "ADS_FEDERATED_LOGIN_ENABLED=true\n"
            "ADS_GOOGLE_OIDC_CLIENT_ID=c\n"
            "ADS_GOOGLE_OIDC_CLIENT_SECRET=s\n",
        )

        assert first_run._federated_login_configured(first_run._load_workspace_env(tmp_path))

    def test_the_switch_off_keeps_the_password_branch(self, tmp_path: Path) -> None:
        self._api_env(
            tmp_path,
            "ADS_FEDERATED_LOGIN_ENABLED=false\n"
            "ADS_GOOGLE_OIDC_CLIENT_ID=c\n"
            "ADS_GOOGLE_OIDC_CLIENT_SECRET=s\n",
        )

        assert not first_run._federated_login_configured(first_run._load_workspace_env(tmp_path))

    def test_the_deployment_environment_also_turns_the_branch_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._api_env(tmp_path, "")
        monkeypatch.setenv("ADS_FEDERATED_LOGIN_ENABLED", "true")
        monkeypatch.setenv("ADS_GOOGLE_OIDC_CLIENT_ID", "c")
        monkeypatch.setenv("ADS_GOOGLE_OIDC_CLIENT_SECRET", "s")

        assert first_run._federated_login_configured(first_run._load_workspace_env(tmp_path))

    def test_the_owner_email_joins_the_allowed_list_without_a_password(
        self, tmp_path: Path
    ) -> None:
        self._api_env(tmp_path, "ADS_FEDERATED_ALLOWED_EMAILS=otro@example.com\n")

        first_run._authorize_federated_owner(
            tmp_path, first_run._load_workspace_env(tmp_path), _OWNER_EMAIL
        )

        api = _env_values(tmp_path / "secrets/api.env")
        assert api["ADS_FEDERATED_ALLOWED_EMAILS"] == f"otro@example.com,{_OWNER_EMAIL}"
        assert (tmp_path / "secrets/api.env").stat().st_mode & 0o777 == 0o600

    def test_an_already_authorized_email_is_left_alone(self, tmp_path: Path) -> None:
        self._api_env(tmp_path, f"ADS_FEDERATED_ALLOWED_EMAILS={_OWNER_EMAIL}\n")
        before = (tmp_path / "secrets/api.env").read_text(encoding="utf-8")

        first_run._authorize_federated_owner(
            tmp_path, first_run._load_workspace_env(tmp_path), _OWNER_EMAIL
        )

        assert (tmp_path / "secrets/api.env").read_text(encoding="utf-8") == before

    def test_a_json_allowed_list_is_understood(self, tmp_path: Path) -> None:
        self._api_env(tmp_path, 'ADS_FEDERATED_ALLOWED_EMAILS=["Uno@example.com"]\n')

        first_run._authorize_federated_owner(
            tmp_path, first_run._load_workspace_env(tmp_path), _OWNER_EMAIL
        )

        api = _env_values(tmp_path / "secrets/api.env")
        assert api["ADS_FEDERATED_ALLOWED_EMAILS"] == f"uno@example.com,{_OWNER_EMAIL}"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestStackVerification:
    def test_health_200_plus_mcp_401_is_a_healthy_stack(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200 if request.url.path.endswith("health") else 401)

        with _client(handler) as client:
            assert first_run._probe(client, first_run._health_candidates(_PUBLIC_BASE_URL)[0])

    def test_mcp_answering_200_is_a_stack_that_does_not_ask_for_authorization(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        with _client(handler) as client:
            with pytest.raises(first_run.StackUnhealthyError, match="401"):
                first_run._probe(client, first_run._health_candidates(_PUBLIC_BASE_URL)[0])

    def test_a_connection_error_is_not_yet_ready_not_a_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("todavia no escucha")

        with _client(handler) as client:
            assert not first_run._probe(client, first_run._health_candidates(_PUBLIC_BASE_URL)[0])

    def test_the_internal_candidates_declare_the_public_host(self) -> None:
        """Con `Host: 127.0.0.1` el guardia anti DNS-rebinding acepta
        siempre, así que el sondeo no podía detectar un
        `ADS_PUBLIC_BASE_URL` equivocado y aun así lo culpaba."""
        candidates = first_run._health_candidates(_PUBLIC_BASE_URL)

        assert candidates[0].host_header is None
        assert [candidate.host_header for candidate in candidates[1:]] == [
            "ads.example.com",
            "ads.example.com",
        ]

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_a_5xx_is_still_warming_up_not_a_broken_stack(self, status: int) -> None:
        """Con Caddy o nginx delante, el proxy contesta 502/503 mientras
        `ads-api` arranca: abortar ahí sería fallar por ir rápido."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status)

        with _client(handler) as client:
            assert not first_run._probe(client, first_run._health_candidates(_PUBLIC_BASE_URL)[0])

    def test_when_only_the_internal_candidate_answers_it_says_so(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        internal = first_run._health_candidates(_PUBLIC_BASE_URL)[1]

        first_run._report_healthy(internal, _PUBLIC_BASE_URL)

        printed = capsys.readouterr().out
        assert "no contestó desde dentro del contenedor" in printed
        assert f"curl -fsS {_PUBLIC_BASE_URL}/api/v1/health" in printed

    def test_when_the_public_url_answers_it_names_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        public = first_run._health_candidates(_PUBLIC_BASE_URL)[0]

        first_run._report_healthy(public, _PUBLIC_BASE_URL)

        printed = capsys.readouterr().out
        assert f"pila sana en {_PUBLIC_BASE_URL}" in printed
        assert "no contestó" not in printed


# Lo minimo que `scripts/primer-arranque.sh` necesita para arrancar: sin
# `docker`, para que el unico que exista sea el falso (o ninguno).
_WRAPPER_TOOLS = ("bash", "env", "dirname", "id")


def _compose_published_api_port() -> int:
    """`compose.yaml` es la única fuente de verdad del puerto publicado."""
    compose = yaml.safe_load((_REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    published = str(compose["services"]["ads-api"]["ports"][0])  # 127.0.0.1:8410:8410
    return int(published.split(":")[-2])


_PUBLISHED_API_PORT = _compose_published_api_port()


def _isolated_bin(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for tool in _WRAPPER_TOOLS:
        resolved = shutil.which(tool)
        assert resolved is not None, tool
        (directory / tool).symlink_to(resolved)
    return directory


def _fake_docker_bin(directory: Path, log: Path) -> Path:
    """Doble de `docker` que apunta la llamada Y el entorno con el que la
    recibe: es la única forma de comprobar que los valores de arranque no
    llegan a `up -d ads-db`."""
    _isolated_bin(directory)
    fake = directory / "docker"
    # Solo builtins de bash: el `PATH` del test está aislado a propósito y
    # no tiene `grep` ni `sed` (se descubrió aquí: el doble los llamaba y
    # fallaba en silencio, dejando el entorno sin registrar).
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "{\n"
        '  echo "CALL $*"\n'
        "  for nombre in $(compgen -e); do\n"
        '    case "$nombre" in POSTGRES*) echo "ENV $nombre=${!nombre}" ;; esac\n'
        "  done\n"
        f'}} >> "{log}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return directory


def _calls(log: Path) -> list[str]:
    return [
        line.removeprefix("CALL ")
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith("CALL ")
    ]


def _environment_of(log: Path, call: str) -> list[str]:
    """Líneas `ENV …` que siguen a la llamada indicada, hasta la siguiente."""
    collected: list[str] = []
    collecting = False
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.startswith("CALL "):
            collecting = call in line
            continue
        if collecting and line.startswith("ENV "):
            collected.append(line.removeprefix("ENV "))
    return collected


def _run_wrapper(path_dir: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- ruta fija del repo, sin shell
        [str(_REPO_ROOT / "scripts/primer-arranque.sh"), *extra],
        capture_output=True,
        text=True,
        env={"PATH": str(path_dir), "HOME": str(path_dir.parent)},
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=60,
    )


class TestWrapper:
    def test_dry_run_never_starts_the_stack(self, tmp_path: Path) -> None:
        log = tmp_path / "docker.log"
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        result = _run_wrapper(path_dir, "--dry-run")

        assert result.returncode == 0, result.stderr
        calls = "\n".join(_calls(log))
        assert "compose up -d" not in calls
        assert "ads-migrate" not in calls
        assert "--skip-start --dry-run" in calls

    def test_dry_run_never_builds_the_image(self, tmp_path: Path) -> None:
        """Construir son varios minutos: una pasada que no escribe nada no
        los gasta."""
        log = tmp_path / "docker.log"
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        _run_wrapper(path_dir, "--dry-run")

        assert not any("build" in call for call in _calls(log))

    def test_the_full_flow_starts_the_stack_between_the_two_invocations(
        self, tmp_path: Path
    ) -> None:
        log = tmp_path / "docker.log"
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        result = _run_wrapper(path_dir, "--no-composio")

        assert result.returncode == 0, result.stderr
        calls = _calls(log)
        order = [index for index, line in enumerate(calls) if "first_run" in line]
        migrate = next(index for index, line in enumerate(calls) if "ads-migrate" in line)
        assert len(order) == 2
        assert order[0] < migrate < order[1]
        assert "--skip-start" in calls[order[0]]
        assert "--skip-start" not in calls[order[1]]

    def test_the_bootstrap_credentials_never_reach_the_database(self, tmp_path: Path) -> None:
        """Los dos valores que solo existen para interpolar `compose.yaml`
        mientras se generan los ficheros: si sobrevivieran hasta `up -d
        ads-db`, la base nacería con unas credenciales y
        `ADS_DATABASE_URL` diría otras."""
        log = tmp_path / "docker.log"
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        _run_wrapper(path_dir, "--no-composio")

        assert _environment_of(log, "up -d ads-db") == []
        assert _environment_of(log, "run --rm ads-migrate") == []
        assert "POSTGRES_USER=primer-arranque" in _environment_of(log, "--skip-start")

    def test_the_preflight_validates_the_compose_file(self, tmp_path: Path) -> None:
        log = tmp_path / "docker.log"
        path_dir = _fake_docker_bin(tmp_path / "bin", log)

        _run_wrapper(path_dir, "--dry-run")

        assert any(call.startswith("compose config") for call in _calls(log))

    def test_the_wrapper_watches_the_port_that_compose_publishes(self) -> None:
        """El envoltorio lleva el puerto como constante (leerlo de
        `compose.yaml` en bash exigiría `jq` o un `docker compose config`
        que el preflight aún no ha validado). Esta prueba es la que impide
        que las dos copias diverjan."""
        script = (_REPO_ROOT / "scripts/primer-arranque.sh").read_text(encoding="utf-8")

        assert f"PUERTO_API={_PUBLISHED_API_PORT}" in script

    def test_a_published_port_taken_by_someone_else_fails_preflight(
        self, tmp_path: Path
    ) -> None:
        """Ocupado por NUESTRA pila es un segundo arranque y no se avisa;
        ocupado por otro, `ads-api` no podría publicarlo."""
        log = tmp_path / "docker.log"
        path_dir = _fake_docker_bin(tmp_path / "bin", log)
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", _PUBLISHED_API_PORT))
            except OSError:  # pragma: no cover - depende de la máquina
                pytest.skip(f"127.0.0.1:{_PUBLISHED_API_PORT} ya está ocupado aquí")
            listener.listen(1)

            result = _run_wrapper(path_dir, "--dry-run")

        assert result.returncode == first_run.EXIT_PREFLIGHT
        assert str(_PUBLISHED_API_PORT) in result.stderr

    def test_without_docker_it_fails_preflight_with_exit_2(self, tmp_path: Path) -> None:
        result = _run_wrapper(_isolated_bin(tmp_path / "bin"))

        assert result.returncode == first_run.EXIT_PREFLIGHT
        assert "docker" in result.stderr
