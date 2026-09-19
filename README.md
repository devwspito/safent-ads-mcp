# safent-ads

Servidor **MCP** de anuncios, autoalojado, para Google Ads y Meta Ads. Lo
instalas en tu máquina o en tu VM, conectas tus cuentas y se lo enchufas a
Claude Code o Codex.

El MCP es el **puente**: da acceso a las cuentas, aplica las reglas, controla el
único punto de escritura hacia las plataformas y deja auditoría de todo. La
inteligencia la pone el agente. Este servidor no recorta ni guía al modelo.

Nada se publica ni se gasta sin aprobación humana en el panel.

## Qué incluye

- Servidor MCP con tres niveles de permiso: ver, proponer, aprobar
  (`ads-view`, `ads-propose`, `ads-approve`).
- Panel de aprobación con registro de decisiones encadenado.
- Bróker: el único proceso que ve las credenciales y el único que escribe en
  Google Ads, Meta Ads o Google Tag Manager.
- Servidor OAuth 2.1 propio para autorizar agentes desde el navegador.
- Topes duros por cuenta: sin tope, no se escribe.
- Cero credenciales de fábrica. Todo lo de terceros lo pones tú.

## Requisitos

Docker con `compose`. Nada más.

`ads-api` solo escucha en `127.0.0.1:8410`. Exponerlo es decisión tuya y lo
haces por tu cuenta; este repo nunca lo hace por ti. Por ejemplo, con Tailscale:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8410     # solo tu tailnet
tailscale funnel --bg --https=443 http://127.0.0.1:8410    # público en internet
```

Caddy, nginx o cualquier otro proxy con TLS valen igual. Dos consecuencias:

- `ADS_PUBLIC_BASE_URL` tiene que ser exactamente ese dominio, o las cookies
  `Secure`/`SameSite` y el OAuth rechazan las peticiones.
- Con un proxy delante, `ADS_TRUSTED_PROXY_HOPS=1`: es el salto de confianza
  que hace que el límite de tasa cuente la IP real y no la que un cliente
  pueda falsear en `X-Forwarded-For`.

## Instalar

```bash
git clone https://github.com/devwspito/safent-ads-mcp.git safent-ads && cd safent-ads
git checkout vX.Y.Z   # el tag cuya imagen vas a verificar abajo
```

## Verificar la imagen publicada

Cada release firma su imagen con [cosign](https://github.com/sigstore/cosign)
en modo *keyless*: sin clave privada que nadie custodie, la identidad la da el
OIDC de GitHub Actions y la firma queda registrada en el log público de
[Sigstore](https://www.sigstore.dev/) (Rekor). La imagen lleva además
procedencia SLSA completa y un SBOM SPDX como *attestations* del propio
manifiesto.

```bash
cosign verify "ghcr.io/devwspito/safent-ads-mcp@<digest>" \
  --certificate-identity-regexp '^https://github.com/devwspito/safent-ads-mcp/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Verifica siempre por **digest**, nunca por tag: un tag es mutable, un digest
no. Si solo tienes el tag a mano, resuélvelo primero — `--format
'{{.Manifest.Digest}}'` no imprime nada en algunas versiones de buildx (por
ejemplo 0.31.1, que solo saca la salida en texto plano); `awk` sobre esa
salida funciona en todas:

```bash
digest="$(docker buildx imagetools inspect ghcr.io/devwspito/safent-ads-mcp:vX.Y.Z \
  | awk '/^Digest:/{print $2; exit}')"
```

Comprobar la procedencia y el SBOM antes de confiar en una imagen:

```bash
docker buildx imagetools inspect "ghcr.io/devwspito/safent-ads-mcp@<digest>" --format '{{ json .Provenance }}'
docker buildx imagetools inspect "ghcr.io/devwspito/safent-ads-mcp@<digest>" --format '{{ json .SBOM }}'
```

Con la firma comprobada, deja la referencia exacta a mano para el paso
siguiente. Comillas simples a propósito: una referencia con `;`, `` ` `` o
`$(...)` pegada de algún sitio no debe ejecutarse en tu shell.

```bash
export ADS_IMAGE='ghcr.io/devwspito/safent-ads-mcp@<digest>'
```

## Primer arranque

`make first-run` hace todo esto en un solo comando: genera `.env`,
`secrets/api.env`, `secrets/broker.env` y `config/caps.yaml` con material
aleatorio (cero credenciales de fábrica), levanta `ads-db → migraciones →
ads-broker/ads-api/ads-worker`, te da de alta como dueño y espera a que
`ads-api` responda sano.

Con `ADS_IMAGE` ya exportada (paso anterior), un solo comando la usa: nunca
compila, siempre descarga esa imagen exacta si todavía no la tienes.

```bash
make first-run
```

Queda escrita en `.env`, así que un `make up` posterior reutiliza la misma
imagen verificada. **Alternativa: compilar de fuente.** Sin `ADS_IMAGE`
exportada, `make first-run` construye `safent-ads:local` con el
`Containerfile` de este repo en vez de descargar nada.

`ADS_IMAGE` (variable de entorno, arriba) es la vía recomendada: nunca
pasa por el intérprete de `make`. El equivalente en línea de comandos es
`--image <ref>` (`./scripts/primer-arranque.sh --image '<ref>'`, o `make
first-run ARGS="--image '<ref>'"` si usas `make`) — pero `ARGS` la
expande tu shell **dos veces** (una vez al construir la línea, otra dentro
del `Makefile`): una referencia copiada de un sitio que no controlas y que
trajera `;`, `` ` `` o `$(...)` se ejecutaría como si la hubieras tecleado
tú. Prefiere siempre `ADS_IMAGE=... make first-run`.

