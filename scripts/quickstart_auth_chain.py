#!/usr/bin/env python3
"""Quickstart — cadena de autenticacion 004 (Enterprise emite, companion resuelve).

Prueba, local y offline, TODO el camino real de un puesto de anuncios:

    Enterprise (venv propio)              companion (este venv)
    -------------------------             -------------------------
    crea org/persona/negocio     -->
    POST /api/ads/seats          -->      (nada; API real de Enterprise)
    POST .../credentials         -->
    sirve HTTPS real 127.0.0.1   <==>      EnterpriseSeatAuthority (httpx real)
                                            EnterpriseSeatCallerScopeResolver

No hay mocks de HTTP ni del resolver: Enterprise corre como servidor HTTPS de
verdad (certificado autofirmado efimero, solo loopback) en un subproceso con
SU PROPIO venv (`PYTHONPATH=src <enterprise>/.venv/bin/python`), y este script
corre con el venv del companion (`uv run --frozen python`) y llama a las
clases reales de produccion (`EnterpriseSeatTrust`, `EnterpriseSeatAuthority`,
`EnterpriseSeatCallerScopeResolver`) contra ese servidor. El unico seam de
prueba usado es el parametro `transport=` que `EnterpriseSeatAuthority` ya
expone para poder fijar la CA del certificado efimero — nunca se toca
`src/` de ningun repo.

Uso:
    cd <companion>
    uv run --frozen python scripts/quickstart_auth_chain.py

Variables de entorno opcionales:
    ADS_QUICKSTART_ENTERPRISE_REPO   ruta al worktree de Enterprise
                                      (por defecto: ../enterprise)
    ADS_QUICKSTART_KEEP_TMP=1        no borra el directorio temporal al salir
                                      (certificado, clave, sqlite de Enterprise)
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which
from uuid import UUID

import httpx

COMPANION_REPO = Path(__file__).resolve().parents[1]
# El worktree de Enterprise esta fuera de este repo y su ruta cambia en
# cada maquina: por defecto, el hermano `../enterprise`. Nunca la ruta
# de nadie escrita a mano -- este arbol se publica.
DEFAULT_ENTERPRISE_REPO = COMPANION_REPO.parent / "enterprise"
ENTERPRISE_REPO = Path(
    os.environ.get("ADS_QUICKSTART_ENTERPRISE_REPO", str(DEFAULT_ENTERPRISE_REPO))
)
READY_TIMEOUT_SECONDS = 45
KEEP_TMP = os.environ.get("ADS_QUICKSTART_KEEP_TMP", "") == "1"

sys.path.insert(0, str(COMPANION_REPO))

from tests.unit.mcp.presentation.test_catalog_registries_by_permission import (  # noqa: E402
    _full_registry,
)

from safent_ads.iam.infrastructure.enterprise_seat_authority import (  # noqa: E402
    EnterpriseSeatAuthority,
    EnterpriseSeatTrust,
)
from safent_ads.mcp.application.caller_scope import Permission  # noqa: E402
from safent_ads.mcp.application.seat_authority import (  # noqa: E402
    SeatAuthorityDeniedError,
)
from safent_ads.mcp.infrastructure.enterprise_seat_caller_scope_resolver import (  # noqa: E402
    EnterpriseSeatCallerScopeResolver,
)
from safent_ads.mcp.presentation.catalog import registries_by_permission  # noqa: E402

# ── informe ───────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    results: list[CheckResult] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append(CheckResult(name, ok, detail))
        status = "PASS" if ok else "FAIL"
        line = f"[{status}] {name}"
        if detail:
            line += f" — {detail}"
        print(line, flush=True)

    @property
    def all_ok(self) -> bool:
        return all(r.ok for r in self.results)


REPORT = Report()


async def _expect_denied(name: str, coro, *, expected=SeatAuthorityDeniedError) -> None:
    try:
        scope = await coro
    except expected as exc:
        REPORT.check(name, True, f"denegado ({type(exc).__name__}: {exc})")
    except Exception as exc:  # noqa: BLE001 — cualquier otra excepcion es un fallo del check
        REPORT.check(name, False, f"excepcion inesperada {type(exc).__name__}: {exc}")
    else:
        REPORT.check(name, False, f"NO denegado — devolvio {scope!r}")


# ── infraestructura local efimera (certificado, puerto, subproceso) ───────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _generate_self_signed_cert(tmp_dir: Path) -> tuple[Path, Path]:
    certfile, keyfile = tmp_dir / "cert.pem", tmp_dir / "key.pem"
    openssl = which("openssl")
    if openssl is None:
        raise RuntimeError("openssl no esta en el PATH (falta para el certificado efimero)")
    subprocess.run(  # noqa: S603 — argumentos estaticos, sin entrada externa
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1",
            "-nodes", "-keyout", str(keyfile), "-out", str(certfile),
            "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
            "-addext", "basicConstraints=critical,CA:TRUE",
        ],
        check=True, capture_output=True,
    )
    return certfile, keyfile


# Bootstrap que corre DENTRO del venv de Enterprise (PYTHONPATH=src). Usa
# unicamente lo que ya usan sus propios tests (test_ads_seats_api.py,
# test_cerebro_security_hardening.py): ProvisioningService para org/negocio,
# repo.save_user/save_membership para las personas (Enterprise no tiene alta
# HTTP de personas — igual que sus fixtures), y las rutas HTTP REALES
# (/api/mfa/*, /api/ads/seats*) para el puesto y la credencial, que es lo que
# el contrato exige probar de verdad.
_ENTERPRISE_BOOTSTRAP_SOURCE = textwrap.dedent(
    '''
    from __future__ import annotations

    import json
    import os
    import time
    import uuid

    os.environ.setdefault("ENVIRONMENT", "dev")

    from fastapi.testclient import TestClient

    from safent_control.api import deps
    from safent_control.api.app import create_app
    from safent_control.application.auth_service import SESSION_COOKIE, AuthService
    from safent_control.application.provisioning import ProvisioningService
    from safent_control.domain import totp
    from safent_control.domain.entities import Membership, MembershipRole, User
    from safent_control.infrastructure.config import get_settings
    from safent_control.infrastructure.mfa_crypto import encrypt_mfa_secret


    def _code_for(secret: str, step_offset: int = 0) -> str:
        counter = int(time.time()) // 30 + step_offset
        return totp._hotp(secret=secret, counter=counter)


    def main() -> None:
        deps.reset_caches()
        app = create_app()
        client = TestClient(app, raise_server_exceptions=True)

        repo = deps.get_repo()
        keystore = deps.get_keystore()
        provisioning = ProvisioningService(repo=repo, keystore=keystore)
        auth = AuthService(repo=repo)

        org = provisioning.create_org(name="Acme", seat_limit=5)
        os.environ["SAFENT_ADS_SERVICE_ORGS"] = org.org_id
        get_settings.cache_clear()

        def make_member(email: str, role: MembershipRole) -> User:
            user = User(str(uuid.uuid4()), email, email.split("@")[0])
            repo.save_user(user)
            repo.save_membership(Membership(str(uuid.uuid4()), user.user_id, org.org_id, role))
            return user

        owner = make_member("owner@acme.quickstart", MembershipRole.OWNER)
        person_view = make_member("ver@acme.quickstart", MembershipRole.VIEWER)
        person_propose = make_member("proponer@acme.quickstart", MembershipRole.VIEWER)
        person_revoke = make_member("revocado@acme.quickstart", MembershipRole.VIEWER)

        owner_session = auth.upsert_user_and_session(email=owner.email, name=owner.name)
        repo.update_session_org(owner_session.session_id, org.org_id)

        business_id = str(uuid.uuid4())
        repo._ads_sql(
            "INSERT INTO ads_business_owner(business_id,org_id) VALUES(?,?)",
            (business_id, org.org_id),
        )

        headers = {"X-Safent-Org": org.org_id}
        cookies = {SESSION_COOKIE: owner_session.session_id}

        enroll = client.post("/api/mfa/enroll", cookies=cookies, headers=headers)
        assert enroll.status_code == 200, enroll.text
        secret = enroll.json()["secret"]
        secret_enc = encrypt_mfa_secret(secret)
        verify = client.post(
            "/api/mfa/verify", json={"code": _code_for(secret)}, cookies=cookies, headers=headers,
        )
        assert verify.status_code == 200, verify.text

        def next_code() -> str:
            # RFC 6238 anti-replay (mfa_service.verify_step_up) solo acepta un
            # contador estrictamente mayor que mfa_last_counter, dentro de una
            # ventana de +-1 paso del reloj REAL -- dos verificaciones en el
            # mismo bloque de 30s no pueden validar ambas sin esperar al
            # reloj. Reiniciar el contador antes de cada llamada (dato propio
            # de este seed, no toca src/) mantiene el script determinista y
            # sin sleep().
            repo.update_user_mfa(
                owner.user_id, mfa_secret_enc=secret_enc, mfa_enabled=True, mfa_last_counter=0,
            )
            return _code_for(secret)

        def create_seat(user_id: str, permission: str) -> dict:
            body = {
                "operation_id": str(uuid.uuid4()), "user_id": user_id,
                "business_id": business_id, "permission": permission, "totp": next_code(),
            }
            r = client.post("/api/ads/seats", json=body, cookies=cookies, headers=headers)
            assert r.status_code == 201, r.text
            return r.json()

        view_result = create_seat(person_view.user_id, "view")
        propose_result = create_seat(person_propose.user_id, "propose")
        revoke_result = create_seat(person_revoke.user_id, "view")

        def revoke_seat(seat_payload: dict) -> dict:
            body = {
                "operation_id": str(uuid.uuid4()),
                "expected_revision": seat_payload["seat"]["revision"],
                "totp": next_code(),
            }
            r = client.post(
                f"/api/ads/seats/{seat_payload['seat']['seat_id']}/revoke",
                json=body, cookies=cookies, headers=headers,
            )
            assert r.status_code == 200, r.text
            return r.json()

        revoke_seat(revoke_result)

        # Credencial de la MISMA persona pero scope="mcp" (la que usa el
        # bridge del Cerebro), nunca "ads" -- ProvisioningService.
        # mint_service_account_token es el primitivo generico que
        # AdsSeats.issue_credential ya reutiliza para scope="ads"
        # (application/ads_seats.py::_mint_credential); mintarlo aqui
        # directamente con otro scope es sembrar un dato adversarial, no un
        # atajo alrededor de la API de puestos.
        mcp_raw, _mcp_expiry = provisioning.mint_service_account_token(
            user_id=person_view.user_id, org_id=org.org_id, scope="mcp", ttl_seconds=900,
        )
        mcp_scope_credential = "sfa_" + mcp_raw

        pre_seats = client.get("/api/ads/seats", cookies=cookies, headers=headers).json()["seats"]
        pre_last_seen = {s["seat_id"]: s["last_seen_at"] for s in pre_seats}

        payload = {
            "org_id": org.org_id,
            "business_id": business_id,
            "owner_session_cookie": owner_session.session_id,
            "seats": {
                "view": {
                    "seat_id": view_result["seat"]["seat_id"], "user_id": person_view.user_id,
                    "credential": view_result["credential"]["credential"],
                    "person_label": person_view.name,
                },
                "propose": {
                    "seat_id": propose_result["seat"]["seat_id"], "user_id": person_propose.user_id,
                    "credential": propose_result["credential"]["credential"],
                    "person_label": person_propose.name,
                },
                "revoked": {
                    "seat_id": revoke_result["seat"]["seat_id"], "user_id": person_revoke.user_id,
                    "credential": revoke_result["credential"]["credential"],
                },
            },
            "mcp_scope_credential": mcp_scope_credential,
            "pre_last_seen_at": pre_last_seen,
        }
        print("READY " + json.dumps(payload), flush=True)

        import uvicorn
        uvicorn.run(
            app, host="127.0.0.1", port=int(os.environ["ADS_QUICKSTART_PORT"]),
            ssl_certfile=os.environ["ADS_QUICKSTART_CERTFILE"],
            ssl_keyfile=os.environ["ADS_QUICKSTART_KEYFILE"], log_level="warning",
        )


    if __name__ == "__main__":
        main()
    '''
).strip("\n")


class EnterpriseProcess:
    """Lanza Enterprise en SU venv, real HTTPS en loopback, y expone la
    linea `READY <json>` que imprime tras sembrar org/personas/negocio y
    emitir los puestos/credenciales por la API real."""

    def __init__(
        self, tmp_dir: Path, port: int, *, certfile: Path, keyfile: Path, service_secret: str,
    ):
        self.tmp_dir = tmp_dir
        self.port = port
        self.certfile = certfile
        self.keyfile = keyfile
        self.service_secret = service_secret
        self.log_path = tmp_dir / "enterprise.log"
        self.data_dir = tmp_dir / "enterprise-data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.bootstrap_path = tmp_dir / "enterprise_bootstrap.py"
        self.bootstrap_path.write_text(_ENTERPRISE_BOOTSTRAP_SOURCE)
        self.proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str] = queue.Queue()
        self._log_file = None

    def start(self) -> dict:
        enterprise_python = self.ENTERPRISE_PYTHON
        env = {
            **os.environ,
            "PYTHONPATH": str(ENTERPRISE_REPO / "src"),
            "SAFENT_CONTROL_DATA_DIR": str(self.data_dir),
            "ENVIRONMENT": "dev",
            "SAFENT_ADS_SERVICE_SECRET": self.service_secret,
            "ADS_QUICKSTART_PORT": str(self.port),
            "ADS_QUICKSTART_CERTFILE": str(self.certfile),
            "ADS_QUICKSTART_KEYFILE": str(self.keyfile),
        }
        self._log_file = open(self.log_path, "w")
        self.proc = subprocess.Popen(  # noqa: S603 — args estaticos, venv/ruta conocidos
            [str(enterprise_python), "-u", str(self.bootstrap_path)],
            cwd=str(ENTERPRISE_REPO), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        threading.Thread(target=self._drain, daemon=True).start()
        return self._wait_for_ready()

    @property
    def ENTERPRISE_PYTHON(self) -> Path:  # noqa: N802 — nombre deliberado, ver docstring
        return ENTERPRISE_REPO / ".venv" / "bin" / "python"

    def _drain(self) -> None:
        if self.proc is None or self.proc.stdout is None:
            raise RuntimeError("start() debe llamarse antes de _drain()")
        for line in self.proc.stdout:
            self._log_file.write(line)
            self._log_file.flush()
            self._lines.put(line)
        self._lines.put("")  # centinela: EOF

    def _wait_for_ready(self) -> dict:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                line = self._lines.get(timeout=max(0.1, remaining))
            except queue.Empty:
                break
            if line == "":
                break
            if line.startswith("READY "):
                return json.loads(line[len("READY "):])
        tail = self.log_path.read_text()[-4000:] if self.log_path.exists() else ""
        raise TimeoutError(
            f"Enterprise no emitio READY en {READY_TIMEOUT_SECONDS}s. Log:\\n{tail}"
        )

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self._log_file:
            self._log_file.close()


# ── cliente HTTP simple hacia Enterprise para las comprobaciones de estado
# (GET /api/ads/seats con la sesion del owner) — no forma parte del contrato
# companion<->Enterprise bajo prueba, solo observa el efecto lateral
# `last_seen_at` para probar "sin acceso a BD" en el secreto incorrecto. ────


async def _get_seats(base_url: str, ca: Path, cookie: str, org_id: str) -> list[dict]:
    async with httpx.AsyncClient(verify=str(ca)) as client:
        r = await client.get(
            f"{base_url}/api/ads/seats",
            cookies={"lc_session": cookie}, headers={"X-Safent-Org": org_id},
        )
        r.raise_for_status()
        return r.json()["seats"]


async def _last_seen(base_url: str, ca: Path, cookie: str, org_id: str, seat_id: str) -> str | None:
    seats = await _get_seats(base_url, ca, cookie, org_id)
    return next(s["last_seen_at"] for s in seats if s["seat_id"] == seat_id)


async def _wait_until_listening(
    base_url: str, ca: Path, *, cookie: str, org_id: str, seat_id: str, timeout: float,
) -> None:
    """`READY` is printed right BEFORE `uvicorn.run()` starts binding the
    real TLS socket — poll (never a blind sleep) until the port actually
    accepts the handshake, bounded by `timeout`."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            await _last_seen(base_url, ca, cookie, org_id, seat_id)
            return
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_error = exc
            await asyncio.sleep(0.1)
    raise TimeoutError(f"Enterprise no acepto conexiones TLS a tiempo: {last_error}")


