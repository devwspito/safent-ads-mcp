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
#   ./scripts/primer-arranque.sh --image ghcr.io/<owner>/<repo>@sha256:...   # imagen publicada
set -euo pipefail
cd "$(dirname "$0")/.."

# `compose.yaml` interpola `image: ${ADS_IMAGE:-safent-ads:local}` en los
# cuatro servicios propios: sin exportar nada, el tag local de siempre.
# `--image <ref>` (o la variable `ADS_IMAGE` ya puesta antes de invocar este
# script) fija cuál -- típicamente la publicada, verificada por digest
# (README "Verificar la imagen publicada"). Llega hasta
# `safent_ads.tools.first_run` (que tiene el mismo flag y la escribe en
# `.env` para que `make up` la reutilice después) vía `ARGUMENTOS_FIRST_RUN`
# (abajo): si ya viene en "$@" viaja tal cual; si solo viene de `ADS_IMAGE`
# se le añade a mano -- ese proceso corre DENTRO de un contenedor aparte y
# no ve el entorno de este shell.
IMAGEN="${ADS_IMAGE:-safent-ads:local}"
IMAGEN_EXPLICITA=0
[ -z "${ADS_IMAGE:-}" ] || IMAGEN_EXPLICITA=1
# Puerto que `compose.yaml` publica para ads-api. Copia deliberada: leerlo
# aquí exigiría `jq` o un `docker compose config` que el preflight todavía
# no ha validado. Que las dos copias no diverjan lo vigila
# `tests/unit/tools/test_first_run.py::TestWrapper::test_the_wrapper_watches_the_port_that_compose_publishes`.
PUERTO_API=8410

# Códigos de `contracts/first-run-cli.md` (README "Primer arranque",
# tabla de salida): 1 uso incorrecto, 2 preflight (docker/entorno). Un
# invocación mal formada (revisión de PR 45: `--password-stdin` sin
# tubería real es exactamente lo mismo que `first_run._read_stdin_secret`
# clasifica como `UsageError`, exit 1, cuando el mismo caso llega DENTRO
# del contenedor) no es un problema de docker ni del entorno.
fallo_de_uso() { echo "ERROR: $*" >&2; exit 1; }
fallo_preflight() { echo "ERROR: $*" >&2; exit 2; }

# `--dry-run` no escribe nada, así que tampoco construye una imagen (varios
# minutos). `--skip-start` sí escribe: necesita la imagen igual que una
# pasada normal, pero no levanta la pila. `--image` se lee aquí para
# decidir build-vs-pull (abajo) y para saber si hay que añadirlo a
# `ARGUMENTOS_FIRST_RUN` (mas abajo).
SOLO_FICHEROS=0
SIN_EFECTOS=0
LEE_COMPOSIO_STDIN=0
LEE_PASSWORD_STDIN=0
# Si `--image` ya viene en "$@" no hay nada que añadir: reenviarlo tal
# cual ya se lo lleva a `first_run`. Si la imagen viene SOLO de
# `ADS_IMAGE` (exportada antes de invocar el script, README "Verificar la
# imagen publicada"), `first_run` no la ve por ningun otro lado -- no lee
# el entorno de este proceso, corre en un contenedor aparte -- así que hay
# que añadírsela a mano (bug real T049: `.env` se quedaba sin
# `ADS_IMAGE` y `make up` recompilaba en vez de reutilizar la verificada).
IMAGEN_EN_ARGV=0
argumento_siguiente_es_imagen=0
for argumento in "$@"; do
  if [ "$argumento_siguiente_es_imagen" = 1 ]; then
    IMAGEN="$argumento"
    IMAGEN_EXPLICITA=1
    argumento_siguiente_es_imagen=0
    continue
  fi
  case "$argumento" in
    --dry-run) SIN_EFECTOS=1; SOLO_FICHEROS=1 ;;
    --skip-start) SOLO_FICHEROS=1 ;;
    --image) argumento_siguiente_es_imagen=1; IMAGEN_EN_ARGV=1 ;;
    --image=*) IMAGEN="${argumento#--image=}"; IMAGEN_EXPLICITA=1; IMAGEN_EN_ARGV=1 ;;
    --composio-api-key-stdin) LEE_COMPOSIO_STDIN=1 ;;
    --password-stdin) LEE_PASSWORD_STDIN=1 ;;
  esac
done
export ADS_IMAGE="$IMAGEN"

