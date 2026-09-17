"""Primitiva SSRF compartida (threat-model.md C-11/C-12): `ip_guard` es
puro (dominio puede importarlo), `safe_egress` anade resolucion DNS y
fijado de conexion (I/O, solo infraestructura)."""

from __future__ import annotations
