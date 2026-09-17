#!/usr/bin/env bash
# Instala el MCP de anuncios en Claude Code y/o Codex de ESTA máquina
# (contracts/instalar-mcp-cli.md).
#
# Sin token (lo normal): registra el servidor y autorizas en el navegador
# (entrando con Google o con tu contraseña). Ningún secreto queda en esta
# máquina; el acceso se quita desde el panel
# (Conexiones → Aplicaciones con acceso).
#
# Con token (vía de emergencia o CI): SOLO por `--token-stdin`, leyendo el
# bearer de la entrada estándar -- nunca como argumento posicional (queda
# en el historial del shell y en `ps`).
#
# Uso:
#   ./scripts/instalar-mcp.sh --url https://ads.example.com/mcp
#   ./scripts/instalar-mcp.sh --url https://ads.example.com/mcp --nombre mis-ads
#   ./scripts/instalar-mcp.sh --config ./mi-despliegue.env
#   printf '%s' "$TOKEN" | ./scripts/instalar-mcp.sh --url ... --token-stdin
set -euo pipefail

NOMBRE_DEFECTO="safent-ads"
NOMBRE=""
URL=""
CONFIG_FILE=""
TOKEN_STDIN=0
SOLO=""
# Par heredado que retirar en modo OAuth: la variable y el directorio que
# escribía una versión anterior de este instalador, atada a UN despliegue.
# El instalador estándar no conoce ningún nombre de cliente: quien tuvo esa
# versión los declara en su `--config` (`LEGACY_ENV_VAR=`,
# `LEGACY_CONFIG_DIR=`) y solo entonces se retiran. Vacíos = nada que
# retirar, que es el caso de cualquier instalación nueva.
LEGACY_ENV_VAR=""
LEGACY_CONFIG_DIR=""

uso() {
  cat <<'EOF'
Uso: instalar-mcp.sh --url <URL> [--nombre <nombre>] [--config <fichero>]
                      [--token-stdin] [--solo claude|codex]

  --url <URL>        Obligatorio (salvo por --config/entorno). URL completa
                      del endpoint /mcp. Debe ser https:// salvo loopback.
  --nombre <nombre>   Por defecto "safent-ads". [a-z0-9-]{1,32}.
  --config <fichero>  Fichero CLAVE=valor con URL=/NOMBRE=. Los flags
                      explícitos ganan. Un despliegue que venga de un
                      instalador anterior declara aquí el par que hay que
                      retirar: LEGACY_ENV_VAR= y LEGACY_CONFIG_DIR=.
  --token-stdin       Activa la vía estática leyendo el bearer de stdin.
                      Única forma de pasar un token.
  --solo claude|codex Restringe a un agente.
  -h, --help          Este uso.

Entorno equivalente (misma precedencia que --config): ADS_MCP_URL, ADS_MCP_NAME.
No existe argumento posicional de token.
EOF
}

fallo() { echo "  ✘ $*" >&2; }

# `CLAVE=valor` por línea, sin ejecutar el fichero (nunca `source`): un
# fichero de configuración no es un script.
# Quita espacios en los bordes y, si sobreviven, UN par de comillas
# (simples o dobles) a juego alrededor de todo el valor -- "URL = 'x'" y
# 'NOMBRE="y"' son formas razonables de escribir un CLAVE=valor a mano.
_recortar() {
  local valor="$1"
  valor="${valor#"${valor%%[![:space:]]*}"}"
  valor="${valor%"${valor##*[![:space:]]}"}"
  case "$valor" in
    \"*\") valor="${valor#\"}"; valor="${valor%\"}" ;;
    \'*\') valor="${valor#\'}"; valor="${valor%\'}" ;;
  esac
  printf '%s' "$valor"
}

leer_config() {
  local fichero="$1" clave valor
  [ -f "$fichero" ] || { fallo "no existe el fichero de configuración: $fichero"; return 1; }
  while IFS='=' read -r clave valor || [ -n "$clave" ]; do
    clave="$(_recortar "$clave")"
    valor="$(_recortar "$valor")"
    case "$clave" in
      URL) [ -z "$URL" ] && URL="$valor" ;;
      NOMBRE) [ -z "$NOMBRE" ] && NOMBRE="$valor" ;;
      LEGACY_ENV_VAR) [ -z "$LEGACY_ENV_VAR" ] && LEGACY_ENV_VAR="$valor" ;;
      LEGACY_CONFIG_DIR) [ -z "$LEGACY_CONFIG_DIR" ] && LEGACY_CONFIG_DIR="$valor" ;;
      ''|'#'*) ;;
    esac
  done < "$fichero"
}

