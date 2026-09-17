"""Endurecimiento de `ads-api` (tasks.md T015, threat-model.md C-23/C-26):
cabeceras de seguridad, CSRF de doble envio, limite de tasa en proceso para
`/auth/*`, `/mcp` y `/api/v1/brand/discover` (F-7), `/api/v1/health/deep`,
y el montaje de los routers de `iam`/`audit`. Todo entra por `harden_api`,
que `composition/app.py` llama desde su bloque `# --- lane: secfound ---`
(aditivo, sin tocar el resto de la fabrica de la app)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from typing import Protocol
from urllib.parse import SplitResult, parse_qs, urlsplit

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from safent_ads.audit.presentation.router import router as decision_log_router
from safent_ads.composition.container import Container
from safent_ads.composition.federated_routes import register_federated_login_routes
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.iam.presentation.router import build_auth_router
from safent_ads.shared.net.client_ip import resolve_client_ip

logger = structlog.get_logger(__name__)

_CSRF_COOKIE_NAME = "ads_csrf"
_CSRF_HEADER_NAME = "x-csrf-token"
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# `/auth/exchange` (026, contracts/sso.md §4): la llama el shell-server
# server-a-servidor, sin la cookie `ads_csrf` de un navegador -- "Exento de
# CSRF (no lleva cookie de sesión)" es literal en el contrato.
#
# Spec 002 (mcp_oauth) tasks.md T014, threat-model.md C-52: `/authorize`,
# `/token`, `/register`, `/revoke` y `/.well-known/` son el AS -- clientes
# OAuth autenticados por PKCE/registro, no por cookie de sesion, asi que no
# hay superficie CSRF que proteger ahi. `/api/v1/mcp-oauth/*` (consent,
# grants) NUNCA entra aqui: lleva `ads_session` + CSRF de doble envio como
# el resto del panel (C-40).
_CSRF_EXEMPT_PREFIXES = (
    "/mcp",
    "/api/v1/auth/exchange",
    "/authorize",
    "/token",
    "/register",
    "/revoke",
    "/.well-known/",
)
# H3/AL-7 (revision de codigo, threat-model.md #364, tasks.md T126): el
# doble envio de arriba protege toda mutacion, pero `/api/v1/packages/**`
# alcanza una escritura real de plataforma (`undo` revierte una campana ya
# publicada) -- AL-7 pide una segunda capa propia de esa superficie:
# `Origin`/`Sec-Fetch-Site` comprobados, `Origin` ausente => 403.
_PACKAGES_ORIGIN_GUARDED_PREFIX = "/api/v1/packages"


def _security_headers(*, companion_mode: bool) -> tuple[tuple[str, str], ...]:
    """`frame-ancestors 'self'` + `X-Frame-Options: SAMEORIGIN` SOLO en modo
    companion (026, contracts/cockpit-read-model.md §5 / sso.md §7 E-2): el
    iframe mismo-origen del puente necesita poder empotrarse en Safent;
    fuera de ese modo siguen en `'none'`/`DENY` -- clickjacking cero por
    defecto."""
    frame_ancestors = "'self'" if companion_mode else "'none'"
    x_frame_options = "SAMEORIGIN" if companion_mode else "DENY"
    return (
        (
            "content-security-policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; "
            f"frame-ancestors {frame_ancestors}; base-uri 'none'; "
            "form-action 'self'; object-src 'none'",
        ),
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "no-referrer"),
        ("x-frame-options", x_frame_options),
        ("permissions-policy", "geolocation=(), microphone=(), camera=()"),
    )


# 026, contracts/sso.md §7 T-1: allow-list de `X-Forwarded-Prefix`; cualquier
# otro valor -> "" (peor caso, ruta del mismo origen -- nunca se confia en
# un prefijo arbitrario para construir URLs).
_ALLOWED_FORWARDED_PREFIXES = frozenset({"", "/ads"})

def _per_minute(capacity: int) -> float:
    """Tasa de recarga del cubo de tokens para un tope de `capacity` por
    MINUTO (nit, code review 17-sep): antes cada regla repetia `capacity /
    60` a mano -- un numero magico que invita a que alguien copie el
    divisor equivocado (`/3600`, hora) sin darse cuenta."""
    return capacity / 60


_AUTH_RATE_LIMIT_PREFIX = "/api/v1/auth"
_AUTH_RATE_LIMIT_CAPACITY = 20
_AUTH_RATE_LIMIT_REFILL_PER_SECOND = _per_minute(_AUTH_RATE_LIMIT_CAPACITY)

# T084 C-76: sin esto, `_AUTH_RATE_LIMIT_PREFIX` (mas abajo) se traga las
# tres rutas federadas -- son un prefijo suyo -- y ademas comparten cupo
# con `/login`/`/logout`: un anonimo que machaque `/federated/status`
# (publica, sin sesion) agota el mismo cubo que necesita el dueno para
# entrar. Cada ruta federada recibe su PROPIO presupuesto, mas estrecho
# cuanto mas caro es el efecto (`start` abre una fila en BD y dispara un
# viaje a Google; `callback` la consume; `status` solo lee un booleano).
# Las tres van por `forwarded_for_rate_limit_key` (`ADS_TRUSTED_PROXY_HOPS`),
# nunca por el defecto `_rate_limit_key` -- deben quedar ANTES que
# `_AUTH_RATE_LIMIT_PREFIX` en el dict que arma `_rate_limit_rules` (el
# primer prefijo que casa gana, `RateLimitMiddleware._rule_for`).
_FEDERATED_START_RATE_LIMIT_PREFIX = "/api/v1/auth/federated/start"
_FEDERATED_START_RATE_LIMIT_CAPACITY = 10
_FEDERATED_START_RATE_LIMIT_REFILL_PER_SECOND = _per_minute(_FEDERATED_START_RATE_LIMIT_CAPACITY)
_FEDERATED_CALLBACK_RATE_LIMIT_PREFIX = "/api/v1/auth/federated/callback"
_FEDERATED_CALLBACK_RATE_LIMIT_CAPACITY = 20
_FEDERATED_CALLBACK_RATE_LIMIT_REFILL_PER_SECOND = _per_minute(
    _FEDERATED_CALLBACK_RATE_LIMIT_CAPACITY
)
_FEDERATED_STATUS_RATE_LIMIT_PREFIX = "/api/v1/auth/federated/status"
_FEDERATED_STATUS_RATE_LIMIT_CAPACITY = 60
_FEDERATED_STATUS_RATE_LIMIT_REFILL_PER_SECOND = _per_minute(_FEDERATED_STATUS_RATE_LIMIT_CAPACITY)
# T075/F2-F3 TOTP re-auth defecto 3 (CWE-307): `RateLimitMiddleware`
# empareja por prefijo y solo conocia `/auth`, `/mcp` y `/brand/discover` --
# `/api/v1/rules/autonomy-gate*` (la vista Y sus confirmaciones, unico
# `_require_reauth` fuera de `/auth`) se quedaba sin limite alguno. Mismo
# presupuesto que `/auth/*` (misma naturaleza: adivinar un codigo de un
# solo uso), no uno nuevo inventado. Code review 17-sep (item 3): iba por
# `key_of=_rate_limit_key` (el defecto) mientras `_AUTH_RATE_LIMIT_PREFIX`
# ya se habia movido a `forwarded_for_rate_limit_key` -- detras de Caddy,
# el cubo de reauth TOTP era 20/min para todo Internet junto.
_AUTONOMY_GATE_RATE_LIMIT_PREFIX = "/api/v1/rules/autonomy-gate"
_MCP_RATE_LIMIT_PREFIX = "/mcp"
# Presupuesto por SESION MCP (`Mcp-Session-Id`, la fija el SDK en la
# respuesta de `initialize` y el cliente la repite en cada llamada
# siguiente): un ciclo de agente normal es `initialize` + `tools/list` +
# una docena de lecturas, varias veces por hora -- 600/min es holgado para
# eso y sigue estrecho para un abuso real dentro de una sesion ya abierta.
_MCP_SESSION_RATE_LIMIT_CAPACITY = 600
_MCP_SESSION_RATE_LIMIT_REFILL_PER_SECOND = 600 / 60
# Presupuesto SIN SESION (bearer, o IP si tampoco hay bearer): aqui cae
# `initialize` -- la unica llamada de streamable-http que todavia no trae
# `Mcp-Session-Id` -- asi que se queda con el mismo cupo de antes (60/min)
# para seguir cortando una inundacion de `initialize` sin sesion abierta.
_MCP_SESSION_LESS_RATE_LIMIT_CAPACITY = 60
_MCP_SESSION_LESS_RATE_LIMIT_REFILL_PER_SECOND = 60 / 60
# F-7: cada peticion a /brand/discover dispara N peticiones salientes
# (pagina, robots.txt, paginas enlazadas, hojas de estilo, manifest,
# logos) -- 3 por hora, por `business_id` (no por sesion), es generoso
# para un uso legitimo (el propietario ajusta su web y vuelve a rastrear
# un par de veces) y estrecho para un abuso.
_BRAND_DISCOVER_RATE_LIMIT_PREFIX = "/api/v1/brand/discover"
_BRAND_DISCOVER_RATE_LIMIT_CAPACITY = 3
_BRAND_DISCOVER_RATE_LIMIT_REFILL_PER_SECOND = 3 / 3600

# T220: `/conversions/webhook` no lleva cookie de sesion (lo llama el CRM
# externo del propietario) -- adivinar el token a fuerza bruta es el unico
# vector, asi que se limita por el propio token presentado (o IP si falta)
# en vez de por sesion/bearer estandar (`_rate_limit_key` solo mira
# `authorization`). El prefijo tambien cubre `/webhook-token` (subcadena):
# inofensivo, esa ruta ya exige sesion + CSRF + reauth TOTP antes de
# llegar aqui, compartir el mismo presupuesto generoso no la debilita.
_CONVERSIONS_WEBHOOK_RATE_LIMIT_PREFIX = "/api/v1/conversions/webhook"
_CONVERSIONS_WEBHOOK_RATE_LIMIT_CAPACITY = 120
_CONVERSIONS_WEBHOOK_RATE_LIMIT_REFILL_PER_SECOND = 120 / 60

# Spec 027 (contracts/crm-link.md §2): mismo criterio que el bloque de
# arriba -- `/api/v1/crm/*` no lleva cookie de sesion en sus rutas de
# ingesta (las llama el runtime via `X-Bridge-Token`), asi que se limita
# por el token presentado. El prefijo tambien cubre `/crm/bridge-token`
# (emision, con sesion + CSRF + reauth TOTP): sin `X-Bridge-Token` cae a
# IP, inofensivo -- mismo razonamiento que `/webhook-token` arriba.
_CRM_BRIDGE_RATE_LIMIT_PREFIX = "/api/v1/crm/"
_CRM_BRIDGE_RATE_LIMIT_CAPACITY = 120
_CRM_BRIDGE_RATE_LIMIT_REFILL_PER_SECOND = 120 / 60

# Spec 002 (mcp_oauth) tasks.md T014, threat-model.md C-42/C-51: los
# endpoints del AS no llevan sesion ni bearer -- se limitan por IP. H1 de
# la revision de seguridad (16-sep): el proxy de confianza real depende
# del despliegue -- Caddy delante del host de `ADS_PUBLIC_BASE_URL`, o
# `tailscale serve`/`tailscale funnel` en desarrollo -- asi que el numero
# de saltos de confianza es `ApiSettings.trusted_proxy_hops`
# (`ADS_TRUSTED_PROXY_HOPS`), no un supuesto fijo aqui. `/token` no conoce
# el `client_id` hasta parsear el cuerpo `application/x-www-form-urlencoded`,
# asi que tambien va por IP.
_OAUTH_AUTHORIZE_RATE_LIMIT_PREFIX = "/authorize"
_OAUTH_AUTHORIZE_RATE_LIMIT_CAPACITY = 30
_OAUTH_AUTHORIZE_RATE_LIMIT_REFILL_PER_SECOND = 30 / 60
_OAUTH_TOKEN_RATE_LIMIT_PREFIX = "/token"  # noqa: S105 - ruta del AS, no una contrasena
_OAUTH_TOKEN_RATE_LIMIT_CAPACITY = 60
_OAUTH_TOKEN_RATE_LIMIT_REFILL_PER_SECOND = 60 / 60
_OAUTH_REGISTER_RATE_LIMIT_PREFIX = "/register"
_OAUTH_REGISTER_RATE_LIMIT_CAPACITY = 10
_OAUTH_REGISTER_RATE_LIMIT_REFILL_PER_SECOND = 10 / 3600
_OAUTH_REVOKE_RATE_LIMIT_PREFIX = "/revoke"
_OAUTH_REVOKE_RATE_LIMIT_CAPACITY = 30
_OAUTH_REVOKE_RATE_LIMIT_REFILL_PER_SECOND = 30 / 60
# `/api/v1/mcp-oauth/consent` SI lleva sesion (C-40), pero el TOTP fresco
# por transaccion (C-41) ya es el freno real -- este limite es solo contra
# un cliente que reintenta `approve`/`deny` sin parar.
_OAUTH_CONSENT_RATE_LIMIT_PREFIX = "/api/v1/mcp-oauth/consent"
_OAUTH_CONSENT_RATE_LIMIT_CAPACITY = 20
_OAUTH_CONSENT_RATE_LIMIT_REFILL_PER_SECOND = 20 / 60
# T126/AL-7 (revision de seguridad 0.2.22): `/api/v1/packages/**` alcanza
# una escritura real de plataforma (`undo` revierte una campana ya
# publicada) -- sin limite propio, el doble envio de `CsrfMiddleware` y el
# guardia de Origin bastan contra CSRF pero no contra una sesion legitima
# reintentando sin parar. Por SESION (`ads_session`), no por IP: varias
# sesiones del propietario en la misma IP no deben compartir cupo.
# `_PACKAGES_ORIGIN_GUARDED_PREFIX` (arriba) es el mismo prefijo -- una
# sola constante, dos guardias distintos.
_PACKAGES_RATE_LIMIT_CAPACITY = 30
_PACKAGES_RATE_LIMIT_REFILL_PER_SECOND = 30 / 60

# Revision de seguridad de la conexion Cloudflare (2026-09-15, hallazgo
# bajo): `POST /api/v1/integrations/cloudflare/token` valida el token
# contra la API real de Cloudflare ANTES de guardarlo -- sin limite
# propio, es un oraculo de validacion de tokens (cada intento revela si
# un token adivinado/robado es valido). Por SESION, no por IP -- mismo
# criterio que `_PACKAGES_RATE_LIMIT_*` -- y solo POST (`methods`, T126/
# AL-7): GET/DELETE no tocan la red de Cloudflare, no son el oraculo.
_CLOUDFLARE_TOKEN_RATE_LIMIT_PREFIX = "/api/v1/integrations/cloudflare/token"  # noqa: S105
_CLOUDFLARE_TOKEN_RATE_LIMIT_CAPACITY = 5
_CLOUDFLARE_TOKEN_RATE_LIMIT_REFILL_PER_SECOND = 5 / 60


_BROKER_SOCKET_TIMEOUT_SECONDS = 2.0


def harden_api(app: FastAPI, settings: ApiSettings, container: Container) -> None:
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
    _warn_if_https_without_a_trusted_proxy(settings)
    _add_hardening_middlewares(app, settings)
    app.include_router(build_auth_router(settings))
    register_federated_login_routes(app, settings, container)
    app.include_router(decision_log_router)
    _mount_health_deep(app, settings, container)


def _warn_if_https_without_a_trusted_proxy(settings: ApiSettings) -> None:
    """I-1 (revision de seguridad 17-sep): C-76 y el tope por IP de C-79
    dependen enteramente de `ADS_TRUSTED_PROXY_HOPS=1` -- su defecto es
    `0` (`compose.companion.yaml`, conexion directa). Un despliegue con
    `ADS_PUBLIC_BASE_URL` en `https://` es, casi siempre, uno detras de un
    proxy TLS (Caddy, `tailscale serve`/`funnel`) que SI reenvia
    `X-Forwarded-For` -- con `trusted_proxy_hops=0` esa cabecera se
    ignora y toda `/api/v1/auth/*`/`/api/v1/rules/autonomy-gate*` queda
    particionada por la IP del proxy, no la del llamante real (R-19). Sin
    secretos: solo un evento, para que quede en el arranque y no solo en
    `.env`."""
    if settings.public_base_url.startswith("https://") and settings.trusted_proxy_hops == 0:
        logger.warning("public_https_without_a_trusted_proxy_at_startup")


def _add_hardening_middlewares(app: FastAPI, settings: ApiSettings) -> None:
    """L3 de la revision de seguridad (16-sep): el ORDEN de estas llamadas
    es contraintuitivo y antes estaba al reves. `Starlette.add_middleware`
    inserta cada middleware nuevo delante de los ya registrados
    (`user_middleware.insert(0, ...)`), asi que el ULTIMO que se registra
    aqui termina siendo el que queda MAS AFUERA en tiempo de ejecucion --
    el primero en ver la peticion entrante y el ultimo en ver la respuesta
    saliente. Para que la envoltura real sea, de fuera adentro, RequestId
    -> ForwardedPrefix -> SecurityHeaders -> Csrf -> RateLimit -- y asi un
    403/429 que corta en `CsrfMiddleware`/`RateLimitMiddleware` siga
    llevando `request_id` en los logs y las cabeceras de seguridad en la
    respuesta -- hay que registrarlos en el orden EXACTAMENTE INVERSO al
    de esa lista."""
    forwarded_for_rate_limit_key = _make_forwarded_for_rate_limit_key(settings.trusted_proxy_hops)
    app.add_middleware(
        RateLimitMiddleware,
        limiters=_rate_limit_rules(forwarded_for_rate_limit_key),
    )
    app.add_middleware(
        CsrfMiddleware,
        allowed_origins=_allowed_origins_for(
            settings.public_base_url,
            extra_allowed_hosts=frozenset(settings.mcp_extra_allowed_hosts),
        ),
    )
    # AL-7 (revision de seguridad 0.2.22): registrada DESPUES de
    # `CsrfMiddleware` -- y por tanto evaluada ANTES, ver el docstring de
    # arriba -- para que un replay de `ads_session` sin `Origin` ni
    # `Sec-Fetch-Site` de 403 `CSRF_ORIGIN_REQUIRED` en vez del
    # `CSRF_REJECTED` generico del doble envio.
    app.add_middleware(PackagesOriginRequiredMiddleware)
    app.add_middleware(
        SecurityHeadersMiddleware,
        headers=_security_headers(companion_mode=settings.companion_mode),
    )
    app.add_middleware(ForwardedPrefixMiddleware, companion_mode=settings.companion_mode)
    # T127: unica correlacion que faltaba fuera de `cycle_id`
    # (orquestacion, orchestration/application/logging.py) y `trace_id`
    # (MCP, mcp/presentation/dispatcher.py) -- el resto de la superficie
    # REST (brand, creative, proposals, settings...) no ataba ningun campo
    # de peticion a sus logs. Registrado ULTIMO (envoltura mas externa,
    # ver docstring de arriba) para que quede disponible en todo lo que
    # corre despues, incluidas las cabeceras de seguridad y el propio
    # CSRF/limite de tasa si llegan a loguear.
    app.add_middleware(RequestIdMiddleware)


def _ip_rate_limit(
    capacity: int, refill_per_second: float, *, key_of: Callable[[Scope], str]
) -> RateLimitRule:
    """I9: factoriza las cinco reglas del AS OAuth (`/authorize`, `/token`,
    `/register`, `/revoke`, `/api/v1/mcp-oauth/consent`), que solo
    difieren en capacidad/ventana y comparten la misma clave por IP de
    confianza."""
    return RateLimitRule(
        TokenBucketRateLimiter(capacity=capacity, refill_per_second=refill_per_second),
        key_of=key_of,
    )


_OAUTH_RATE_LIMITS: tuple[tuple[str, int, float], ...] = (
    (
        _OAUTH_AUTHORIZE_RATE_LIMIT_PREFIX,
        _OAUTH_AUTHORIZE_RATE_LIMIT_CAPACITY,
        _OAUTH_AUTHORIZE_RATE_LIMIT_REFILL_PER_SECOND,
    ),
    (
        _OAUTH_TOKEN_RATE_LIMIT_PREFIX,
        _OAUTH_TOKEN_RATE_LIMIT_CAPACITY,
        _OAUTH_TOKEN_RATE_LIMIT_REFILL_PER_SECOND,
    ),
    (
        _OAUTH_REGISTER_RATE_LIMIT_PREFIX,
        _OAUTH_REGISTER_RATE_LIMIT_CAPACITY,
        _OAUTH_REGISTER_RATE_LIMIT_REFILL_PER_SECOND,
    ),
    (
        _OAUTH_REVOKE_RATE_LIMIT_PREFIX,
        _OAUTH_REVOKE_RATE_LIMIT_CAPACITY,
        _OAUTH_REVOKE_RATE_LIMIT_REFILL_PER_SECOND,
    ),
    (
        _OAUTH_CONSENT_RATE_LIMIT_PREFIX,
        _OAUTH_CONSENT_RATE_LIMIT_CAPACITY,
        _OAUTH_CONSENT_RATE_LIMIT_REFILL_PER_SECOND,
    ),
)

_FEDERATED_LOGIN_RATE_LIMITS: tuple[tuple[str, int, float], ...] = (
    (
        _FEDERATED_START_RATE_LIMIT_PREFIX,
        _FEDERATED_START_RATE_LIMIT_CAPACITY,
        _FEDERATED_START_RATE_LIMIT_REFILL_PER_SECOND,
    ),
    (
        _FEDERATED_CALLBACK_RATE_LIMIT_PREFIX,
        _FEDERATED_CALLBACK_RATE_LIMIT_CAPACITY,
        _FEDERATED_CALLBACK_RATE_LIMIT_REFILL_PER_SECOND,
    ),
    (
        _FEDERATED_STATUS_RATE_LIMIT_PREFIX,
        _FEDERATED_STATUS_RATE_LIMIT_CAPACITY,
        _FEDERATED_STATUS_RATE_LIMIT_REFILL_PER_SECOND,
    ),
)


def _rate_limit_rules(
    forwarded_for_rate_limit_key: Callable[[Scope], str],
) -> dict[str, RateLimitRule]:
    # Las tres reglas federadas van PRIMERO: son un prefijo de
    # `_AUTH_RATE_LIMIT_PREFIX` y `_rule_for` devuelve el primer prefijo del
    # dict que casa (orden de insercion), asi que si fueran despues nunca se
    # alcanzarian.
    rules: dict[str, RateLimitRule] = {
        prefix: _ip_rate_limit(capacity, refill_per_second, key_of=forwarded_for_rate_limit_key)
        for prefix, capacity, refill_per_second in _FEDERATED_LOGIN_RATE_LIMITS
    }
    rules.update(
        {
            _AUTH_RATE_LIMIT_PREFIX: RateLimitRule(
                TokenBucketRateLimiter(
                    capacity=_AUTH_RATE_LIMIT_CAPACITY,
                    refill_per_second=_AUTH_RATE_LIMIT_REFILL_PER_SECOND,
                ),
                key_of=forwarded_for_rate_limit_key,
            ),
            _AUTONOMY_GATE_RATE_LIMIT_PREFIX: RateLimitRule(
                TokenBucketRateLimiter(
                    capacity=_AUTH_RATE_LIMIT_CAPACITY,
                    refill_per_second=_AUTH_RATE_LIMIT_REFILL_PER_SECOND,
                ),
                key_of=forwarded_for_rate_limit_key,
            ),
            _MCP_RATE_LIMIT_PREFIX: RateLimitRule(
                McpRateLimiter(
                    session_limiter=TokenBucketRateLimiter(
                        capacity=_MCP_SESSION_RATE_LIMIT_CAPACITY,
                        refill_per_second=_MCP_SESSION_RATE_LIMIT_REFILL_PER_SECOND,
                    ),
                    session_less_limiter=TokenBucketRateLimiter(
                        capacity=_MCP_SESSION_LESS_RATE_LIMIT_CAPACITY,
                        refill_per_second=_MCP_SESSION_LESS_RATE_LIMIT_REFILL_PER_SECOND,
                    ),
                ),
                key_of=_mcp_rate_limit_key,
            ),
            _BRAND_DISCOVER_RATE_LIMIT_PREFIX: RateLimitRule(
                TokenBucketRateLimiter(
                    capacity=_BRAND_DISCOVER_RATE_LIMIT_CAPACITY,
                    refill_per_second=_BRAND_DISCOVER_RATE_LIMIT_REFILL_PER_SECOND,
                ),
                key_of=_business_id_rate_limit_key,
            ),
            _CONVERSIONS_WEBHOOK_RATE_LIMIT_PREFIX: RateLimitRule(
                TokenBucketRateLimiter(
                    capacity=_CONVERSIONS_WEBHOOK_RATE_LIMIT_CAPACITY,
                    refill_per_second=_CONVERSIONS_WEBHOOK_RATE_LIMIT_REFILL_PER_SECOND,
                ),
                key_of=_webhook_token_rate_limit_key,
            ),
            _CRM_BRIDGE_RATE_LIMIT_PREFIX: RateLimitRule(
                TokenBucketRateLimiter(
                    capacity=_CRM_BRIDGE_RATE_LIMIT_CAPACITY,
                    refill_per_second=_CRM_BRIDGE_RATE_LIMIT_REFILL_PER_SECOND,
                ),
                key_of=_bridge_token_rate_limit_key,
            ),
            _PACKAGES_ORIGIN_GUARDED_PREFIX: RateLimitRule(
                TokenBucketRateLimiter(
                    capacity=_PACKAGES_RATE_LIMIT_CAPACITY,
                    refill_per_second=_PACKAGES_RATE_LIMIT_REFILL_PER_SECOND,
                ),
                key_of=_packages_rate_limit_key,
                methods=_MUTATING_METHODS,
            ),
            _CLOUDFLARE_TOKEN_RATE_LIMIT_PREFIX: RateLimitRule(
                TokenBucketRateLimiter(
                    capacity=_CLOUDFLARE_TOKEN_RATE_LIMIT_CAPACITY,
                    refill_per_second=_CLOUDFLARE_TOKEN_RATE_LIMIT_REFILL_PER_SECOND,
                ),
                key_of=_cloudflare_token_rate_limit_key,
                methods=frozenset({"POST"}),
            ),
        }
    )
    rules.update(
        {
            prefix: _ip_rate_limit(
                capacity, refill_per_second, key_of=forwarded_for_rate_limit_key
            )
            for prefix, capacity, refill_per_second in _OAUTH_RATE_LIMITS
        }
    )
    return rules


async def _handle_api_error(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApiError):
        raise exc
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
        headers={"cache-control": "no-store"},
    )


async def _handle_validation_error(_request: Request, exc: Exception) -> JSONResponse:
    """`RequestValidationError` (revision de seguridad de la conexion
    Cloudflare, 2026-09-15, hallazgo bajo): el manejador por defecto de
    FastAPI devuelve `errors()` tal cual, y cada error trae `input` -- el
    VALOR sometido, no solo el campo. Un token pegado por error en
    `account_id` (`ConnectCloudflareTokenRequest`, forma invalida) se veia
    reflejado en el cuerpo del 422. Mismo criterio que
    `platform_apps_router.py::_validation_error` (campo + mensaje, nunca
    el valor) pero app-wide: cualquier router que valide un `BaseModel`
    como parametro de FastAPI pasa por aqui, no solo Cloudflare."""
    if not isinstance(exc, RequestValidationError):
        raise exc
    errors = [
        {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Los datos enviados no son validos.",
                "details": {"errors": errors},
            }
        },
        headers={"cache-control": "no-store"},
    )


async def _handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    """Red de seguridad en la frontera HTTP (plan.md §8): un fallo de
    infraestructura (DB caida, socket del broker, lo que sea) nunca debe
    devolver una traza ni un mensaje tecnico al cliente."""
    logger.error("api_unhandled_error", error_type=type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "Error interno.", "details": {}}},
    )


# ---------------------------------------------------------------------------
# /api/v1/health/deep (C-26): comprueba lo que este carril posee -- DB,
# socket del broker, migraciones a la cabeza, si Telegram esta configurado
# y cuantas credenciales de plataforma estan degradadas (tasks.md T126,
# threat-model.md C-21). `all_ok` (y el 503) siguen dependiendo solo de
# db/broker a proposito: un esquema desalineado, Telegram ausente o una
# credencial caducada son informativos, no una caida del propio `ads-api`
# -- subirlos a `all_ok` convertiria un smoke F0 sano en 503 por una
# condicion que el propio F1 ya trata como opcional (worker.py: sin
# Telegram, el mensajero registra la entrega como fallida y sigue;
# `CredentialHealthCycle` ya alerta al propietario por su propio canal).
# ---------------------------------------------------------------------------

_ALEMBIC_INI_FILENAME = "alembic.ini"
_ALEMBIC_SCRIPT_LOCATION = "alembic"

# Solo estados terminales (nunca sirven una lectura fiable sin reconectar);
# `expiring_soon` no es un `status` persistido (`credential_refs.status`
# sigue en `CONNECTED` mientras se acerca la caducidad) y queda fuera a
# proposito: es un aviso temprano, no una degradacion todavia.
_SELECT_UNHEALTHY_CREDENTIAL_COUNTS = text("""
    SELECT
        count(*) FILTER (WHERE status IN ('EXPIRED', 'REVOKED', 'INVALID')) AS unhealthy,
        count(*) AS total
    FROM credential_refs
