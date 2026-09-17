"""Primer arranque de un comando (008-mcp-ads-estandar T016;
`contracts/first-run-cli.md`): genera todo lo derivable, pregunta solo lo
que nadie puede decidir por el operador (URL publica, correo del dueno y,
opcionalmente, la clave gestionada) y deja la instancia lista para entrar.

Reparto con `scripts/primer-arranque.sh` (T019): el envoltorio es el UNICO
que habla docker -- preflight, imagen y arranque de la pila (paso 7 del
contrato) -- porque este modulo corre DENTRO de la imagen, donde no hay CLI
de docker ni socket que lo permita, y darselo seria regalar root del host
por un script de instalacion. Aqui viven los pasos que escriben ficheros,
dan de alta al dueno y verifican por HTTP. El envoltorio invoca dos veces
(primero `--skip-start`, despues sin el) y la idempotencia hace que la
segunda pasada no regenere ni pise nada.

Ningun secreto entra por argv ni sale por stdout: se generan aqui, se
escriben con 0600 y de ellos solo se nombra la CLAVE, nunca el valor.

Dos desvios del contrato, ambos deliberados y por la misma razon (un
fichero que nadie lee es un paso a medias, no un paso):

- Los ficheros NO se copian de sus `*.example`: esas plantillas traen
  marcadores `change-me-...` que `make check-secrets` rechaza y que
  ademas encenderian a medias integraciones opcionales (el bot de
  Telegram con un token de mentira). Se escribe exactamente el juego de
  claves del contrato, con una cabecera que remite al ejemplo para lo
  opcional.
- `ADS_SINGLE_OWNER_MODE`, `ADS_TRUSTED_PROXY_HOPS` y
  `ADS_MCP_STATIC_TOKEN_ENABLED` van a `secrets/api.env`, no a `.env`:
  `compose.yaml` solo reenvia a `ads-api`/`ads-worker` las cinco variables
  de `x-ads-app-env`, asi que en `.env` habrian quedado inertes y
  `ApiSettings` seguiria muriendo al arrancar ("exactamente uno de
  ADS_SEAT_AUTHORITY_ENABLED/ADS_SINGLE_OWNER_MODE").
- `.env` gana `ADS_OWNER_EMAIL`, que NINGUN proceso de la aplicacion lee:
  es estado del instalador. Sin el, la segunda invocacion del envoltorio
  volveria a preguntar el correo, y el contrato dice que una respuesta que
  ya vive en `.env` no se repite.

`.env` se escribe con 0600 (el contrato solo lo exige para `secrets/*.env`)
porque lleva `POSTGRES_PASSWORD` y el DSN con esa contrasena dentro. Aun
asi, esas variables son visibles en `docker inspect ads-db` para quien
pueda hablar con el demonio de docker: quien tiene ese socket ya es root
de la maquina, asi que no es una frontera nueva, pero conviene saberlo.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import errno
import json
import os
import re
import secrets
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote, unquote, urlsplit

import asyncpg  # type: ignore[import-untyped]
import httpx

from safent_ads.tools.gen_keys import ApprovalKeyMaterialError, generate_key_pair, public_key_for
from safent_ads.tools.seed_owner import to_asyncpg_dsn, upsert_owner_password

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PREFLIGHT = 2
EXIT_INCOHERENT_CONFIG = 3
EXIT_STACK_UNHEALTHY = 4
EXIT_OWNER = 5
EXIT_PERMISSIONS = 6


class FirstRunError(RuntimeError):
    """Fallo con codigo de salida propio (`contracts/first-run-cli.md`)."""

    exit_code: int = EXIT_USAGE


class UsageError(FirstRunError):
    exit_code = EXIT_USAGE


class PreflightError(FirstRunError):
    exit_code = EXIT_PREFLIGHT


class IncoherentConfigError(FirstRunError):
    exit_code = EXIT_INCOHERENT_CONFIG


class StackUnhealthyError(FirstRunError):
    exit_code = EXIT_STACK_UNHEALTHY


class OwnerBootstrapError(FirstRunError):
    exit_code = EXIT_OWNER


class SecretPermissionsError(FirstRunError):
    exit_code = EXIT_PERMISSIONS


_SECRET_FILE_MODE = 0o600
_SECRET_DIR_MODE = 0o700
_PUBLIC_DIR_MODE = 0o755
_GROUP_AND_OTHER_WRITE = 0o022
_CAPS_FILE_MODE = 0o644
_RANDOM_SECRET_BYTES = 32
_MIN_PASSWORD_CHARS = 12
_MAX_PASSWORD_CHARS = 256
_MAX_PROMPT_ATTEMPTS = 3
_HEALTH_DEADLINE_SECONDS = 90.0
_HEALTH_POLL_SECONDS = 2.0
_HTTP_TIMEOUT_SECONDS = 5.0

_DEFAULT_DB_USER = "ads"
_DEFAULT_DB_NAME = "ads"
_DEFAULT_TZ = "Europe/Madrid"
_DEFAULT_ACTIVE_HOURS = "08:00-21:00"
_BROKER_SOCKET = "/run/ads-broker/broker.sock"
_BROKER_CAPS_FILE = "/etc/ads-broker/caps.yaml"
_BROKER_CREDENTIAL_STORE_DIR = "/var/lib/ads-broker/credentials"
_BROKER_ALLOWED_UIDS = "[10001]"
_AGENT_SERVER_NAME = "safent-ads"
# El nombre del interruptor, no un secreto (`S105` mira el sufijo TOKEN).
_STATIC_TOKEN_SWITCH = "ADS_MCP_STATIC_TOKEN_ENABLED"  # noqa: S105

_PLACEHOLDER_MARK = "change-me"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Las TRES preguntas del contrato, en este orden y ninguna mas (SC-005).
_URL_QUESTION = "URL pública de esta instancia (https://…): "
_EMAIL_QUESTION = "Correo del dueño: "
_COMPOSIO_QUESTION = "Clave de Composio para conectar cuentas en un clic (Enter para omitir): "
_ENV_LINE_RE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")

_DOTENV_HEADER = """# Generado por `make first-run` (safent_ads.tools.first_run).
# Solo lo que `compose.yaml` sustituye como `${VAR}`. Lo opcional (companion,
# perfiles) esta documentado en `.env.example`.
# 0600: lleva POSTGRES_PASSWORD y el DSN con esa contrasena dentro. Esas
# variables se ven ademas en `docker inspect ads-db`, al alcance de quien
# pueda hablar con el demonio de docker.
# ADS_OWNER_EMAIL no lo lee ningun proceso: es la memoria del instalador
# para no volver a preguntarlo.
"""
_API_ENV_HEADER = """# Generado por `make first-run` (safent_ads.tools.first_run).
# Secretos de ads-api/ads-worker. 0600, nunca en git. Lo opcional (Telegram,
# respaldo cloud de creatividad) esta documentado en `secrets/api.env.example`.
"""
_BROKER_ENV_HEADER = """# Generado por `make first-run` (safent_ads.tools.first_run).
# Secretos de ads-broker. 0600, nunca en git. Las credenciales de plataforma
# las teclea el dueno en el panel; aqui no vive ninguna.
"""
_CAPS_TEMPLATE = """# Generado por `make first-run` (safent_ads.tools.first_run).
# Topes duros del broker. `config/caps.example.yaml` documenta cada clave.
# `accounts: {}` = ninguna cuenta puede escribir todavia: sin tope no se
# escribe, y cada intento queda auditado.
defaults:
  max_step_pct: 30
  max_changes_per_day: 2
  autonomy_enabled: false