parsear_argumentos() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --url) URL="${2:?--url exige un valor}"; shift 2 ;;
      --nombre) NOMBRE="${2:?--nombre exige un valor}"; shift 2 ;;
      --config) CONFIG_FILE="${2:?--config exige un valor}"; shift 2 ;;
      --token-stdin) TOKEN_STDIN=1; shift ;;
      --solo) SOLO="${2:?--solo exige claude o codex}"; shift 2 ;;
      -h|--help) uso; exit 0 ;;
      *) fallo "argumento no reconocido: $1 (ver --help)"; exit 1 ;;
    esac
  done
}

resolver_url_y_nombre() {
  [ -n "$CONFIG_FILE" ] && { leer_config "$CONFIG_FILE" || exit 1; }
  [ -z "$URL" ] && URL="${ADS_MCP_URL:-}"
  [ -z "$NOMBRE" ] && NOMBRE="${ADS_MCP_NAME:-}"
  [ -z "$NOMBRE" ] && NOMBRE="$NOMBRE_DEFECTO"
  if [ -z "$URL" ]; then
    fallo "falta --url (o ADS_MCP_URL, o URL= en --config): sin defecto de cliente"
    exit 1
  fi
  case "$URL" in
    https://*) ;;
    http://127.0.0.1*|http://localhost*) ;;
    *) fallo "--url debe ser https:// (o http://localhost para desarrollo): $URL"; exit 1 ;;
  esac
  # `[[ =~ ]]` sobre la cadena ENTERA, nunca `grep` línea a línea: `grep`
  # valida cada línea por separado, así que un valor con un salto de línea
  # dentro («safent-ads\nrm -rf ~») pasaría por la primera y el resto se
  # colaría entero.
  if ! [[ "$NOMBRE" =~ ^[a-z0-9-]{1,32}$ ]]; then
    fallo "--nombre inválido (debe casar con [a-z0-9-]{1,32}): $NOMBRE"
    exit 1
  fi
  case "$SOLO" in
    ''|claude|codex) ;;
    *) fallo "--solo debe ser 'claude' o 'codex': $SOLO"; exit 1 ;;
  esac
  # Las dos claves heredadas salen de un fichero de configuración y acaban
  # en una búsqueda sobre los perfiles de shell y en un `rm`: se validan
  # ANTES de que se borre nada.
  if [ -n "$LEGACY_ENV_VAR" ] && ! [[ "$LEGACY_ENV_VAR" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    fallo "LEGACY_ENV_VAR inválido (debe casar con [A-Za-z_][A-Za-z0-9_]*): $LEGACY_ENV_VAR"
    exit 1
  fi
  # `LEGACY_CONFIG_DIR` acaba en `rm -f "$dir/mcp.env"`: se acota al HOME
  # de quien ejecuta —`~/…` o `$HOME/…`, sin expandir en el fichero— y a
  # caracteres de ruta corrientes. Sin `..` (saldría del HOME), sin
  # comodines, sin espacios ni comillas. Una ruta absoluta cualquiera no
  # vale: el par heredado que este instalador retira siempre vivió bajo el
  # HOME, y ampliar eso es un permiso que nadie ha pedido.
  if [ -n "$LEGACY_CONFIG_DIR" ] \
    && ! [[ "$LEGACY_CONFIG_DIR" =~ ^(~|\$HOME)(/[A-Za-z0-9._@+-]+)+$ && "$LEGACY_CONFIG_DIR" != *..* ]]; then
    fallo "LEGACY_CONFIG_DIR inválido (debe ser ~/… o \$HOME/…, sin '..' ni comodines): $LEGACY_CONFIG_DIR"
    exit 1
  fi
}

# `ADS_MCP_TOKEN_<NOMBRE_EN_MAYUSCULAS, -→_>` (contracts/instalar-mcp-cli.md).
nombre_variable_token() {
  printf 'ADS_MCP_TOKEN_%s' "$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')"
}

perfil_shell() {
  case "$(basename "${SHELL:-sh}")" in
    zsh)  echo "${ZDOTDIR:-$HOME}/.zshrc" ;;
    bash) if [ -f "$HOME/.bash_profile" ] || [ "$(uname -s)" = Darwin ]; then
            echo "$HOME/.bash_profile"; else echo "$HOME/.bashrc"; fi ;;
    *)    echo "$HOME/.profile" ;;
  esac
}

