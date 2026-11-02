# Conciliación de reservas y continuidad de campañas locales

No concluir que una reserva falta enMeta sólo porque no aparece en `actions`.
Consultar también `conversions` y `cost_per_conversion`,con la ventana y periodo
del conjunto. `schedule_total` y `schedule_website` pueden describir la misma
reserva; no sumarlas. Un eventoLead emitido para esa misma cita tampoco añade
otra cita. Registrar una corrección explícita si una consulta incompleta llevó
a un diagnóstico incorrecto.

Conciliar referencia de reserva,identificador de anuncio,UTMs,clickIDs presentes,
consentimiento,acuse del servidor y métrica atribuida. Los eventos recibidos en
el píxel no equivalen por sí solos a citas únicas ni a atribución de campaña.
La atribución declarada por el tutor/operador se conserva separada de la medida.
No inventar consentimiento o identificadores para completar un informe.

Una reserva sin señales de canal no pasa automáticamente aGoogle o aorgánico.
No confundir la falta de consentimiento con un fallo de seguimiento ni usarCAPI
para eludir esa decisión. No exportar contactos o clickIDs en evidencia pública;
usar contadores,banderas de presencia y referencias privadas cuando hagan falta.

El coste por cita de un anuncio no es elCACdel conjunto,campaña ocohorte. Mostrar
el nivel y periodo del gasto. Mantener reservas,servicios realizados,cobros y
repetición como métricas distintas.

Para campañas locales,verificar estado vivo e historial de cambios después de
una pausa. Una ficha vinculada con anuncios aprobados no sirve mientras la
campaña esté pausada. Leer presupuesto,estado y elegibilidad después de actuar;
no activar campañas históricas o aumentar gasto para reparar una pausa.

Búsqueda con recursos deubicación es una vía de acceso a anuncios locales enMaps;
no supone emplazamiento exclusivo ni asegura impresiones. Distinguir configuración,
elegibilidad y entrega real. [Documentación deGoogle](https://support.google.com/google-ads/answer/3246303?hl=es).