accounts: {}

# Sobre de gasto opcional: sin este bloque el panel no puede fijar ningun
# tope (008 fase D). No hay valor por defecto a proposito; elige el tuyo.
#panel_managed:
#  max_daily_cap_minor: 5000
#  max_monthly_cap_minor: 100000
#  max_accounts: 3
"""


# ── Preguntas ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Prompter:
    """Las dos formas de preguntar: con eco y sin eco."""

    visible: Callable[[str], str]
    secret: Callable[[str], str]


def terminal_prompter() -> Prompter:
    return Prompter(visible=input, secret=_terminal_secret)


def _terminal_secret(message: str) -> str:
    """Pregunta sin eco, y solo si hay terminal.

    `getpass` cae a `sys.stdin` cuando no puede apagar el eco: sin
    terminal, esta pregunta se comeria lo que venga por la tuberia -- en
    el flujo documentado (`--password-stdin < pass.txt`), la contraseña
    del dueño, que acabaria guardada como `ADS_COMPOSIO_API_KEY`, un
    bearer de un tercero. Falla cerrado y nombra los flags."""
    if not sys.stdin.isatty():
        raise UsageError(
            "sin terminal para preguntar sin eco: declara --no-composio, "
            "--composio-api-key-stdin o --password-stdin, según lo que falte"
        )
    return getpass(message)


def _refuse(_message: str) -> NoReturn:
    raise UsageError(
        "--dry-run no pregunta: pasa --public-base-url, --owner-email y "
        "--no-composio (o --composio-api-key-stdin)"
    )


def silent_prompter() -> Prompter:
    """`--dry-run`: informa, nunca pregunta ni escribe."""
    return Prompter(visible=_refuse, secret=_refuse)


@dataclass(frozen=True)
class Answers:
    public_base_url: str
    owner_email: str
    composio_api_key: str | None


def _validate_public_base_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    parts = urlsplit(url)
    loopback = parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}
    if not parts.netloc or (parts.scheme != "https" and not loopback):
        raise ValueError(f"URL inválida: {url!r}; se esperaba https://… (o http://127.0.0.1)")
    return url


def _validate_email(raw: str) -> str:
    email = raw.strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError(f"correo inválido: {email!r}; se esperaba algo como nombre@dominio.com")
    return email


def _ask(
    prompt: Callable[[str], str], message: str, validate: Callable[[str], str], flag: str
) -> str:
    for attempt in range(_MAX_PROMPT_ATTEMPTS):
        try:
            answer = prompt(message)
        except EOFError as exc:
            raise UsageError(f"falta {flag}: no hay terminal para preguntarlo") from exc
        try:
            return validate(answer)
        except ValueError as exc:
            if attempt == _MAX_PROMPT_ATTEMPTS - 1:
                raise UsageError(str(exc)) from exc
            print(f"  ✘ {exc}", file=sys.stderr)
    raise UsageError(f"falta {flag}: sin respuesta válida")


def _read_stdin_secret(flag: str) -> str:
    if sys.stdin.isatty():
        raise UsageError(f"{flag} exige que el valor llegue por stdin, no tecleado en un terminal")
    value = sys.stdin.read().strip()
    if not value:
        raise UsageError(f"{flag}: stdin no traía ningún valor")
    return value


def _resolve_answers(
    args: argparse.Namespace, existing: WorkspaceEnv, prompter: Prompter
) -> Answers:
    """Maximo 3 preguntas, en el orden del contrato. Una respuesta que ya
    vive en los ficheros no se vuelve a pedir."""
    known_url = args.public_base_url or existing.kept(".env", "ADS_PUBLIC_BASE_URL")
    url = (
        _validate_public_base_url(known_url)
        if known_url
        else _ask(prompter.visible, _URL_QUESTION, _validate_public_base_url, "--public-base-url")
    )
    known_email = args.owner_email or existing.kept(".env", "ADS_OWNER_EMAIL")
    email = (
        _validate_email(known_email)
        if known_email
        else _ask(prompter.visible, _EMAIL_QUESTION, _validate_email, "--owner-email")
    )
    return Answers(
        public_base_url=url,
        owner_email=email,
        composio_api_key=_resolve_composio(args, existing, prompter),
    )


def _resolve_composio(
    args: argparse.Namespace, existing: WorkspaceEnv, prompter: Prompter
) -> str | None:
    # El fichero manda sobre el flag, y se mira ANTES de tocar stdin: el
    # envoltorio invoca dos veces con los mismos argumentos y en la segunda
    # la tuberia ya esta vacia -- leerla ahi mataba el arranque con la pila
    # levantada y el dueño sin dar de alta. Ademas, la decision de la via
    # gestionada pertenece a la CREACION de `secrets/broker.env`: repetir
    # la pregunta en cada pasada convertiria el arranque en un
    # interrogatorio.
    if args.no_composio or existing.exists("secrets/broker.env"):
        _report_composio_flag_ignored(args, existing)
        return None
    if args.composio_api_key_stdin:
        return _read_stdin_secret("--composio-api-key-stdin")
    return prompter.secret(_COMPOSIO_QUESTION).strip() or None


def _report_composio_flag_ignored(args: argparse.Namespace, existing: WorkspaceEnv) -> None:
    """El primer arranque no rota claves: lo dice en vez de callarse."""
    if not args.composio_api_key_stdin:
        return
    if existing.kept("secrets/broker.env", "ADS_COMPOSIO_API_KEY") is not None:
        print("· ya hay ADS_COMPOSIO_API_KEY en secrets/broker.env: no se rota aquí.")
        return
    print("· secrets/broker.env ya existe: añade ADS_COMPOSIO_API_KEY a mano si la quieres.")


# ── Ficheros del workspace ──────────────────────────────────────────────


def _read_managed_text(path: Path) -> str:
    """Lee un fichero gestionado SIN seguir enlaces simbolicos.

    Un `secrets/api.env` plantado como enlace a otro fichero se adoptaria
    como secreto propio (revision de seguridad I1): lo que hubiera dentro
    pasaria a ser `ADS_SESSION_SECRET` y, peor, `_enforce_mode` le pondria
    0600 a la victima."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SecretPermissionsError(
                f"{path} es un enlace simbólico: los ficheros gestionados se leen y se "
                "escriben tal cual, nunca a través de un enlace"
            ) from exc
        raise SecretPermissionsError(f"no se pudo leer {path}: {exc}") from exc
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        return handle.read()


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise SecretPermissionsError(
            f"{path} es un enlace simbólico: los ficheros gestionados se leen y se "
            "escriben tal cual, nunca a través de un enlace"
        )


