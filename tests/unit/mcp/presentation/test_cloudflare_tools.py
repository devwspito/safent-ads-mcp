"""`cloudflare_tools.py` (Anadido del dueno, 14-sep; lane 006-cloudflare-ui,
15-sep): registro en el `ToolRegistry` real, `CLOUDFLARE_NOT_CONNECTED`
(con el enlace de conexion) cuando `CloudflarePort` devuelve el sentinel
`CloudflareNotConfigured`, y que las excepciones del conector se traducen
en el borde -- zona no permitida a `CLOUDFLARE_ZONE_FORBIDDEN`, zona no
encontrada/registro ambiguo a `VALIDATION_ERROR`, y cualquier fallo de
Cloudflare a `CLOUDFLARE_UPSTREAM_ERROR` -- sin que ningun mensaje incluya
el token ni una URL cruda del proveedor."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.integrations.cloudflare.client import (
    CloudflareApiError,
    CloudflareResponseTooLargeError,
    CloudflareTransportError,
)
from safent_ads.integrations.cloudflare.connection_port import CloudflareConnectionStatus
from safent_ads.integrations.cloudflare.port import (
    CloudflareDnsRecord,
    CloudflareDnsRecordDeleted,
    CloudflareNotConfigured,
    CloudflareRecordAmbiguousError,
    CloudflareZone,
    CloudflareZoneNotAllowedError,
    CloudflareZoneNotFoundError,
    DnsRecordType,
)
from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import ToolValidationError
from safent_ads.mcp.presentation.cloudflare_tools import (
    CloudflareNotConnectedError,
    CloudflareToolServices,
    CloudflareUpstreamError,
    CloudflareZoneForbiddenError,
    DeleteDnsRecordArgs,
    GetCloudflareConnectionStatusArgs,
    ListDnsRecordsArgs,
    ListDnsZonesArgs,
    UpsertDnsRecordArgs,
    build_cloudflare_tool_definitions,
)
from safent_ads.mcp.presentation.registry import ToolClass, ToolRegistry

_NOT_CONNECTED = CloudflareNotConfigured(
    create_token_url="https://dash.cloudflare.com/profile/api-tokens"
)

_BUSINESS_ID = "9d9b8b1a-6b8e-4f0a-9d1e-8f2c6b7a5e10"
_ZONE = CloudflareZone(zone_id="zone-1", name="example.com", status="active")
_RECORD = CloudflareDnsRecord(
    record_id="rec-1",
    zone_id="zone-1",
    type="A",
    name="www.example.com",
    content="1.2.3.4",
    ttl=300,
    proxied=False,
    comment=None,
)
_FAKE_TOKEN_MARKER = "sk-cloudflare-super-secret-token-do-not-leak"  # noqa: S105


def _caller_scope() -> CallerScope:
    return CallerScope(
        caller_id="test",
        allowed_business_ids=frozenset({_BUSINESS_ID}),
        permission=Permission.VIEW,
        person_label="Agente de prueba",
    )


class _FakeCloudflarePort:
    def __init__(
        self,
        *,
        zones: object = (),
        records: object = (),
        upsert_result: object = None,
        delete_result: object = None,
        error: Exception | None = None,
    ) -> None:
        self._zones = zones
        self._records = records
        self._upsert_result = upsert_result if upsert_result is not None else _RECORD
        self._delete_result = delete_result or CloudflareDnsRecordDeleted(
            record_id="rec-1", zone="example.com"
        )
        self._error = error
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def list_dns_zones(self) -> object:
        self.calls.append(("list_dns_zones", ()))
        if self._error is not None:
            raise self._error
        return self._zones

    async def list_dns_records(self, zone: str, *, record_type, name) -> object:
        self.calls.append(("list_dns_records", (zone, record_type, name)))
        if self._error is not None:
            raise self._error
        return self._records

    async def upsert_dns_record(
        self, zone: str, *, record_type, name, content, ttl, proxied, comment
    ) -> object:
        self.calls.append(
            ("upsert_dns_record", (zone, record_type, name, content, ttl, proxied, comment))
        )
        if self._error is not None:
            raise self._error
        return self._upsert_result

    async def delete_dns_record(self, zone: str, record_id: str) -> object:
        self.calls.append(("delete_dns_record", (zone, record_id)))
        if self._error is not None:
            raise self._error
        return self._delete_result


class _FakeConnectionStatusPort:
    def __init__(self, status: CloudflareConnectionStatus | None = None) -> None:
        self._status = status or CloudflareConnectionStatus(
            connected=False,
            account_id=None,
            zones=(),
            connected_at=None,
            create_token_url="https://dash.cloudflare.com/profile/api-tokens",
            required_permissions=("Zone.Read", "DNS.Edit"),
        )

    async def execute(self) -> CloudflareConnectionStatus:
        return self._status


def _services(
    port: _FakeCloudflarePort | None = None,
    status_port: _FakeConnectionStatusPort | None = None,
) -> CloudflareToolServices:
    return CloudflareToolServices(
        cloudflare=port or _FakeCloudflarePort(),
        connection_status=status_port or _FakeConnectionStatusPort(),
    )


def _definitions(
    port: _FakeCloudflarePort | None = None,
    status_port: _FakeConnectionStatusPort | None = None,
) -> dict[str, object]:
    return {d.name: d for d in build_cloudflare_tool_definitions(_services(port, status_port))}


def test_build_cloudflare_tool_definitions_registers_the_five_tools() -> None:
    definitions = _definitions()

    assert set(definitions) == {
        "get_cloudflare_connection_status",
        "list_dns_zones",
        "list_dns_records",
        "upsert_dns_record",
        "delete_dns_record",
    }
    assert definitions["get_cloudflare_connection_status"].tool_class is ToolClass.READ
    assert definitions["list_dns_zones"].tool_class is ToolClass.READ
    assert definitions["list_dns_records"].tool_class is ToolClass.READ
    assert definitions["upsert_dns_record"].tool_class is ToolClass.CATALOG_WRITE
    assert definitions["delete_dns_record"].tool_class is ToolClass.CATALOG_WRITE


def test_definitions_pass_the_registry_naming_guard() -> None:
    ToolRegistry(build_cloudflare_tool_definitions(_services()))


def test_every_tool_resolves_business_id_for_authorization() -> None:
    definitions = _definitions()

    for definition in definitions.values():
        assert definition.business_id_of is not None


# --- get_cloudflare_connection_status -----------------------------------


async def test_get_connection_status_reports_connected() -> None:
    status = CloudflareConnectionStatus(
        connected=True,
        account_id="a" * 32,
        zones=("example.com",),
        connected_at=datetime(2026, 9, 15, tzinfo=UTC),
        create_token_url=f"https://dash.cloudflare.com/{'a' * 32}/api-tokens",
        required_permissions=("Zone.Read", "DNS.Edit"),
    )
    definitions = _definitions(status_port=_FakeConnectionStatusPort(status))

    result = await definitions["get_cloudflare_connection_status"].handler(
        GetCloudflareConnectionStatusArgs(business_id=_BUSINESS_ID), _caller_scope()
    )

    assert result == {
        "connected": True,
        "account_id": "a" * 32,
        "zones": ["example.com"],
        "create_token_url": f"https://dash.cloudflare.com/{'a' * 32}/api-tokens",
        "required_permissions": ["Zone.Read", "DNS.Edit"],
        "connected_at": "2026-09-15T00:00:00+00:00",
    }


async def test_get_connection_status_reports_disconnected_with_the_link() -> None:
    definitions = _definitions()

    result = await definitions["get_cloudflare_connection_status"].handler(
        GetCloudflareConnectionStatusArgs(business_id=_BUSINESS_ID), _caller_scope()
    )

    assert result["connected"] is False
    assert result["create_token_url"] == _NOT_CONNECTED.create_token_url


# --- list_dns_zones ----------------------------------------------------------


async def test_list_dns_zones_returns_the_zones_when_configured() -> None:
    port = _FakeCloudflarePort(zones=(_ZONE,))
    definitions = _definitions(port)

    result = await definitions["list_dns_zones"].handler(
        ListDnsZonesArgs(business_id=_BUSINESS_ID), _caller_scope()
    )

    assert result == {"configured": True, "zones": [_ZONE]}


async def test_list_dns_zones_not_connected_raises_with_the_link() -> None:
    port = _FakeCloudflarePort(zones=_NOT_CONNECTED)
    definitions = _definitions(port)

    with pytest.raises(CloudflareNotConnectedError) as excinfo:
        await definitions["list_dns_zones"].handler(
            ListDnsZonesArgs(business_id=_BUSINESS_ID), _caller_scope()
        )

    assert excinfo.value.code == "CLOUDFLARE_NOT_CONNECTED"
    assert _NOT_CONNECTED.create_token_url in str(excinfo.value)


# --- list_dns_records ----------------------------------------------------------


async def test_list_dns_records_forwards_filters_to_the_port() -> None:
    port = _FakeCloudflarePort(records=(_RECORD,))
    definitions = _definitions(port)
    args = ListDnsRecordsArgs(
        business_id=_BUSINESS_ID, zone="example.com", type="A", name="www.example.com"
    )

    result = await definitions["list_dns_records"].handler(args, _caller_scope())

    assert result == {"configured": True, "records": [_RECORD]}
    expected_call = ("example.com", DnsRecordType.A, "www.example.com")
    assert port.calls == [("list_dns_records", expected_call)]


async def test_list_dns_records_not_connected_raises_with_the_link() -> None:
    port = _FakeCloudflarePort(records=_NOT_CONNECTED)
    definitions = _definitions(port)
    args = ListDnsRecordsArgs(business_id=_BUSINESS_ID, zone="example.com")

    with pytest.raises(CloudflareNotConnectedError) as excinfo:
        await definitions["list_dns_records"].handler(args, _caller_scope())

    assert excinfo.value.code == "CLOUDFLARE_NOT_CONNECTED"


# --- upsert_dns_record ----------------------------------------------------------


async def test_upsert_dns_record_returns_the_confirmed_record() -> None:
    port = _FakeCloudflarePort(upsert_result=_RECORD)
    definitions = _definitions(port)
    args = UpsertDnsRecordArgs(
        business_id=_BUSINESS_ID,
        zone="example.com",
        type="A",
        name="www.example.com",
        content="1.2.3.4",
    )

    result = await definitions["upsert_dns_record"].handler(args, _caller_scope())

    assert result == {"configured": True, "record": _RECORD}
    expected_args = ("example.com", DnsRecordType.A, "www.example.com", "1.2.3.4")
    expected_call = (*expected_args, 300, False, None)
    assert port.calls == [("upsert_dns_record", expected_call)]


async def test_upsert_dns_record_not_connected_raises_with_the_link() -> None:
    port = _FakeCloudflarePort(upsert_result=_NOT_CONNECTED)
    definitions = _definitions(port)
    args = UpsertDnsRecordArgs(
        business_id=_BUSINESS_ID,
        zone="example.com",
        type="A",
        name="www.example.com",
        content="1.2.3.4",
    )

    with pytest.raises(CloudflareNotConnectedError) as excinfo:
        await definitions["upsert_dns_record"].handler(args, _caller_scope())

    assert excinfo.value.code == "CLOUDFLARE_NOT_CONNECTED"


# --- delete_dns_record ----------------------------------------------------------


async def test_delete_dns_record_returns_the_confirmation() -> None:
    confirmation = CloudflareDnsRecordDeleted(record_id="rec-1", zone="example.com")
    port = _FakeCloudflarePort(delete_result=confirmation)
    definitions = _definitions(port)
    args = DeleteDnsRecordArgs(business_id=_BUSINESS_ID, zone="example.com", record_id="rec-1")

    result = await definitions["delete_dns_record"].handler(args, _caller_scope())

    assert result == {"configured": True, "deleted": True, "record": confirmation}


async def test_delete_dns_record_not_connected_raises_with_the_link() -> None:
    port = _FakeCloudflarePort(delete_result=_NOT_CONNECTED)
    definitions = _definitions(port)
    args = DeleteDnsRecordArgs(business_id=_BUSINESS_ID, zone="example.com", record_id="rec-1")

    with pytest.raises(CloudflareNotConnectedError) as excinfo:
        await definitions["delete_dns_record"].handler(args, _caller_scope())

    assert excinfo.value.code == "CLOUDFLARE_NOT_CONNECTED"


# --- traduccion de excepciones en el borde -----------------------------------


async def test_zone_not_allowed_becomes_a_forbidden_error() -> None:
    error = CloudflareZoneNotAllowedError("zona fuera de la lista: otra.com")
    port = _FakeCloudflarePort(error=error)
    definitions = _definitions(port)
    args = ListDnsRecordsArgs(business_id=_BUSINESS_ID, zone="example.com")

    with pytest.raises(CloudflareZoneForbiddenError) as excinfo:
        await definitions["list_dns_records"].handler(args, _caller_scope())

    assert excinfo.value.code == "CLOUDFLARE_ZONE_FORBIDDEN"


@pytest.mark.parametrize(
    "error",
    [
        CloudflareZoneNotFoundError("zona no encontrada en cloudflare: example.com"),
        CloudflareRecordAmbiguousError("2 registros existentes coinciden"),
    ],
)
async def test_zone_not_found_or_ambiguous_becomes_validation_error(error: Exception) -> None:
    port = _FakeCloudflarePort(error=error)
    definitions = _definitions(port)
    args = ListDnsRecordsArgs(business_id=_BUSINESS_ID, zone="example.com")

    with pytest.raises(ToolValidationError):
        await definitions["list_dns_records"].handler(args, _caller_scope())


@pytest.mark.parametrize(
    "error",
    [
        CloudflareApiError(401),
        CloudflareTransportError("cloudflare no respondio"),
        CloudflareResponseTooLargeError("70000 bytes > 65536"),
    ],
)
async def test_upstream_failures_become_cloudflare_upstream_error(error: Exception) -> None:
    port = _FakeCloudflarePort(error=error)
    definitions = _definitions(port)
    args = ListDnsZonesArgs(business_id=_BUSINESS_ID)

    with pytest.raises(CloudflareUpstreamError) as excinfo:
        await definitions["list_dns_zones"].handler(args, _caller_scope())

    assert excinfo.value.code == "CLOUDFLARE_UPSTREAM_ERROR"


# --- barrido de sanitizacion: el borde nunca añade el token ni una URL a un
# error que ya llega sanitizado de `integrations/cloudflare` (`test_client.py`
# ya barre el cuerpo crudo del proveedor; esto barre que `_call_cloudflare`
# no concatena `args`/`raw_arguments` -- que podrian incluir el `content` de
# un registro -- al reenviar el mensaje) ---------------------------------


@pytest.mark.parametrize(
    "error",
    [
        CloudflareZoneNotAllowedError("zona fuera de la lista permitida: otra.com"),
        CloudflareZoneNotFoundError("zona no encontrada en cloudflare: example.com"),
        CloudflareRecordAmbiguousError("2 registros existentes coinciden"),
        CloudflareApiError(401),
        CloudflareTransportError("cloudflare no respondio"),
        CloudflareResponseTooLargeError("70000 bytes > 65536"),
    ],
)
async def test_sanitisation_sweep_never_leaks_a_token_or_a_url(error: Exception) -> None:
    port = _FakeCloudflarePort(error=error)
    definitions = _definitions(port)
    args = UpsertDnsRecordArgs(
        business_id=_BUSINESS_ID,
        zone="example.com",
        type="A",
        name="www.example.com",
        content=f"marker-{_FAKE_TOKEN_MARKER}-should-never-appear-in-the-error",
    )

    expected_errors = (CloudflareZoneForbiddenError, ToolValidationError, CloudflareUpstreamError)
    with pytest.raises(expected_errors) as excinfo:
        await definitions["upsert_dns_record"].handler(args, _caller_scope())

    message = str(excinfo.value)
    assert _FAKE_TOKEN_MARKER not in message
    assert "https://" not in message
    assert "http://" not in message
