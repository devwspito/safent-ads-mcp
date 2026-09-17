"""Puertos de las proyecciones de lectura de `rules` (N4, plan.md §5): cada
consulta SQL propia de `caps_and_pacing.py`/`guardrails_view.py`/
`rule_catalog_view.py` vive detras de una de estas firmas, implementada en
`rules.infrastructure.read_models` -- mismo criterio de `Protocol` sin
`AsyncSession` que `rules.application.ports` ya usa para los repositorios de
dominio (I-1, revision final T130: la aplicacion no importa SQLAlchemy)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from safent_ads.rules.application.read_models.guardrails_view import GuardrailView
    from safent_ads.rules.application.read_models.rule_catalog_view import RuleActivity


class AccountRefReadPort(Protocol):
    """Referencia (`platform`, `external_account_id`) de la PRIMERA cuenta
    publicitaria de un negocio, o `None` si no tiene ninguna -- unico uso:
    `caps_and_pacing.caps_and_pacing`."""

    async def first_account_ref_for_business(
        self, *, business_id: uuid.UUID | str
    ) -> tuple[str, str] | None: ...


class GuardrailViewReadPort(Protocol):
    async def list_for_business(self, *, business_id: str) -> list[GuardrailView]: ...

    async def list_for_account(
        self, *, platform: str, external_account_id: str
    ) -> list[GuardrailView]: ...


class RuleActivityReadPort(Protocol):
    async def activity_by_code(
        self, *, business_id: str, since: datetime
    ) -> dict[str, RuleActivity]: ...