# Quita la línea `export <VAR>=…`; conserva inode y permisos.
#
# El nombre de la variable puede venir de un fichero de configuración
# (`LEGACY_ENV_VAR`), así que aquí es DATO, nunca parte de una expresión
# regular: `awk -v` + `index($0, prefijo) == 1` compara literal y anclado
# al principio de línea. `grep -F` compara literal pero no sabe anclar, y
# `grep "^export $var="` interpretaría el valor -- un `LEGACY_ENV_VAR=.*`
# habría borrado TODOS los `export` del perfil.
#
# `awk -v` no es inmune del todo: interpreta las secuencias de escape del
# valor, así que un `\t` o un `\\` dentro cambiarían el prefijo que se
# busca. Lo que lo hace seguro es la validación de `resolver_url_y_nombre`
# (`^[A-Za-z_][A-Za-z0-9_]*$`, sin barras invertidas posibles); esto es el
# segundo cerrojo, no el único.
_empieza_por() {
  awk -v prefijo="$1" 'index($0, prefijo) == 1 { encontrado = 1 } END { exit !encontrado }' "$2"
}

# Sustituye el CONTENIDO de un perfil conservando su inode y sus permisos
# (puede ser un enlace de un gestor de dotfiles: `mv` lo rompería).
#
# `cat > "$perfil"` trunca ANTES de escribir, así que una escritura que
# falle a mitad -- disco lleno, perfil de solo lectura -- dejaba un fichero
# de arranque cortado y el temporal ya borrado: sin copia de lo que había.
# Se deja una copia con permisos antes de truncar y solo se retira cuando la
# escritura ha terminado bien; si falla, el mensaje dice dónde quedó.
_sustituir_contenido_de() {
  local perfil="$1" nuevo="$2" copia
  # `mktemp`, no un `.bak` fijo: `~/.bashrc.bak` puede ser de quien
  # instala. Con un nombre fijo se sobrescribiría y, al terminar bien, se
  # borraría -- perder un fichero suyo por hacerle una copia de seguridad.
  copia="$(mktemp "${perfil}.bak.XXXXXX")" || return 1
  cp -p "$perfil" "$copia" || { rm -f "$copia"; return 1; }
  if ! cat "$nuevo" > "$perfil"; then
    fallo "no he podido reescribir $perfil; lo que había sigue intacto en $copia"
    return 1
  fi
  rm -f "$copia"
}

quitar_export_de() {
  local perfil="$1" prefijo tmp estado
  prefijo="export $2="
  { [ -f "$perfil" ] && _empieza_por "$prefijo" "$perfil"; } || return 0
  tmp="$(mktemp "${perfil}.XXXXXX")" || return 1
  awk -v prefijo="$prefijo" 'index($0, prefijo) != 1' "$perfil" > "$tmp" \
    || { rm -f "$tmp"; return 1; }
  _sustituir_contenido_de "$perfil" "$tmp" && estado=0 || estado=1
  rm -f "$tmp"
  return "$estado"
}

