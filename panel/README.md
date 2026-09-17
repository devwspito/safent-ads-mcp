# Safent Ads — Panel

SPA de operaciones (Vite + React 18 + TypeScript estricto). Consume `ads-api` en `/api/v1` (ver `contracts/rest-api.md`).

## Desarrollo

```bash
npm install
npm run dev        # http://localhost:5173, con MSW: datos de ejemplo, sin backend
VITE_API_MOCK=0 npm run dev   # contra un ads-api real en local
```

`npm run dev` arranca con mocks (Mock Service Worker) activados por defecto — no hace falta backend para trabajar en el panel. `VITE_API_MOCK=0` los desactiva y las peticiones van a `VITE_API_BASE` (por defecto `/api/v1`, pensado para correr detrás del mismo origen que `ads-api`). En producción (`vite build`) los mocks están excluidos del bundle salvo que se fuerce `VITE_API_MOCK=1` explícitamente.

## Comandos

- `npm run dev` — servidor de desarrollo.
- `npm run build` — `tsc --noEmit` + `vite build` (falla si hay errores de tipos).
- `npm run typecheck` — solo tipos.
- `npm run lint` — ESLint, `--max-warnings 0`.
- `npm test` — Vitest + Testing Library.

## Cómo sirve `ads-api` el `dist/` (T053)

`ads-api` (FastAPI) sirve el panel como estático en `/`, y su propia API bajo `/api/v1`; no hay Node en producción. El flujo esperado:

1. `npm run build` genera `panel/dist/` (HTML + JS/CSS con hash + `mockServiceWorker.js`, que en build de producción no se registra porque `VITE_API_MOCK` no está fijado a `1`).
2. La imagen/target `api` del `Dockerfile` (`compose.yaml`, `ads-api`) copia `panel/dist/` a la ruta que FastAPI monta como estático — p. ej. `StaticFiles(directory="panel_dist", html=True)` montado en `/`, con `/api/v1/*` resuelto antes por los routers de la API para que no colisione con el fallback de `index.html` de la SPA.
3. Las rutas de cliente (`/cartera`, `/campanas/*`, etc.) necesitan un fallback a `index.html` en el servidor (SPA con `react-router` en modo `BrowserRouter`, no hash routing) para que refrescar en una ruta profunda no dé 404.
4. La cookie de sesión (`ads_session`) y el panel comparten origen, así que `credentials: "include"` (ya configurado en `src/api/client.ts`) basta sin CORS.

La integración exacta del build en el pipeline de `ads-api` (Dockerfile, montaje de estáticos, fallback SPA) es trabajo de `backend-engineer`/`devops-engineer` — este documento solo fija el contrato de dónde vive el `dist/` y qué asume el cliente.

## Estructura

```
src/
  api/            cliente HTTP + esquemas Zod + hooks de TanStack Query
  components/     layout, estados comunes, KPI, señales, ticker, campañas
  hooks/          atajos de teclado, filtros ↔ URL, negocio seleccionado
  mocks/          handlers de MSW + fixtures deterministas
  routes/         una vista por archivo, ocho rutas + login + ajustes
  styles/         tokens.css (panel-visual-spec.md §1) + global.css
  utils/          formato es-ES, tiempo relativo, etiquetas de dominio
```

## Subida de ficheros en tests (T220)

`src/test/setup.ts` sustituye `fetch`/`Request`/`Response`/`Headers`/`FormData` globales por los
de `undici` (devDependency, sin CVEs nuevos — `npm audit`) y añade `Blob.prototype.text`/
`.arrayBuffer`/`.stream` (jsdom no los implementa): sin esto, cualquier test que suba un fichero
real a través de MSW (`CsvUpload`/`ConversionsCard`, `POST /conversions/import`) se queda colgado
para siempre, o el fichero llega al handler con 0 bytes. `File`/`Blob` en sí NO se sustituyen —
`user.upload()`/arrastrar-y-soltar construyen sus ficheros con la clase interna de jsdom pase lo
que pase, así que sustituirlas rompería su reconocimiento por `<input type=file>`.

## Reautenticación TOTP (`X-Reauth-Token`)

`rest-api.md` §Seguridad transversal exige un TOTP fresco (<5 min) en cuatro mutaciones que
cambian la postura de seguridad: credenciales de desarrollador (`PUT /platform-apps/{platform}`),
confirmaciones de la puerta de autonomía (`POST /rules/autonomy-gate/confirmations`), emparejar
Telegram (`POST /telegram/pairing/start`) y generar el token del webhook de conversiones
(`POST /conversions/webhook-token`). Las cuatro comparten un único mecanismo:

- `hooks/useReauthedMutation.ts` guarda la acción pendiente (`start`), abre el prompt compartido
  (`components/common/TotpReauthPrompt.tsx`) y solo llama a la mutación real cuando hay un código
  (`confirm`) — ninguna petición sale sin la cabecera.
- Un 401 `REAUTH_REQUIRED` (código incorrecto/caducado) o un 429 (bloqueo por intentos) se
  traducen con `utils/apiError.ts::describeReauthError` y se muestran dentro del propio prompt,
  sin tocar el resto del formulario ni disparar la redirección global a `/login` — esa redirección
  (`api/client.ts::onUnauthorized`) sigue reservada al 401 `UNAUTHORIZED` real (sesión caducada).
- `components/rules/AutonomyGateQuestions.tsx` es la superficie que faltaba para confirmar las
  preguntas 2, 3 y 8 de `spec.md` por cuenta (antes `useConfirmAutonomyGate` no tenía ningún sitio
  desde el que llamarse); vive bajo la franja "Autonomía deshabilitada" de `ReglasPage`.

## Pendiente / seguimiento

- **Fuente Inter Variable**: `src/styles/global.css` referencia `/public/fonts/InterVariable.woff2`, que aún no está alojado (sin acceso de red para traerlo en esta sesión). El panel funciona con la pila de sistema de `--font-sans` mientras tanto; falta descargar la fuente con licencia OFL y colocarla en `panel/public/fonts/`.
- **Revocar una cuenta y pegar el System-User-token de Meta**: no están en `rest-api.md` (sólo `reconnect/start|status|callback`). Antes de construirlos hay que confirmar con `tech-lead`/`backend-engineer` si entran en una v3 del contrato o si "revocar" se resuelve desactivando la cuenta desde el bróker.
- **Auditoría de dependencias**: `npm audit` señala CVEs de gravedad alta/crítica en `react-router-dom` (open redirect, no explotable aquí porque no pasamos URLs de usuario a `navigate`/`Link`), `vite`/`esbuild` (solo servidor de desarrollo) y `vitest` (solo si se expone la UI de Vitest, que no usamos). Arreglarlos del todo exige saltar de major (`react-router` 7, `vite` 8, `vitest` 5), fuera del alcance fijado (Vite 5 / react-router 6) — a decidir con `security-engineer`/`tech-lead`.
