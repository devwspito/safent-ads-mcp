"""`_run_package_publications_once` (M2, repaso de seguridad 0.2.23):
`supervise_worker_tasks` (`orchestration.application.worker_supervisor`)
trata CUALQUIER excepcion sin capturar de un ciclo como fatal para TODOS
los ciclos del worker (observacion, ejecucion, telegram), no solo el de
paquetes -- un `PackageDomainError`/`SQLAlchemyError` de UNA publicacion
nunca debe escapar de este bucle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import BusinessFactory, OwnerFactory
from tests.integration.packages.conftest import seed_package_scope
from tests.unit.packages.application.test_approve_campaign_package import _wide_open_guardrails
from tests.unit.packages.domain.conftest import NOW, propose_meta_package

from safent_ads.composition import worker
from safent_ads.execution.domain.guardrails import GuardrailEvaluator
from safent_ads.execution.testing.fakes import (
    FakeBrakeStatePort,
    FakeGuardrailSetRepository,
    FakeSpendLedger,
)
from safent_ads.packages.application.approve_campaign_package import (
    ApproveCampaignPackage,
    ApproveCampaignPackageCommand,
)
from safent_ads.packages.domain.errors import PackageDomainError
from safent_ads.packages.domain.identifiers import OfferingId
from safent_ads.packages.infrastructure.sql_package_authorization_repository import (
    SqlPackageAuthorizationRepository,
)
from safent_ads.packages.infrastructure.sql_package_repository import SqlCampaignPackageRepository
from safent_ads.packages.infrastructure.sql_publication_repository import (
    SqlPackagePublicationRepository,
)
from safent_ads.proposals.testing.fakes import FakeSignerPort
from safent_ads.shared.clock import FixedClock

pytestmark = pytest.mark.integration


class _BoomExecutor:
    """`RunPackagePublication` de mentira que SIEMPRE revienta -- el punto
    de esta prueba es la resiliencia del bucle alrededor de ella, no lo que
    haga el ejecutor real."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def execute(self, publication_id: str) -> None:
        del publication_id
        raise self._error


@asynccontextmanager
async def _reuse_session(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """`Container.session_factory` es un `async_sessionmaker` real -- para
    esta prueba basta con reutilizar el `db_session` de la fixture (mismo
    criterio que cualquier otra prueba de integracion), envuelto en el
    gestor de contexto que `_run_package_publications_once` espera."""
    yield session


async def _open_publication(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> str:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(db_session, owner_id=owner_id, business_id=business_id)
    package = propose_meta_package(
        business=scope.business_id,
        account=scope.account_ref,
        offering_id=OfferingId(scope.offering_id),
    )
    packages = SqlCampaignPackageRepository(db_session)
    await packages.add(package)
    await db_session.flush()
    account_scope = str(scope.account_ref)
    approve = ApproveCampaignPackage(
        packages=packages,
        publications=SqlPackagePublicationRepository(db_session),
        authorizations=SqlPackageAuthorizationRepository(db_session),
        brakes=FakeBrakeStatePort(),
        guardrail_evaluator=GuardrailEvaluator(),
        guardrail_sets=FakeGuardrailSetRepository(
            {account_scope: _wide_open_guardrails(account_scope)}
        ),
        spend_ledger=FakeSpendLedger(),
        signer=FakeSignerPort(),
        clock=FixedClock(NOW),
    )
    result = await approve.execute(
        ApproveCampaignPackageCommand(
            business_id=package.business_id,
            package_id=package.package_id,
            package_hash=package.package_hash.value,
            approved_by="owner-1",
        )
    )
    # `_run_package_publications_once` hace `session.rollback()` sobre el
    # MISMO `db_session` cuando el ejecutor revienta -- en produccion cada
    # llamada tiene su PROPIA sesion/transaccion real, pero aqui todas
    # comparten el `SAVEPOINT` de la fixture (`join_transaction_mode=
    # "create_savepoint"`): sin este commit, ese rollback deshace tambien
    # el seed/aprobacion de MAS ARRIBA, no solo el intento fallido.
    await db_session.commit()
    return result.publication_id


async def test_a_domain_error_halts_only_its_own_publication_and_the_loop_survives(
    db_session: AsyncSession,
    owner_factory: OwnerFactory,
    business_factory: BusinessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication_id = await _open_publication(db_session, owner_factory, business_factory)
    container = Mock(session_factory=lambda: _reuse_session(db_session), clock=FixedClock(NOW))
    monkeypatch.setattr(
        worker,
        "_build_run_package_publication",
        Mock(return_value=_BoomExecutor(PackageDomainError("invariante violado"))),
    )

    await worker._run_package_publications_once(container, Mock())

    publications = SqlPackagePublicationRepository(db_session)
    record = await publications.get_by_id(publication_id)
    assert record is not None
    assert record.state == "halted"
    assert record.halt_reason == "package_publication_internal_error"


async def test_a_transient_db_error_leaves_the_publication_untouched_and_the_loop_survives(
    db_session: AsyncSession,
    owner_factory: OwnerFactory,
    business_factory: BusinessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication_id = await _open_publication(db_session, owner_factory, business_factory)
    container = Mock(session_factory=lambda: _reuse_session(db_session), clock=FixedClock(NOW))
    monkeypatch.setattr(
        worker,
        "_build_run_package_publication",
        Mock(return_value=_BoomExecutor(SQLAlchemyError("conexion perdida"))),
    )

    await worker._run_package_publications_once(container, Mock())

    publications = SqlPackagePublicationRepository(db_session)
    record = await publications.get_by_id(publication_id)
    assert record is not None
    assert record.state == "pending"
    assert record.halt_reason is None


async def test_an_os_error_from_the_asset_store_does_not_kill_the_loop(
    db_session: AsyncSession,
    owner_factory: OwnerFactory,
    business_factory: BusinessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication_id = await _open_publication(db_session, owner_factory, business_factory)
    container = Mock(session_factory=lambda: _reuse_session(db_session), clock=FixedClock(NOW))
    monkeypatch.setattr(
        worker,
        "_build_run_package_publication",
        Mock(return_value=_BoomExecutor(OSError("fichero de activo desaparecido tras un restore"))),
    )

    await worker._run_package_publications_once(container, Mock())

    publications = SqlPackagePublicationRepository(db_session)
    record = await publications.get_by_id(publication_id)
    assert record is not None
    assert record.state == "pending"
    assert record.halt_reason is None