Pide como mucho tres cosas —URL pública, tu correo y, si quieres conectar
cuentas en un clic, la clave de Composio (Enter la omite)— más tu
contraseña de dueño si el login federado con Google está apagado, que es
el caso por omisión. Vuelve a ejecutarlo tantas veces como haga falta: una
segunda pasada no reescribe ni regenera nada de lo que ya existe.

Automatizado (CI, sin terminal): la contraseña del dueño entra por stdin,
nunca por argumento.

```bash
IFS= read -rsp 'Contraseña del dueño: ' p
printf '%s' "$p" | make first-run ARGS="--password-stdin --public-base-url https://ads.example.com --owner-email tu@correo.com --no-composio"
unset p
```

`IFS=` importa: sin ella, `read` recorta espacios al principio o al final
de lo que tecleas — si tu contraseña lleva uno a propósito, lo pierdes en
silencio y la próxima vez que la escribas no coincidirá.

`POST /mcp` sin credencial responde `401`: es la respuesta correcta, la puerta
pide autorización. `/mcp` no se abre en el navegador; la interfaz para
personas es el panel, en la URL pública que diste arriba.

| Código de salida | Significado |
|---|---|
| `0` | Éxito, incluida una segunda pasada que no hizo nada. |
| `1` | Uso incorrecto: flag desconocido o valor inválido. |
| `2` | Preflight: Docker ausente o parado, puerto 8410 ocupado, `compose.yaml` inválido, o sin espacio/permiso para escribir. |
| `3` | Un fichero ya existente es incoherente (el mensaje nombra fichero y clave). |
| `4` | La pila no llegó a estar sana (el mensaje nombra el servicio). |
| `5` | No se pudo dar de alta al dueño: sin terminal y sin `--password-stdin`, o correo rechazado. |
| `6` | Permisos: no se pudo fijar 0600, `secrets/` escribible por grupo/otros, o un fichero gestionado es un enlace simbólico. |

### A mano, sin `make first-run`

Un paso por línea, si prefieres ver cada fichero antes de arrancar.

```bash
cp .env.example .env
cp secrets/api.env.example secrets/api.env
cp secrets/broker.env.example secrets/broker.env
cp config/caps.example.yaml config/caps.yaml
chmod 600 .env secrets/api.env secrets/broker.env
openssl rand -base64 32                                                    # un valor nuevo por cada clave `change-me` de 32 bytes
docker compose run --rm --no-deps ads-api python -m safent_ads.tools.gen_keys
```

Rellena los `change-me` de los tres ficheros: la contraseña de Postgres (la
misma en `.env` y en `ADS_DATABASE_URL`) y los secretos aleatorios que acabas de
generar. Y cambia `ADS_PUBLIC_BASE_URL` por tu dominio: trae un ejemplo, no un
`change-me`. Del par Ed25519, la semilla privada va a `secrets/api.env` y la
pública a `secrets/broker.env`: **nunca las dos al mismo fichero**.

Borra de `config/caps.yaml` la cuenta de ejemplo `"example_platform_account_id"`:
`make check-secrets` se niega a seguir mientras esté. Puedes dejar `accounts: {}`
(vacío, con las llaves): sin cuentas con tope no se escribe nada, que es el punto.

```bash
make check-secrets                                                         # permisos 0600 y ningún valor de plantilla sin sustituir
make up                                                                    # ads-db → migraciones → ads-broker/ads-api/ads-worker
curl -fsS http://127.0.0.1:8410/api/v1/health
```

Date de alta como dueño. La contraseña entra por stdin: nunca por argumento.

```bash
IFS= read -rsp 'Contraseña del dueño: ' p && printf '%s' "$p" | docker compose run --rm -T --no-deps \
  ads-api python -m safent_ads.tools.seed_owner --email tu@correo.com --password-stdin; unset p
```

Volver a ejecutarlo con el mismo correo sustituye la contraseña del dueño ya
existente: es una herramienta de acceso root local, no un alta con protección
de reintento.

