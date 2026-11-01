# Motor audiovisual: aprendizaje y contrato de integración

Un contenedor MP4 puede ser un pase de imágenes. La petición de vídeo necesita metraje con movimiento del sujeto. El compositor de imágenes y el generador de metraje son capacidades distintas; el sistema no debe sustituir una por otra silenciosamente para declarar el encargo terminado.

El motor compañero está en devwspito/safent-pet-gtm-engine, sin marcas ni datos de clientes en su código. Expone planificación/cola/consulta/cancelación por MCP stdio. Su manifiesto de exportación incluye negocio, especie, necesidad, fuentes, oferta, condiciones, caducidad, URL/contexto y procedencia de cada plano. La salida permanece needs_review y publication_authorized=false. Este documento no implica que el servidor ya esté registrado en el despliegue del MCP de Ads.

La secuencia editorial mínima es comportamiento observable → consecuencia proporcionada → ayuda → reserva. Un cambio de conducta no autoriza un diagnóstico, ni mostrar dolor extremo, un resultado clínico falso o un paciente sintético presentado como real. La landing y confirmación deben conservar especie, oferta y motivo; una pieza potente no compensa una reserva que falla.

Verificar la duración de la narración por plano antes de gastar GPU. Guardar revisión del modelo, semilla y huellas de entrada/salida. Una caché deja de ser válida si cambia el brief, el proveedor o la procedencia/derechos. Una oferta caducada se rechaza antes de encolar y producir.

Las pruebas técnicas verifican protocolo MCP, aislamiento, persistencia, cancelación y exportación real de imagen en movimiento más audio. No prueban continuidad de identidad, anatomía, naturalidad de manos/herramientas, lectura del dolor, valor de producción ni rendimiento comercial. Exigir una revisión por plano independiente de la existencia del archivo.

Una prueba local de un modelo pequeño generó movimiento pero deformó un utensilio y no hizo visible el problema prometido. Se rechazó como pieza final. La lección es comparar modelos/referencias con la misma escena y evaluar el resultado real, en vez de confundir rapidez de render con calidad publicitaria.

En hardware compartido: GPU inicialmente libre, exclusión por tanda y guardia de memoria/temperatura/telemetría. Si se interrumpe una prueba, conservar el fallo real; no rebajar protecciones ni ocultar una previsualización como pieza de alta calidad.

Para validar el funnel: leer el destino efectivo de cada anuncio, recorrer ambas especies hasta el formulario y contrastar descuentos/horas. Abrir la página con HTTP200 no confirma el envío final. Las pruebas aisladas de ese envío tampoco son una cita real en producción; distinguir ambas evidencias explícitamente.

## Aprendizaje de la segunda revisión visual

Un síntoma visual físicamente absurdo invalida una referencia aunque la iluminación
y el animal parezcan fotográficos. Se rechazó pelo supuestamente apelmazado que
parecía rosetas enrolladas. El I2V puede conservar fielmente ese error: revisar la
referencia antes de gastar GPU, documentar qué se observa y bloquear referencias
rechazadas. No añadir problemas a mascotas reales para fabricar antes/después.

Separar la cobertura editorial de la cobertura producida: veterinaria/perro,
veterinaria/gato, peluquería/perro y peluquería/gato deben figurar por separado.
Un ejemplo de peluquería no demuestra que veterinaria esté resuelta. El catálogo
incorpora cambios de movilidad, salud bucal, revisión de vacunas, picor y viaje,
con fuentes primarias y exclusiones por concepto. No convertirlos en diagnósticos
ni extender una oferta de consulta a productos, pruebas o documentación.

Aceptar un parámetro de imagen en una API no prueba que condicione el resultado.
En un runtime fijado se comprobó que una variante destilada ignoraba el primer
fotograma; el pipeline base sí lo codificaba. También se detectó un límite de
resolución impuesto por el preprocesado y un camino VAE que evitaba el tiling.
Guardar revisión de modelo/runtime, referencia, parámetros y resolución fuente;
no llamar HD nativo a un lienzo ampliado. Mantener los fallos de memoria y las
guardias, en vez de rebajar umbrales para terminar un render.

