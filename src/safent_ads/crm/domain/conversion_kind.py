"""Tipos de conversion del CRM (data-model.md §LeadAttribution: 'lead, clic a
WhatsApp, llamada, conversion de negocio/pago').

Definido localmente en `crm` (no importado de `metrics`) para no cruzar el
grafo de contextos de plan.md §4."""

from __future__ import annotations

from enum import StrEnum


class ConversionKind(StrEnum):
    LEAD = "lead"
    WHATSAPP = "whatsapp"
    CALL = "call"
    BUSINESS_CONVERSION = "business_conversion"