def _parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ENV_LINE_RE.match(line.strip())
        if match is not None:
            values[match.group("key")] = match.group("value").strip()
    return values


def _is_placeholder(value: str | None) -> bool:
    return value is None or not value.strip() or _PLACEHOLDER_MARK in value


@dataclass(frozen=True)
class WorkspaceEnv:
    """Lo que ya hay escrito en el workspace, por fichero."""

    root: Path
    files: Mapping[str, dict[str, str]]

    def kept(self, relative: str, key: str) -> str | None:
        """Valor utilizable: `None` si falta o es un marcador `change-me`."""
        value = self.files.get(relative, {}).get(key)
        return None if _is_placeholder(value) else value

    def exists(self, relative: str) -> bool:
        return (self.root / relative).is_file()


_MANAGED_FILES = (".env", "secrets/api.env", "secrets/broker.env")


def _load_workspace_env(root: Path) -> WorkspaceEnv:
    files: dict[str, dict[str, str]] = {}
    for relative in _MANAGED_FILES:
        path = root / relative
        files[relative] = _parse_env_text(_read_managed_text(path))
    return WorkspaceEnv(root=root, files=files)


def _configured(existing: WorkspaceEnv, key: str) -> str | None:
    """Valor EFECTIVO de una variable de `ads-api`: la del workspace o, si
    la capa de despliegue la inyecta por entorno (`deploy/**`), la que ve
    este proceso. Leer solo el fichero dejaria la rama federada sin
    detectar en un despliegue que configura el OIDC por entorno."""
    value = existing.kept("secrets/api.env", key)
    if value is not None:
        return value
    from_environment = os.environ.get(key)
    return None if _is_placeholder(from_environment) else from_environment


@dataclass(frozen=True)
class FileSpec:
    relative: str
    mode: int
    header: str
    values: dict[str, str]


@dataclass(frozen=True)
class FileOutcome:
    """`filled_keys` a `None` = este fichero no se completa por claves
    (`config/caps.yaml`): decir «0 claves añadidas» de un YAML sin claves
    era contar algo que no existe."""

    relative: str
    mode: int
    created: bool
    filled_keys: tuple[str, ...] | None


def _merge_env_text(current: str, values: Mapping[str, str]) -> tuple[str, tuple[str, ...]]:
    lines = current.splitlines()
    index_of = {
        match.group("key"): position
        for position, line in enumerate(lines)
        if (match := _ENV_LINE_RE.match(line.strip())) is not None
    }
    filled: list[str] = []
    for key, value in values.items():
        position = index_of.get(key)
        if position is not None and not _is_placeholder(lines[position].split("=", 1)[1]):
            continue
        if position is None:
            lines.append(f"{key}={value}")
        else:
            lines[position] = f"{key}={value}"
        filled.append(key)
    return "\n".join(lines).rstrip("\n") + "\n", tuple(filled)


def _upsert_env_text(current: str, key: str, value: str) -> str:
    """Fija una clave aunque ya tenga valor -- al contrario que
    `_merge_env_text`, que solo rellena lo ausente."""
    lines = current.splitlines()
    for position, line in enumerate(lines):
        match = _ENV_LINE_RE.match(line.strip())
        if match is not None and match.group("key") == key:
            lines[position] = f"{key}={value}"
            return "\n".join(lines).rstrip("\n") + "\n"
    lines.append(f"{key}={value}")
    return "\n".join(lines).rstrip("\n") + "\n"


