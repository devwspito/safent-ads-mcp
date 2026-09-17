"""`BrokerPlatformWriter`: como se traduce una propuesta a `WriteIntent` y
que hace el adaptador con cada veredicto del broker."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from safent_ads.accounts.application.ports import WriteOperation, WriteOutcome
from safent_ads.accounts.infrastructure.broker_client import BrokerSocketClient
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.execution.application.ports import WriteCommand
from safent_ads.execution.infrastructure.broker_platform import BrokerPlatformWriter
from safent_ads.execution.infrastructure.errors import (
    NoOpWriteCommandError,
    PlatformWriteDeniedError,
    PlatformWriteRejectedError,
    UnsupportedWriteParameterError,
    WriteContextMissingError,
)
from safent_ads.proposals.domain.authorization import (
    Authorization,
    AuthorizationChannel,
    AuthorizationKind,
    AuthorizationSubject,
    SubjectKind,
    sign_authorization,
)
from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff
from safent_ads.proposals.testing.fakes import FakeProposalRepository, FakeSignerPort
from safent_ads.shared.ids import EntityRef, PlatformCode
from tests.contracts.execution.conftest import NOW, StubPlatform, digest
from tests.contracts.execution.test_platform_ports import authorization_for, budget_proposal
from tests.contracts.sql_fixtures import campaign_ref

CONFIRMED = digest("estado-remoto-tras-aplicar")


async def write(
    platform: StubPlatform, proposal: Proposal, value: object | None = None
) -> object:
    proposals = FakeProposalRepository()
    await proposals.save(proposal)
    writer = BrokerPlatformWriter(platform, proposals)
    return await writer.execute_write(
        WriteCommand(
            entity_ref=proposal.diff.entity_ref,
            parameter=proposal.diff.parameter,
            before=proposal.diff.before,
            value=proposal.diff.after if value is None else value,
        ),
        authorization_for(proposal),
        f"exec-{proposal.proposal_id}-{proposal.diff.diff_hash[:12]}",
    )


async def write_effective(
    platform: StubPlatform, proposal: Proposal, *, before: object, value: object
) -> object:
    """Como `write`, pero con `before`/`value` explicitos e independientes
    del diff CRUDO de la propuesta -- simula el `WriteCommand` EFECTIVO
    (recortado) que `ExecutionChokepoint._build_write_command` construye
    desde `effective_diff`, nunca desde `proposal.diff` a secas (BUG
    corregido: `before`/`after` divergiendo de la propuesta es exactamente
    lo que un recorte de guardarraíl produce)."""
    proposals = FakeProposalRepository()
    await proposals.save(proposal)
    writer = BrokerPlatformWriter(platform, proposals)
    return await writer.execute_write(
        WriteCommand(
            entity_ref=proposal.diff.entity_ref,
            parameter=proposal.diff.parameter,
            before=before,
            value=value,
        ),
        authorization_for(proposal),
        f"exec-{proposal.proposal_id}-{proposal.diff.diff_hash[:12]}",
    )


def _authorization_for_diff_hash(proposal: Proposal, diff_hash: str) -> Authorization:
    """Como `authorization_for` (tests.contracts.execution.test_platform_ports),
    pero firmando el `diff_hash` que se le pida -- necesario para probar
    que el bróker recibe el hash del diff EFECTIVO, no el del diff CRUDO de
    la propuesta, cuando los dos difieren por un recorte."""
    return sign_authorization(
        authorization_id=AuthorizationId.new(),
        proposal_id=proposal.proposal_id,
        kind=AuthorizationKind.HUMAN_APPROVAL,
        proposal_classification=proposal.classification,
        diff_hash=diff_hash,
        guardrail_verdict_hash=digest("veredicto"),
        issued_by="owner-de-contrato",
        channel=AuthorizationChannel.PANEL,
        decided_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        signer=FakeSignerPort(),
    )


def with_parameter(
    entity_ref: EntityRef, parameter: str, before: object, after: object
) -> Proposal:
    proposal = budget_proposal(entity_ref)
    proposal.diff = ProposedDiff.build(
        entity_ref=entity_ref, parameter=parameter, before=before, after=after
    )
    return proposal


async def test_the_real_broker_client_sends_execute_write_over_the_socket(
    tmp_path: Path,
) -> None:
    """`BrokerSocketClient` serializa `execute_write` de verdad sobre un
    socket Unix real (contracts/platform-port.md); un veredicto `DENIED`
    del broker sigue el mismo camino tipado que el resto de veredictos --
    `BrokerPlatformWriter` lo levanta como `PlatformWriteDeniedError`."""
    proposal = budget_proposal(campaign_ref("c-produccion"))
    denied_outcome = _outcome("DENIED", error_code="invalid_signature")
    socket_path = tmp_path / "broker.sock"
    # `campaign_ref` (tests/contracts/sql_fixtures.py) construye sobre META
    # por defecto -- el registro tiene que resolver esa misma plataforma.
    registry = PlatformAdapterRegistry({PlatformCode.META: StubPlatform(outcome=denied_outcome)})
    server = await serve(socket_path, registry, frozenset({os.getuid()}))
    try:
        with pytest.raises(PlatformWriteDeniedError) as denied:
            await write(BrokerSocketClient(socket_path), proposal)
    finally:
        server.close()
        await server.wait_closed()

    assert denied.value.outcome == "DENIED"
    assert denied.value.error_code == "invalid_signature"


async def test_the_real_broker_client_raises_a_connection_error_when_the_socket_is_missing(
    tmp_path: Path,
) -> None:
    """Sin broker escuchando, `BrokerSocketClient` falla con un error de
    infraestructura, nunca con un `WriteOutcome` inventado (fail loud, no
    fail-open)."""
    proposal = budget_proposal(campaign_ref("c-sin-broker"))

    with pytest.raises(BrokerConnectionError):
        await write(BrokerSocketClient(tmp_path / "does-not-exist.sock"), proposal)


async def test_a_write_without_its_proposal_is_refused() -> None:
    proposal = budget_proposal(campaign_ref("c-sin-propuesta"))
    writer = BrokerPlatformWriter(StubPlatform(), FakeProposalRepository())

    with pytest.raises(WriteContextMissingError):
        await writer.execute_write(
            WriteCommand(
                entity_ref=proposal.diff.entity_ref,
                parameter=proposal.diff.parameter,
                before=proposal.diff.before,
                value=proposal.diff.after,
            ),
            authorization_for(proposal),
            "exec-sin-propuesta",
        )


class _ProposalRepositoryThatRejectsNone:
    """Espia minimo del `Protocol` (solo `.get`, lo unico que
    `BrokerPlatformWriter` invoca): `SqlProposalRepository.get` recibe
    `str(proposal_id)` para una consulta SQL -- con `proposal_id=None` eso
    es el literal `"None"`, ni un UUID valido ni un `NOT FOUND` limpio.
    `FakeProposalRepository` (testing/fakes.py) tolera esa cadena en
    silencio (`dict.get` sobre un diccionario en memoria) y por eso no basta
    para fijar esta regresion -- este espia reproduce el fallo real
    rechazando explicito cualquier `get(None)`."""

    def __init__(self) -> None:
        self.calls: list[ProposalId | None] = []

    async def get(self, proposal_id: ProposalId | None) -> Proposal | None:
        self.calls.append(proposal_id)
        if proposal_id is None:
            raise AssertionError("BrokerPlatformWriter no debe consultar el repositorio con None")
        return None


async def test_a_package_authorization_without_a_proposal_id_is_refused() -> None:
    """T104 (`proposals/domain/authorization.py::AuthorizationSubject`):
    `proposal_id` es opcional -- una autorizacion de paquete lleva `subject`
    en su lugar. `BrokerPlatformWriter` solo ejecuta escrituras respaldadas
    por una `Proposal` (todavia no existe la saga de publicacion de
    paquetes); debe rechazar explicito, ANTES de consultar el repositorio,
    nunca dejar que `None` llegue a `ProposalRepository.get` (mypy
    `arg-type`: `proposal_id` paso a ser `ProposalId | None`)."""
    proposal = budget_proposal(campaign_ref("c-autorizacion-de-paquete"))
    package_authorization = sign_authorization(
        authorization_id=AuthorizationId.new(),
        subject=AuthorizationSubject(kind=SubjectKind.PACKAGE, id="pkg-01"),
        kind=AuthorizationKind.HUMAN_APPROVAL,
        diff_hash=proposal.diff.diff_hash,
        guardrail_verdict_hash=digest("veredicto"),
        issued_by="owner-de-contrato",
        channel=AuthorizationChannel.PANEL,
        decided_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        signer=FakeSignerPort(),
    )
    proposals = _ProposalRepositoryThatRejectsNone()
    writer = BrokerPlatformWriter(StubPlatform(), proposals)

    with pytest.raises(WriteContextMissingError):
        await writer.execute_write(
            WriteCommand(
                entity_ref=proposal.diff.entity_ref,
                parameter=proposal.diff.parameter,
                before=proposal.diff.before,
                value=proposal.diff.after,
            ),
            package_authorization,
            "exec-autorizacion-de-paquete",
        )

    assert proposals.calls == []


async def test_a_success_without_confirmed_state_is_not_a_success() -> None:
    """FR-21: sin estado remoto posterior no hay verificacion, luego no hay
    exito que devolver."""
    platform = StubPlatform(outcome=_outcome("SUCCEEDED", state_hash_after=None))

    with pytest.raises(PlatformWriteRejectedError):
        await write(platform, budget_proposal(campaign_ref("c-sin-hash")))


async def test_other_verdicts_keep_their_name() -> None:
    platform = StubPlatform(outcome=_outcome("BLOCKED_HARD_CAP", error_code="hard_cap"))

    with pytest.raises(PlatformWriteRejectedError) as rejected:
        await write(platform, budget_proposal(campaign_ref("c-tope-duro")))

    assert rejected.value.outcome == "BLOCKED_HARD_CAP"
    assert rejected.value.error_code == "hard_cap"


async def test_lowering_and_raising_a_budget_are_different_operations() -> None:
    """El tope duro del broker distingue subir de bajar: la direccion del
    cambio no puede perderse por el camino."""
    entity_ref = campaign_ref("c-presupuesto")
    lowering = StubPlatform(outcome=_outcome("SUCCEEDED", state_hash_after=CONFIRMED))
    raising = StubPlatform(outcome=_outcome("SUCCEEDED", state_hash_after=CONFIRMED))

    lower = with_parameter(entity_ref, "daily_budget", Money.of("100"), Money.of("70"))
    raise_ = with_parameter(entity_ref, "daily_budget", Money.of("70"), Money.of("100"))
    await write(lowering, lower)
    await write(raising, raise_)

    assert lowering.write_calls[0].operation is WriteOperation.LOWER_BUDGET
    assert raising.write_calls[0].operation is WriteOperation.RAISE_BUDGET


async def test_pausing_and_resuming_are_read_from_the_value() -> None:
    entity_ref = campaign_ref("c-estado")
    pausing = StubPlatform(outcome=_outcome("SUCCEEDED", state_hash_after=CONFIRMED))
    resuming = StubPlatform(outcome=_outcome("SUCCEEDED", state_hash_after=CONFIRMED))

    await write(pausing, with_parameter(entity_ref, "status", "ACTIVE", "PAUSED"))
    await write(resuming, with_parameter(entity_ref, "status", "PAUSED", "ACTIVE"))

    assert pausing.write_calls[0].operation is WriteOperation.PAUSE
    assert resuming.write_calls[0].operation is WriteOperation.RESUME


async def test_a_parameter_the_port_does_not_expose_is_refused() -> None:
    """FR-41: una palanca que el puerto no expone no se traduce a la que mas
    se le parezca."""
    proposal = with_parameter(campaign_ref("c-palanca"), "bid_strategy", "manual", "auto")

    with pytest.raises(UnsupportedWriteParameterError):
        await write(StubPlatform(), proposal)


async def test_the_intent_carries_the_values_the_broker_will_rehash() -> None:
    """Comprobacion 3 de contracts/platform-port.md: el broker recomputa el
    `diff_hash` desde los campos vivos del intento."""
    proposal = budget_proposal(campaign_ref("c-hash"))
    platform = StubPlatform(outcome=_outcome("SUCCEEDED", state_hash_after=CONFIRMED))

    await write(platform, proposal)

    intent = platform.write_calls[0]
    assert intent.valor_actual == Money.of("100").to_canonical()
    assert intent.valor_propuesto == Money.of("70").to_canonical()
    assert intent.diff_hash == proposal.diff.diff_hash
    assert intent.expected_state_hash == proposal.expected_state_hash


class TestOperationClassifiedFromTheEffectiveDiff:
    """BUG corregido: `_operation`/`_budget_operation` clasificaban
    RAISE/LOWER desde `proposal.diff.before/after` (el diff CRUDO) en vez
    del `WriteCommand` EFECTIVO que el chokepoint verifico y firmo. Si un
    ambito ya estaba fuera de suelo/techo por deriva externa, el recorte
    del guardarraíl podia invertir la direccion real del cambio sin que la
    infraestructura se enterase."""

    async def test_a_clamp_above_the_ceiling_that_flips_the_direction_is_lower(self) -> None:
        """`before`=350 ya esta por encima del techo (300) por deriva
        externa; la propuesta pide subirlo aun mas, a 400. El guardarraíl
        recorta el EFECTIVO al techo: 300, que es MENOR que `before`. El
        bróker tiene que recibir `LOWER_BUDGET` de 350 a 300 -- nunca el
        `RAISE_BUDGET` que el diff CRUDO (350 -> 400) sugeriria."""
        entity_ref = campaign_ref("c-techo-invertido")
        proposal = with_parameter(entity_ref, "daily_budget", Money.of("350"), Money.of("400"))
        platform = StubPlatform(outcome=_outcome("SUCCEEDED", state_hash_after=CONFIRMED))

        await write_effective(platform, proposal, before=Money.of("350"), value=Money.of("300"))

        intent = platform.write_calls[0]
        assert intent.operation is WriteOperation.LOWER_BUDGET
        assert intent.valor_actual == Money.of("350").to_canonical()
        assert intent.valor_propuesto == Money.of("300").to_canonical()

    async def test_a_clamp_below_the_floor_that_flips_the_direction_is_raise(self) -> None:
        """Caso espejo: `before`=8 ya esta por debajo del suelo (10) por
        deriva externa; la propuesta pide bajarlo aun mas, a 5. El
        guardarraíl recorta el EFECTIVO al suelo: 10, que es MAYOR que
        `before`. `RAISE_BUDGET`, no `LOWER_BUDGET`."""
        entity_ref = campaign_ref("c-suelo-invertido")
        proposal = with_parameter(entity_ref, "daily_budget", Money.of("8"), Money.of("5"))
        platform = StubPlatform(outcome=_outcome("SUCCEEDED", state_hash_after=CONFIRMED))

        await write_effective(platform, proposal, before=Money.of("8"), value=Money.of("10"))

        intent = platform.write_calls[0]
        assert intent.operation is WriteOperation.RAISE_BUDGET
        assert intent.valor_actual == Money.of("8").to_canonical()
        assert intent.valor_propuesto == Money.of("10").to_canonical()

    async def test_the_wire_diff_hash_is_the_effective_ones_not_the_raw_ones(self) -> None:
        """El bróker recomputa `diff_hash` desde `valor_actual`/
        `valor_propuesto` (comprobacion 3): si `WriteIntent` llevase el
        valor RECORTADO pero la autorizacion citase el hash del diff CRUDO,
        esa recomputacion jamas coincidiria. Firmando (como de verdad hacen
        `AuthorizeRuleAction`/`SubmitApproval`) con el hash del diff
        EFECTIVO, `WriteIntent.diff_hash` tiene que ser exactamente ese."""
        entity_ref = campaign_ref("c-paridad-hash")
        proposal = with_parameter(entity_ref, "daily_budget", Money.of("350"), Money.of("400"))
        effective_diff_hash = compute_diff_hash(
            entity_ref, "daily_budget", Money.of("350"), Money.of("300")
        )
        authorization = _authorization_for_diff_hash(proposal, effective_diff_hash)
        platform = StubPlatform(outcome=_outcome("SUCCEEDED", state_hash_after=CONFIRMED))
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        writer = BrokerPlatformWriter(platform, proposals)

        await writer.execute_write(
            WriteCommand(
                entity_ref=entity_ref,
                parameter="daily_budget",
                before=Money.of("350"),
                value=Money.of("300"),
            ),
            authorization,
            "exec-paridad-hash",
        )

        intent = platform.write_calls[0]
        assert intent.diff_hash == effective_diff_hash
        assert intent.diff_hash != proposal.diff.diff_hash

    async def test_a_noop_effective_diff_never_reaches_the_broker(self) -> None:
        """Un recorte que deja el EFECTIVO igual a `before` (ya en el
        limite) es justo el caso que `ExecutionChokepoint.
        _skip_guardrail_noop` corta ANTES de construir el `WriteCommand`
        -- si algo se saltase ese paso, el propio adaptador lo rechaza,
        nunca le pide al broker que clasifique una direccion para un valor
        que no cambia."""
        entity_ref = campaign_ref("c-recorte-sin-cambio")
        proposal = with_parameter(entity_ref, "daily_budget", Money.of("300"), Money.of("400"))
        platform = StubPlatform(outcome=_outcome("SUCCEEDED", state_hash_after=CONFIRMED))

        with pytest.raises(NoOpWriteCommandError):
            await write_effective(
                platform, proposal, before=Money.of("300"), value=Money.of("300")
            )

        assert platform.write_calls == []


def _outcome(
    verdict: str, *, state_hash_after: str | None = None, error_code: str | None = None
) -> WriteOutcome:
    return WriteOutcome(
        outcome=verdict,  # type: ignore[arg-type]
        applied_value=Money.of("70").to_canonical(),
        state_hash_after=state_hash_after,
        error_code=error_code,
        platform_request_id="req-1",
    )
