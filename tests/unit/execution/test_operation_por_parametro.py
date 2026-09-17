"""H-1 (004 tasks-2.md §1) + su regla de no-recaida: `_operation()`
(`execution/infrastructure/broker_platform.py`) traduce el `parameter` del
diff EFECTIVO a un `WriteOperation` de puerto. Antes de este arreglo,
`propose_creative_publication` escribia `parameter="creative_publication"`,
que no tenia entrada en `_OPERATION_BY_PARAMETER`: una propuesta aprobada
moria en `UnsupportedWriteParameterError` justo antes de tocar la
plataforma. Este fichero prueba (a) el caso concreto contra el "fake
platform" (`StubPlatform`, mismo doble que `tests/unit/execution/
test_broker_platform.py`) y (b) que NINGUN `WriteOperation` vivo queda sin
un parametro que lo alcance, ni ningun parametro que una herramienta MCP
escriba se queda sin operacion -- el test que impide que H-1 vuelva a
pasar con la proxima operacion nueva."""

from __future__ import annotations

from uuid import uuid4

import pytest

from safent_ads.accounts.application.ports import WriteOperation, WriteOutcome
from safent_ads.execution.application.ports import WriteCommand
from safent_ads.execution.infrastructure.broker_platform import BrokerPlatformWriter, _operation
from safent_ads.execution.infrastructure.errors import UnsupportedWriteParameterError
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.proposals.testing.fakes import FakeProposalRepository
from safent_ads.shared.ids import EntityRef
from tests.contracts.execution.conftest import StubPlatform, digest
from tests.contracts.execution.test_platform_ports import authorization_for, budget_proposal
from tests.unit.broker.platforms.test_ad_child_creation import child_plan
from tests.unit.execution.test_campaign_creation_budget import creation_payload

_ACCOUNT_REF = EntityRef.parse("google:account:123")
_CAMPAIGN_REF = EntityRef.parse("google:campaign:456")
_AD_SET_REF = EntityRef.parse("google:ad_set:789")
_META_AD_REF = EntityRef.parse("meta:ad:987")

# (entity_ref, parameter, before, after, operacion esperada). Cubre los
# parametros de valor simple que hoy escribe algun `propose_*` de MCP.
_SIMPLE_CASES = (
    (_CAMPAIGN_REF, "daily_budget", Money.of("100"), Money.of("70"), WriteOperation.LOWER_BUDGET),
    (_CAMPAIGN_REF, "daily_budget", Money.of("70"), Money.of("100"), WriteOperation.RAISE_BUDGET),
    (_CAMPAIGN_REF, "status", "ACTIVE", "PAUSED", WriteOperation.PAUSE),
    (_CAMPAIGN_REF, "status", "PAUSED", "ACTIVE", WriteOperation.RESUME),
    (_CAMPAIGN_REF, "status", "ACTIVE", "DELETED", WriteOperation.DELETE),
    (_CAMPAIGN_REF, "bid_target", None, Money.of("1.25"), WriteOperation.SET_BID_TARGET),
    (_CAMPAIGN_REF, "targeting", None, {"geo": ["ES"]}, WriteOperation.SET_TARGETING),
    (_CAMPAIGN_REF, "negative_keywords", None, ["gratis"], WriteOperation.ADD_NEGATIVE_KEYWORD),
    (_META_AD_REF, "creative", "ACTIVE", "PAUSED", WriteOperation.ROTATE_OUT_CREATIVE),
    # H-1: forma de `propose_creative_publication`, mismo parametro que la
    # rotacion (composition/mcp_write_adapter.py::_CREATIVE_PARAMETER).
    (
        _AD_SET_REF,
        "creative",
        None,
        {"creative_asset_ids": ["a1"], "ad_copy": {}},
        WriteOperation.ROTATE_OUT_CREATIVE,
    ),
)


def _structured_commands() -> tuple[WriteCommand, ...]:
    """Los parametros cuyo `after` no es un valor simple (creacion de
    hijos/campana, escritura nativa): cada uno construido con el mismo
    payload que su herramienta MCP produce de verdad."""
    return (
        WriteCommand(
            entity_ref=_CAMPAIGN_REF,
            parameter="new_ad_set:exact",
            before=None,
            value={"child_plan": child_plan("google", "ad_set")},
        ),
        WriteCommand(
            entity_ref=_AD_SET_REF,
            parameter="new_ad:exact",
            before=None,
            value={"child_plan": child_plan("google", "ad")},
        ),
        WriteCommand(
            entity_ref=_ACCOUNT_REF,
            parameter="new_campaign:exact",
            before=None,
            value=creation_payload(),
        ),
        WriteCommand(
            entity_ref=_CAMPAIGN_REF,
            parameter="native:meta:update_targeting",
            before=None,
            value={"custom_audiences": ["123"]},
        ),
    )