def _write_file(path: Path, content: str, mode: int) -> None:
    """Sustitucion atomica: el fichero final nunca existe a medias ni con
    permisos laxos, ni siquiera un instante.

    El temporal lo crea `mkstemp` (nombre impredecible, `O_EXCL`, 0600):
    un nombre fijo se puede adelantar con un enlace simbolico y hacer que
    este proceso escriba los secretos donde quiera el atacante."""
    _reject_symlink(path)
    # Un directorio de secretos nace 0700: si naciera con el umask, la
    # ventana entre crearlo y escribir dentro seria de todos.
    directory_mode = _SECRET_DIR_MODE if mode == _SECRET_FILE_MODE else _PUBLIC_DIR_MODE
    temporary: Path | None = None
    try:
        path.parent.mkdir(mode=directory_mode, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".first-run"
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        temporary = None
    except PermissionError as exc:
        raise SecretPermissionsError(
            f"no se pudo escribir {path} con permisos {mode:04o}: {exc}"
        ) from exc
    except OSError as exc:
        # Disco lleno, sistema de ficheros de solo lectura, cuota agotada:
        # el entorno no da para esto. Salida 6 queda para lo que de verdad
        # es un permiso, que es cuando hay un secreto expuesto en juego.
        raise PreflightError(f"no se pudo escribir {path}: {exc}") from exc
    finally:
        # Un fallo a mitad no deja un temporal con secretos dentro.
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    """Sin bajar al disco el fichero Y la entrada de directorio, un corte
    de luz deja un secreto vacio o a medias, y la pasada siguiente lo
    leeria como «solo le faltan claves» (revision de seguridad B1)."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _enforce_mode(path: Path, mode: int) -> None:
    _reject_symlink(path)  # `chmod` sigue enlaces: le pondria 0600 a la victima.
    if (path.stat().st_mode & 0o777) == mode:
        return
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise SecretPermissionsError(
            f"{path} no tiene permisos {mode:04o} y no se pudieron corregir: {exc}"
        ) from exc


def _apply_env_file(root: Path, spec: FileSpec, *, dry_run: bool) -> FileOutcome:
    path = root / spec.relative
    _reject_symlink(path)
    created = not path.is_file()
    current = spec.header if created else _read_managed_text(path)
    merged, filled = _merge_env_text(current, spec.values)
    outcome = FileOutcome(
        relative=spec.relative, mode=spec.mode, created=created, filled_keys=filled
    )
    if dry_run:
        return outcome
    if created or filled:
        _write_file(path, merged, spec.mode)
    else:
        _enforce_mode(path, spec.mode)
    return outcome


def _apply_caps_file(root: Path, *, dry_run: bool) -> FileOutcome:
    """`config/caps.yaml` no se toca si existe: en un despliegue real lo
    posee root y este proceso no debe poder cambiarlo."""
    relative = "config/caps.yaml"
    path = root / relative
    _reject_symlink(path)
    if path.is_file():
        return FileOutcome(relative=relative, mode=_CAPS_FILE_MODE, created=False, filled_keys=None)
    if not dry_run:
        _write_file(path, _CAPS_TEMPLATE, _CAPS_FILE_MODE)
    return FileOutcome(relative=relative, mode=_CAPS_FILE_MODE, created=True, filled_keys=None)


# ── Material generado ───────────────────────────────────────────────────


def _random_base64() -> str:
    return base64.b64encode(os.urandom(_RANDOM_SECRET_BYTES)).decode("ascii")


def _random_url_safe() -> str:
    return secrets.token_urlsafe(_RANDOM_SECRET_BYTES)


def _approval_key_pair(existing: WorkspaceEnv) -> tuple[str, str]:
    """Nunca regenera un par que ya existe: el broker ya confia en esa
    publica. Con la privada presente y la publica perdida, la deriva."""
    seed = existing.kept("secrets/api.env", "ADS_APPROVAL_SIGNING_KEY")
    if seed is None:
        return generate_key_pair()
    try:
        return seed, public_key_for(seed)
    except ApprovalKeyMaterialError as exc:
        raise IncoherentConfigError(f"secrets/api.env, ADS_APPROVAL_SIGNING_KEY: {exc}") from exc


def _dotenv_values(existing: WorkspaceEnv, answers: Answers) -> dict[str, str]:
    user = existing.kept(".env", "POSTGRES_USER") or _DEFAULT_DB_USER
    database = existing.kept(".env", "POSTGRES_DB") or _DEFAULT_DB_NAME
    password = existing.kept(".env", "POSTGRES_PASSWORD") or _random_url_safe()
    return {
        "POSTGRES_USER": user,
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": database,
        "ADS_DATABASE_URL": (
            f"postgresql+asyncpg://{user}:{quote(password, safe='')}@ads-db:5432/{database}"
        ),
        "ADS_PUBLIC_BASE_URL": answers.public_base_url,
        "ADS_OWNER_EMAIL": answers.owner_email,
        "ADS_TZ": _DEFAULT_TZ,
        "ADS_ACTIVE_HOURS": _DEFAULT_ACTIVE_HOURS,
        "ADS_BROKER_SOCKET": _BROKER_SOCKET,
    }


def _static_token_path_is_open(existing: WorkspaceEnv) -> bool:
    """La via estatica nace apagada (`ApiSettings.mcp_static_token_enabled`
    = `False`), y solo un operador que la enciende a proposito -- en
    `secrets/api.env` o por entorno, como hace una capa de despliegue --
    necesita un bearer de dueño."""
    return (_configured(existing, _STATIC_TOKEN_SWITCH) or "").strip().lower() == "true"


def _api_env_values(existing: WorkspaceEnv, signing_key: str) -> dict[str, str]:
    values = {
        "ADS_APPROVAL_SIGNING_KEY": signing_key,
        "ADS_SESSION_SECRET": _random_base64(),
        "ADS_TOTP_ENC_KEY": _random_base64(),
        "ADS_SINGLE_OWNER_MODE": "true",
        "ADS_TRUSTED_PROXY_HOPS": "0",
        _STATIC_TOKEN_SWITCH: "false",
    }
    # `ADS_MCP_TOKEN` ya no se genera "por si acaso" (seguimiento aceptado
    # en la revision T022): con la via estatica apagada -- el caso normal --
    # el bearer de dueño nacia sin que nadie lo pidiera y se quedaba
    # DORMIDO en disco, eterno y sin rotar. Si el operador enciende el
    # interruptor, el bearer pasa a ser obligatorio (`ApiSettings` no
    # arranca sin el) y entonces si se genera aqui.
    if _static_token_path_is_open(existing):
        values["ADS_MCP_TOKEN"] = _random_url_safe()
    return values


def _broker_env_values(public_key: str, composio_api_key: str | None) -> dict[str, str]:
    values = {
        "ADS_BROKER_SOCKET": _BROKER_SOCKET,
        "ADS_APPROVAL_PUBLIC_KEY": public_key,
        "ADS_BROKER_ALLOWED_UIDS": _BROKER_ALLOWED_UIDS,
        "ADS_BROKER_HARD_CAPS_FILE": _BROKER_CAPS_FILE,
        "ADS_CREDENTIAL_MASTER_KEY": _random_base64(),
        "ADS_CREDENTIAL_STORE_DIR": _BROKER_CREDENTIAL_STORE_DIR,
    }
    if composio_api_key is not None:
        values["ADS_COMPOSIO_API_KEY"] = composio_api_key
    return values


def _file_specs(existing: WorkspaceEnv, answers: Answers) -> tuple[FileSpec, ...]:
    signing_key, public_key = _approval_key_pair(existing)
    return (
        FileSpec(".env", _SECRET_FILE_MODE, _DOTENV_HEADER, _dotenv_values(existing, answers)),
        FileSpec(
            "secrets/api.env",
            _SECRET_FILE_MODE,
            _API_ENV_HEADER,
            _api_env_values(existing, signing_key),
        ),
        FileSpec(
            "secrets/broker.env",
            _SECRET_FILE_MODE,
            _BROKER_ENV_HEADER,
            _broker_env_values(public_key, answers.composio_api_key),
        ),
    )


# ── Coherencia de lo ya escrito (salida 3) ──────────────────────────────


# Claves que este programa GENERA y que, una vez creadas, no se pueden
# volver a generar sin romper algo que ya existe: la contrasena con la que
# nacio la base, la firma que el broker verifica, el cifrado del material
# TOTP guardado, las sesiones vivas y el almacen cifrado de credenciales.
# `ADS_APPROVAL_PUBLIC_KEY` no esta: no se genera, se DERIVA de la semilla.
_CRITICAL_KEYS: Mapping[str, tuple[str, ...]] = {
    ".env": ("POSTGRES_PASSWORD",),
    "secrets/api.env": ("ADS_APPROVAL_SIGNING_KEY", "ADS_SESSION_SECRET", "ADS_TOTP_ENC_KEY"),
    "secrets/broker.env": ("ADS_CREDENTIAL_MASTER_KEY",),
}

# Las cuatro que este programa genera como material criptografico: 32 bytes
# en base64 o base64url, nunca menos de 32 caracteres. `POSTGRES_PASSWORD`
# queda fuera a proposito -- una instalacion heredada puede traer una
# contrasena de base de datos mas corta y no es este el sitio de discutirla.
_CRITICAL_CRYPTO_KEYS = frozenset(
    {
        "ADS_APPROVAL_SIGNING_KEY",
        "ADS_SESSION_SECRET",
        "ADS_TOTP_ENC_KEY",
        "ADS_CREDENTIAL_MASTER_KEY",
    }
)
_MIN_CRYPTO_VALUE_CHARS = 32


def _check_secrets_directory(root: Path) -> None:
    """`secrets/` nunca escribible por grupo u otros: quien pueda crear
    ficheros ahi puede plantar un enlace y decidir donde escribe este
    programa (revision de seguridad I1).

    Un `git clone` con `umask 002` lo deja en 0775, asi que se corrige en
    vez de rechazar el arranque; solo se falla si ni eso se puede. Un
    directorio que todavia no existe lo crea `_write_file` ya en 0700."""
    directory = root / "secrets"
    if not directory.is_dir():
        return
    mode = directory.stat().st_mode & 0o777
    if not mode & _GROUP_AND_OTHER_WRITE:
        return
    try:
        directory.chmod(_SECRET_DIR_MODE)
    except OSError as exc:
        raise SecretPermissionsError(
            f"{directory} tiene permisos {mode:04o} (escribible por grupo u otros) y no "
            f"se pudieron corregir: {exc}"
        ) from exc
    print(f"· secrets/ estaba en {mode:04o}: corregido a {_SECRET_DIR_MODE:04o}.")


def _check_coherence(existing: WorkspaceEnv) -> None:
    _check_critical_keys_present(existing)
    _check_database_url(existing)
    _check_public_base_url(existing)
    _check_approval_pair(existing)


def _check_critical_keys_present(existing: WorkspaceEnv) -> None:
    """Un fichero que YA existe y ha perdido una de sus claves criticas no
    se completa a ciegas (revision de seguridad B1: es lo que deja un corte
    de luz a mitad de escritura). Generar una nueva dejaria la base con una
    contrasena que nadie sabe, el TOTP guardado ilegible o al broker
    verificando firmas con una publica que ya no corresponde.

    «Perdida» es tambien vacia o truncada a la mitad: un `ADS_TOTP_ENC_KEY=`
    a secas, o seis caracteres donde hay 44, no son una clave.

    Un marcador `change-me` SI se rellena: es la plantilla del ejemplo
    diciendo «esto lo pones tu», no un fichero mutilado."""
    for relative, keys in _CRITICAL_KEYS.items():
        if not existing.exists(relative):
            continue
        unusable = _unusable_critical_keys(existing.files.get(relative, {}), keys)
        if unusable:
            raise IncoherentConfigError(
                f"{relative}: {', '.join(unusable)} ausente, vacía o demasiado corta; no "
                "genero una clave nueva a ciegas (borra el fichero para empezar de cero)"
            )


def _unusable_critical_keys(present: Mapping[str, str], keys: tuple[str, ...]) -> list[str]:
    unusable: list[str] = []
    for key in keys:
        value = (present.get(key) or "").strip()
        if not value:
            unusable.append(key)
        elif _is_placeholder(value):
            continue
        elif key in _CRITICAL_CRYPTO_KEYS and len(value) < _MIN_CRYPTO_VALUE_CHARS:
            unusable.append(key)
    return unusable


def _check_database_url(existing: WorkspaceEnv) -> None:
    url = existing.kept(".env", "ADS_DATABASE_URL")
    password = existing.kept(".env", "POSTGRES_PASSWORD")
    if url is None or password is None:
        return
    if unquote(urlsplit(url).password or "") != password:
        raise IncoherentConfigError(
            ".env, ADS_DATABASE_URL: la contraseña no es la de POSTGRES_PASSWORD"
        )


def _check_public_base_url(existing: WorkspaceEnv) -> None:
    url = existing.kept(".env", "ADS_PUBLIC_BASE_URL")
    if url is None:
        return
    try:
        _validate_public_base_url(url)
    except ValueError as exc:
        raise IncoherentConfigError(f".env, ADS_PUBLIC_BASE_URL: {exc}") from exc


def _check_approval_pair(existing: WorkspaceEnv) -> None:
    seed = existing.kept("secrets/api.env", "ADS_APPROVAL_SIGNING_KEY")
    public_key = existing.kept("secrets/broker.env", "ADS_APPROVAL_PUBLIC_KEY")
    if public_key is not None and seed is None:
        # `secrets/api.env` entero desaparecido con el broker ya confiando
        # en una publica: generar una semilla nueva dejaria toda aprobacion
        # firmada sin verificar (revision de seguridad B1).
        raise IncoherentConfigError(
            "secrets/broker.env tiene ADS_APPROVAL_PUBLIC_KEY pero secrets/api.env no tiene "
            "ADS_APPROVAL_SIGNING_KEY: recupera la semilla o borra los dos ficheros"
        )
    if seed is None or public_key is None:
        return
    try:
        expected = public_key_for(seed)
    except ApprovalKeyMaterialError as exc:
        raise IncoherentConfigError(f"secrets/api.env, ADS_APPROVAL_SIGNING_KEY: {exc}") from exc
    if expected != public_key:
        raise IncoherentConfigError(
            "secrets/broker.env, ADS_APPROVAL_PUBLIC_KEY: no es la pública de "
            "ADS_APPROVAL_SIGNING_KEY (secrets/api.env)"
        )


# ── Alta del dueno (paso 8) ─────────────────────────────────────────────

_SELECT_SOLE_OWNER_EMAIL_SQL = "SELECT email FROM owners ORDER BY created_at ASC LIMIT 1"


def _database_dsn(root: Path) -> str:
    url = _load_workspace_env(root).kept(".env", "ADS_DATABASE_URL")
    if url is None:
        raise IncoherentConfigError(
            ".env, ADS_DATABASE_URL: ausente; genera los ficheros con `make first-run`"
        )
    return to_asyncpg_dsn(url)


async def _fetch_sole_owner_email(dsn: str) -> str | None:
    connection = await asyncpg.connect(dsn)
    try:
        return await connection.fetchval(_SELECT_SOLE_OWNER_EMAIL_SQL)  # type: ignore[no-any-return]
    finally:
        await connection.close()


def _existing_owner_email(dsn: str) -> str | None:
    try:
        return asyncio.run(_fetch_sole_owner_email(dsn))
    except asyncpg.UndefinedTableError as exc:
        raise StackUnhealthyError(
            "la base no tiene la tabla owners: falta `docker compose run --rm ads-migrate`"
        ) from exc
    except (OSError, asyncpg.PostgresError) as exc:
        raise StackUnhealthyError(
            f"ads-db no responde ({exc}); revisa ADS_DATABASE_URL en .env"
        ) from exc


_FEDERATED_SWITCH = "ADS_FEDERATED_LOGIN_ENABLED"
_FEDERATED_CLIENT_ID = "ADS_GOOGLE_OIDC_CLIENT_ID"
_FEDERATED_CLIENT_SECRET = "ADS_GOOGLE_OIDC_CLIENT_SECRET"  # noqa: S105 -- nombre, no valor
_FEDERATED_ALLOWED_EMAILS = "ADS_FEDERATED_ALLOWED_EMAILS"
_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})


def _federated_login_configured(existing: WorkspaceEnv) -> bool:
    """Interruptor encendido MAS cliente OIDC completo (spec 002b). La
    lista de correos no entra en la condicion: es justamente lo que este
    paso escribe. Sin las tres piezas, la rama no existe -- nunca a
    medias: o se autoriza el correo, o se crea contrasena."""
    switch = (_configured(existing, _FEDERATED_SWITCH) or "").strip().lower()
    return (
        switch in _TRUE_VALUES
        and _configured(existing, _FEDERATED_CLIENT_ID) is not None
        and _configured(existing, _FEDERATED_CLIENT_SECRET) is not None
    )


def _parse_allowed_emails(raw: str | None) -> list[str]:
    """CSV o JSON, igual que `ApiSettings._parse_federated_allowed_emails`."""
    if raw is None or not raw.strip():
        return []
    text = raw.strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise IncoherentConfigError(
                f"secrets/api.env, {_FEDERATED_ALLOWED_EMAILS}: JSON invalido ({exc})"
            ) from exc
        if not isinstance(parsed, list):
            raise IncoherentConfigError(
                f"secrets/api.env, {_FEDERATED_ALLOWED_EMAILS}: debe ser una lista"
            )
        items = [str(item) for item in parsed]
    else:
        items = text.split(",")
    return [item.strip().lower() for item in items if item.strip()]


def _authorize_federated_owner(root: Path, existing: WorkspaceEnv, email: str) -> None:
    """Escribe el correo en la lista de autorizados; no crea contrasena.
    La primera entrada por Google da de alta al dueno (TOFU, 002b S1)."""
    path = root / "secrets/api.env"
    allowed = _parse_allowed_emails(_configured(existing, _FEDERATED_ALLOWED_EMAILS))
    if email in allowed:
        print(f"· {email} ya estaba autorizado para «Entrar con Google»: no se toca.")
        return
    current = _read_managed_text(path) or _API_ENV_HEADER
    value = ",".join([*allowed, email])
    updated = _upsert_env_text(current, _FEDERATED_ALLOWED_EMAILS, value)
    _write_file(path, updated, _SECRET_FILE_MODE)
    print(f"  ✔ {email} autorizado en secrets/api.env ({_FEDERATED_ALLOWED_EMAILS}).")
    print("· reinicia ads-api para que lea la lista: docker compose restart ads-api")


def _validate_password(raw: str) -> str:
    if not _MIN_PASSWORD_CHARS <= len(raw) <= _MAX_PASSWORD_CHARS:
        raise ValueError(
            f"la contraseña debe tener entre {_MIN_PASSWORD_CHARS} y "
            f"{_MAX_PASSWORD_CHARS} caracteres"
        )
    return raw


def _ask_password(prompter: Prompter) -> str:
    """Una contraseña corta o mal repetida se vuelve a pedir: matar el
    arranque por un dedazo obligaria a repetir todo el comando."""
    for _attempt in range(_MAX_PROMPT_ATTEMPTS):
        try:
            password = _validate_password(prompter.secret("Contraseña del dueño: "))
        except ValueError as exc:
            print(f"  ✘ {exc}", file=sys.stderr)
            continue
        if password == prompter.secret("Repite la contraseña: "):
            return password
        print("  ✘ las dos contraseñas no coinciden", file=sys.stderr)
    raise OwnerBootstrapError(f"contraseña del dueño no fijada en {_MAX_PROMPT_ATTEMPTS} intentos")


def _owner_password(args: argparse.Namespace, prompter: Prompter) -> str:
    if args.password_stdin:
        return _validate_password(_read_stdin_secret("--password-stdin"))
    if not sys.stdin.isatty():
        raise OwnerBootstrapError(
            "sin terminal para pedir la contraseña del dueño: usa --password-stdin"
        )
    return _ask_password(prompter)


def _register_owner(
    root: Path, args: argparse.Namespace, answers: Answers, prompter: Prompter
) -> str:
    """Devuelve la linea «Entra con» del resumen. Exactamente una via:
    federada o contrasena, nunca las dos en la misma pasada."""
    dsn = _database_dsn(root)
    already = _existing_owner_email(dsn)
    if already is not None:
        print(f"· este despliegue ya tiene dueño ({already}): no se toca.")
        return already
    existing = _load_workspace_env(root)
    if _federated_login_configured(existing):
        _authorize_federated_owner(root, existing, answers.owner_email)
        return f"{answers.owner_email} · «Entrar con Google» (la primera entrada te da de alta)"
    password = _owner_password(args, prompter)
    try:
        asyncio.run(upsert_owner_password(dsn=dsn, email=answers.owner_email, password=password))
    except (OSError, asyncpg.PostgresError) as exc:
        raise OwnerBootstrapError(
            f"no se pudo dar de alta a {answers.owner_email} en ads-db: {exc}"
        ) from exc
    print(f"  ✔ dueño dado de alta: {answers.owner_email}")
    return answers.owner_email


# ── Verificacion (paso 9) ───────────────────────────────────────────────


@dataclass(frozen=True)
class _Candidate:
    """Donde preguntar por la salud y con que `Host`.

    El candidato publico no fuerza cabecera: es el unico que prueba el
    camino entero. Los internos declaran el host PUBLICO, no `127.0.0.1`
    (revision de seguridad I2): el guardia anti DNS-rebinding acepta
    siempre la IP de loopback, asi que con ella el sondeo no podia
    detectar un `ADS_PUBLIC_BASE_URL` equivocado y aun asi culpaba a esa
    variable. Con el host publico, un 403 significa de verdad que `.env`
    y el `ads-api` que corre no dicen lo mismo."""

    base_url: str
    host_header: str | None


def _health_candidates(public_base_url: str) -> tuple[_Candidate, ...]:
    host = urlsplit(public_base_url).netloc
    return (
        _Candidate(public_base_url, None),
        _Candidate("http://ads-api:8410", host),
        _Candidate("http://127.0.0.1:8410", host),
    )


def _probe(client: httpx.Client, candidate: _Candidate) -> bool:
    """`True` si esta pila responde sana; `False` si todavia no contesta.

    Un 5xx es «todavia no»: con Caddy o nginx delante, el proxy contesta
    502/503 mientras `ads-api` calienta. Solo una respuesta LOGICAMENTE
    equivocada (un `/mcp` que no pide autorizacion, un health que dice
    4xx) aborta antes del plazo."""
    headers = {} if candidate.host_header is None else {"Host": candidate.host_header}
    try:
        health = client.get(f"{candidate.base_url}/api/v1/health", headers=headers)
        unauthorized = client.post(f"{candidate.base_url}/mcp", headers=headers)
    except httpx.HTTPError:
        return False
    if _is_still_warming_up(health) or _is_still_warming_up(unauthorized):
        return False
    if health.status_code != httpx.codes.OK:
        raise StackUnhealthyError(
            f"ads-api responde {health.status_code} en /api/v1/health; revisa "
            "docker compose logs ads-api"
        )
    if unauthorized.status_code != httpx.codes.UNAUTHORIZED:
        raise StackUnhealthyError(
            f"POST /mcp responde {unauthorized.status_code} en vez de 401; revisa "
            "ADS_PUBLIC_BASE_URL en .env"
        )
    return True


def _is_still_warming_up(response: httpx.Response) -> bool:
    return response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR


def _report_healthy(candidate: _Candidate, public_base_url: str) -> None:
    if candidate.host_header is None:
        print(f"  ✔ pila sana en {public_base_url} · /api/v1/health 200 · POST /mcp 401")
        return
    print(f"  ✔ ads-api sana en {candidate.base_url} · /api/v1/health 200 · POST /mcp 401")
    print(
        "· la URL pública no contestó desde dentro del contenedor — normal con un proxy "
        f"delante; compruébala desde fuera: curl -fsS {public_base_url}/api/v1/health"
    )


def _verify_stack(
    public_base_url: str, *, deadline_seconds: float = _HEALTH_DEADLINE_SECONDS
) -> None:
    deadline = time.monotonic() + deadline_seconds
    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
        while True:
            for candidate in _health_candidates(public_base_url):
                if _probe(client, candidate):
                    _report_healthy(candidate, public_base_url)
                    return
            if time.monotonic() >= deadline:
                raise StackUnhealthyError(
                    "ads-api no llegó a responder; revisa docker compose logs ads-api y "
                    "ADS_PUBLIC_BASE_URL en .env"
                )
            time.sleep(_HEALTH_POLL_SECONDS)


# ── Salida (paso 10) ────────────────────────────────────────────────────


def _applied_line(outcome: FileOutcome) -> str:
    if outcome.created:
        return f"  ✔ {outcome.relative} creado ({outcome.mode:04o})."
    if outcome.filled_keys is None:
        return f"· {outcome.relative} ya existía: nada que completar."
    if not outcome.filled_keys:
        return f"· {outcome.relative} ya existía: respetado, nada que añadir."
    return f"· {outcome.relative} ya existía: añadidas {', '.join(outcome.filled_keys)}."


def _planned_line(outcome: FileOutcome) -> str:
    if outcome.created:
        return f"  · {outcome.relative} se crearía ({outcome.mode:04o})."
    if not outcome.filled_keys:
        return f"  · {outcome.relative} ya existía: no se tocaría."
    return f"  · {outcome.relative} ya existía: se añadirían {', '.join(outcome.filled_keys)}."


def _report_files(outcomes: Iterable[FileOutcome], render: Callable[[FileOutcome], str]) -> None:
    for outcome in outcomes:
        print(render(outcome))


def _print_summary(answers: Answers, entry: str) -> None:
    """El panel se cita con el esquema REAL de la instancia: en un
    desarrollo local (`http://127.0.0.1:8410`) un `https://` inventado
    manda al operador a una puerta que no existe."""
    agente = f"./scripts/instalar-mcp.sh --url {answers.public_base_url}/mcp"
    print(
        f"""
Instancia lista.
  Panel:     {answers.public_base_url}/
  Entra con: {entry}
  Agente:    {agente} --nombre {_AGENT_SERVER_NAME}
  Topes:     ninguna cuenta puede escribir todavía — fija topes en Ajustes → Topes.
  Proxy:     si pones Caddy, nginx o tailscale delante, fija ADS_TRUSTED_PROXY_HOPS=1."""
    )


# ── CLI ─────────────────────────────────────────────────────────────────


class _Parser(argparse.ArgumentParser):
    """`argparse` sale con 2 ante un uso incorrecto; el contrato dice 1."""

    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = _Parser(prog="first_run", description="Primer arranque de un comando.")
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--public-base-url")
    parser.add_argument("--owner-email")
    parser.add_argument("--composio-api-key-stdin", action="store_true")
    parser.add_argument("--no-composio", action="store_true")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--skip-start", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.composio_api_key_stdin and args.no_composio:
        raise UsageError("--composio-api-key-stdin y --no-composio se excluyen")
    if args.composio_api_key_stdin and args.password_stdin:
        raise UsageError("--composio-api-key-stdin y --password-stdin no caben en la misma stdin")
    return args


def _workspace(args: argparse.Namespace) -> Path:
    root = Path(args.workspace).resolve()
    if not root.is_dir():
        raise PreflightError(f"--workspace no existe: {root} (¿falta el montaje del repo?)")
    return root


def _warn_about_a_new_credential_master_key(existing: WorkspaceEnv) -> None:
    """`secrets/broker.env` entero desaparecido en una instalacion que ya
    existia: la clave maestra no esta en ninguna otra parte, asi que se
    genera una nueva -- el almacen cifrado ya era ilegible sin ella -- pero
    no en silencio. En una instalacion limpia no hay nada que avisar."""
    if existing.exists("secrets/broker.env"):
        return
    if not (existing.exists(".env") or existing.exists("secrets/api.env")):
        return
    print(
        "· secrets/broker.env no está: ADS_CREDENTIAL_MASTER_KEY nueva; si ya conectaste "
        "cuentas, el almacén cifrado quedará ilegible — recupéralo del respaldo."
    )


def _run(args: argparse.Namespace, prompter: Prompter) -> None:
    root = _workspace(args)
    if not args.dry_run:
        _check_secrets_directory(root)
    existing = _load_workspace_env(root)
    _check_coherence(existing)
    _warn_about_a_new_credential_master_key(existing)
    answers = _resolve_answers(args, existing, prompter)
    specs = _file_specs(existing, answers)
    outcomes = [_apply_env_file(root, spec, dry_run=args.dry_run) for spec in specs]
    outcomes.append(_apply_caps_file(root, dry_run=args.dry_run))
    _report_files(outcomes, _planned_line if args.dry_run else _applied_line)
    if args.dry_run:
        print("· --dry-run: nada escrito, nada arrancado.")
        return
    if args.skip_start:
        print("· pila sin arrancar (--skip-start): arráncala con `make up`.")
        return
    entry = _register_owner(root, args, answers, prompter)
    _verify_stack(answers.public_base_url)
    _print_summary(answers, entry)


def main(argv: list[str] | None = None, *, prompter: Prompter | None = None) -> int:
    try:
        args = _parse_args(argv)
        _run(args, prompter or (silent_prompter() if args.dry_run else terminal_prompter()))
    except FirstRunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
