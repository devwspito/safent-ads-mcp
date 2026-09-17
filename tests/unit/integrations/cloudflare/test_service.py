"""`NullCloudflareService`/`LiveCloudflareService` (Anadido del dueno,
14-sep) contra un doble de `CloudflareHttpClient` -- application layer,
sin red ni transporte HTTP real (esos ya los cubre `test_client.py`).
`_FakeCloudflareClient` implementa exactamente los cinco metodos que
`LiveCloudflareService` usa, con la misma forma que devuelve la API v4."""

from __future__ import annotations

import pytest

from safent_ads.integrations.cloudflare.port import (
    CloudflareDnsRecordDeleted,
    CloudflareNotConfigured,
    CloudflareRecordAmbiguousError,
    CloudflareZone,
    CloudflareZoneNotAllowedError,
    CloudflareZoneNotFoundError,
    DnsRecordType,
)
from safent_ads.integrations.cloudflare.service import (
    LiveCloudflareService,
    NullCloudflareService,
    build_cloudflare_service,
)

_ZONE_ID = "023e105f4ecef8ad9ca31a8372d0c353"
_ZONE_JSON = {"id": _ZONE_ID, "name": "example.com", "status": "active"}
_OTHER_ZONE_JSON = {"id": "abc", "name": "otra.com", "status": "active"}
_RECORD_JSON = {
    "id": "372e67954025e0ba6aaa6d586b9e0b59",
    "zone_id": _ZONE_ID,
    "type": "A",
    "name": "www.example.com",
    "content": "1.2.3.4",
    "ttl": 300,
    "proxied": False,
    "comment": None,
}


class _FakeCloudflareClient:
    def __init__(
        self,
        *,
        zones: list[dict[str, object]] | None = None,
        records: list[dict[str, object]] | None = None,
        created: dict[str, object] | None = None,
        updated: dict[str, object] | None = None,
    ) -> None:
        self._zones = zones if zones is not None else [_ZONE_JSON]
        self._records = records if records is not None else []
        self._created = created or _RECORD_JSON
        self._updated = updated or _RECORD_JSON
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def list_zones(self, *, name: str | None = None) -> list[dict[str, object]]:
        self.calls.append(("list_zones", (name,)))
        if name is None:
            return self._zones
        return [zone for zone in self._zones if zone["name"] == name]

    async def list_dns_records(
        self, zone_id: str, *, record_type: str | None, name: str | None
    ) -> list[dict[str, object]]:
        self.calls.append(("list_dns_records", (zone_id, record_type, name)))
        return self._records

    async def create_dns_record(
        self, zone_id: str, payload: dict[str, object]
    ) -> dict[str, object]:
        self.calls.append(("create_dns_record", (zone_id, payload)))
        return self._created

    async def update_dns_record(
        self, zone_id: str, record_id: str, payload: dict[str, object]
    ) -> dict[str, object]:
        self.calls.append(("update_dns_record", (zone_id, record_id, payload)))
        return self._updated

    async def delete_dns_record(self, zone_id: str, record_id: str) -> dict[str, object]:
        self.calls.append(("delete_dns_record", (zone_id, record_id)))
        return {"id": record_id}


