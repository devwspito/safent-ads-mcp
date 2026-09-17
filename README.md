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
- Bróker: el único proceso que ve las credenciales y el único que escribe.
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
git clone <url-del-repo> safent-ads && cd safent-ads
```

## Verificar la imagen publicada

Cada release firma su imagen con [cosign](https://github.com/sigstore/cosign)
en modo *keyless*: sin clave privada que nadie custodie, la identidad la da el
OIDC de GitHub Actions y la firma queda registrada en el log público de
[Sigstore](https://www.sigstore.dev/) (Rekor). La imagen lleva además
procedencia SLSA completa y un SBOM SPDX como *attestations* del propio
manifiesto.

Sustituye `<owner>/<repo>` por la ruta de este repositorio en GitHub, en
minúsculas (`ghcr.io/${GITHUB_REPOSITORY,,}` en el propio `release.yml` —
GHCR rechaza mayúsculas, así que coincide siempre con la URL del repo pero
en minúsculas):

```bash
cosign verify "ghcr.io/<owner>/<repo>@<digest>" \
  --certificate-identity-regexp '^https://github.com/<owner>/<repo>/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Verifica siempre por **digest** (`docker buildx imagetools inspect
ghcr.io/<owner>/<repo>:vX.Y.Z --format '{{.Manifest.Digest}}'` si solo tienes
el tag a mano), nunca por tag: un tag es mutable, un digest no. Comprobar la
procedencia y el SBOM antes de confiar en una imagen:

```bash
docker buildx imagetools inspect "ghcr.io/<owner>/<repo>@<digest>" --format '{{ json .Provenance }}'
docker buildx imagetools inspect "ghcr.io/<owner>/<repo>@<digest>" --format '{{ json .SBOM }}'
```

## Primer arranque

Un paso por línea.

```bash
cp .env.example .env
cp secrets/api.env.example secrets/api.env
cp secrets/broker.env.example secrets/broker.env
cp config/caps.example.yaml config/caps.yaml
chmod 600 secrets/api.env secrets/broker.env
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
make check-secrets                                                         # permisos 0600 y ningún `change-me` suelto
make up                                                                    # ads-db → migraciones → ads-broker/ads-api/ads-worker
curl -fsS http://127.0.0.1:8410/api/v1/health
```

Date de alta como dueño. La contraseña entra por stdin: nunca por argumento.

```bash
read -rsp 'Contraseña del dueño: ' p && printf '%s' "$p" | docker compose run --rm -T --no-deps \
  ads-api python -m safent_ads.tools.seed_owner --email tu@correo.com --password-stdin; unset p
```

Volver a ejecutarlo con el mismo correo sustituye la contraseña del dueño ya
existente: es una herramienta de acceso root local, no un alta con protección
de reintento.

Entra en el panel con ese correo y esa contraseña. Un despliegue, un dueño.

`POST /mcp` sin credencial responde `401`: es la respuesta correcta, la puerta
pide autorización. `/mcp` no se abre en el navegador; la interfaz para personas
es el panel.

Un solo comando sustituirá estos pasos.

## Conectar un agente

Desde tu copia del repo, en la máquina donde usas los agentes:

```bash
./scripts/instalar-mcp.sh --url https://ads.tudominio.com/mcp --nombre safent-ads
```

Registra el servidor por HTTP nativo en Claude Code y/o Codex, en ámbito
usuario. Sin token: ningún secreto queda en esa máquina.

- Claude Code: `/mcp` → `safent-ads` → **Authenticate** (se abre el navegador).
- Codex: `codex mcp login safent-ads` si el instalador no lo abrió solo.
- Quitar el acceso: panel → **Conexiones → Aplicaciones con acceso → Quitar
  acceso** (te pedirá confirmar con Google o el código, según cómo entraras).

Sin el script, a mano: `claude mcp add -s user --transport http safent-ads
https://ads.tudominio.com/mcp` y `codex mcp add safent-ads --url
https://ads.tudominio.com/mcp`, y luego autorizas igual.

Comprueba que responde pidiéndoselo al agente: «usa solo las herramientas
`mcp__safent-ads__*` y lista mis negocios».

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