""")


@dataclass(frozen=True, slots=True)
class _CredentialHealthCounts:
    unhealthy: int
    total: int

    @property
    def ok(self) -> bool:
        return self.unhealthy == 0


@dataclass(frozen=True, slots=True)
class _DeepHealthReport:
    db_ok: bool
    broker_ok: bool
    migrations_at_head: bool
    telegram_configured: bool
    credentials: _CredentialHealthCounts
    checked_at: datetime

    @property
    def all_ok(self) -> bool:
        return self.db_ok and self.broker_ok


async def _check_database(container: Container) -> bool:
    try:
        async with container.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - un health check nunca debe propagar
        logger.warning("health_deep_db_unreachable")
        return False
    return True


async def _check_broker_socket(settings: ApiSettings) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path=str(settings.broker_socket_path)),
            timeout=_BROKER_SOCKET_TIMEOUT_SECONDS,
        )
    except (TimeoutError, OSError):
        logger.warning("health_deep_broker_unreachable")
        return False
    writer.close()
    await writer.wait_closed()
    return True


def _alembic_head_revision() -> str | None:
    # Rutas relativas al cwd del proceso (`WORKDIR /app` en el Containerfile,
    # donde `alembic.ini`/`alembic/` se copian junto al codigo), no a
    # `__file__`: el paquete se instala `--no-editable`
    # (Containerfile: "sin el resto del contexto de build, el import
    # rompe"), asi que una ruta relativa al fichero fuente no apuntaria a
    # nada dentro del venv instalado.
    try:
        from alembic.config import Config  # noqa: PLC0415
        from alembic.script import ScriptDirectory  # noqa: PLC0415

        config = Config(_ALEMBIC_INI_FILENAME)
        config.set_main_option("script_location", _ALEMBIC_SCRIPT_LOCATION)
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:  # noqa: BLE001 - un health check nunca debe propagar
        logger.warning("health_deep_alembic_head_unreadable")
        return None


async def _check_migrations_at_head(container: Container) -> bool:
    head = _alembic_head_revision()
    if head is None:
        return False
    try:
        async with container.engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            current = result.scalar_one_or_none()
    except Exception:  # noqa: BLE001 - un health check nunca debe propagar
        logger.warning("health_deep_alembic_version_unreadable")
        return False
    return current == head


def _telegram_configured(settings: ApiSettings) -> bool:
    return bool(settings.telegram_bot_token.get_secret_value()) and bool(
        settings.telegram_owner_chat_ids
    )


async def _check_credentials(container: Container) -> _CredentialHealthCounts:
    try:
        async with container.engine.connect() as connection:
            row = (await connection.execute(_SELECT_UNHEALTHY_CREDENTIAL_COUNTS)).one()
    except Exception:  # noqa: BLE001 - un health check nunca debe propagar
        logger.warning("health_deep_credentials_unreadable")
        return _CredentialHealthCounts(unhealthy=0, total=0)
    return _CredentialHealthCounts(unhealthy=row.unhealthy, total=row.total)


def _mount_health_deep(app: FastAPI, settings: ApiSettings, container: Container) -> None:
    @app.get("/api/v1/health/deep", include_in_schema=True)
    async def health_deep(_owner: object = CURRENT_OWNER) -> JSONResponse:
        db_ok, broker_ok, migrations_at_head, credentials = await asyncio.gather(
            _check_database(container),
            _check_broker_socket(settings),
            _check_migrations_at_head(container),
            _check_credentials(container),
        )
        report = _DeepHealthReport(
            db_ok=db_ok,
            broker_ok=broker_ok,
            migrations_at_head=migrations_at_head,
            telegram_configured=_telegram_configured(settings),
            credentials=credentials,
            checked_at=datetime.now(UTC),
        )
        body = {
            "db": "ok" if report.db_ok else "error",
            "broker": "ok" if report.broker_ok else "error",
            "migrations_at_head": report.migrations_at_head,
            "telegram": "ok" if report.telegram_configured else "not_configured",
            "credentials": {
                "status": "ok" if report.credentials.ok else "degraded",
                "unhealthy": report.credentials.unhealthy,
                "total": report.credentials.total,
            },
            "checked_at": report.checked_at.isoformat(),
        }
        return JSONResponse(body, status_code=200 if report.all_ok else 503)


# ---------------------------------------------------------------------------
# Cabeceras de seguridad (C-26): CSP sin unsafe-inline/unsafe-eval, sin
# cache de contenido sensible entre origenes.
# ---------------------------------------------------------------------------


def _inject_headers(send: Send, extra_headers: tuple[tuple[str, str], ...]) -> Send:
    async def wrapped_send(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            headers.extend((name.encode(), value.encode()) for name, value in extra_headers)
            message["headers"] = headers
        await send(message)

    return wrapped_send


class RequestIdMiddleware:
    """`request_id` por peticion en `structlog.contextvars` (T127): un
    UUID nuevo en cada peticion HTTP, limpiado al terminar para que no se
    filtre a la siguiente peticion que reutilice el mismo hilo/tarea de
    `asyncio` (`clear_contextvars` en el `finally`, mismo patron que
    `structlog` documenta para servidores ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        structlog.contextvars.bind_contextvars(request_id=str(uuid.uuid4()))
        try:
            await self._app(scope, receive, send)
        finally:
            structlog.contextvars.clear_contextvars()


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, headers: tuple[tuple[str, str], ...]) -> None:
        self._app = app
        self._headers = headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        await self._app(scope, receive, _inject_headers(send, self._headers))


