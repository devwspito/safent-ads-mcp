# syntax=docker/dockerfile:1.7
# =============================================================================
# safent-ads — imagen unica compartida por ads-api, ads-worker y ads-broker
# (compose.yaml). Las tres corren el mismo codigo; lo que cambia es el
# comando, el usuario y que secretos monta cada una (plan.md S3, S9). El
# aislamiento de credenciales de plataforma no viene de dependencias
# distintas por imagen -- viene de que solo ads-broker recibe
# secrets/broker.env y de que api/worker nunca importan
# safent_ads.broker.infrastructure.platforms.* (revisado en code review, no
# solo en tiempo de build).
# =============================================================================

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.11@sha256:3472e43b4e738cf911c99d41bb34331280efad54c73b1def654a6227bb59b2b4 /uv /uvx /usr/local/bin/

# WORKDIR es /app aqui tambien (no /build): los scripts de consola que uv
# genera (alembic, uvicorn...) llevan un shebang con la ruta absoluta del
# venv grabada en tiempo de instalacion. Si el venv se crea en /build y la
# etapa runtime lo copia a /app, el shebang sigue apuntando a
# /build/.venv/bin/python -> ENOENT al exec. Mismo path en ambas etapas.
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
# --no-editable: una instalacion editable enlaza el paquete a esta etapa de
# build por ruta; en runtime, sin el resto del contexto de build, el import
# de safent_ads rompe con ModuleNotFoundError.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# -----------------------------------------------------------------------------
# Stage 2: panel-builder — compila el SPA (React/Vite, `panel/`) aparte del
# backend. `panel/package.json` no declara "engines" y no hay `.nvmrc`: LTS
# fijado a mano por tag, mismo `node-version` que el job `panel` de
# .github/workflows/ci.yml para que build local y CI compilen con el mismo
# Node. Esta etapa nunca llega a runtime -- solo su `dist/` de salida.
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS native-mcp-builder
COPY --from=ghcr.io/astral-sh/uv:0.10.11@sha256:3472e43b4e738cf911c99d41bb34331280efad54c73b1def654a6227bb59b2b4 /uv /usr/local/bin/
COPY infra/native-mcp/requirements.lock /build/requirements.lock
RUN uv venv /opt/google-ads-mcp && \
    uv pip sync --python /opt/google-ads-mcp/bin/python --require-hashes /build/requirements.lock

FROM node:26-bookworm-slim@sha256:cd9f682fa2885cd1056e830424764158570061c59736a1da836bc3d73df095ae AS panel-builder

WORKDIR /panel
# package.json + lockfile primero: cache de `npm ci` invalida solo cuando
# cambian dependencias, no en cada cambio de fuente del panel.
COPY panel/package.json panel/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --ignore-scripts
COPY panel/ ./
# `npm run build` = `tsc --noEmit && vite build` (panel/package.json):
# typecheck y build en el mismo paso, salida en panel/dist (default de Vite).
RUN npm run build

# -----------------------------------------------------------------------------
# Stage 3: runtime — sin build-essential, sin uv, sin node, dos usuarios sin privilegios.
# adsapi=10001 coincide con el ADS_BROKER_ALLOWED_UIDS por defecto de
# .env.example: es la identidad que el broker deja pasar por el socket.
# adsbroker=10002 es la identidad propia del broker, distinta a proposito.
#
# ads-broker-clients=10003 es un grupo suplementario compartido, no una
# tercera identidad: el socket del broker no puede ser 0600 propiedad de
# adsbroker (adsapi/adsworker no podrian ni conectar) ni 0666 (cualquier
# proceso del host que alcance el volumen podria). 0660 + este grupo es el
# permiso minimo que deja pasar al cliente legitimo; SO_PEERCRED contra
# ADS_BROKER_ALLOWED_UIDS (composition/broker.py) sigue siendo la
# autorizacion real, esto es solo la primera puerta del filesystem. Compose
# activa el grupo con `group_add`, porque `user: uid:gid` no aplica grupos
# suplementarios de /etc/group por si solo.
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS runtime

RUN groupadd --gid 10001 adsapi \
    && useradd --uid 10001 --gid adsapi --no-create-home --shell /usr/sbin/nologin adsapi \
    && groupadd --gid 10002 adsbroker \
    && useradd --uid 10002 --gid adsbroker --no-create-home --shell /usr/sbin/nologin adsbroker \
    && groupadd --gid 10003 ads-broker-clients \
    && usermod -aG ads-broker-clients adsapi \
    && usermod -aG ads-broker-clients adsbroker \
    && mkdir -p /run/ads-broker \
    && chown adsbroker:adsbroker /run/ads-broker \
    # ApiSettings.brand_asset_storage_dir defaults to the relative
    # "data/brand-assets" (composition/settings.py) so LocalBrandAssetStorage
    # resolves it under WORKDIR /app on every deploy that doesn't override
    # ADS_BRAND_ASSET_STORAGE_DIR. Without this, ads-api crashes on every
    # boot (PermissionError creating /app/data) because WORKDIR /app is
    # created root-owned before the --chown COPYs below, and those COPYs
    # only chown the paths they copy, never /app itself.
    && mkdir -p /app/data \
    && chown adsapi:adsapi /app/data \
    # credential_store.py mkdir's ADS_CREDENTIAL_STORE_DIR under
    # /var/lib/ads-broker on every boot; compose.yaml mounts a named volume
    # there (ads-broker runs read_only: true) -- pre-creating it owned by
    # adsbroker here means Docker seeds the new volume with this ownership
    # on first mount instead of the root:root an empty volume gets by default.
    && mkdir -p /var/lib/ads-broker \
    && chown adsbroker:adsbroker /var/lib/ads-broker

WORKDIR /app
COPY --from=builder --chown=adsapi:adsapi /app/.venv /app/.venv
COPY --from=native-mcp-builder /opt/google-ads-mcp /opt/google-ads-mcp
COPY --from=builder --chown=adsapi:adsapi /app/src /app/src
COPY --from=builder --chown=adsapi:adsapi /app/alembic /app/alembic
COPY --from=builder --chown=adsapi:adsapi /app/alembic.ini /app/alembic.ini
# `ApiSettings.panel_dist_dir` (composition/settings.py) por defecto es la
# ruta relativa "panel/dist", resuelta contra WORKDIR /app -- mismo destino
# que `panel-builder` produce en /panel/dist. Sin esto, `_mount_panel_spa`
# (composition/app.py) loguea `panel_dist_not_found` y no sirve UI alguna.
COPY --from=panel-builder --chown=adsapi:adsapi /panel/dist /app/panel/dist

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Sin usuario por defecto: cada servicio de compose.yaml fija `user:`
# (adsapi para ads-api/ads-worker, adsbroker para ads-broker).
# `composition/app.py:main()` decide host/puerto/TLS por si mismo (plano
# 127.0.0.1:8410 salvo `ADS_COMPANION_MODE=true`, entonces 0.0.0.0:8443
# con TLS) -- por eso ya no hay `--host`/`--port` fijos aqui.
CMD ["python", "-m", "safent_ads.composition.app"]