# Quita una línea EXACTA (p.ej. la que carga un ENV_FILE); conserva inode y permisos.
quitar_linea_de() {
  local perfil="$1" linea="$2" tmp estado
  { [ -f "$perfil" ] && grep -qF -- "$linea" "$perfil"; } || return 0
  tmp="$(mktemp "${perfil}.XXXXXX")" || return 1
  { grep -vF -- "$linea" "$perfil"; [ $? -le 1 ]; } > "$tmp" || { rm -f "$tmp"; return 1; }
  _sustituir_contenido_de "$perfil" "$tmp" && estado=0 || estado=1
  rm -f "$tmp"
  return "$estado"
}

# El token va al fichero 0600 (sustituido atómicamente); el perfil solo recibe la línea que lo carga.
guardar_token() {
  local perfil="$1" variable="$2" env_file="$3" token="$4" source_line="$5" p tmp
  for p in "${ZDOTDIR:-$HOME}/.zshrc" "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    quitar_export_de "$p" "$variable" || return 1
  done
  mkdir -p "$(dirname "$env_file")" || return 1
  tmp="$(mktemp "${env_file}.XXXXXX")" || return 1
  chmod 600 "$tmp" || { rm -f "$tmp"; return 1; }
  printf 'export %s=%q\n' "$variable" "$token" > "$tmp" || { rm -f "$tmp"; return 1; }
  mv -f "$tmp" "$env_file" || { rm -f "$tmp"; return 1; }
  grep -qF -- "$source_line" "$perfil" 2>/dev/null && return 0
  { [ -f "$perfil" ] || ( umask 077; : > "$perfil" ); } || return 1
  printf '\n%s\n' "$source_line" >> "$perfil"
}

instalar_claude_code() {
  local token="$1" estado
  echo "→ Claude Code: registrando «$NOMBRE» en ámbito usuario (vale desde cualquier carpeta)…"
  claude mcp remove "$NOMBRE" -s local >/dev/null 2>&1 || true # registro por carpeta de versiones anteriores
  claude mcp remove "$NOMBRE" -s user >/dev/null 2>&1 || true
  # La cabecera es la única vía que ofrece `claude mcp add`; queda en ~/.claude.json (0600).
  claude mcp add -s user --transport http "$NOMBRE" "$URL" \
    --header "Authorization: Bearer ${token}" >/dev/null \
    || { fallo "claude mcp add ha fallado para «$NOMBRE». Revisa: claude mcp list"; return 1; }
  estado="$(claude mcp get "$NOMBRE" 2>/dev/null || true)"
  case "$estado" in
    *Connected*) echo "  ✔ conectado" ;;
    *) fallo "registrado, pero no conecta. Revisa: claude mcp list"; return 3 ;;
  esac
}

instalar_codex() {
  local token="$1" variable="$2" env_file="$3" source_line="$4" perfil
  echo "→ Codex: registrando «$NOMBRE» (lee el token de \$$variable; config.toml nunca lo contiene)…"
  mkdir -p "${CODEX_HOME:-$HOME/.codex}" \
    || { fallo "no puedo crear ${CODEX_HOME:-$HOME/.codex} (CODEX_HOME)."; return 1; }
  { codex mcp add "$NOMBRE" --url "$URL" --bearer-token-env-var "$variable" >/dev/null \
    && codex mcp get "$NOMBRE" >/dev/null 2>&1; } \
    || { fallo "Codex no ha aceptado el registro. Revisa: codex mcp list"; return 3; }
  perfil="$(perfil_shell)"
  guardar_token "$perfil" "$variable" "$env_file" "$token" "$source_line" \
    || { fallo "no he podido guardar el token en $env_file (perfil: $perfil)."; return 4; }
  echo "  ✔ registrado · token en $env_file (0600), cargado desde $perfil · abre una terminal nueva antes de arrancar codex"
}

