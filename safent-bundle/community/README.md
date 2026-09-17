# Agente de Anuncios en Safent Community

Community no recibe bundles firmados: el propietario carga a mano lo que en Enterprise llega en `enterprise/agent_template.json`. Son tres pasos y ninguno requiere terminal.

Antes de empezar: `ads-api` accesible por HTTPS en `https://ADS_HOST/mcp` (Tailscale Funnel, T124) y la sesión de Codex iniciada en la jaula (`hermes auth add openai-codex`, código de dispositivo).

## 1. Añadir el MCP remoto `safent-ads`

1. Safent → **Ajustes** → **MCP** → **Añadir servidor**.
2. Tipo: **Remoto gestionado**. Slug: `safent-ads`. Etiqueta: `Safent Ads`.
3. Comando: `npx -y mcp-remote https://ADS_HOST/mcp` (sustituye `ADS_HOST` por tu host; sin variables de entorno).
4. **Guardar**. Safent abre el navegador para la autorización OAuth del servidor; concede y vuelve.
5. Comprueba en el listado que el servidor aparece **Conectado** y que las herramientas empiezan por `list_`, `get_`, `explain_`, `propose_`, `apply_defensive_action`. No debe existir ningún `approve_*`.

## 2. Crear el agente

1. **Agentes** → **Nuevo agente**. Nombre: `Agente de Anuncios`. Idioma: español.
2. **Misión**: pega `agent/primary_mission.md`. **Instrucciones**: pega `agent/instructions.md`. **Reglas de oro**: una por línea, las de `agent/golden_rules.json`.
3. **Proveedor**: `Codex (ChatGPT)`. Modelo: `gpt-6-astra`. Respaldo: `gpt-5.6-sol`. (Reparto por tarea en `models.yaml`.)
4. **Herramientas** → **MCP** → marca `safent-ads`. En la política por herramienta, deja habilitadas todas menos `withdraw_proposal` (véase `policy/policy_overlay.json`).
5. **Guardar**. Abre un chat y pide «lista mis negocios»: debe responder con `list_businesses` sin tarjeta de aprobación.

## 3. Autorizar los tres disparadores

Por cada fichero de `triggers/` (`hourly-review.json`, `daily-structural.json`, `weekly-opportunities.json`):

1. **Automatizaciones** → **Nueva programada**.
2. Agente: `Agente de Anuncios`. Título: el campo `title`. Cron: `scope_value` (zona horaria Europe/Madrid). Instrucción: `task_instruction`.
3. Capacidades permitidas: exactamente `allowed_capabilities`. Techo de riesgo: **bajo**. Repetible (no de un solo uso).
4. **Autorizar** y confirma la acción exacta en el panel. Community no pide OTP; el disparador conserva su firma y los límites de la jaula.

Resultado: tres entradas activas en el calendario; la horaria se dispara de 08:00 a 21:00, la estructural a las 07:15 y la de oportunidades los lunes a las 09:00.

## Qué NO configurar

- Telegram en Safent. El ticker, las tarjetas y el freno viven en el sidecar `ads-api`. Si un gateway de Telegram apareciera en Safent, déjalo apagado.
- Ninguna variable con credenciales de Google o Meta: la jaula no las necesita ni debe verlas.
- Nada que aumente gasto en modo automático: el servidor lo rechaza igualmente, pero no lo pidas.

## Comprobación final

1. `/estado` en Telegram responde con la hora del último dato.
2. En el siguiente disparo horario, Safent registra una ejecución y el ticker llega por Telegram desde el sidecar.
3. Activa el freno (`/freno on`, dos toques) y espera un ciclo: el informe empieza por «FRENO ACTIVO · solo propuestas» y no hay acciones.
