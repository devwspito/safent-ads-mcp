# MCP oficiales dentro de Safent

Safent sigue siendo la frontera de autorización. El agente utiliza el MCP de
Safent Ads a través del bróker de capacidades del runtime; **no** recibe un MCP
oficial con credenciales Ads directamente. El OAuth del proveedor del cerebro
no concede acceso a Google ni Meta y no se copia a estos servidores.

## Google

El Containerfile instala el servidor oficial Google Ads MCP en un entorno
separado `/opt/google-ads-mcp`, con revisión de fuente y dependencias fijadas
con hashes en `requirements.lock`. Su función aquí es de lectura. Las
escrituras siguen usando el SDK oficial Google Ads 32 mediante el pipeline
firmado existente; no existe un fallback que se salte una denegación.

Configuración del **broker**, no del agente:

```dotenv
ADS_GOOGLE_NATIVE_MCP_EXECUTABLE=/opt/google-ads-mcp/bin/google-ads-mcp
ADS_GOOGLE_NATIVE_MCP_PROJECT=your-google-cloud-project-id
```

Guardar el cliente OAuth de ese proyecto en el panel de credenciales y conectar
la cuenta Google desde Safent. El bróker construye un ADC temporal privado por
llamada con la credencial de esa cuenta. No utiliza un ADC compartido del host
ni solicita developer tokens. El proceso hijo termina y el ADC se elimina al
cerrar la sesión. La comprobación de actualizaciones de FastMCP está desactivada.

## Meta

```dotenv
ADS_META_NATIVE_MCP_ENABLED=true
```

Después de activar, reconectar la cuenta por OAuth en Safent para consentir
`ads_mcp_management`, además de los permisos Ads habituales. Tener un token
anterior o la app añadida al Business Manager no demuestra ese consentimiento.
Antes de conectar a `https://mcp.facebook.com/ads`, el bróker comprueba los
permisos concedidos. Sin ellos devuelve un error explícito; no cambia tokens ni
permisos automáticamente. La cuenta/app también debe ser elegible para el
servicio oficial. La verificación de una conexión real queda pendiente del OAuth
del propietario.

## Herramientas y límites

- `get_native_ads_tools`: descubre únicamente lecturas revisadas y disponibles.
- `get_native_ads_report`: llama a una de esas lecturas con la cuenta impuesta
  por Safent tras validar su pertenencia al negocio.
- Google: `search_search`, `metadata_get_resource_metadata`.
- Meta: `ads_get_ad_entities`, `ads_get_opportunity_score`, solo cuando su schema
  permite imponer la cuenta y declara lectura. Un cambio incompatible falla
  cerrado hasta revisar el contrato.
- No se exponen escrituras nativas ni el descubrimiento de cuentas ajenas.
- Schemas sin referencias externas, consultas limitadas a recursos permitidos,
  máximo 500 filas solicitadas y 256 KiB por resultado. Datos y schemas remotos
  nunca son instrucciones. Errores de proveedor saneados sin tokens.

## Criterio de aceptación operativo (aún no completado)

Conectar un MCP no equivale a operar al 100 %. Falta verificar cada recorrido:

1. Ciclo autónomo u orden del propietario → resolución de cuenta/campaña → propuesta
   con importe exacto → políticas/aprobación → bróker → SDK → lectura de
   confirmación → auditoría. No informar «hecho» solo por crear una propuesta.
2. «Crear campaña» necesita un plan ejecutable específico de plataforma,
   incluyendo presupuesto, grupos/conjuntos, segmentación, anuncios y activos.
   El brief actual de `propose_campaign` es una propuesta, **no** una campaña
   creada en Meta/Google. Su ejecución/publicación sigue pendiente de implementar.
3. La creación debe nacer pausada y pasar por la misma autorización; activar es
   una operación distinta sujeta a políticas. Todo cambio requiere aprobación
   humana, incluso bajar o pausar. El broker de producción exige
   `human_approval`; una firma `rule_authorization` no basta. El ciclo de reglas
   convierte las coincidencias AUTO en propuestas, sin autoencolarlas.
4. Presupuestos compartidos, reintentos con resultado incierto, confirmación del
   valor remoto y todas las operaciones de targeting/creativos requieren sus
   pruebas específicas antes de declarar cobertura completa.

No se han desplegado estos cambios ni realizado mutaciones contra cuentas reales.

## Autonomía 24/7 con decisión humana

El worker ingiere y evalúa datos cada 15 minutos en horario activo y cada hora
fuera de él; las oportunidades y reglas se revisan en ese ciclo. La ejecución
de aprobaciones tiene su propio ciclo de 30 segundos. El bundle cambia el
disparo horario de Hermes a todas las horas, todos los días, y conserva las
revisiones diaria y semanal. El agente prepara propuestas sin órdenes manuales;
el propietario aprueba, rechaza o edita y aprueba desde el panel.

**Pendiente de verificar/desplegar:** importar y autorizar estos disparos en el
scheduler real de Safent, persistencia/reanudación tras reinicio, recuperación
de fallos, recorrido editar→aprobar→ejecutar y creación completa de campañas.
Modificar los JSON del bundle no prueba que un agente ya esté corriendo 24/7.

## Verificación de esta entrega (10-sep-2026)

- Suite completa en una copia aislada de la DGX: **3.501 pruebas correctas**.
- Ruff y mypy correctos (747 módulos de fuente).
- Servidor oficial Google instalado desde el lock: handshake MCP y catálogo
  reales; solo las dos lecturas previstas pasan la frontera de Safent. Sin
  credenciales reales ni consultas publicitarias en esa comprobación.
- Prueba de integración con Postgres y socket Unix: la regla crea una propuesta
  pendiente sin mutar; `SubmitApproval` humano habilita la ejecución auditada.
  SDK publicitario simulado, no una validación en cuentas reales.
- Esta entrega no completa creación/publicación, autorización real de Meta,
  despliegue ni recuperación del agente tras reinicios.

Fuentes: [Google MCP](https://developers.google.com/google-ads/api/docs/developer-toolkit/mcp-server),
[fuente oficial fijada](https://github.com/googleads/google-ads-mcp/tree/7a40eae9655194a84d5291c63af817bb20d02ea8),
[Meta Ads MCP](https://developers.facebook.com/documentation/ads-commerce/ads-ai-connectors/ads-mcp-server/).