Entra en el panel con ese correo y esa contraseña. Un despliegue, un dueño.

## Conectar un agente

Desde tu copia del repo, en la máquina donde usas los agentes:

```bash
./scripts/instalar-mcp.sh --url https://ads.tudominio.com/mcp --nombre safent-ads --solo codex --background
```

Registra el servidor por HTTP nativo y vincula ese equipo con el panel, sin
copiar claves. Usa `--solo claude` para Claude Code. Comprueba el código que
muestran terminal y panel y autoriza con tu cuenta de dueño. Cada equipo requiere
su propia autorización; repetir la instalación reutiliza una vinculación válida.
Requiere `uv` y el CLI elegido instalado y con sesión iniciada.

`--background` instala un servicio de usuario (macOS/Linux) que recibe trabajos
aprobados, consume cuota del runtime y devuelve resultados al panel. Sin ese flag
queda vinculado, pero debes iniciar el conector manualmente. Conserva el repositorio
y su entorno Python mientras uses el servicio. Las credenciales locales tienen
permisos 0600, caducan a los 30 días y son revocables. No activa anuncios.
Consulta [operación y revocación](docs/runtime-bridge.md).

- Claude Code: `/mcp` → `safent-ads` → **Authenticate** (se abre el navegador).
- Codex: `codex mcp login safent-ads` si el instalador no lo abrió solo.
- Quitar el acceso: panel → **Conexiones → Aplicaciones con acceso → Quitar
  acceso** (te pedirá confirmar con Google o el código, según cómo entraras).

Sin el script, a mano: `claude mcp add -s user --transport http safent-ads
https://ads.tudominio.com/mcp` y `codex mcp add safent-ads --url
https://ads.tudominio.com/mcp`, y luego autorizas igual. Esos comandos nativos sólo
registran herramientas: **no vinculan ni arrancan un runtime local**. También puedes
elegir explícitamente `--solo-mcp` en el script para ese modo avanzado.

Comprueba que responde pidiéndoselo al agente: «usa solo las herramientas
`mcp__safent-ads__*` y lista mis negocios».

### Google Tag Manager

La conexión nativa de Google incluye permisos de Google Ads y de Tag Manager.
El MCP puede inventariar cuentas, contenedores, workspaces, etiquetas,
activadores, variables y versiones con `get_google_tag_manager`. Los cambios
se crean con `propose_google_tag_manager_change`: nunca escribe al proponer.
Después de la aprobación, crear/editar el workspace, crear una versión y
publicarla son operaciones auditadas; publicar una versión es siempre una
propuesta separada y puede fijar su `fingerprint` para detectar deriva.

Una cuenta Google conectada antes de esta función solo tiene el alcance de
Ads. Hay que reconectarla una vez desde **Conexiones** para conceder los
alcances de Tag Manager. El MCP no admite esta ruta con una conexión Google
delegada por Composio: necesita el OAuth nativo, porque el token sigue
residiendo exclusivamente en el bróker.

Si OAuth no está disponible (arranque roto, CI, pérdida del segundo factor),
enciende el bearer de dueño en el servidor: en `secrets/api.env`,
`ADS_MCP_STATIC_TOKEN_ENABLED=true` **y** `ADS_MCP_TOKEN=<bearer largo y
aleatorio>`, los dos o ninguno —vienen comentados en el ejemplo porque un
bearer escrito con la vía apagada es un secreto eterno durmiendo en disco, y
`ads-api` no arranca con la vía abierta y sin bearer—. Luego pásalo **por
stdin**:

```bash
printf '%s' "$TOKEN" | ./scripts/instalar-mcp.sh --url https://ads.tudominio.com/mcp --token-stdin
```

## Topes duros

`config/caps.yaml` es el último control antes de tocar una plataforma real. Lo
lee el bróker; `ads-api` no puede escribirlo.

- Una cuenta sin entrada en el fichero **no puede escribir**, y cada intento
  queda auditado.
- Fichero ausente o inválido: el bróker no arranca y dice cuál es.
- En un despliegue real: `chown root:root config/caps.yaml && chmod 644 config/caps.yaml`.

Una instalación recién hecha deniega el 100 % de las escrituras. Es a propósito.

## Configuración