class ForwardedPrefixMiddleware:
    """`X-Forwarded-Prefix` -> `scope["root_path"]` (026, contracts/sso.md
    §5/§7 T-1). Fuera de modo companion la cabecera se ignora por completo
    (nunca se confía en un prefijo que nadie validó aguas arriba); dentro,
    solo se acepta si está en la allow-list -- cualquier otro valor
    default-deny a `""`, nunca se propaga tal cual."""

    def __init__(self, app: ASGIApp, *, companion_mode: bool) -> None:
        self._app = app
        self._companion_mode = companion_mode

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        scope["root_path"] = self._resolved_prefix(scope) if self._companion_mode else ""
        await self._app(scope, receive, send)

    def _resolved_prefix(self, scope: Scope) -> str:
        headers: dict[bytes, bytes] = dict(scope.get("headers", []))
        raw = headers.get(b"x-forwarded-prefix")
        value = raw.decode("latin-1") if raw is not None else ""
        return value if value in _ALLOWED_FORWARDED_PREFIXES else ""


# ---------------------------------------------------------------------------
# CSRF de doble envio (C-26): cookie `ads_csrf` (no HttpOnly, JS la lee y la
# devuelve) comparada en tiempo constante contra `X-CSRF-Token` en toda
# peticion mutante. `/mcp` queda fuera: se autentica con bearer, no con
# cookies, asi que no hay superficie CSRF que proteger ahi.
#
# M4 de la revision de seguridad (16-sep, threat-model.md C-40): el doble
# envio por si solo no basta contra un atacante que puede leer la cookie
# `ads_csrf` (XSS en otro origen, extension maliciosa, red compartida sin
# `Secure` real) -- se comprueba primero que la peticion sea del MISMO
# origen via `Sec-Fetch-Site` (lo manda todo navegador moderno) y, si el
# cliente no lo manda, via `Origin` contra `ApiSettings.public_base_url`.
#
# AL-7 (threat-model.md #364): `/api/v1/packages/**` ademas comprueba
# `Origin`/`Sec-Fetch-Site` contra la lista explicita de origenes del panel
# -- esa superficie alcanza una escritura real de plataforma (`undo`
# revierte una campana ya publicada), asi que ni el doble envio ni la
# comprobacion global de arriba bastan solos.
# ---------------------------------------------------------------------------