class _ExplodingCloudflareClient:
    """Cualquier llamada es un fallo de test: usado para probar que el
    rechazo de zona pasa CERO por la red (F-4)."""

    async def list_zones(
        self, *, name: str | None = None  # noqa: ARG002 - nunca deberia llamarse
    ) -> list[dict[str, object]]:
        raise AssertionError("no deberia llamar a Cloudflare para una zona no permitida")

    async def list_dns_records(
        self,
        zone_id: str,  # noqa: ARG002 - nunca deberia llamarse
        *,
        record_type: str | None,  # noqa: ARG002 - nunca deberia llamarse
        name: str | None,  # noqa: ARG002 - nunca deberia llamarse
    ) -> list[dict[str, object]]:
        raise AssertionError("no deberia llamar a Cloudflare para una zona no permitida")

    async def create_dns_record(
        self,
        zone_id: str,  # noqa: ARG002 - nunca deberia llamarse
        payload: dict[str, object],  # noqa: ARG002 - nunca deberia llamarse
    ) -> dict[str, object]:
        raise AssertionError("no deberia llamar a Cloudflare para una zona no permitida")

    async def update_dns_record(
        self,
        zone_id: str,  # noqa: ARG002 - nunca deberia llamarse
        record_id: str,  # noqa: ARG002 - nunca deberia llamarse
        payload: dict[str, object],  # noqa: ARG002 - nunca deberia llamarse
    ) -> dict[str, object]:
        raise AssertionError("no deberia llamar a Cloudflare para una zona no permitida")

    async def delete_dns_record(
        self,
        zone_id: str,  # noqa: ARG002 - nunca deberia llamarse
        record_id: str,  # noqa: ARG002 - nunca deberia llamarse
    ) -> dict[str, object]:
        raise AssertionError("no deberia llamar a Cloudflare para una zona no permitida")


def _live(client: object, *, allowed_zones: frozenset[str] = frozenset()) -> LiveCloudflareService:
    return LiveCloudflareService(client, allowed_zones=allowed_zones)  # type: ignore[arg-type]


# --- NullCloudflareService: sin token, sin red, siempre el sentinel --------


async def test_null_service_never_touches_the_network() -> None:
    service = NullCloudflareService()

    zones = await service.list_dns_zones()
    records = await service.list_dns_records("example.com", record_type=None, name=None)
    upserted = await service.upsert_dns_record(
        "example.com",
        record_type=DnsRecordType.A,
        name="www.example.com",
        content="1.2.3.4",
        ttl=300,
        proxied=False,
        comment=None,
    )
    deleted = await service.delete_dns_record("example.com", "some-id")

    assert isinstance(zones, CloudflareNotConfigured)
    assert isinstance(records, CloudflareNotConfigured)
    assert isinstance(upserted, CloudflareNotConfigured)
    assert isinstance(deleted, CloudflareNotConfigured)


def test_build_cloudflare_service_returns_null_when_token_is_empty() -> None:
    assert isinstance(build_cloudflare_service(None, frozenset()), NullCloudflareService)
    assert isinstance(build_cloudflare_service("", frozenset()), NullCloudflareService)


def test_build_cloudflare_service_returns_live_when_token_is_set() -> None:
    assert isinstance(build_cloudflare_service("tok", frozenset()), LiveCloudflareService)


# --- LiveCloudflareService: lista de zonas -----------------------------------


async def test_list_dns_zones_filters_to_the_allow_list() -> None:
    client = _FakeCloudflareClient(zones=[_ZONE_JSON, _OTHER_ZONE_JSON])
    service = _live(client, allowed_zones=frozenset({"example.com"}))

    zones = await service.list_dns_zones()

    assert zones == (CloudflareZone(zone_id=_ZONE_ID, name="example.com", status="active"),)


async def test_list_dns_zones_returns_everything_when_allow_list_is_empty() -> None:
    client = _FakeCloudflareClient(zones=[_ZONE_JSON, _OTHER_ZONE_JSON])
    service = _live(client, allowed_zones=frozenset())

    zones = await service.list_dns_zones()

    assert {zone.name for zone in zones} == {"example.com", "otra.com"}


# --- allow-list: cero llamadas de red para una zona no permitida ------------


async def test_zone_not_allowed_never_calls_the_client() -> None:
    service = _live(_ExplodingCloudflareClient(), allowed_zones=frozenset({"example.com"}))

    with pytest.raises(CloudflareZoneNotAllowedError):
        await service.list_dns_records("otra.com", record_type=None, name=None)


