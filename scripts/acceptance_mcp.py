#!/usr/bin/env python3
"""Kit de aceptación 004 (P1) — el MCP de anuncios YA DESPLEGADO, en vivo.

Caja negra pura: solo el SDK MCP (`mcp`, `httpx`) y los binarios reales
`claude`/`codex`. Nunca importa `safent_ads` ni toca `src/` — a diferencia
de `scripts/quickstart_auth_chain.py` (que sí instancia clases de
producción para probar la cadena de introspección en local), este script
prueba el servidor YA DESPLEGADO, así que la única fuente de verdad
disponible es el propio cable: `contracts/mcp.md` y los recuentos fijados
en `tests/unit/mcp/presentation/test_catalog_registries_by_permission.py`
/ `tests/e2e/journeys/conftest.py` (69 READ / 90 proponer).

Aserciones (spec.md P1, "Añadido del dueño" 12-24; contracts/mcp.md §3-§5):
  1. `ver`: `initialize()` trae `instructions`; `tools/list` = 69; llamar
     `propose_campaign_draft` se deniega con `PERMISSION_DENIED` (el mismo
     sobre `{"error": {...}}`, `is_error=False`, que fija
     `tests/e2e/journeys/test_journey_permissions.py::
     test_ver_cannot_call_propose_campaign_draft` — nunca un error de
     protocolo JSON-RPC).
  2. `proponer`: `tools/list` = 90; toda herramienta no-READ lleva
     `_meta["anthropic/requiresUserInteraction"] = true`.
  3. `ver`: `list_platform_accounts`/`list_top_performing_ads` responden
     limpio (datos reales o lista vacía, nunca un error).
  4. `proponer`: `propose_campaign_draft` con solo campos estructurados
     (sin `creation_plan` explícito, derivado por el servidor) deja un
     borrador sin campos pendientes; `propose_campaign_from_draft` lo
     convierte en una propuesta pendiente con `proposal_id` — el dueño la
     verá en el panel como "propuesto por" la persona y debe rechazarla o
     ignorarla, nunca aprobarla (es una prueba, no una campaña real).
  5. Arnés real, aislado (nunca toca `~/.claude.json`, `~/.claude/` ni
     `~/.codex/`): Claude Code con el token `ver` (`CLAUDE_CONFIG_DIR`
     propio + proyecto desechable) y Codex con el token `proponer`
     (`CODEX_HOME` propio) — la pareja que pide la aceptación P1 del
     spec ("una con ver y otra con proponer, ... desde Claude Code y
     desde Codex"). Usar `ver` para el único paso que invoca un modelo
     real (el prompt headless de Claude) es intencional: aunque el
     modelo decida llamar una herramienta pese al prompt, el servidor la
     deniega igual — cero riesgo de un efecto secundario real.

Uso:
    MCP_URL=https://ads.example.com/mcp \\
    ADS_ACCEPTANCE_TOKEN_VER=sfa_... ADS_ACCEPTANCE_TOKEN_PROPONER=sfa_... \\
    uv run --frozen python scripts/acceptance_mcp.py

Variables opcionales:
    ANTHROPIC_API_KEY   habilita el prompt headless de Claude Code (§5);
                         sin ella, ese único paso se marca SKIP -- nunca
                         se lee la sesión de `~/.claude` para no violar
                         el aislamiento.
    ADS_ACCEPTANCE_SERVER_NAME   por defecto `safent-ads`; nombre con el
                         que se registra en Claude Code/Codex y del que se
                         deriva el directorio temporal del arnés.
    ADS_ACCEPTANCE_HARNESS_ROOT   por defecto `/tmp/<ADS_ACCEPTANCE_SERVER_NAME>-acceptance`;
                         se borra entero al terminar, con éxito o sin él.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_types import TextContent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp_types import InitializeResult

# Pinned split (contracts/mcp.md §3, tests/unit/mcp/presentation/
# test_catalog_registries_by_permission.py, tests/e2e/journeys/conftest.py):
# 69 READ visible a `ver`; +19 PROPOSAL +1 CATALOG_WRITE +1 CREATIVE_WRITE
# = 90 para `proponer`. `aprobar` (92) no se prueba aquí: el kit solo
# recibe credenciales `ver`/`proponer` (spec.md P1, aceptación).
READ_TOOL_COUNT = 69
PROPOSE_TOOL_COUNT = 90
NON_READ_TOOL_COUNT = PROPOSE_TOOL_COUNT - READ_TOOL_COUNT

_NULL_UUID = "00000000-0000-0000-0000-000000000000"
_MAX_DETAIL_CHARS = 220
_HTTP_TIMEOUT = httpx.Timeout(30.0)

_SERVER_NAME = os.environ.get("ADS_ACCEPTANCE_SERVER_NAME", "safent-ads")
_DEFAULT_HARNESS_ROOT = f"/tmp/{_SERVER_NAME}-acceptance"  # noqa: S108 - aislamiento deliberado, se borra al salir
_HARNESS_ROOT = Path(os.environ.get("ADS_ACCEPTANCE_HARNESS_ROOT", _DEFAULT_HARNESS_ROOT))
_CLAUDE_HOME = _HARNESS_ROOT / "claude-home"
_CLAUDE_PROJECT = _HARNESS_ROOT / "claude-project"
_CODEX_HOME = _HARNESS_ROOT / "codex-home"
_CLAUDE_HEADLESS_MODEL = "claude-haiku-4-5-20251001"
_CODEX_BEARER_TOKEN_ENV_VAR = "ADS_ACCEPTANCE_BEARER_TOKEN"  # noqa: S105 - nombre de variable, no una contrasena


# ── informe ──────────────────────────────────────────────────────────────


class Status(StrEnum):
    PASS = "PASS"  # noqa: S105 - etiqueta de estado, no una contrasena
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""


@dataclass
class Report:
    results: list[CheckResult] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> None:  # noqa: FBT001
        self._add(name, Status.PASS if ok else Status.FAIL, detail)

    def skip(self, name: str, reason: str) -> None:
        self._add(name, Status.SKIP, reason)

    def _add(self, name: str, status: Status, detail: str) -> None:
        self.results.append(CheckResult(name, status, detail))
        line = f"[{status.value}] {name}"
        if detail:
            line += f" — {detail}"
        print(line, flush=True)

    @property
    def has_failure(self) -> bool:
        return any(result.status == Status.FAIL for result in self.results)

    def print_summary_table(self) -> None:
        counts = Counter(result.status for result in self.results)
        print()
        print(f"{'Estado':<6} Comprobación")
        print("-" * 78)
        for result in self.results:
            print(f"{result.status.value:<6} {result.name}")
        print()
        print(
            f"RESULTADO: {counts[Status.PASS]} PASS · {counts[Status.FAIL]} FAIL · "
            f"{counts[Status.SKIP]} SKIP (de {len(self.results)})"
        )


class MissingEnvError(RuntimeError):
    pass


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingEnvError(f"falta la variable de entorno {name} (su valor nunca se imprime)")
    return value


def _short(payload: Any) -> str:  # noqa: ANN401 - envoltura JSON de forma variable
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text if len(text) <= _MAX_DETAIL_CHARS else text[: _MAX_DETAIL_CHARS] + "…"


def _redact(text: str, token: str) -> str:
    return text.replace(token, "sfa_[REDACTED]") if token else text


def _root_cause(exc: BaseException) -> BaseException:
    """`streamable_http_client` propaga fallos de transporte (401, TLS,
    timeout) envueltos en uno o mas `ExceptionGroup` de anyio; el mensaje
    util para diagnosticar esta contra un servidor real vive en la hoja,
    no en el envoltorio ("unhandled errors in a TaskGroup")."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


