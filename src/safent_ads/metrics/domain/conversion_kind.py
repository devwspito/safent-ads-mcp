"""Tipos de conversion que reporta una plataforma (spec.md §Lenguaje ubicuo:
'Conversion: evento de valor: lead, clic a WhatsApp, llamada, conversion de
negocio/pago. Pesos distintos.').

Definido localmente en `metrics` (no importado de `crm`) para no cruzar el
grafo de contextos de plan.md §4: `metrics` no depende de `crm`."""

from __future__ import annotations

from enum import StrEnum


class ConversionKind(StrEnum):
    LEAD = "lead"
    WHATSAPP = "whatsapp"
    CALL = "call"
    BUSINESS_CONVERSION = "business_conversion"
