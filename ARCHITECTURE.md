# Arquitectura

Una página: el diseño completo de este servidor, sin más documentos detrás.

La invariante que manda sobre todo lo demás: **nada llega a una plataforma de
anuncios sin aprobación humana y sin un tope duro por cuenta.** El resto del
diseño existe para que esa frase sea cierta incluso con la API comprometida.

## Procesos

| Proceso | Qué hace | Qué ve |
|---|---|---|
| `ads-api` | REST del panel, servidor MCP, servidor OAuth 2.1, sirve el SPA | La base. **Nunca** las credenciales de plataforma ni el fichero de topes |
| `ads-worker` | Ciclos de ingesta, señales, reglas, avisos y ejecución | Lo mismo que `ads-api` |
| `ads-broker` | Único punto de escritura hacia Google Ads, Meta Ads y Google Tag Manager | Las credenciales cifradas y el fichero de topes |
| `ads-db` | PostgreSQL | — |

El panel es un SPA que `ads-api` sirve desde la misma imagen; no es un
despliegue aparte.

El conector opcional de catálogo y stock es de **solo lectura**, independiente
de las campañas. Su origen HTTPS lo fija el operador; el dueño introduce el
token en Conexiones. Igual que la conexión Cloudflare, se guarda cifrado en
la base de `ads-api`, con separación criptográfica por negocio y origen. No
se expone por MCP ni se envía al navegador de vuelta. Las lecturas usan tres
rutas cerradas, paginación acotada, filtrado de campos y protección SSRF;
no existe operación de escritura de catálogo o inventario.

Los planes de lanzamiento son material de revisión del directorio del kit,
aislado por negocio. Solo la landing y sus activos explícitamente marcados
como públicos se sirven sin sesión. Los documentos y vídeos cargados en el
panel siguen privados: subir un vídeo no publica un anuncio ni autoriza gasto.

## Capas

Cada contexto (`accounts`, `proposals`, `execution`, `metrics`, `signals`,
`rules`, `iam`, `mcp`, `mcp_oauth`, `broker`, …) repite las mismas cuatro:

- **domain** — reglas de negocio puras. Sin framework, sin SQL, sin HTTP.
- **application** — casos de uso y **puertos** (interfaces) que necesitan.
- **infrastructure** — adaptadores: SQL, ficheros, red. El bróker añade
  `platforms/`: adaptadores de proveedor (Google Ads, Meta y Tag Manager) sobre un
  `write_pipeline` común, más los clientes vivos de cada SDK.
- **presentation** — REST, herramientas MCP, socket del bróker, bot.

Las dependencias van hacia dentro: presentación → aplicación → dominio. La
infraestructura implementa puertos de aplicación, nunca al revés.
`composition/` es el único sitio que cablea las piezas y casi el único que lee
el entorno: la excepción es `mcp/infrastructure/env_capability_probe.py`, que
mira si una clave BYOK está **presente**, nunca su valor.

## El punto único de escritura

Ninguna escritura sale de `ads-api`. Todas pasan por el bróker, por un **socket
unix**, y el bróker las vuelve a validar como si no se fiara de quien llama —
porque no se fía:

1. `SO_PEERCRED` sobre el socket: solo los uid declarados en
   `ADS_BROKER_ALLOWED_UIDS`.
2. Esquema tipado por operación. Una operación sin esquema cae en `DENIED`.
3. Recalcula el `diff_hash` del cambio: lo que se firmó es lo que se ejecuta.
4. Verifica la firma **Ed25519** de la autorización con la clave pública.
5. Resuelve el tope duro de esa cuenta y lo aplica.
6. Idempotencia persistida y comprobación de deriva contra el estado remoto.
7. Solo entonces llama al SDK de la plataforma.

Las credenciales de las cuentas viven cifradas en el disco del bróker
(`ADS_CREDENTIAL_STORE_DIR`, clave `ADS_CREDENTIAL_MASTER_KEY`) y no salen de
ahí: quien pide una escritura nombra la cuenta, nunca lleva el token.

El par de firma está repartido a propósito: la semilla privada en
`secrets/api.env`, la pública en `secrets/broker.env`. Nunca las dos juntas.

Tag Manager reutiliza la identidad OAuth nativa de Google, pero no se trata
como un proxy HTTP abierto. El MCP solo admite recursos y acciones de una
lista cerrada. Crear o editar entidades, crear una versión y publicar esa
versión pasan por el mismo `diff_hash`, firma, idempotencia y libro del
bróker que una mutación publicitaria. Publicar es un cambio independiente:
aprobar un workspace no concede automáticamente publicarlo.

## Dónde vive cada control

| Control | Dónde vive | Qué pasa si falta |
|---|---|---|
| Topes duros por cuenta | `config/caps.yaml`, root-only, leído por el bróker | Cuenta sin entrada: escritura denegada y auditada. Fichero ausente o inválido: el bróker no arranca |
| Freno de emergencia | Base de datos, global o por negocio; se comprueba antes de ejecutar y el agente lo ve en `get_capabilities` | Activo: la acción se detiene con su motivo |
| Aprobación humana | Panel: el dueño firma la autorización con Ed25519 en `ads-api` | Sin firma válida el bróker no escribe |
| Acceso de los agentes | Servidor OAuth 2.1 propio (registro dinámico + PKCE, consentimiento, revocación) en `ads-api` | Sin autorización, `401` con descubrimiento. Revocar corta al instante |
| Identificación fresca | TOTP por acción o sesión federada reciente, en el borde REST | Sin presencia demostrada, `401 REAUTH_REQUIRED` |
| Cuotas del MCP | Límites por herramienta y por minuto (`ADS_MCP_QUOTA_*`) | Se rechaza la llamada, no se degrada el servicio |
| Auditoría | Registro de decisiones encadenado en la base | Toda decisión y toda denegación quedan escritas |
| Permisos del agente | Tres servidores MCP: `ads-view`, `ads-propose`, `ads-approve` | Una herramienta fuera del nivel concedido no existe para ese agente |

Los tres nombres de servidor son **niveles de permiso**, no marca: no cambian.

## Qué es configuración y qué es código

Configuración (cambia entre despliegues, nunca se toca el código):

- `ADS_INSTANCE_NAME` — cómo se llama **este servidor** para quien lo autoriza:
  metadatos OAuth, título del panel, pantalla de consentimiento.
- `ADS_BRAND_NAME` — el **negocio del que se anuncia**: instrucciones del MCP y
  descripciones del catálogo.
- `.env` (no secreto), `secrets/api.env` y `secrets/broker.env` (0600, cada uno
  con su `env_file:`), `config/caps.yaml` (root).
- El directorio de despliegue de cada operador: su compose, su unidad de
  sistema, su proxy, sus runbooks. Vive fuera del motor y el motor no lo cita.

Código es todo lo demás. Un despliegue que necesite un parche en `src/` para
funcionar es un fallo de este diseño, no una particularidad suya.

## Lo que no hay

Ninguna telemetría. Ninguna credencial de fábrica: toda integración de terceros
la aporta el operador. Un despliegue, un dueño; ni asientos ni facturación.