async def test_zone_not_found_in_cloudflare_raises_not_found() -> None:
    client = _FakeCloudflareClient(zones=[])
    service = _live(client)

    with pytest.raises(CloudflareZoneNotFoundError):
        await service.list_dns_records("example.com", record_type=None, name=None)


# --- lectura de registros -----------------------------------------------


async def test_list_dns_records_resolves_zone_id_and_forwards_filters() -> None:
    client = _FakeCloudflareClient(records=[_RECORD_JSON])
    service = _live(client)

    records = await service.list_dns_records(
        "example.com", record_type=DnsRecordType.A, name="www.example.com"
    )

    assert len(records) == 1
    assert records[0].record_id == _RECORD_JSON["id"]
    assert ("list_dns_records", (_ZONE_ID, "A", "www.example.com")) in client.calls


# --- upsert: crear, actualizar, ambiguo --------------------------------------


async def test_upsert_creates_when_no_existing_record_matches() -> None:
    client = _FakeCloudflareClient(records=[])
    service = _live(client)

    record = await service.upsert_dns_record(
        "example.com",
        record_type=DnsRecordType.A,
        name="www.example.com",
        content="1.2.3.4",
        ttl=300,
        proxied=False,
        comment=None,
    )

    assert record.record_id == _RECORD_JSON["id"]
    assert any(name == "create_dns_record" for name, _ in client.calls)
    assert not any(name == "update_dns_record" for name, _ in client.calls)


async def test_upsert_updates_the_single_existing_match() -> None:
    client = _FakeCloudflareClient(records=[_RECORD_JSON])
    service = _live(client)

    await service.upsert_dns_record(
        "example.com",
        record_type=DnsRecordType.A,
        name="www.example.com",
        content="5.6.7.8",
        ttl=300,
        proxied=False,
        comment=None,
    )

    update_calls = [args for name, args in client.calls if name == "update_dns_record"]
    assert update_calls == [(_ZONE_ID, _RECORD_JSON["id"], {
        "type": "A",
        "name": "www.example.com",
        "content": "5.6.7.8",
        "ttl": 300,
        "proxied": False,
    })]
    assert not any(name == "create_dns_record" for name, _ in client.calls)


async def test_upsert_raises_when_more_than_one_record_matches() -> None:
    duplicate = dict(_RECORD_JSON, id="another-id")
    client = _FakeCloudflareClient(records=[_RECORD_JSON, duplicate])
    service = _live(client)

    with pytest.raises(CloudflareRecordAmbiguousError):
        await service.upsert_dns_record(
            "example.com",
            record_type=DnsRecordType.A,
            name="www.example.com",
            content="1.2.3.4",
            ttl=300,
            proxied=False,
            comment=None,
        )


async def test_upsert_includes_comment_only_when_provided() -> None:
    client = _FakeCloudflareClient(records=[])
    service = _live(client)

    await service.upsert_dns_record(
        "example.com",
        record_type=DnsRecordType.TXT,
        name="example.com",
        content="v=spf1 -all",
        ttl=300,
        proxied=False,
        comment="SPF",
    )

    create_calls = [args for name, args in client.calls if name == "create_dns_record"]
    assert create_calls[0][1]["comment"] == "SPF"


# --- delete ------------------------------------------------------------------


async def test_delete_dns_record_resolves_zone_and_confirms() -> None:
    client = _FakeCloudflareClient()
    service = _live(client)

    result = await service.delete_dns_record("example.com", _RECORD_JSON["id"])

    assert result == CloudflareDnsRecordDeleted(record_id=_RECORD_JSON["id"], zone="example.com")
    assert ("delete_dns_record", (_ZONE_ID, _RECORD_JSON["id"])) in client.calls


async def test_delete_dns_record_zone_not_allowed_never_calls_the_client() -> None:
    service = _live(_ExplodingCloudflareClient(), allowed_zones=frozenset({"example.com"}))

    with pytest.raises(CloudflareZoneNotAllowedError):
        await service.delete_dns_record("otra.com", "some-id")