Medir copys con la fuente real antes de producir, separar CTA/condiciones/aviso,
y distinguir el origen por plano en montajes mixtos. Inventariar archivos y tomas
originales: seis versiones editadas de un mismo vídeo no son seis rodajes.
La transcripción automática ayuda a comprobar la locución y el descuento, pero no
prueba expresividad, calidad sonora, claridad visual o rentabilidad comercial.

## Resolución, memoria y comprobaciones que se aplicaron al motor

El adaptador ahora registra dimensiones nativas por plano y rechaza reducciones
silenciosas en el perfil nativo. En el runtime fijado se separó difusión y VAE:
latentes safetensors con huella, salida del primer proceso y VAE teselado en otro
proceso supervisado. La conversión puede reanudarse sin repetir el muestreo si
coinciden referencias, parámetros y checksum. Un ensayo completo generó4s a
736×1280 en566,9s en GB10, pico66°C, manteniendo45GiB libres como mínimo exigido
por la guardia y sin relajar el tope térmico. Es un resultado de ese ensayo, no
una promesa de rendimiento ni de calidad publicitaria universal.

La diversidad de fotogramas excluye audio: una imagen fija con voz no pasa por
vídeo. Fuentes con color distinto se normalizan antes de concatenar para evitar
reinicios de filtros que pierdan muestras. Ambas regresiones tienen prueba real.
Las instrucciones negativas por escena ayudan con elementos ajenos, pero no
sustituyen revisar anatomía, manos y objetos en todo el fragmento elegido. Una
salida720p con planos480p conserva esa limitación: resolución de exportación,
realismo, claridad del problema y aprobación comercial son criterios separados.

La comprobación MCP completa debe incluir guiones personalizados por escena y
material del encargo, no sólo el ejemplo por defecto. Se detectó un fallo entre
planificación y cola por persistir campos del compilador dentro del brief. Se
corrigió sin hacer permisiva la entrada pública: contrato de entrada persistido,
lectura específica del formato interno antiguo y manifiesto público válido. Una
reproducción real completó plan→cola→worker→needs_review con vídeo10s, sin publicar.
Las44 pruebas incluyen ahora esa regresión. Un protocolo que inicializa o un
archivo que existe no prueban por sí solos el recorrido completo del usuario.

La capacidad depende también de la duración. En el perfil GB10 probado, una
petición nativa de6s se paró por memoria antes del primer paso de difusión,
aunque el VAE ya estaba separado. No relajar la guardia para completarla. Dos
tomas de3s sí generaron un tramo montado de6s, con el último fotograma revisado
de la primera como referencia de la segunda y procedencia de ambas conservada.
No describir ese montaje como una única toma generada de6s ni prometer continuidad
por aceptar una referencia: comprobar la unión, postura, manos, identidad y acción.
La orquestación de continuación se está ejercitando por el operador; no se afirma
que sea una capacidad automática del adaptador público.

La selección editorial también necesita procedencia. Una generación de manto
normal añadió una figura ajena al final del fondo. Se descartó esa cola y se
montó únicamente una ventana inicial revisada, con ralentización e interpolación
de movimiento registradas. Guardar ventana fuente, factor temporal, algoritmo,
huella de origen/salida y motivo del descarte. Un plano ralentizado conserva su
origen generado; no se convierte en grabación real. Nunca alterar la marcha,
respiración o reacción de un animal para fabricar evidencia de dolor o enfermedad.

La tanda posterior sustituyó las fuentes480p de los pilotos por fuentes que
cubren720p y entregó las cuatro combinaciones servicio/especie. No equivale a
aprobación de alta producción ni prueba de conversión. Mantener necesidades de
revisión y destinos todavía desalineados como pendientes explícitos, aunque CI,
protocolo y resolución pasen.