# ── orquestacion ────────────────────────────────────────────────────────


async def _run_companion_assertions(
    ready: dict, port: int, ca: Path, *, service_secret: str, wrong_secret: str,
) -> None:
    origin = f"https://127.0.0.1:{port}"
    org_ids = frozenset({UUID(ready["org_id"])})
    view = ready["seats"]["view"]
    propose = ready["seats"]["propose"]
    revoked = ready["seats"]["revoked"]
    owner_cookie = ready["owner_session_cookie"]
    org_id = ready["org_id"]

    async def seat_last_seen() -> str | None:
        return await _last_seen(origin, ca, owner_cookie, org_id, view["seat_id"])

    def _authority(secret: str) -> EnterpriseSeatAuthority:
        trust = EnterpriseSeatTrust(origin=origin, service_secret=secret, allowed_org_ids=org_ids)
        transport = httpx.AsyncHTTPTransport(verify=str(ca))
        return EnterpriseSeatAuthority(trust, transport=transport)

    authority_ok = _authority(service_secret)
    resolver_ok = EnterpriseSeatCallerScopeResolver(authority_ok)
    authority_wrong = _authority(wrong_secret)
    resolver_wrong = EnterpriseSeatCallerScopeResolver(authority_wrong)

    try:
        await _wait_until_listening(
            origin, ca, cookie=owner_cookie, org_id=org_id, seat_id=view["seat_id"], timeout=15,
        )

        # 1) Antes de CUALQUIER introspeccion valida, last_seen_at es None.
        before = await seat_last_seen()
        REPORT.check(
            "control previo: last_seen_at del puesto 'ver' es None antes de introspeccionar",
            before is None, f"valor={before!r}",
        )

        # 2) Secreto de servicio incorrecto -> denegado, y SIN tocar la BD:
        #    el 401 de central_service ocurre en la dependencia de FastAPI,
        #    antes de que el cuerpo de la ruta (y por tanto AdsSeats.
        #    introspect_seat / touch_ads_seat_last_seen) llegue a ejecutarse
        #    -- lo observamos pidiendo el mismo puesto otra vez y viendo que
        #    last_seen_at sigue igual que en el paso 1.
        await _expect_denied(
            "secreto de servicio incorrecto -> denegado",
            resolver_wrong.resolve(view["credential"]),
        )
        after_wrong = await seat_last_seen()
        REPORT.check(
            "secreto incorrecto no toco la BD (last_seen_at sin cambios)",
            after_wrong == before, f"antes={before!r} despues={after_wrong!r}",
        )

        # 3) Credencial 'ver' correcta -> CallerScope(permission=VIEW).
        scope_view = await resolver_ok.resolve(view["credential"])
        REPORT.check(
            "credencial 'ver' -> CallerScope permission=view, negocio correcto",
            scope_view.permission == Permission.VIEW
            and scope_view.caller_id == f"person:{view['user_id']}"
            and scope_view.allowed_business_ids == frozenset({ready["business_id"]}),
            repr(scope_view),
        )

        # 3b) Control positivo: una introspeccion CORRECTA si actualiza
        # last_seen_at (para que el paso 2 sea una prueba real, no un
        # last_seen_at que nunca se mueve por otra razon).
        after_ok = await seat_last_seen()
        REPORT.check(
            "control positivo: introspeccion correcta SI actualiza last_seen_at",
            after_ok is not None, f"valor={after_ok!r}",
        )

        # 4) Credencial 'proponer' correcta -> CallerScope(permission=PROPOSE).
        scope_propose = await resolver_ok.resolve(propose["credential"])
        REPORT.check(
            "credencial 'proponer' -> CallerScope permission=propose",
            scope_propose.permission == Permission.PROPOSE, repr(scope_propose),
        )

        # 5) Credencial manipulada (un caracter hex distinto) -> denegada.
        tampered = list(view["credential"])
        tampered[-1] = "0" if tampered[-1] != "0" else "1"
        await _expect_denied(
            "credencial manipulada -> denegada", resolver_ok.resolve("".join(tampered)),
        )

        # 6) Credencial scope='mcp' (misma persona, otro scope) -> denegada.
        await _expect_denied(
            "credencial scope='mcp' -> denegada (una credencial de puesto exige scope='ads')",
            resolver_ok.resolve(ready["mcp_scope_credential"]),
        )

        # 7) Puesto revocado -> su credencial queda denegada.
        await _expect_denied(
            "puesto revocado -> credencial denegada", resolver_ok.resolve(revoked["credential"]),
        )
    finally:
        await resolver_ok.aclose()
        await resolver_wrong.aclose()


