"""`GetHealthStatus` (mcp.application.health): agrega el ping de BD y las
plataformas con cuentas vinculadas en un unico `HealthReport`, degradando
`status`/`db` cuando la BD esta caida."""

from __future__ import annotations

from safent_ads.mcp.application.health import CONTRACT_VERSION, GetHealthStatus


class _FakeAccounts:
    def __init__(self, linked: frozenset[str]) -> None:
        self._linked = linked

    async def linked_platforms(self) -> frozenset[str]:
        return self._linked


class _FakeDatabase:
    def __init__(self, *, healthy: bool) -> None:
        self._healthy = healthy

    async def ping(self) -> bool:
        return self._healthy


async def test_reports_ok_with_linked_platforms_when_database_is_healthy() -> None:
    use_case = GetHealthStatus(
        accounts=_FakeAccounts(frozenset({"google"})), database=_FakeDatabase(healthy=True)
    )

    report = await use_case.execute()

    assert report.status == "ok"
    assert report.db == "ok"
    assert report.contract_version == CONTRACT_VERSION
    assert report.accounts_linked.google is True
    assert report.accounts_linked.meta is False


async def test_reports_both_platforms_linked() -> None:
    use_case = GetHealthStatus(
        accounts=_FakeAccounts(frozenset({"google", "meta"})),
        database=_FakeDatabase(healthy=True),
    )

    report = await use_case.execute()

    assert report.accounts_linked.google is True
    assert report.accounts_linked.meta is True


async def test_degrades_when_database_ping_fails() -> None:
    use_case = GetHealthStatus(
        accounts=_FakeAccounts(frozenset()), database=_FakeDatabase(healthy=False)
    )

    report = await use_case.execute()

    assert report.status == "degraded"
    assert report.db == "error"