def _csrf_rejected_response() -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "CSRF_REJECTED", "message": "Token CSRF ausente o invalido."}},
        status_code=403,
    )


def _origin_required_rejected_response() -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": "CSRF_ORIGIN_REQUIRED",
                "message": "Origin o Sec-Fetch-Site requeridos junto a la cookie de sesion.",
            }
        },
        status_code=403,
    )


def _new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _is_csrf_exempt(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _CSRF_EXEMPT_PREFIXES)


def _csrf_token_valid(request: Request) -> bool:
    cookie_token = request.cookies.get(_CSRF_COOKIE_NAME)
    header_token = request.headers.get(_CSRF_HEADER_NAME)
    if not cookie_token or not header_token:
        return False
    return hmac.compare_digest(cookie_token, header_token)


_SAME_ORIGIN_SEC_FETCH_SITE_VALUES = frozenset({"same-origin", "none"})


def _is_cross_site_request(request: Request) -> bool:
    """RFC del `Fetch Metadata Request Headers`: todo navegador moderno
    manda `Sec-Fetch-Site`; `same-origin` es la propia SPA, `none` es
    navegacion directa (barra de direcciones, marcador) -- cualquier otro
    valor (`cross-site`, `same-site`) es una peticion iniciada por OTRO
    origen, exactamente el vector que el doble envio de cookie por si solo
    no cubre si la cookie se filtra."""
    sec_fetch_site = request.headers.get("sec-fetch-site")
    return sec_fetch_site is not None and sec_fetch_site not in _SAME_ORIGIN_SEC_FETCH_SITE_VALUES


