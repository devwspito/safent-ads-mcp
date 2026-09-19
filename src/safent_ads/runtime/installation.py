"""One guided MCP + runtime install, with encrypted owner pairing and user services.

No runtime starts without --background or an explicit run command. macOS and
Linux user services are supported; other systems fail before changing configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import platform
import plistlib
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from safent_ads.runtime.bridge import resolve_executable, serve
from safent_ads.runtime.pairing import KEY_BITS, PairingRequest, oaep
from safent_ads.runtime.store import digest
from safent_ads.shared.net.safe_egress import build_pinned_async_client

PAIR_TIMEOUT = 600
PROFILE_MODE = 0o600
DIRECTORY_MODE = 0o700


def validated_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Usa el origen HTTPS público del panel, sin ruta ni credenciales.")
    return value.rstrip("/")


def profile_path(origin: str, runtime: str) -> Path:
    identifier = hashlib.sha256((origin + ":" + runtime).encode()).hexdigest()[:20]
    return Path.home() / ".local" / "state" / "safent-runtime" / identifier / "profile.json"


def private_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("No se admiten enlaces simbólicos para credenciales o servicios.")
    descriptor, temporary = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, PROFILE_MODE)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_profile(path: Path) -> dict[str, Any]:
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("El perfil debe ser un archivo privado con permisos 0600.")
    data: dict[str, Any] = json.loads(path.read_text())
    validated_origin(data["url"])
    if data["runtime"] not in {"codex", "claude"} or not isinstance(data["token"], str):
        raise ValueError("Perfil de conector inválido.")
    return data


def command(
    arguments: list[str], *, check: bool = True, interactive: bool = False
) -> subprocess.CompletedProcess[str]:
    # All commands are fixed CLI operations, never shell strings or remote input.
    result = subprocess.run(  # noqa: S603
        arguments,
        capture_output=not interactive,
        text=True,
        timeout=600 if interactive else 60,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"Falló {Path(arguments[0]).name} {arguments[1]}; revisa la instalación."
        )
    return result


def register_mcp(executable: str, runtime: str, name: str, origin: str) -> None:
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,79}", name):
        raise ValueError("Nombre MCP inválido.")
    target = origin + "/mcp"
    get = [executable, "mcp", "get", name]
    if runtime == "codex":
        get.append("--json")
    existing = command(get, check=False)
    if existing.returncode == 0:
        if runtime == "codex":
            same = json.loads(existing.stdout).get("transport", {}).get("url") == target
        else:
            same = any(line.strip() == "URL: " + target for line in existing.stdout.splitlines())
        if not same:
            raise ValueError("Ya existe ese nombre MCP con otra configuración; no se sobrescribe.")
        return
    args = (
        [executable, "mcp", "add", name, "--url", target]
        if runtime == "codex"
        else [executable, "mcp", "add", "--scope", "user", "--transport", "http", name, target]
    )
    command(args, interactive=True)


async def pair(origin: str, runtime: Literal["codex", "claude"], label: str) -> dict[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
    encoded = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).decode()
    verifier = secrets.token_hex(32)
    request = PairingRequest(
        challenge=digest(verifier), public_key=encoded, runtime=runtime, label=label
    )
    url = origin + "/runtime/vincular?" + urlencode(request.model_dump())
    print(f"Autoriza sólo si el código del panel coincide: {request.fingerprint()}", flush=True)
    print("Se abrirá el panel. No se enviará ninguna clave a la URL.", flush=True)
    if not webbrowser.open(url):
        print("Abre este enlace de autorización (no contiene secretos):\n" + url, flush=True)
    deadline = time.monotonic() + PAIR_TIMEOUT
    async with build_pinned_async_client(
        allowed_hosts=frozenset({urlsplit(origin).hostname or ""}), timeout=30, trust_env=False
    ) as client:
        while time.monotonic() < deadline:
            response = await client.post(
                origin + "/runtime/v1/pair/poll", json={"verifier": verifier}
            )
            response.raise_for_status()
            result = response.json()
            if result.get("ready"):
                if result.get("runtime") != runtime:
                    raise ValueError("El runtime autorizado no coincide.")
                token = key.decrypt(base64.b64decode(result["sealed_token"], validate=True), oaep())
                return {
                    "token": token.decode(),
                    "connection_id": result["connection_id"],
                    "business_id": result["business_id"],
                }
            await asyncio.sleep(3)
    raise TimeoutError("Vinculación no autorizada en diez minutos; vuelve a instalar.")


async def ping(data: dict[str, Any]) -> bool:
    async with build_pinned_async_client(
        allowed_hosts=frozenset({urlsplit(data["url"]).hostname or ""}), timeout=30, trust_env=False
    ) as client:
        response = await client.post(
            data["url"] + "/runtime/v1/ping", headers={"Authorization": "Bearer " + data["token"]}
        )
        if response.status_code in {401, 403}:
            return False
        response.raise_for_status()
        body = response.json()
        return bool(
            body["connection_id"] == data["connection_id"]
            and body["business_id"] == data["business_id"]
        )


def service_spec(path: Path, system: str) -> tuple[str, Path, bytes]:
    identifier = "center.safent.runtime." + path.parent.name
    args = [sys.executable, "-m", "safent_ads.runtime.installation", "run", "--profile", str(path)]
    if system == "Darwin":
        destination = Path.home() / "Library" / "LaunchAgents" / (identifier + ".plist")
        payload = {
            "Label": identifier,
            "ProgramArguments": args,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 60,
            "WorkingDirectory": str(path.parent),
            "StandardOutPath": str(path.parent / "service.log"),
            "StandardErrorPath": str(path.parent / "service-error.log"),
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        }
        return identifier, destination, plistlib.dumps(payload)
    if system == "Linux":
        destination = Path.home() / ".config" / "systemd" / "user" / (identifier + ".service")
        # systemd quoting is not shell quoting; escape percent specifiers explicitly.
        quoted = [json.dumps(arg.replace("%", "%%")) for arg in args]
        body = (
            "[Unit]\nDescription=Safent runtime preparation connector\n"
            "[Service]\nExecStart=" + " ".join(quoted) + "\nRestart=on-failure\nRestartSec=60\n"
            "UMask=0077\n[Install]\nWantedBy=default.target\n"
        )
        return identifier, destination, body.encode()
    raise ValueError("Servicio automático disponible en macOS y Linux; no se instaló nada.")


def service(path: Path, action: str) -> None:
    system = platform.system()
    identifier, destination, content = service_spec(path, system)
    if action == "start":
        private_write(destination, content)
    if system == "Darwin":
        launchctl = shutil.which("launchctl") or "/bin/launchctl"
        domain = f"gui/{os.getuid()}"
        command([launchctl, "bootout", f"{domain}/{identifier}"], check=False)
        if action == "start":
            command([launchctl, "bootstrap", domain, str(destination)])
    else:
        systemctl = shutil.which("systemctl")
        if not systemctl:
            raise ValueError("Falta systemd de usuario; utiliza el comando run en primer plano.")
        if action == "start":
            command([systemctl, "--user", "daemon-reload"])
            command([systemctl, "--user", "enable", "--now", destination.name])
            command([systemctl, "--user", "restart", destination.name])
        else:
            command([systemctl, "--user", "disable", "--now", destination.name])
    if action == "stop" and destination.exists():
        destination.unlink()  # only this exact generated service; profile remains for reconnection


async def install(args: argparse.Namespace) -> None:
    origin = validated_origin(args.url)
    path = profile_path(origin, args.runtime)
    if args.background:
        service_spec(path, platform.system())  # platform preflight before any mutation
    executable = resolve_executable(args)
    register_mcp(executable, args.runtime, args.name, origin)
    data = read_profile(path) if path.exists() else None
    if data is None or not await ping(data):
        data = {
            "url": origin,
            "runtime": args.runtime,
            "executable": executable,
            **await pair(origin, args.runtime, args.runtime + " · " + platform.node()[:60]),
        }
        private_write(path, json.dumps(data).encode())
    if not await ping(data):
        raise RuntimeError("La vinculación no pudo verificarse; no se inicia el conector.")
    if args.background:
        service(path, "start")
    print(f"MCP registrado y runtime vinculado. Perfil privado: {path}", flush=True)
    print("El MCP del chat mantiene su propio OAuth: si solicita iniciar sesión, autorízalo.")
    print(
        "Proceso automático iniciado; usa stop para detenerlo."
        if args.background
        else "Sin proceso automático. Usa run --profile con esa ruta para recibir encargos."
    )


async def run_profile(path: Path, once: bool = False) -> None:
    import fcntl  # noqa: PLC0415 - POSIX user-service platforms only

    data = read_profile(path)
    with (path.parent / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Ya hay un conector para este perfil.", flush=True)
            return
        os.environ["SAFENT_RUNTIME_TOKEN"] = data["token"]
        current = asyncio.current_task()
        if current:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, current.cancel)
        try:
            await serve(
                argparse.Namespace(
                    url=data["url"],
                    runtime=data["runtime"],
                    executable=data["executable"],
                    once=once,
                    timeout=600,
                )
            )
        except SystemExit:
            # Auth expiry/revocation is terminal; do not create a launchd restart loop.
            print(
                "Conector detenido: revisa el acceso en el panel y vuelve a vincular.", flush=True
            )
        finally:
            os.environ.pop("SAFENT_RUNTIME_TOKEN", None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    setup = sub.add_parser("install")
    setup.add_argument("--url", required=True)
    setup.add_argument("--runtime", required=True, choices=("codex", "claude"))
    setup.add_argument("--name", default="safent-ads")
    setup.add_argument("--executable")
    setup.add_argument(
        "--background",
        action="store_true",
        help="Autoriza el servicio de usuario y el consumo de cuota del runtime.",
    )
    for name in ("run", "stop", "status"):
        operation = sub.add_parser(name)
        operation.add_argument("--profile", type=Path, required=True)
        if name == "run":
            operation.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "install":
            asyncio.run(install(args))
        elif args.action == "run":
            asyncio.run(run_profile(args.profile, args.once))
        elif args.action == "stop":
            service(args.profile, "stop")
            print(
                "Servicio detenido y retirado. El perfil se conserva; revoca el acceso en el panel."
            )
        else:
            active = asyncio.run(ping(read_profile(args.profile)))
            print(
                "Vinculación válida (no certifica proceso activo)."
                if active
                else "Requiere vinculación."
            )
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Conector detenido.")
    except (
        ValueError,
        KeyError,
        OSError,
        httpx.HTTPError,
        RuntimeError,
        TimeoutError,
        subprocess.TimeoutExpired,
    ):
        # Never include response bodies, argv output or profile contents in diagnostics.
        print(
            "No se completó la operación. Comprueba sesión, permisos y conexión del equipo.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
