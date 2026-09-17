"""Release metadata and the served application must describe the same build."""

import tomllib
from contextlib import contextmanager
from pathlib import Path

import pytest
import structlog

from safent_ads import __version__
from safent_ads.composition.app import create_app
from tests.unit.composition.factories import build_api_settings
from tests.unit.composition.test_managed_app import settings as managed_settings


def test_project_and_editable_lock_match_imported_release():
    root = Path(__file__).resolve().parents[3]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    lock = tomllib.loads((root / "uv.lock").read_text())
    packages = [package for package in lock["package"] if package["name"] == "safent-ads"]
    assert len(packages) == 1
    assert packages[0]["source"] == {"editable": "."}
    assert project["project"]["version"] == packages[0]["version"] == __version__


@contextmanager
def _structlog_config_restored():
    """`create_app()` calls `configure_logging()`, which reconfigures
    `structlog` for the WHOLE process (`logging_setup.py`: "Configura
    structlog ... para todo el proceso"). Under random test order this
    test's invocation of `create_app()` is not the only one in the suite
    (`grep create_app( tests/`), and without restoring the config afterward
    it leaks into whatever test runs next in the same session -- in
    particular, `structlog.testing.capture_logs()` relies on the global
    config it swaps back into place, and a later test using it can silently
    capture zero events. Snapshot/restore, not `reset_defaults()`: this
    test must not assume it is the FIRST to configure structlog. A plain
    context manager, not only a fixture, so `test_structlog_config_*` below
    can pin this exact restore behavior without pytest's own fixture
    machinery hiding whether teardown really ran."""
    before = structlog.get_config()
    try:
        yield
    finally:
        structlog.configure(**before)


@pytest.fixture
def _restore_structlog_config():
    with _structlog_config_restored():
        yield


async def test_structlog_config_survives_a_create_app_call_unmodified():
    """Regression for the contamination fixed by `_restore_structlog_config`
    (T035 security re-check, 2026-09-15, "Info"): `configure_logging()`
    always builds a fresh `logger_factory` instance (`PrintLoggerFactory`,
    never memoized -- unlike `wrapper_class`, which structlog DOES memoize
    per level, so it is not a reliable signal here), so a real, unrestored
    `create_app()` call is guaranteed to change it; leaving
    `_structlog_config_restored()` must undo that, not just the
    `processors` list `capture_logs()` (structlog.testing) already restores
    on its own."""
    before = structlog.get_config()
    with _structlog_config_restored():
        app = create_app(build_api_settings())
        try:
            assert structlog.get_config()["logger_factory"] is not before["logger_factory"]
        finally:
            await app.state.container.aclose()
    assert structlog.get_config() == before


@pytest.mark.parametrize("managed", [False, True])
async def test_served_app_reports_current_release(managed, _restore_structlog_config):
    app = create_app(managed_settings() if managed else build_api_settings())
    try:
        assert app.version == __version__
    finally:
        await app.state.container.aclose()