_EXPECTED_VIEW_TOOLS = 55
_EXPECTED_PROPOSE_TOOLS = 69


def _run_tools_list_assertions() -> None:
    registry = _full_registry()
    by_permission = registries_by_permission(registry)
    view_count = len(by_permission[Permission.VIEW])
    propose_count = len(by_permission[Permission.PROPOSE])
    REPORT.check(
        f"tools/list de 'ver' tiene {_EXPECTED_VIEW_TOOLS} herramientas",
        view_count == _EXPECTED_VIEW_TOOLS, f"obtenido={view_count}",
    )
    REPORT.check(
        f"tools/list de 'proponer' tiene {_EXPECTED_PROPOSE_TOOLS} herramientas",
        propose_count == _EXPECTED_PROPOSE_TOOLS, f"obtenido={propose_count}",
    )


def main() -> int:
    if not (ENTERPRISE_REPO / "src" / "safent_control").is_dir():
        print(f"ERROR: no encuentro Enterprise en {ENTERPRISE_REPO}", file=sys.stderr)
        return 2

    tmp_dir = Path(tempfile.mkdtemp(prefix="ads-auth-quickstart-"))
    print(f"directorio temporal: {tmp_dir}")
    port = _free_port()
    certfile, keyfile = _generate_self_signed_cert(tmp_dir)
    service_secret = os.urandom(32).hex()
    wrong_secret = ("0" if service_secret[0] != "0" else "1") + service_secret[1:]

    enterprise = EnterpriseProcess(
        tmp_dir, port, certfile=certfile, keyfile=keyfile, service_secret=service_secret,
    )
    try:
        print(
            f"arrancando Enterprise ({enterprise.ENTERPRISE_PYTHON}) "
            f"en https://127.0.0.1:{port} ...",
        )
        ready = enterprise.start()
        print("Enterprise listo, org sembrada:", ready["org_id"])

        asyncio.run(_run_companion_assertions(
            ready, port, certfile, service_secret=service_secret, wrong_secret=wrong_secret,
        ))
        _run_tools_list_assertions()
    finally:
        enterprise.stop()
        if not KEEP_TMP:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            print(f"directorio temporal conservado: {tmp_dir}")

    print()
    total = len(REPORT.results)
    passed = sum(1 for r in REPORT.results if r.ok)
    print(f"RESULTADO: {passed}/{total} comprobaciones en verde")
    return 0 if REPORT.all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