def _outcome(verdict: str, *, applied_value: object, state_hash_after: str | None) -> WriteOutcome:
    return WriteOutcome(
        outcome=verdict,  # type: ignore[arg-type]
        applied_value=applied_value,
        state_hash_after=state_hash_after,
        error_code=None,
        platform_request_id="req-1",
    )


async def test_h1_una_propuesta_de_publicacion_de_creatividad_aprobada_se_ejecuta() -> None:
    """Regresion H-1: `propose_creative_publication` escribe `parameter=
    "creative"` (ya arreglado) -- antes escribia `"creative_publication"`,
    que `_operation()` no traducia, y la propuesta aprobada moria en
    `UnsupportedWriteParameterError` antes de tocar el "fake platform"."""
    entity_ref = EntityRef.parse(f"meta:ad_set:{uuid4()}:{uuid4()}:999")
    after = {"creative_asset_ids": ["asset-1"], "ad_copy": {"headline": "Nuevo anuncio"}}
    proposal = budget_proposal(entity_ref)
    proposal.diff = ProposedDiff.build(
        entity_ref=entity_ref, parameter="creative", before=None, after=after
    )
    proposals = FakeProposalRepository()
    await proposals.save(proposal)
    outcome = _outcome("SUCCEEDED", applied_value=after, state_hash_after=digest("tras-aplicar"))
    platform = StubPlatform(outcome=outcome)
    writer = BrokerPlatformWriter(platform, proposals)

    result = await writer.execute_write(
        WriteCommand(entity_ref=entity_ref, parameter="creative", before=None, value=after),
        authorization_for(proposal),
        f"exec-{proposal.proposal_id}",
    )

    assert result.applied_value == after
    assert platform.write_calls[0].operation is WriteOperation.ROTATE_OUT_CREATIVE


class TestCadaWriteOperationVivaTieneExactamenteUnParametroQueLaProduce:
    """Recorre `WriteOperation` (fuente de verdad de dominio) y falla si
    alguna no es alcanzable desde ningun `parameter`, o si alguno de los
    parametros que un `propose_*` de MCP escribe hoy no traduce a
    operacion."""

    @pytest.mark.parametrize(
        ("entity_ref", "parameter", "before", "after", "expected"), _SIMPLE_CASES
    )
    def test_parametros_de_valor_simple(  # noqa: PLR0913 - tabla de casos, no logica de negocio
        self,
        entity_ref: EntityRef,
        parameter: str,
        before: object,
        after: object,
        expected: WriteOperation,
    ) -> None:
        command = WriteCommand(
            entity_ref=entity_ref, parameter=parameter, before=before, value=after
        )

        assert _operation(command) is expected

    @pytest.mark.parametrize(
        ("command", "expected"),
        list(
            zip(
                _structured_commands(),
                (
                    WriteOperation.CREATE_AD_SET,
                    WriteOperation.CREATE_AD,
                    WriteOperation.CREATE_CAMPAIGN,
                    WriteOperation.NATIVE_WRITE,
                ),
                strict=True,
            )
        ),
    )
    def test_parametros_de_payload_estructurado(
        self, command: WriteCommand, expected: WriteOperation
    ) -> None:
        assert _operation(command) is expected

    def test_todos_los_write_operation_vivos_son_alcanzables(self) -> None:
        simple = {
            _operation(
                WriteCommand(entity_ref=ref, parameter=parameter, before=before, value=after)
            )
            for ref, parameter, before, after, _expected in _SIMPLE_CASES
        }
        structured = {_operation(command) for command in _structured_commands()}

        missing = set(WriteOperation) - simple - structured
        assert not missing, f"WriteOperation sin parametro que lo alcance: {missing}"

    def test_un_parametro_desconocido_se_rechaza_en_vez_de_adivinar(self) -> None:
        command = WriteCommand(
            entity_ref=_CAMPAIGN_REF, parameter="bid_strategy", before="manual", value="auto"
        )

        with pytest.raises(UnsupportedWriteParameterError):
            _operation(command)