instalar_claude_code_oauth() {
  echo "→ Claude Code: registrando «$NOMBRE» en ámbito usuario (vale desde cualquier carpeta)…"
  claude mcp remove "$NOMBRE" -s local >/dev/null 2>&1 || true # registro por carpeta de versiones anteriores
  claude mcp remove "$NOMBRE" -s user >/dev/null 2>&1 || true
  claude mcp add -s user --transport http "$NOMBRE" "$URL" >/dev/null \
    || { fallo "claude mcp add ha fallado para «$NOMBRE». Revisa: claude mcp list"; return 3; }
  echo "  ✔ registrado · para autorizar: abre claude, escribe /mcp, elige «$NOMBRE» → Authenticate (se abre el navegador)"
}

instalar_codex_oauth() {
  echo "→ Codex: registrando «$NOMBRE»…"
  mkdir -p "${CODEX_HOME:-$HOME/.codex}" \
    || { fallo "no puedo crear ${CODEX_HOME:-$HOME/.codex} (CODEX_HOME)."; return 1; }
  { codex mcp add "$NOMBRE" --url "$URL" >/dev/null \
    && codex mcp get "$NOMBRE" >/dev/null 2>&1; } \
    || { fallo "Codex no ha aceptado el registro. Revisa: codex mcp list"; return 3; }
  if [ -t 0 ] && [ -t 1 ]; then
    echo "  abriendo el navegador para autorizar a Codex…"
    codex mcp login "$NOMBRE" \
      || { fallo "la autorización no se completó; repite: codex mcp login $NOMBRE"; return 3; }
    echo "  ✔ autorizado"
  else
    echo "  ✔ registrado · para autorizar: codex mcp login $NOMBRE"
  fi
}

# Fichero del par heredado, TAL CUAL lo escribió la versión anterior en el
# perfil: si `LEGACY_CONFIG_DIR` trae `$HOME/...` sin expandir, esa es la
# cadena que `grep -F` tiene que encontrar en el perfil, no la expandida.
fichero_heredado_literal() {
  [ -n "$LEGACY_CONFIG_DIR" ] || return 1
  printf '%s/mcp.env' "$LEGACY_CONFIG_DIR"
}