# Espejo de `first_run._validate_image_ref` (CWE-74, revisión de
# seguridad PR 45): `$IMAGEN` acaba en un `docker image inspect`/`pull`/
# `build` de este mismo script ANTES de que `first_run` la vea -- vale la
# pena rechazar aquí también un valor que no es una referencia real
# (espacio, `;`, un `\n` incrustado) en vez de dejar que sea `docker`
# quien decida qué hacer con él.
IMAGEN_REF_RE='^[A-Za-z0-9][A-Za-z0-9._/-]*(:[A-Za-z0-9._-]+)?(@sha256:[a-f0-9]{64})?$'
if [ "$IMAGEN_EXPLICITA" = 1 ] && ! [[ "$IMAGEN" =~ $IMAGEN_REF_RE ]]; then
  fallo_de_uso "--image/ADS_IMAGE: referencia de imagen inválida: $IMAGEN"
fi

# Argumentos que de verdad llegan a `first_run` (las dos invocaciones de
# `primer_arranque`, abajo): "$@" tal cual, más `--image "$IMAGEN"` SOLO
# si la imagen es explícita y todavía no viene en "$@" -- nunca duplicado
# ni inventado cuando no hace falta.
ARGUMENTOS_FIRST_RUN=("$@")
if [ "$IMAGEN_EXPLICITA" = 1 ] && [ "$IMAGEN_EN_ARGV" = 0 ]; then
  ARGUMENTOS_FIRST_RUN+=(--image "$IMAGEN")
fi

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
  VERBO_IMAGEN="build"
  [ "$IMAGEN_EXPLICITA" = 0 ] || VERBO_IMAGEN="pull"
  [ "$SIN_EFECTOS" = 0 ] \
    || fallo_preflight "no hay imagen $IMAGEN y --dry-run no construye ni descarga: docker compose $VERBO_IMAGEN ads-api"
  if [ "$IMAGEN_EXPLICITA" = 1 ]; then
    # `--image`/`ADS_IMAGE` explícitos: SIEMPRE se descarga, nunca se
    # construye. Con `build:` presente en compose.yaml, dejar que `docker
    # compose run/up` decidiera por su cuenta habría compilado de fuente en
    # vez de tirar de la imagen firmada en cuanto esa referencia no
    # estuviera ya en local -- justo el bug de esta spec (T049).
    docker compose pull ads-api \
      || fallo_preflight "no se pudo descargar $IMAGEN; verifica la referencia (cosign verify primero, README \"Verificar la imagen publicada\")"
  else
    docker compose build ads-api
  fi
fi

# `-T` solo cuando stdin no es un terminal (`--password-stdin`, CI): con
# terminal hacen falta el TTY y el eco apagado de las preguntas.
TTY_ARGS=()
[ -t 0 ] || TTY_ARGS=(-T)

# `docker compose run` (con o sin `-T`) reenvía la stdin del host al
# contenedor entera, hasta EOF -- la lea la app de dentro o no. Con las DOS
# invocaciones de `primer_arranque` de abajo (mas `ads-migrate` en medio)
# heredando sin tocar la MISMA tuberia real de este script, la primera que
# arrancaba se la bebia entera y la que de verdad pedia
# `--password-stdin`/`--composio-api-key-stdin` se encontraba "stdin no
# traía ningún valor" en un `make first-run --password-stdin` de un solo
# comando, no interactivo (T049, spec 008). Se lee aqui UNA VEZ, en este
# shell -- nunca a un fichero, nunca a argv, nunca a un log -- y solo se
# vuelve a servir, por una tuberia propia, al paso que de verdad la pide;
# el resto recibe `/dev/null` explicito para no depender de lo que quede
# en la tuberia compartida.
# `set -x` (bash -x, o `SHELLOPTS` heredado en algún CI) imprime cada
# orden con sus argumentos YA EXPANDIDOS -- una asignación, una
# comparación `[ -n "$SECRETO_STDIN" ]`, hasta el propio `printf`
# quedarían con la contraseña en la traza. Las dos funciones envuelven
# CUALQUIER bloque que toque `$SECRETO_STDIN` (lectura, recorte,
# comprobación de vacío, uso), y solo tocan `-x` si ya estaba encendido
# -- nunca lo encienden por su cuenta.
_TRAZABA_ANTES_DEL_SECRETO=0
ocultar_traza_del_secreto() {
  case "$-" in *x*) _TRAZABA_ANTES_DEL_SECRETO=1; set +x ;; esac
}
restaurar_traza_del_secreto() {
  [ "$_TRAZABA_ANTES_DEL_SECRETO" = 0 ] || set -x
}