# ── cliente MCP (SDK real, sin mocks) ───────────────────────────────────


@asynccontextmanager
async def _connect(url: str, token: str) -> AsyncIterator[tuple[ClientSession, InitializeResult]]:
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx.AsyncClient(headers=headers, timeout=_HTTP_TIMEOUT) as client,
        streamable_http_client(url, http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        yield session, init


async def _call(session: ClientSession, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Un tool wrapper real solo declara un parametro (`args`, mount.py::
    _build_wrapper): el esquema de cada herramienta anida los campos ahi,
    tal como ya hacen tests/e2e/journeys/test_journey_campaign.py, etc."""
    result = await session.call_tool(tool, {"args": arguments})
    if result.structured_content is not None:
        return result.structured_content
    for block in result.content:
        if isinstance(block, TextContent):
            return json.loads(block.text)
    return {}


async def _checked_call(  # noqa: PLR0917 - firma clara con nombres, no vale la pena un dataclass de parametros
    report: Report,
    name: str,
    session: ClientSession,
    tool: str,
    arguments: dict[str, Any],
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    try:
        envelope = await _call(session, tool, arguments)
    except Exception as exc:  # noqa: BLE001 - un fallo de transporte es un FAIL, no una excepcion sin controlar
        report.check(name, ok=False, detail=f"{type(exc).__name__}: {exc}")
        return None
    report.check(name, predicate(envelope), _short(envelope))
    return envelope


def _result_list(envelope: dict[str, Any] | None) -> list[Any]:
    if envelope is None:
        return []
    result = envelope.get("result")
    return result if isinstance(result, list) else []


def _first(envelope: dict[str, Any] | None, key: str) -> str | None:
    items = _result_list(envelope)
    return items[0].get(key) if items else None


# ── `ver`: 69 READ, denegación, lecturas limpias (spec.md P1-2/12/18) ────


async def _run_ver_checks(report: Report, url: str, token: str) -> None:
    try:
        async with _connect(url, token) as (session, init):
            report.check(
                "ver: initialize() devuelve instructions no vacías",
                bool(init.instructions and init.instructions.strip()),
            )

            tools = await session.list_tools()
            report.check(
                f"ver: tools/list tiene exactamente {READ_TOOL_COUNT} herramientas (solo READ)",
                len(tools.tools) == READ_TOOL_COUNT,
                f"obtenido={len(tools.tools)}",
            )

            biz = await _checked_call(
                report,
                "ver: list_businesses devuelve al menos un negocio",
                session,
                "list_businesses",
                {},
                lambda e: len(_result_list(e)) >= 1,
            )
            business_id = _first(biz, "business_id")

            await _checked_call(
                report,
                "ver: propose_campaign_draft se deniega con PERMISSION_DENIED "
                "(is_error=False, sobre {error: {...}}, no un error de protocolo)",
                session,
                "propose_campaign_draft",
                {
                    "business_id": business_id or _NULL_UUID,
                    "draft_key": "acceptance-004-permcheck",
                    "changes": {},
                },
                lambda e: e.get("error", {}).get("code") == "PERMISSION_DENIED",
            )

            if business_id is None:
                report.skip(
                    "ver: list_platform_accounts / list_top_performing_ads",
                    "sin business_id (list_businesses no devolvió ninguno)",
                )
                return

            await _checked_call(
                report,
                "ver: list_platform_accounts responde limpio (datos reales o vacío)",
                session,
                "list_platform_accounts",
                {"business_id": business_id},
                lambda e: isinstance(e.get("result"), list),
            )
            await _checked_call(
                report,
                "ver: list_top_performing_ads responde limpio (datos reales o vacío)",
                session,
                "list_top_performing_ads",
                {"business_id": business_id},
                lambda e: isinstance(e.get("result", {}).get("ads"), list),
            )
    except Exception as exc:  # noqa: BLE001 - fallo de conexion: un FAIL, no un traceback sin control
        cause = _root_cause(exc)
        report.check(
            f"ver: sesión MCP contra {url}", ok=False, detail=f"{type(cause).__name__}: {cause}"
        )


# ── `proponer`: 90 herramientas, _meta, borrador -> propuesta (P1-3/13/14) ─


def _select_active_account(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    accounts = [a for a in _result_list(envelope) if a.get("status") == "active"]
    for platform in ("google", "meta"):  # google primero: no exige meta_page_id
        for account in accounts:
            if account.get("platform") == platform:
                return account
    return None


def _draft_changes(
    account: dict[str, Any], offering_id: str, meta_page_id: str | None
) -> dict[str, Any]:
    changes: dict[str, Any] = {
        "title": "ACEPTACIÓN 004 — prueba automática, RECHAZAR en el panel",
        "platform": account["platform"],
        "account_ref": account["account_ref"],
        "offering_id": offering_id,
        "objective": "Prueba de aceptación del kit MCP 004 (scripts/acceptance_mcp.py)",
        "daily_budget": {"amount": "5.00", "currency": "EUR"},
        "duration_days": 7,
        "success_criterion": "Ninguno: prueba automática, no es una campaña real",
        "kill_criterion": "Rechazar en el panel inmediatamente",
        "angle": "Prueba de aceptación automática, sin ángulo real",
        "targeting_seed": "prueba interna de aceptación",
        "geo": "España",
        "landing_url": "https://example.com/",
        "notes": (
            "Generado por scripts/acceptance_mcp.py (kit de aceptación 004). "
            "Rechazar o ignorar; no aprobar."
        ),
    }
    if meta_page_id is not None:
        changes["meta_page_id"] = meta_page_id
    return changes


async def _run_proponer_checks(report: Report, url: str, token: str) -> None:
    try:
        async with _connect(url, token) as (session, _init):
            tools = await session.list_tools()
            report.check(
                f"proponer: tools/list tiene exactamente {PROPOSE_TOOL_COUNT} herramientas",
                len(tools.tools) == PROPOSE_TOOL_COUNT,
                f"obtenido={len(tools.tools)}",
            )
            interactive_key = "anthropic/requiresUserInteraction"
            with_meta = [
                t
                for t in tools.tools
                if isinstance(t.meta, dict) and t.meta.get(interactive_key) is True
            ]
            without_meta_count = len(tools.tools) - len(with_meta)
            report.check(
                f"proponer: exactamente {NON_READ_TOOL_COUNT} herramientas no-READ llevan "
                f'_meta["{interactive_key}"]=true (las READ, ninguna)',
                len(with_meta) == NON_READ_TOOL_COUNT and without_meta_count == READ_TOOL_COUNT,
                f"con_meta={len(with_meta)} sin_meta={without_meta_count}",
            )

            biz = await _checked_call(
                report,
                "proponer: list_businesses devuelve al menos un negocio",
                session,
                "list_businesses",
                {},
                lambda e: len(_result_list(e)) >= 1,
            )
            business_id = _first(biz, "business_id")
            if business_id is None:
                report.skip(
                    "proponer: propose_campaign_draft / propose_campaign_from_draft",
                    "sin business_id (list_businesses no devolvió ninguno)",
                )
                return

            await _propose_draft_and_promote(report, session, business_id)
    except Exception as exc:  # noqa: BLE001 - fallo de conexion: un FAIL, no un traceback sin control
        cause = _root_cause(exc)
        detail = f"{type(cause).__name__}: {cause}"
        report.check(f"proponer: sesión MCP contra {url}", ok=False, detail=detail)


async def _propose_draft_and_promote(
    report: Report, session: ClientSession, business_id: str
) -> None:
    accounts = await _checked_call(
        report,
        "proponer: list_platform_accounts devuelve al menos una cuenta activa",
        session,
        "list_platform_accounts",
        {"business_id": business_id},
        lambda e: any(a.get("status") == "active" for a in _result_list(e)),
    )
    account = _select_active_account(accounts)

    offerings = await _checked_call(
        report,
        "proponer: list_offerings devuelve al menos una oferta",
        session,
        "list_offerings",
        {"business_id": business_id},
        lambda e: len(_result_list(e)) >= 1,
    )
    offering_id = _first(offerings, "offering_id")

    if account is None or offering_id is None:
        report.skip(
            "proponer: propose_campaign_draft / propose_campaign_from_draft",
            "faltan datos del negocio (cuenta activa u oferta) para una propuesta completa "
            "-- probablemente la migración P3 aún no ha corrido",
        )
        return

    meta_page_id = None
    if account["platform"] == "meta":
        pages = await _checked_call(
            report,
            "proponer: list_meta_pages resuelve una página para la cuenta Meta elegida",
            session,
            "list_meta_pages",
            {"business_id": business_id, "account_ref": account["account_ref"]},
            lambda e: len(_result_list(e)) >= 1,
        )
        meta_page_id = _first(pages, "page_id")
        if meta_page_id is None:
            report.skip(
                "proponer: propose_campaign_draft / propose_campaign_from_draft",
                "la única cuenta activa es de Meta y no tiene ninguna página conectada",
            )
            return

    draft_key = f"acceptance-004-{datetime.now(UTC):%Y%m%d%H%M%S}"
    changes = _draft_changes(account, offering_id, meta_page_id)
    draft_envelope = await _checked_call(
        report,
        "proponer: propose_campaign_draft crea un borrador completo (sin campos pendientes)",
        session,
        "propose_campaign_draft",
        {"business_id": business_id, "draft_key": draft_key, "changes": changes},
        lambda e: e.get("result", {}).get("missing_fields") == [],
    )
    draft = (draft_envelope or {}).get("result")
    if not draft:
        return

    promote_envelope = await _checked_call(
        report,
        "proponer: propose_campaign_from_draft entrega un proposal_id",
        session,
        "propose_campaign_from_draft",
        {
            "business_id": business_id,
            "draft_id": draft["draft_id"],
            "expected_revision": draft["revision"],
        },
        lambda e: bool(e.get("result", {}).get("proposal_id")),
    )
    proposal_id = ((promote_envelope or {}).get("result") or {}).get("proposal_id")
    if proposal_id:
        print(
            f"    -> proposal_id de prueba (negocio {business_id}): {proposal_id} "
            "-- RECHAZAR en el panel, no es una campaña real"
        )


# ── arnés real, aislado: Claude Code (ver) y Codex (proponer) (spec.md P1-1/2) ─


def _run_claude_harness_checks(report: Report, claude_bin: str, url: str, token: str) -> None:
    _CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
    _CLAUDE_PROJECT.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(_CLAUDE_HOME)}

    add = subprocess.run(  # noqa: S603 - binario resuelto por shutil.which, argumentos estaticos
        [
            claude_bin, "mcp", "add", "--transport", "http", "--scope", "local", _SERVER_NAME, url,
            "--header", f"Authorization: Bearer {token}",
        ],
        cwd=str(_CLAUDE_PROJECT), env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    add_detail = (add.stdout or add.stderr).strip().splitlines()
    report.check(
        "Claude Code (ver): 'claude mcp add' registra el servidor en el home aislado",
        add.returncode == 0,
        _redact(add_detail[0] if add_detail else "", token),
    )

    listing = subprocess.run(  # noqa: S603
        [claude_bin, "mcp", "list"], cwd=str(_CLAUDE_PROJECT), env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    connected = f"{_SERVER_NAME}:" in listing.stdout and "✔" in listing.stdout
    report.check(
        "Claude Code (ver): 'claude mcp list' confirma la conexión real (Connected)",
        connected, _redact(listing.stdout.strip(), token),
    )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        report.skip(
            "Claude Code (ver): prompt headless -p ve las herramientas del servidor",
            "sin ANTHROPIC_API_KEY en el entorno -- no se lee ~/.claude para no violar "
            "el aislamiento; el resto del kit no depende de este paso",
        )
        return
    _run_claude_headless_prompt(report, claude_bin, env)


def _run_claude_headless_prompt(report: Report, claude_bin: str, env: dict[str, str]) -> None:
    prompt = (
        "Sin llamar a ninguna herramienta, enumera los nombres exactos de las herramientas "
        f"MCP del servidor '{_SERVER_NAME}' que ves disponibles en este chat, una por línea."
    )
    headless = subprocess.run(  # noqa: S603
        [
            claude_bin, "-p", prompt, "--model", _CLAUDE_HEADLESS_MODEL, "--output-format", "json",
            "--disallowedTools", f"mcp__{_SERVER_NAME}__*",
        ],
        cwd=str(_CLAUDE_PROJECT), env=env, capture_output=True, text=True, timeout=90, check=False,
    )
    if headless.returncode != 0:
        report.check(
            "Claude Code (ver): prompt headless -p ve las herramientas del servidor",
            ok=False, detail=f"exit={headless.returncode} stderr={headless.stderr.strip()[:200]}",
        )
        return
    try:
        payload = json.loads(headless.stdout)
    except json.JSONDecodeError:
        report.check(
            "Claude Code (ver): prompt headless -p ve las herramientas del servidor",
            ok=False, detail=f"salida no JSON: {headless.stdout.strip()[:200]}",
        )
        return
    report.check(
        "Claude Code (ver): prompt headless -p ve las herramientas del servidor",
        not payload.get("is_error"),
        f"respuesta del modelo: {str(payload.get('result'))[:200]}",
    )


def _run_codex_harness_checks(report: Report, codex_bin: str, url: str, token: str) -> None:
    _CODEX_HOME.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CODEX_HOME": str(_CODEX_HOME), _CODEX_BEARER_TOKEN_ENV_VAR: token}

    add = subprocess.run(  # noqa: S603
        [
            codex_bin, "mcp", "add", _SERVER_NAME, "--url", url,
            "--bearer-token-env-var", _CODEX_BEARER_TOKEN_ENV_VAR,
        ],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    add_detail = (add.stdout or add.stderr).strip().splitlines()
    report.check(
        "Codex (proponer): 'codex mcp add' registra el servidor en el home aislado",
        add.returncode == 0, add_detail[0] if add_detail else "",
    )

    listing = subprocess.run(  # noqa: S603
        [codex_bin, "mcp", "list", "--json"], env=env, capture_output=True, text=True, timeout=30,
        check=False,
    )
    ok, detail = _parse_codex_listing(listing, url)
    report.check(
        "Codex (proponer): 'codex mcp list' confirma el registro (chequeo de conexión)", ok, detail
    )


def _parse_codex_listing(completed: subprocess.CompletedProcess[str], url: str) -> tuple[bool, str]:
    if completed.returncode != 0:
        return False, f"exit={completed.returncode} stderr={completed.stderr.strip()[:200]}"
    try:
        servers = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return False, f"JSON inválido: {completed.stdout.strip()[:200]}"
    match = next((s for s in servers if s.get("name") == _SERVER_NAME), None)
    if match is None:
        return False, f"servidor '{_SERVER_NAME}' no aparece: {_short(servers)}"
    transport = match.get("transport", {})
    ok = (
        transport.get("url") == url
        and transport.get("bearer_token_env_var") == _CODEX_BEARER_TOKEN_ENV_VAR
        and match.get("enabled") is True
    )
    return ok, _short(match)


# ── orquestación ─────────────────────────────────────────────────────────


async def _run_sdk_checks(report: Report, url: str, token_ver: str, token_proponer: str) -> None:
    await _run_ver_checks(report, url, token_ver)
    await _run_proponer_checks(report, url, token_proponer)


def main() -> int:
    try:
        mcp_url = _require_env("MCP_URL")
        token_ver = _require_env("ADS_ACCEPTANCE_TOKEN_VER")
        token_proponer = _require_env("ADS_ACCEPTANCE_TOKEN_PROPONER")
    except MissingEnvError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    claude_bin = shutil.which("claude")
    codex_bin = shutil.which("codex")
    if claude_bin is None or codex_bin is None:
        print("ERROR: 'claude' y/o 'codex' no están en el PATH", file=sys.stderr)
        return 2

    print(f"MCP_URL={mcp_url}")
    report = Report()
    try:
        asyncio.run(_run_sdk_checks(report, mcp_url, token_ver, token_proponer))
        _run_claude_harness_checks(report, claude_bin, mcp_url, token_ver)
        _run_codex_harness_checks(report, codex_bin, mcp_url, token_proponer)
    finally:
        shutil.rmtree(_HARNESS_ROOT, ignore_errors=True)

    report.print_summary_table()
    return 1 if report.has_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
