"""Orden de la cola de propuestas (profitability-engine.md §8: "la cola
cambia de eje" -- de `urgency, money_at_stake` a `expected_contribution_delta
DESC`, con `urgency` como desempate).

Pura: sin repositorio, sin SQL. El `read-model hook` que aplica este mismo
criterio en Postgres vive en `infrastructure/sql_proposal_repository.py`
(`_LIST_BY_LENS_COLUMNS_TEMPLATE`) -- esta funcion es la version en memoria,
usada por quien ya tiene una lista de `Proposal` cargada (p. ej. el digest
de Telegram) y por el test de contrato que fija el criterio."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from safent_ads.proposals.domain.priority import Urgency
from safent_ads.proposals.domain.proposal import Proposal

_HAS_DELTA_RANK = 0
_NO_DELTA_RANK = 1
_URGENCY_RANK: dict[Urgency, int] = {
    Urgency.CRITICAL: 0,
    Urgency.RECOMMENDED: 1,
    Urgency.MINOR: 2,
}


def _ranking_key(proposal: Proposal) -> tuple[int, Decimal, int, datetime]:
    delta = proposal.expected_contribution_delta
    urgency_rank = _URGENCY_RANK[proposal.priority.urgency]
    if delta is None:
        # Sin contribucion estimada (reglas M01-M24, todavia no vienen de
        # `optimization`): al final, ordenadas por urgencia.
        return (_NO_DELTA_RANK, Decimal("0"), urgency_rank, proposal.expires_at)
    # DESC sobre un campo que se ordena ASC: se invierte el signo.
    return (_HAS_DELTA_RANK, -delta.amount, urgency_rank, proposal.expires_at)


def rank_proposals(proposals: Sequence[Proposal]) -> tuple[Proposal, ...]:
    """`expected_contribution_delta DESC`, `urgency` como desempate; las
    propuestas sin contribucion estimada van al final, tambien por
    `urgency` (profitability-engine.md §8)."""
    return tuple(sorted(proposals, key=_ranking_key))