# La misma ruta, ya expandida: es la que `rm` necesita.
expandir_home() {
  case "$1" in
    '$HOME'/*) printf '%s' "${HOME}${1#\$HOME}" ;;
    '~'/*)     printf '%s' "${HOME}${1#\~}" ;;
    *)         printf '%s' "$1" ;;
  esac
}

# Retira de UN perfil el par heredado que declare el `--config`. Sin
# declaración no hay nada que retirar y la función no toca el perfil.
quitar_par_heredado_de() {
  local perfil="$1" literal
  [ -z "$LEGACY_ENV_VAR" ] || quitar_export_de "$perfil" "$LEGACY_ENV_VAR" || return 1
  literal="$(fichero_heredado_literal)" || return 0
  quitar_linea_de "$perfil" "[ -f \"${literal}\" ] && . \"${literal}\""
}

borrar_fichero_heredado() {
  local literal fichero
  literal="$(fichero_heredado_literal)" || return 0
  fichero="$(expandir_home "$literal")"
  [ -f "$fichero" ] && { rm -f "$fichero" && echo "· token heredado retirado de $fichero"; }
  return 0
}

# En modo OAuth no debe quedar un token estático antiguo en la máquina --
# ni el de este nombre, ni el par heredado que declare el `--config` del
# despliegue. Justificación de seguridad, no de compatibilidad (plan.md
# US2): dejar un bearer de dueño olvidado en una máquina es una regresión.
retirar_token_estatico() {
  local variable env_file source_line p
  variable="$(nombre_variable_token "$NOMBRE")"
  env_file="$HOME/.config/$NOMBRE/mcp.env"
  source_line="[ -f \"$env_file\" ] && . \"$env_file\""
  for p in "${ZDOTDIR:-$HOME}/.zshrc" "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    quitar_export_de "$p" "$variable" || return 1
    quitar_linea_de "$p" "$source_line" || return 1
    quitar_par_heredado_de "$p" || return 1
  done
  [ -f "$env_file" ] && { rm -f "$env_file" && echo "· token estático antiguo retirado de $env_file"; }
  borrar_fichero_heredado
  return 0
}

# Conserva el codigo de salida MAS especifico entre varias llamadas (4 >
# 3 > 1 > 0): "no pude escribir el token con 0600" (4) es mas accionable
# que "registrado pero no conecta" (3), que a su vez es mas accionable que
# el generico "algo ha fallado" (1).
_prioridad_salida() {
  case "$1" in 0) echo 0 ;; 4) echo 3 ;; 3) echo 2 ;; *) echo 1 ;; esac
}

registrar_fallo() {
  local nuevo="$1"
  # `if ... ; then` (nunca `[ ... ] && salida=...`): un `&&` que no dispara
  # devuelve 1 -- si esta funcion se llama como el ULTIMO eslabon de un
  # `cmd || registrar_fallo "$?"`, ese 1 se convierte en el exit status de
  # toda la linea y `set -e` mata el script en el sitio, saltandose el
  # resumen final y el codigo de salida correcto (3/4). Esta funcion
  # siempre tiene exito: solo actualiza estado, nunca decide si el script
  # deberia parar.
  if [ "$(_prioridad_salida "$nuevo")" -gt "$(_prioridad_salida "$salida")" ]; then
    salida="$nuevo"
  fi
  return 0
}

main() {
  parsear_argumentos "$@"
  resolver_url_y_nombre

  local variable env_file source_line token="" salida=0 alguno=0
  variable="$(nombre_variable_token "$NOMBRE")"
  env_file="$HOME/.config/$NOMBRE/mcp.env"
  source_line="[ -f \"$env_file\" ] && . \"$env_file\""

  if [ "$TOKEN_STDIN" = 1 ]; then
    if [ -t 0 ]; then
      fallo "--token-stdin exige que el token llegue por stdin (evita un TTY: quedaría tecleado y visible)."
      exit 1
    fi
    IFS= read -r token || true # `read` devuelve 1 sin salto de línea final; el valor ya quedó leído.
    [ -n "$token" ] || { fallo "stdin no traía ningún token."; exit 1; }
    echo "→ Modo: token estático (vía de emergencia; lo normal es sin token, por OAuth)."
  else
    echo "→ Modo: OAuth (autorizas en el navegador, entrando con Google o con tu contraseña; ningún secreto queda en esta máquina)."
    retirar_token_estatico || registrar_fallo "$?"
  fi

  if [ -z "$SOLO" ] || [ "$SOLO" = claude ]; then
    if command -v claude >/dev/null 2>&1; then
      alguno=1
      if [ -n "$token" ]; then instalar_claude_code "$token" || registrar_fallo "$?"; else instalar_claude_code_oauth || registrar_fallo "$?"; fi
    elif [ "$SOLO" = claude ]; then
      fallo "Claude Code no encontrado (--solo claude)."
      registrar_fallo 1
    else
      echo "· Claude Code no encontrado (salto)."
    fi
  fi

  if [ -z "$SOLO" ] || [ "$SOLO" = codex ]; then
    if command -v codex >/dev/null 2>&1; then
      alguno=1
      if [ -n "$token" ]; then instalar_codex "$token" "$variable" "$env_file" "$source_line" || registrar_fallo "$?"; else instalar_codex_oauth || registrar_fallo "$?"; fi
    elif [ "$SOLO" = codex ]; then
      fallo "Codex no encontrado (--solo codex)."
      registrar_fallo 1
    else
      echo "· Codex no encontrado (salto)."
    fi
  fi

  [ "$alguno" = 1 ] || { echo "ERROR: ni Claude Code ni Codex en esta máquina: instala uno y repite." >&2; exit 2; }
  if [ "$salida" != 0 ]; then
    echo "✘ Instalación incompleta: revisa los avisos de arriba." >&2
    exit "$salida"
  fi
  cat <<EOF

Listo.
  Panel: ${URL%/mcp}/
  Entra en el panel con tu cuenta de dueño. Las aplicaciones autorizadas se ven y
  se quitan en Conexiones → Aplicaciones con acceso.
  En Claude Code o Codex pide, por ejemplo: «Lista mis negocios y sus cuentas de anuncios».
EOF
}

main "$@"
