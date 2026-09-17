#!/usr/bin/env bash
# scripts/acceptance_mcp.sh — kit de aceptación 004 (P1) contra el MCP de
# anuncios YA DESPLEGADO.
#
# Envoltorio fino: valida que las dos credenciales y los binarios lleguen,
# y delega toda la lógica en `scripts/acceptance_mcp.py` (caja negra pura
# sobre el SDK MCP + `claude`/`codex` reales; nunca importa `safent_ads`
# ni toca `src/`). El procedimiento completo -- cómo emitir las
# credenciales, qué mirar en el panel después -- está en el quickstart de
# la instalación.
#
# Variables de entorno:
#   MCP_URL                          obligatoria, sin defecto de cliente:
#                                     URL completa del endpoint `/mcp`.
#   ADS_ACCEPTANCE_TOKEN_VER         credencial de puesto "ver" (obligatoria)
#   ADS_ACCEPTANCE_TOKEN_PROPONER    credencial de puesto "proponer" (obligatoria)
#   ADS_ACCEPTANCE_SERVER_NAME       opcional, por defecto `safent-ads`; nombre
#                                     de registro en Claude Code/Codex.
#   ANTHROPIC_API_KEY                opcional; habilita el prompt headless de
#                                     Claude Code (§5 de acceptance_mcp.py). Sin
#                                     ella, ese único paso se marca SKIP.
#
# Ninguna credencial se imprime nunca ni se escribe a fichero, salvo los
# propios almacenes efímeros de `claude`/`codex` en sus homes aislados
# (`/tmp/<ADS_ACCEPTANCE_SERVER_NAME>-acceptance/{claude,codex}-home`), que
# el propio script de Python borra al terminar, con éxito o sin él.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

if [[ -z "${MCP_URL:-}" ]]; then
  echo "ERROR: falta MCP_URL (URL completa del endpoint /mcp, sin defecto)" >&2
  exit 2
fi
export MCP_URL
if [[ -z "${ADS_ACCEPTANCE_TOKEN_VER:-}" ]]; then
  echo "ERROR: falta ADS_ACCEPTANCE_TOKEN_VER (credencial de puesto 'ver')" >&2
  exit 2
fi
if [[ -z "${ADS_ACCEPTANCE_TOKEN_PROPONER:-}" ]]; then
  echo "ERROR: falta ADS_ACCEPTANCE_TOKEN_PROPONER (credencial de puesto 'proponer')" >&2
  exit 2
fi

for bin in claude codex uv; do
  if ! command -v "${bin}" >/dev/null 2>&1; then
    echo "ERROR: '${bin}' no está en el PATH" >&2
    exit 2
  fi
done

echo "Kit de aceptación 004 (P1) contra ${MCP_URL}"
exec uv run --frozen python scripts/acceptance_mcp.py
