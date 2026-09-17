"""Regresion (defecto encontrado en esta lane): `mcp.application.dto.
ProposalState` tenia valores en castellano mientras
`proposals.domain.proposal.ProposalState` y la migracion `0008_proposals`
ya usaban ingles, asi que `list_proposals` no podia filtrar `state` contra
la columna real. Cada estado del dominio debe mapear al mismo valor en el
DTO -- sin eso, `sql_proposal_read_port.SqlProposalReadPort` no podria
traducir filas ni filtros entre las dos capas."""

from __future__ import annotations

import pytest

from safent_ads.mcp.application.dto import ProposalState as DtoProposalState
from safent_ads.proposals.domain.proposal import ProposalState as DomainProposalState


@pytest.mark.parametrize("domain_state", list(DomainProposalState))
def test_every_domain_state_maps_to_the_same_dto_value(
    domain_state: DomainProposalState,
) -> None:
    dto_state = DtoProposalState(domain_state.value)
    assert dto_state.value == domain_state.value
    assert dto_state.name == domain_state.name


def test_dto_and_domain_declare_exactly_the_same_states() -> None:
    domain_values = {state.value for state in DomainProposalState}
    dto_values = {state.value for state in DtoProposalState}
    assert dto_values == domain_values