| Variable | Para qué |
|---|---|
| `ADS_PUBLIC_BASE_URL` | Dominio público de esta instancia. Las cookies y el OAuth dependen de que coincida con el real. |
| `ADS_SINGLE_OWNER_MODE` | Obligatoria (o su alternativa de abajo): sin ninguna de las dos, `ads-api` no arranca. `true` para este repo autoalojado; `make first-run` ya la escribe. |
| `ADS_SEAT_AUTHORITY_ENABLED` | La otra vía: el alta de usuarios la resuelve una consola externa (Enterprise alojado) en vez de esta instancia. Excluyente con la de arriba. |
| `ADS_INSTANCE_NAME` | Cómo se llama este servidor para quien lo autoriza. Defecto: `Ads MCP`. |
| `ADS_BRAND_NAME` | El negocio del que se anuncia. Defecto: `tu negocio`. |
| `ADS_TRUSTED_PROXY_HOPS` | `1` si tienes un proxy con TLS delante. `0` (defecto) solo sin proxy. |
| `ADS_MCP_EXTRA_ALLOWED_HOSTS` | Hosts adicionales aceptados en `/mcp`. Quítalo en cuanto uses tu dominio. |
| `ADS_MCP_STATIC_TOKEN_ENABLED` | Bearer de dueño como vía de emergencia. Apagado por defecto. |
| `ADS_COMPOSIO_API_KEY` | Conectar cuentas en un clic. Vacío: usas tus credenciales de desarrollador. |
| `ADS_GOOGLE_CHANNELS_ENABLED` | Canales de Google permitidos. Defecto: solo `SEARCH`. |
| `ADS_KIT_DIR` | Directorio de solo lectura con el kit de marketing del negocio. |
| `ADS_FEDERATED_LOGIN_ENABLED` | Entrar al panel con Google. Apagado por defecto; sin las cuatro de abajo completas, cerrado. |
| `ADS_GOOGLE_OIDC_CLIENT_ID` | Cliente OAuth de Google. En la consola de Google, autoriza el redirect URI `<ADS_PUBLIC_BASE_URL>/api/v1/auth/federated/callback` (sin barra final). |
| `ADS_GOOGLE_OIDC_CLIENT_SECRET` | Su secreto. En `secrets/api.env`, nunca en `.env`. |
| `ADS_FEDERATED_ALLOWED_EMAILS` | Correos completos que pueden entrar, por comas. Ni dominios ni comodines. **Lista vacía = login federado cerrado.** |

No secretos en `.env`: viven en `secrets/api.env` y `secrets/broker.env`, cada
uno con su `env_file:`, y ningún servicio ve los del otro.

`ADS_SINGLE_OWNER_MODE`, `ADS_TRUSTED_PROXY_HOPS` y
`ADS_MCP_STATIC_TOKEN_ENABLED` de la tabla de arriba viven en
`secrets/api.env` aunque no parezcan secretos: `compose.yaml` solo reenvía a
`ads-api`/`ads-worker` cinco variables fijas desde `.env`
(`ADS_PUBLIC_BASE_URL`, `ADS_DATABASE_URL`, `ADS_BROKER_SOCKET`, `ADS_TZ`,
`ADS_ACTIVE_HOURS`). Cualquier otra puesta en `.env` llegaría inerte — nunca
al proceso —, mientras que `secrets/api.env` entra entero por `env_file:`.

## Modos avanzados

Una línea cada uno. Ninguno hace falta para lo de arriba.

- **Companion** (`ADS_COMPANION_MODE=true` + `compose.companion.yaml`): `ads-api` escucha con TLS propio para un anfitrión que lo preinstala.
- **Autoridad de asientos** (`ADS_SEAT_AUTHORITY_ENABLED=true`, `ADS_ENTERPRISE_ORIGIN`): el alta de usuarios la lleva una consola externa en vez del modo de dueño único.
- **Login federado con Google** (`ADS_FEDERATED_LOGIN_ENABLED` y las tres variables que lo acompañan, arriba): entrar al panel con Google en vez de contraseña.
- **Creatividad local** (`ADS_CREATIVE_LOCAL_ENABLED` + ComfyUI): render de imágenes en GPU propia; si no, respaldo cloud desde el bróker.
- **Avisos por Telegram** (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_IDS` en `secrets/api.env`).

## Copias de seguridad

```bash
make backup                                  # base, almacén cifrado de credenciales y config/caps.yaml
make restore FILE=backups/ads-backup-....dump
```

`backups/` está en `.gitignore`: puede contener datos personales.

## Desarrollo

```bash
make sync lint type test
make test-integration                        # Postgres real vía testcontainers, necesita Docker
make build
cd panel && npm ci --ignore-scripts && npm run lint && npm run typecheck && npm run test && npm run build
```

Antes de un PR, `CONTRIBUTING.md`.

## Dónde vive el diseño

`ARCHITECTURE.md` es el documento de diseño: los cuatro procesos y qué ve cada
uno, las cuatro capas y hacia dónde van las dependencias, el punto único de
escritura con sus siete controles, dónde vive cada control y qué es
configuración y qué es código. Cabe en una página a propósito.

Este README es el punto de entrada —qué es, cómo se levanta, cómo se conecta un
agente—, no el diseño.

## Licencia

Apache-2.0 (`LICENSE`, `NOTICE`). Fallos de seguridad: `SECURITY.md`.

**Sin soporte ni SLA.**