_DEFAULT_PORT_BY_SCHEME = {"http": 80, "https": 443}


def _origin_port(parsed: SplitResult) -> int | None:
    return parsed.port if parsed.port is not None else _DEFAULT_PORT_BY_SCHEME.get(parsed.scheme)


def _same_origin(origin: str, public_base_url: str) -> bool:
    """Compara esquema + host + puerto ya normalizado (`:443` explicito
    en `Origin` es el MISMO origen que `https://host` sin puerto) -- una
    comparacion de cadena exacta (la version anterior a esta nit) rechaza
    ese `Origin`, con falsos positivos reales: un proxy o un navegador que
    SI incluye el puerto por defecto. `Origin` es una cabecera de cliente
    sin validar (trust boundary): un valor que `urlsplit`/`.port` no puede
    parsear (p.ej. un puerto no numerico) se trata como mismatch, nunca
    como un 500."""
    try:
        parsed_origin, parsed_base = urlsplit(origin), urlsplit(public_base_url)
        return (
            parsed_origin.scheme == parsed_base.scheme
            and parsed_origin.hostname == parsed_base.hostname
            and _origin_port(parsed_origin) == _origin_port(parsed_base)
        )
    except ValueError:
        return False


def _is_origin_mismatch(request: Request, allowed_origins: frozenset[str]) -> bool:
    """Respaldo para clientes que no mandan `Sec-Fetch-Site` (navegadores
    viejos): si mandan `Origin`, debe coincidir (esquema+host+puerto,
    `_same_origin`) con alguno de los origenes legitimos del panel. Sin
    ninguna de las dos cabeceras, esta comprobacion no aplica -- el doble
    envio de cookie sigue siendo la unica defensa, igual que antes de M4.

    Fusion lane/003: se compara contra el conjunto de `_allowed_origins_for`
    (origen publico + loopback + `ADS_MCP_EXTRA_ALLOWED_HOSTS`), no solo
    contra `public_base_url` -- el panel real servido por un host
    provisional del mismo Caddy no puede auto-rechazarse. Un conjunto
    VACIO significa "sin origenes configurados" (montajes sueltos de
    `CsrfMiddleware` en tests de router): ahi esta comprobacion no aplica;
    `harden_api` siempre pasa el conjunto real."""
    origin = request.headers.get("origin")
    if origin is None or not allowed_origins:
        return False
    return not any(_same_origin(origin, allowed) for allowed in allowed_origins)


