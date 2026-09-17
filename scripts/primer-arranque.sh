#!/usr/bin/env bash
# Primer arranque de un comando (008-mcp-ads-estandar T019;
# contracts/first-run-cli.md). Equivalente a `make first-run`.
#
# Este script es el ÚNICO que habla docker: preflight, imagen y arranque de
# la pila (pasos 1 y 7 del contrato). Todo lo demás —preguntas, secretos,
# alta del dueño y verificación— lo hace `safent_ads.tools.first_run`
# DENTRO de la imagen, que es donde vive la criptografía y donde ningún
# secreto necesita pasar por el shell del operador.
#
# Uso:
#   ./scripts/primer-arranque.sh
#   ./scripts/primer-arranque.sh --dry-run
#   ./scripts/primer-arranque.sh --public-base-url https://ads.example.com --no-composio
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGEN="safent-ads:local"
# Puerto que `compose.yaml` publica para ads-api. Copia deliberada: leerlo
# aquí exigiría `jq` o un `docker compose config` que el preflight todavía
# no ha validado. Que las dos copias no diverjan lo vigila
# `tests/unit/tools/test_first_run.py::TestWrapper::test_the_wrapper_watches_the_port_that_compose_publishes`.
PUERTO_API=8410

fallo_preflight() { echo "ERROR: $*" >&2; exit 2; }

# `--dry-run` no escribe nada, así que tampoco construye una imagen (varios
# minutos). `--skip-start` sí escribe: necesita la imagen igual que una
# pasada normal, pero no levanta la pila.
SOLO_FICHEROS=0
SIN_EFECTOS=0
for argumento in "$@"; do
  case "$argumento" in
    --dry-run) SIN_EFECTOS=1; SOLO_FICHEROS=1 ;;
    --skip-start) SOLO_FICHEROS=1 ;;
  esac
done

command -v docker >/dev/null 2>&1 || fallo_preflight "docker no está instalado."
docker compose version >/dev/null 2>&1 || fallo_preflight "falta el plugin 'docker compose'."
docker info >/dev/null 2>&1 || fallo_preflight "docker no responde (¿está parado?)."

if [ ! -f .env ]; then
  # `.env` todavía no existe y `compose.yaml` exige POSTGRES_USER/PASSWORD
  # para interpolar. Estos dos valores solo satisfacen esa interpolación
  # mientras se generan los ficheros: `ads-db` no se levanta con ellos
  # (`--no-deps`) y se olvidan antes de arrancar nada.
  export POSTGRES_USER=primer-arranque POSTGRES_PASSWORD=primer-arranque
fi

docker compose config -q >/dev/null 2>&1 \
  || fallo_preflight "compose.yaml no es válido aquí; mira el detalle con: docker compose config"

# Puerto publicado por ads-api (compose.yaml). Si lo tiene nuestra propia
# pila, es un segundo arranque y no hay nada que avisar.
puerto_ocupado() { (exec 3<>"/dev/tcp/127.0.0.1/${PUERTO_API}") 2>/dev/null; }
if puerto_ocupado && [ -z "$(docker compose ps -q ads-api 2>/dev/null)" ]; then
  fallo_preflight "127.0.0.1:${PUERTO_API} está ocupado por otro proceso; compose.yaml publica ese puerto para ads-api."
fi

echo "→ Imagen…"
if ! docker image inspect "$IMAGEN" >/dev/null 2>&1; then
  [ "$SIN_EFECTOS" = 0 ] \
    || fallo_preflight "no hay imagen $IMAGEN y --dry-run no construye: docker compose build ads-api"
  docker compose build ads-api
fi

# `-T` solo cuando stdin no es un terminal (`--password-stdin`, CI): con
# terminal hacen falta el TTY y el eco apagado de las preguntas.
TTY_ARGS=()
[ -t 0 ] || TTY_ARGS=(-T)

primer_arranque() {
  docker compose run --rm --no-deps --user "$(id -u):$(id -g)" \
    --volume "$PWD:/workspace" "${TTY_ARGS[@]+"${TTY_ARGS[@]}"}" \
    ads-api python -m safent_ads.tools.first_run --workspace /workspace "$@"
}

echo "→ Ficheros de configuración…"
primer_arranque --skip-start "$@"

[ "$SOLO_FICHEROS" = 0 ] || exit 0

unset POSTGRES_USER POSTGRES_PASSWORD

# Las mismas tres líneas que `Makefile:up`. No se invoca `make` a propósito:
# el contrato ofrece este script como la vía para quien no lo tiene.
echo "→ Pila…"
docker compose up -d ads-db
docker compose run --rm ads-migrate
docker compose up -d ads-broker ads-api ads-worker

echo "→ Dueño y verificación…"
primer_arranque "$@"
