"""Constantes de politica de sesion y bloqueo (tasks.md T011,
threat-model.md C-25). `ApiSettings`/`WorkerSettings` (composition/settings.py)
estan fuera del alcance editable de este carril: estos valores viven aqui
como constantes de codigo, no de entorno -- cambiarlos exige un despliegue,
lo cual es deliberado para una politica de seguridad (evita downgrades por
variable de entorno mal puesta)."""

from __future__ import annotations

from datetime import timedelta

SESSION_IDLE_TTL = timedelta(minutes=30)
SESSION_ABSOLUTE_TTL = timedelta(hours=12)
LOCKOUT_WINDOW = timedelta(minutes=15)
LOCKOUT_THRESHOLD = 5
# 002b NFR-104: ventana de frescura de la identificacion federada. Es
# politica de seguridad (codigo, no entorno) por el mismo motivo que las de
# arriba. Bajarla rompe SC-102 (el segundo agente en un clic); subirla alarga
# la ventana en la que un XSS podria encadenar aprobaciones.
FEDERATED_IDENTIFICATION_TTL = timedelta(minutes=5)
# 002b FR-115: vida de la transaccion federada (state/nonce de un solo uso).
# Cubre el tiempo real de elegir cuenta en Google sin dejar abierta una
# referencia utilizable mas de lo necesario.
FEDERATED_TRANSACTION_TTL = timedelta(minutes=10)
# T084 threat-model.md C-79: mismo criterio que
# `mcp_oauth.application.policy.MAX_PENDING_PER_CLIENT` -- tope de
# transacciones (ni consumidas ni caducadas) que una misma IP puede tener
# abiertas a la vez, para que `/start` no se convierta en un barrido de
# estado sin fondo.
MAX_PENDING_FEDERATED_TRANSACTIONS_PER_IP = 5