def _allowed_origins_for(
    public_base_url: str, *, extra_allowed_hosts: frozenset[str] = frozenset()
) -> frozenset[str]:
    """AL-7: origenes que un navegador legitimo puede declarar para el
    panel real -- mismo criterio que `mcp/presentation/http.py::
    _transport_security_for` (localhost incluido para desarrollo/tests
    locales, nunca en lugar del origen publico real).

    B-4 (revision de seguridad 0.2.22): `extra_allowed_hosts`
    (`ADS_MCP_EXTRA_ALLOWED_HOSTS`, settings.py) cubre el mismo host
    provisional servido por el Caddy delante de `/mcp` -- sin esto, el
    panel real que llega por ese host se auto-rechaza en toda mutacion de
    `/api/v1/packages/**`. Siempre `https://`, igual que en
    `_transport_security_for`."""
    return frozenset(
        {public_base_url, "https://127.0.0.1", "http://127.0.0.1"}
        | {f"https://{host}" for host in extra_allowed_hosts}
    )


def _requires_origin_guard(path: str) -> bool:
    return path.startswith(_PACKAGES_ORIGIN_GUARDED_PREFIX)


def _origin_guard_rejected(request: Request, allowed_origins: frozenset[str]) -> bool:
    """AL-7: `Origin` ausente rechaza siempre (T126, literal); cuando el
    navegador manda `Sec-Fetch-Site` (Fetch Metadata, mayoria de
    navegadores modernos) un valor `cross-site` rechaza tambien, incluso
    si alguien logra falsificar un `Origin` de la lista -- las dos
    cabeceras se comprueban, ninguna sustituye a la otra."""
    origin = request.headers.get("origin")
    if not origin or origin not in allowed_origins:
        return True
    return request.headers.get("sec-fetch-site") == "cross-site"