SECRETO_STDIN=""
if [ "$LEE_COMPOSIO_STDIN" = 1 ] || [ "$LEE_PASSWORD_STDIN" = 1 ]; then
  if [ -t 0 ]; then
    fallo_de_uso "--password-stdin/--composio-api-key-stdin exige que el valor llegue por una tubería, no un terminal"
  fi
  ocultar_traza_del_secreto
  # `$(cat)` a secas recorta TODOS los saltos de línea finales, no solo
  # uno (revisión de seguridad PR 45): un marcador (`x`) al final impide
  # que la sustitución de comandos se coma ninguno; se quita el marcador
  # y, como mucho, UN salto de línea final (el que añadiría `echo "$p" |
  # ...` en vez de `printf '%s' "$p" | ...`) -- nunca espacios de verdad,
  # que `$(cat)` ya respetaba de por sí.
  SECRETO_STDIN="$(cat; printf x)"
  SECRETO_STDIN="${SECRETO_STDIN%x}"
  case "$SECRETO_STDIN" in
    *$'\r\n') SECRETO_STDIN="${SECRETO_STDIN%$'\r\n'}" ;;
    *$'\n' | *$'\r') SECRETO_STDIN="${SECRETO_STDIN%?}" ;;
  esac
  SECRETO_STDIN_VACIO=0
  [ -n "$SECRETO_STDIN" ] || SECRETO_STDIN_VACIO=1
  restaurar_traza_del_secreto
  # Vacío de verdad (no solo el salto de línea que se acaba de quitar):
  # se rechaza AQUÍ, antes de levantar nada -- fallar después del `up -d`
  # habría malgastado el arranque entero de la pila por nada.
  [ "$SECRETO_STDIN_VACIO" = 0 ] \
    || fallo_de_uso "--password-stdin/--composio-api-key-stdin: la tubería no traía ningún valor"
fi

sirve_secreto_stdin() {
  ocultar_traza_del_secreto
  printf '%s' "$SECRETO_STDIN" | "$@"
  local estado=$?
  restaurar_traza_del_secreto
  return "$estado"
}

primer_arranque() {
  docker compose run --rm --no-deps --user "$(id -u):$(id -g)" \
    --volume "$PWD:/workspace" "${TTY_ARGS[@]+"${TTY_ARGS[@]}"}" \
    ads-api python -m safent_ads.tools.first_run --workspace /workspace "$@"
}

echo "→ Ficheros de configuración…"
if [ "$LEE_COMPOSIO_STDIN" = 1 ]; then
  sirve_secreto_stdin primer_arranque --skip-start "${ARGUMENTOS_FIRST_RUN[@]}"
elif [ "$LEE_PASSWORD_STDIN" = 1 ]; then
  primer_arranque --skip-start "${ARGUMENTOS_FIRST_RUN[@]}" </dev/null
else
  primer_arranque --skip-start "${ARGUMENTOS_FIRST_RUN[@]}"
fi

[ "$SOLO_FICHEROS" = 0 ] || exit 0

unset POSTGRES_USER POSTGRES_PASSWORD

# Las mismas tres líneas que `Makefile:up`. No se invoca `make` a propósito:
# el contrato ofrece este script como la vía para quien no lo tiene. Ninguna
# de las tres necesita nunca stdin -- `/dev/null` explícito, nunca la
# tubería real del script (ya vaciada arriba si hacía falta).
#
# `--no-build` en el `up` final cuando la imagen es explícita: la sección
# «→ Imagen…» de arriba ya garantizó que `$IMAGEN` está en local (tirada,
# nunca compilada) antes de llegar aquí -- esto es la red de seguridad
# por si algo la borrara entre medias (poda concurrente): falla alto y
# claro en vez de compilar de fuente encima de la referencia firmada.
echo "→ Pila…"
docker compose up -d ads-db </dev/null
docker compose run --rm ads-migrate </dev/null
if [ "$IMAGEN_EXPLICITA" = 1 ]; then
  docker compose up -d --no-build ads-broker ads-api ads-worker </dev/null
else
  docker compose up -d ads-broker ads-api ads-worker </dev/null
fi

echo "→ Dueño y verificación…"
if [ "$LEE_PASSWORD_STDIN" = 1 ]; then
  sirve_secreto_stdin primer_arranque "${ARGUMENTOS_FIRST_RUN[@]}"
elif [ "$LEE_COMPOSIO_STDIN" = 1 ]; then
  primer_arranque "${ARGUMENTOS_FIRST_RUN[@]}" </dev/null
else
  primer_arranque "${ARGUMENTOS_FIRST_RUN[@]}"
fi
unset SECRETO_STDIN
