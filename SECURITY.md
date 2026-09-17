# Política de seguridad

## Este proyecto no aloja nada

Cada operador responde de su instancia y de sus datos. No hay servicio
gestionado, no hay servidor nuestro en medio y no se envía ninguna telemetría
a nadie: el software no llama a casa. Un incidente en tu instalación es tuyo;
un fallo en el código es nuestro.

## Cómo reportar un fallo

Por **GitHub Security Advisories**: pestaña *Security* del repositorio →
*Report a vulnerability*. El aviso es privado hasta que se publica.

No abras un issue público, no lo cuentes en un PR y no lo mandes por correo.

Incluye versión, configuración relevante (sin secretos) y los pasos mínimos
para reproducirlo.

## Versiones soportadas

Solo la **última versión menor publicada**. No hay backports a versiones
anteriores: la vía de arreglo es actualizar.

## Divulgación

Divulgación coordinada a **90 días** desde el reporte. Pasado ese plazo, el
aviso se publica con lo que haya, esté arreglado o no.

No hay SLA: no se compromete ningún tiempo de respuesta.

## Sin recompensa

**No hay programa de recompensas.** No se pagan hallazgos. El crédito en el
aviso es tuyo salvo que pidas lo contrario.

## Fuera de alcance

- Instancias de terceros: repórtaselo a quien la opera, no aquí.
- Fallos que exigen ser ya el dueño de la instancia o tener root en su máquina.
- Ausencia de defensa en profundidad sin un impacto demostrable.
- Dependencias de terceros: repórtalo aguas arriba; aquí solo su uso indebido.

## Lo que el diseño da por hecho

- El dueño de la instancia es de confianza; el agente conectado, no.
- El bróker es el único proceso que ve las credenciales de plataforma.
- Sin tope duro por cuenta no se escribe: falta de configuración = denegación.
- Los secretos llegan por ficheros 0600, nunca por argv ni por el chat.

Romper cualquiera de estos cuatro supuestos es un fallo de seguridad. Ver
`ARCHITECTURE.md`.