def _is_session_cookie_replay_without_origin(request: Request) -> bool:
    """AL-7, endurecimiento revision de seguridad 0.2.22: un replay
    no-navegador de la cookie de sesion (`ads_session`, robada por ejemplo
    via XSS y reproducida con curl/un script, nunca desde un navegador
    real) no manda NI `Origin` NI `Sec-Fetch-Site` -- un navegador
    legitimo manda al menos uno de los dos en toda peticion mutante
    moderna. Bearer/MCP (sin cookie de sesion, contracts/mcp.md §1) nunca
    dispara esta condicion -- solo mira la cookie de sesion, no el
    `ads_csrf` de doble envio."""
    if SESSION_COOKIE_NAME not in request.cookies:
        return False
    return not request.headers.get("origin") and not request.headers.get("sec-fetch-site")


class CsrfMiddleware:
    def __init__(self, app: ASGIApp, *, allowed_origins: frozenset[str] = frozenset()) -> None:
        self._app = app
        self._allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        if self._rejects(request):
            await _csrf_rejected_response()(scope, receive, send)
            return

        send = self._ensure_csrf_cookie(request, send)
        await self._app(scope, receive, send)

    def _rejects(self, request: Request) -> bool:
        """M4 (global, todas las mutaciones del panel) + AL-7 (capa extra
        sobre `/api/v1/packages/**`): las dos comprobaciones conviven, ni
        una sustituye a la otra."""
        requires_check = request.method in _MUTATING_METHODS and not _is_csrf_exempt(
            request.url.path
        )
        if not requires_check:
            return False
        if _is_cross_site_request(request):
            return True
        if _is_origin_mismatch(request, self._allowed_origins):
            return True
        if not _csrf_token_valid(request):
            return True
        return _requires_origin_guard(request.url.path) and _origin_guard_rejected(
            request, self._allowed_origins
        )

    def _ensure_csrf_cookie(self, request: Request, send: Send) -> Send:
        if request.cookies.get(_CSRF_COOKIE_NAME):
            return send

        token = _new_csrf_token()
        cookie_header = f"{_CSRF_COOKIE_NAME}={token}; Path=/; Secure; SameSite=Strict"
        return _inject_headers(send, (("set-cookie", cookie_header),))


class PackagesOriginRequiredMiddleware:
    """AL-7, revision de seguridad 0.2.22: capa independiente del doble
    envio de `CsrfMiddleware` para `/api/v1/packages/**` -- protege esa
    superficie aunque `CsrfMiddleware` nunca llegara a evaluarse (p.ej. un
    router montado suelto, sin `harden_api` de por medio, como en pruebas
    de integracion del propio router). En produccion se monta junto a
    `CsrfMiddleware` (`harden_api`), nunca en su lugar: la complementa, no
    la sustituye."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        rejects = (
            request.method in _MUTATING_METHODS
            and _requires_origin_guard(request.url.path)
            and _is_session_cookie_replay_without_origin(request)
        )
        if rejects:
            await _origin_required_rejected_response()(scope, receive, send)
            return
        await self._app(scope, receive, send)


# ---------------------------------------------------------------------------
# Limite de tasa en proceso (C-23): cubeta de tokens por clave (ip o bearer),
# sin dependencias externas -- un unico proceso `ads-api`, sin necesidad de
# Redis para este alcance.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Bucket:
    tokens: float
    last_refill_at: float


# threat-model.md C-51: sin tope, un atacante que rota su IP/bearer en cada
# peticion hace crecer `_buckets` sin limite (agotamiento de memoria del
# proceso, nunca liberado). 10 000 cubetas es una cota generosa para el
# trafico real de una sola instalacion.
_MAX_RATE_LIMIT_BUCKETS = 10_000


def _hashed_rate_limit_key(key: str) -> str:
    """threat-model.md C-51: la clave cruda (IP o bearer/estatico) nunca se
    guarda en el diccionario en memoria -- solo su sha256. No es secreto
    (una IP no lo es), pero evita que un volcado de memoria del proceso
    muestre bearers en claro si alguna regla llega a usar uno como clave."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class TokenBucketRateLimiter:
    def __init__(self, *, capacity: int, refill_per_second: float) -> None:
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        # `OrderedDict` mueve al final la clave que se toca (`move_to_end`):
        # el LRU es "menos usada recientemente", no "mas antigua por orden
        # de llegada" -- una IP que sigue llamando nunca se convierte en la
        # candidata a expulsar solo por haber llegado primero.
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def allow(self, key: str) -> bool:
        hashed_key = _hashed_rate_limit_key(key)
        now = time.monotonic()
        bucket = self._buckets.get(hashed_key)
        if bucket is None:
            bucket = _Bucket(tokens=self._capacity, last_refill_at=now)
            self._evict_oldest_if_full()
            self._buckets[hashed_key] = bucket
        else:
            self._buckets.move_to_end(hashed_key)
        elapsed = now - bucket.last_refill_at
        bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_per_second)
        bucket.last_refill_at = now
        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True

    def _evict_oldest_if_full(self) -> None:
        if len(self._buckets) >= _MAX_RATE_LIMIT_BUCKETS:
            self._buckets.popitem(last=False)


class RateLimiter(Protocol):
    """Abstraccion minima que exige `RateLimitRule`: cualquier cosa que
    decida admitir/rechazar una clave. `TokenBucketRateLimiter` (una
    cubeta) y `McpRateLimiter` (dos cubetas, una por presupuesto) la
    cumplen sin heredar de nada -- `RateLimitMiddleware` depende de esto,
    no de una implementacion concreta."""

    def allow(self, key: str) -> bool: ...


_MCP_SESSION_KEY_PREFIX = "session:"
_MCP_SESSION_LESS_KEY_PREFIX = "anon:"


class McpRateLimiter:
    """Particiona `/mcp` en dos presupuestos (ver constantes
    `_MCP_SESSION_RATE_LIMIT_*`/`_MCP_SESSION_LESS_RATE_LIMIT_*`): una
    cubeta de 600/min por sesion MCP y una de 60/min para lo que todavia
    no tiene sesion. La clave (`_mcp_rate_limit_key`) ya trae el prefijo
    que dice a cual de las dos pertenece, asi que agotar la sesion A nunca
    toca la cubeta de la sesion B ni la de `initialize`."""

    def __init__(
        self,
        *,
        session_limiter: TokenBucketRateLimiter,
        session_less_limiter: TokenBucketRateLimiter,
    ) -> None:
        self._session_limiter = session_limiter
        self._session_less_limiter = session_less_limiter

    def allow(self, key: str) -> bool:
        limiter = (
            self._session_limiter
            if key.startswith(_MCP_SESSION_KEY_PREFIX)
            else self._session_less_limiter
        )
        return limiter.allow(key)


def _rate_limited_response() -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "RATE_LIMITED", "message": "Demasiadas peticiones."}},
        status_code=429,
        headers={"retry-after": "60"},
    )


def _rate_limit_key(scope: Scope) -> str:
    headers: dict[bytes, bytes] = dict(scope.get("headers", []))
    auth = headers.get(b"authorization")
    if auth:
        return auth.decode("latin-1")
    client: tuple[str, int] | None = scope.get("client")
    return client[0] if client else "unknown"


def _mcp_rate_limit_key(scope: Scope) -> str:
    """Particion del limitador de `/mcp`: sesion MCP (`Mcp-Session-Id`,
    cabecera que el cliente repite en cada llamada una vez que `initialize`
    se la ha dado) si ya la trae, bearer/IP (`_rate_limit_key`) si no --
    solo `initialize` (y cualquier llamada malformada sin sesion) cae en
    el presupuesto sin sesion."""
    headers: dict[bytes, bytes] = dict(scope.get("headers", []))
    session_id = headers.get(b"mcp-session-id")
    if session_id:
        return f"{_MCP_SESSION_KEY_PREFIX}{session_id.decode('latin-1')}"
    return f"{_MCP_SESSION_LESS_KEY_PREFIX}{_rate_limit_key(scope)}"


def _webhook_token_rate_limit_key(scope: Scope) -> str:
    """T220: clave por token de webhook presentado (`X-Webhook-Token`), IP
    si falta -- el llamante no trae sesion, asi que `_rate_limit_key`
    (bearer/IP) no encaja."""
    headers: dict[bytes, bytes] = dict(scope.get("headers", []))
    token = headers.get(b"x-webhook-token")
    if token:
        return token.decode("latin-1")
    client: tuple[str, int] | None = scope.get("client")
    return client[0] if client else "unknown"


def _bridge_token_rate_limit_key(scope: Scope) -> str:
    """Spec 027: clave por `X-Bridge-Token` presentado, IP si falta --
    mismo criterio que `_webhook_token_rate_limit_key`."""
    headers: dict[bytes, bytes] = dict(scope.get("headers", []))
    token = headers.get(b"x-bridge-token")
    if token:
        return token.decode("latin-1")
    client: tuple[str, int] | None = scope.get("client")
    return client[0] if client else "unknown"


def _packages_rate_limit_key(scope: Scope) -> str:
    """T126/AL-7: clave por SESION (cookie `ads_session`), IP si falta --
    `/api/v1/packages/**` siempre llega con esa cookie en un uso legitimo
    (`PackagesOriginRequiredMiddleware`/`CsrfMiddleware` ya lo asumen);
    bearer/MCP no pasa por este prefijo."""
    headers: dict[bytes, bytes] = dict(scope.get("headers", []))
    cookie_header = headers.get(b"cookie", b"").decode("latin-1")
    session_token = _cookie_value(cookie_header, SESSION_COOKIE_NAME)
    if session_token:
        return session_token
    client: tuple[str, int] | None = scope.get("client")
    return client[0] if client else "unknown"


def _cloudflare_token_rate_limit_key(scope: Scope) -> str:
    """`POST /api/v1/integrations/cloudflare/token`: clave por SESION
    (cookie `ads_session`), IP si falta -- mismo criterio que
    `_packages_rate_limit_key`, esta ruta tambien exige sesion + CSRF de
    propietario, nunca bearer/MCP."""
    return _packages_rate_limit_key(scope)


def _cookie_value(cookie_header: str, name: str) -> str | None:
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(cookie_header)
    morsel = cookie.get(name)
    return morsel.value if morsel else None


def _business_id_rate_limit_key(scope: Scope) -> str:
    """F-7: `/brand/discover` dispara N salidas de red por peticion --
    limitarlo por `business_id` (no por sesion/IP) para que el negocio en
    si nunca reciba mas de `capacity` rastreos en la ventana, sin importar
    cuantas sesiones distintas del propietario lo pidan."""
    query_string = scope.get("query_string", b"")
    params = parse_qs(query_string.decode("latin-1"))
    business_ids = params.get("business_id")
    return business_ids[0] if business_ids else "unknown"


def _make_forwarded_for_rate_limit_key(trusted_proxy_hops: int) -> Callable[[Scope], str]:
    """Spec 002 (mcp_oauth) tasks.md T014, threat-model.md C-42/C-51: los
    endpoints del AS no tienen sesion ni bearer que limitar por -- IP.

    H1 de la revision de seguridad (16-sep): el reverse proxy real varia
    por despliegue (Caddy delante del host de `ADS_PUBLIC_BASE_URL`, o
    `tailscale serve`/`tailscale funnel`); ambos anaden exactamente un
    salto de confianza. Sin ningun proxy de confianza delante
    (`trusted_proxy_hops=0`, el valor de `compose.companion.yaml`, donde
    Safent conecta directo), `X-Forwarded-For` NUNCA se lee -- cualquier
    cliente puede escribir esa cabecera, y sin un proxy que la sanee un
    atacante directo se saltaria el limite de tasa suplantando IPs
    distintas en cada peticion.

    Code review 17-sep: delega en `shared/net/client_ip.py` (UN solo lugar,
    nunca copiado) para el salto de confianza -- `iam/presentation/
    {router,reauth,federated_router}.py` resuelven la misma pregunta sobre
    `fastapi.Request` con la misma funcion pura. `"unknown"` es solo la
    clave de cubeta cuando ninguna IP valida se pudo resolver -- nunca
    llega a una columna de base de datos, a diferencia del literal que
    `_client_ip` de presentacion evitaba usar aqui."""

    def key_of(scope: Scope) -> str:
        headers: dict[bytes, bytes] = dict(scope.get("headers", []))
        forwarded_for = headers.get(b"x-forwarded-for")
        client: tuple[str, int] | None = scope.get("client")
        resolved = resolve_client_ip(
            forwarded_for=forwarded_for.decode("latin-1") if forwarded_for else None,
            peer_ip=client[0] if client else None,
            trusted_proxy_hops=trusted_proxy_hops,
        )
        return resolved if resolved is not None else "unknown"

    return key_of


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """Une un `RateLimiter` con la clave que lo particiona -- `/auth` por
    sesion/IP (`_rate_limit_key`, valor por defecto), `/mcp` por sesion MCP
    o bearer/IP (`_mcp_rate_limit_key`), `/brand/discover` por
    `business_id` (F-7).

    `methods` (T126/AL-7, revision de seguridad 0.2.22): `None` (defecto)
    limita TODOS los metodos, igual que antes de este campo -- ningun
    limitador existente cambia de comportamiento. `/api/v1/packages/**`
    es el primero en necesitar un subconjunto: `build_package_read_router`
    comparte el mismo prefijo que las mutaciones, y una lectura del panel
    (poll cada pocos segundos) no debe compartir cupo con `approve`/
    `undo`."""

    limiter: RateLimiter
    key_of: Callable[[Scope], str] = _rate_limit_key
    methods: frozenset[str] | None = None

    def applies_to(self, scope: Scope) -> bool:
        return self.methods is None or scope.get("method") in self.methods


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, *, limiters: dict[str, RateLimitRule]) -> None:
        self._app = app
        self._limiters = limiters

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        rule = self._rule_for(path)
        if rule is not None and rule.applies_to(scope) and not rule.limiter.allow(
            rule.key_of(scope)
        ):
            await _rate_limited_response()(scope, receive, send)
            return

        await self._app(scope, receive, send)

    def _rule_for(self, path: str) -> RateLimitRule | None:
        for prefix, rule in self._limiters.items():
            if path.startswith(prefix):
                return rule
        return None
