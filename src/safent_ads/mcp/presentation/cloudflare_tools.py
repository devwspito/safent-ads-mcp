"""`get_cloudflare_connection_status`/`list_dns_zones`/`list_dns_records`/
`upsert_dns_record`/`delete_dns_record` (Anadido del dueno, 14-sep:
"MCP=acceso+control+auditoria, no coaching" -- el arnes no puede llegar a
Cloudflare sin la credencial de la empresa; lane 006-cloudflare-ui,
15-sep: "crear una conexion con Cloudflare pidiendo el token e indicando
el enlace donde crearlo" -- el MCP nunca acepta el token, solo dice
DONDE conectarlo).

Modulo autonomo (mismo criterio de aislamiento que `passthrough_tools.py`/
`connection_tools.py`): declara sus propios `Args` sobre `mcp.presentation.
args.ToolArgs`, sin tocar ese fichero compartido; `catalog.py` lo engancha
con una linea. `business_id` en los cinco modelos hace literal la regla 4
del contrato (`args.py`: "todo argumento resuelve a un business_id") aunque
las zonas de Cloudflare no pertenezcan a un negocio en el sentido de un FK
-- mismo trato que `get_meta_graph` (Anadido, ver `passthrough_tools.py`).

`services.cloudflare` (`DynamicCloudflareService`, `integrations/
cloudflare/connection_service.py`) resuelve el token guardado en el panel
primero, `CLOUDFLARE_API_TOKEN` de respaldo despues, en CADA llamada. Sin
ninguno de los dos, `CloudflareNotConfigured` (Null Object) trae su
propio `create_token_url` -- las cuatro herramientas de DNS lo traducen a
`CloudflareNotConnectedError` (typed, `CLOUDFLARE_NOT_CONNECTED`, con el
enlace en el mensaje) en vez del resultado limpio `{"configured": False}`
de antes: sin conexion, la unica accion util es decirle al agente DONDE
conectarla, no dejar que siga probando cuatro herramientas que van a
fallar igual. Las dos escrituras son `CATALOG_WRITE` (solo `aprobar`) y
rechazan zonas fuera de `CLOUDFLARE_ALLOWED_ZONES` antes de tocar la red;
la auditoria (con la persona que llama) la hace `ToolDispatcher` para las
cinco, como para el resto del catalogo -- este modulo no anade una
segunda."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

from pydantic import Field

from safent_ads.integrations.cloudflare import (
    CloudflareApiError,
    CloudflareConnectionStatus,
    CloudflareNotConfigured,
    CloudflarePort,
    CloudflareRecordAmbiguousError,
    CloudflareResponseTooLargeError,
    CloudflareTransportError,
    CloudflareZoneNotAllowedError,
    CloudflareZoneNotFoundError,
    DnsRecordType,
)
from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.errors import ToolDispatchError, ToolValidationError
from safent_ads.mcp.presentation.args import BusinessId, OpaqueId, ToolArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition

__all__ = ["CloudflareToolServices", "build_cloudflare_tool_definitions"]

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

# Etiqueta de hostname DNS real (1-63 caracteres, sin guion en los extremos):
# sin look-around -- `pydantic_core` valida patrones con el motor `regex` de
# Rust, que no lo soporta (a diferencia de `re` en `args.py`).
_DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_ZONE_PATTERN = rf"^{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})+$"
# Un nombre de registro admite un comodin en la primera etiqueta (`*.foo.com`),
# una zona nunca.
_HOSTNAME_PATTERN = rf"^(?:\*|{_DOMAIN_LABEL})(?:\.{_DOMAIN_LABEL})+$"

_Zone = Annotated[str, Field(pattern=_ZONE_PATTERN, max_length=253)]
_Hostname = Annotated[str, Field(pattern=_HOSTNAME_PATTERN, max_length=253)]
# ASCII imprimible: cubre IPv4/IPv6 (A/AAAA), hostnames (CNAME/MX) y texto
# libre corto (TXT, p.ej. SPF/DKIM) sin admitir caracteres de control.
_RecordContent = Annotated[str, Field(pattern=r"^[\x20-\x7e]+$", max_length=2048)]
_Comment = Annotated[str, Field(max_length=100)]
_Ttl = Annotated[int, Field(ge=60, le=86400)]

_LIST_ZONES_DESCRIPTION = (
    "Zonas DNS de Cloudflare visibles con el token configurado, filtradas a "
    "CLOUDFLARE_ALLOWED_ZONES si esta fijada. Usala antes de list_dns_records/"
    "upsert_dns_record para confirmar el nombre exacto de la zona."
)
_LIST_RECORDS_DESCRIPTION = (
    "Registros DNS de una zona ya vista en list_dns_zones, filtrables por type "
    "(A/AAAA/CNAME/TXT/MX) y name. Solo lectura: no crea, cambia ni borra nada."
)
_UPSERT_DESCRIPTION = (
    "Crea o actualiza (por zona+type+name) un registro DNS A/AAAA/CNAME/TXT/MX. "
    "Los cambios en Cloudflare tardan minutos en propagarse por internet, nunca son "
    "instantaneos -- no esperes que un dominio nuevo resuelva en el acto. proxied=false "
    "es obligatorio si el host lo sirve nuestro propio TLS (con proxied=true, Cloudflare "
    "termina la conexion con SU certificado, no el nuestro); usa proxied=true solo si "
    "quieres que Cloudflare sirva y oculte el origen. Rechaza zonas fuera de "
    "CLOUDFLARE_ALLOWED_ZONES antes de tocar la red."
)
_DELETE_DESCRIPTION = (
    "Borra un registro DNS por su record_id (de list_dns_records). El borrado tambien "
    "tarda minutos en propagarse y no tiene deshacer. Rechaza zonas fuera de "
    "CLOUDFLARE_ALLOWED_ZONES antes de tocar la red."
)
_STATUS_DESCRIPTION = (
    "Estado de la conexion con Cloudflare: si esta conectada, la cuenta y las zonas que "
    "el token puede leer, y el enlace exacto donde el propietario crea un token nuevo "
    "(create_token_url) si no lo esta. Nunca devuelve el token. Usala antes de las cuatro "
    "herramientas de DNS cuando no sepas si Cloudflare esta conectado, o cuando alguna de "
    "ellas falle con CLOUDFLARE_NOT_CONNECTED."
)


class CloudflareZoneForbiddenError(ToolDispatchError):
    """La zona pedida no esta en `CLOUDFLARE_ALLOWED_ZONES`."""

    code = "CLOUDFLARE_ZONE_FORBIDDEN"


class CloudflareUpstreamError(ToolDispatchError):
    """Cloudflare rechazo la peticion o no respondio: fallo del proveedor,
    nunca de los argumentos del llamador."""

    code = "CLOUDFLARE_UPSTREAM_ERROR"


class CloudflareNotConnectedError(ToolDispatchError):
    """Ni conexion guardada en el panel ni `CLOUDFLARE_API_TOKEN` de
    respaldo (lane 006-cloudflare-ui). El mensaje lleva `create_token_url`
    -- el unico dato que el agente necesita para decirle al propietario
    donde conectarlo, nunca un secreto."""

    code = "CLOUDFLARE_NOT_CONNECTED"


class ConnectionStatusPort(Protocol):
    """`GetCloudflareConnectionStatus` (`integrations/cloudflare/
    connection_service.py`) cumple esto -- Protocol propio para que este
    modulo no dependa de la clase concreta ni de `session_factory`."""

    async def execute(self) -> CloudflareConnectionStatus: ...


class GetCloudflareConnectionStatusArgs(ToolArgs):
    business_id: BusinessId


class ListDnsZonesArgs(ToolArgs):
    business_id: BusinessId


class ListDnsRecordsArgs(ToolArgs):
    business_id: BusinessId
    zone: _Zone
    type: DnsRecordType | None = None
    name: _Hostname | None = None


class UpsertDnsRecordArgs(ToolArgs):
    business_id: BusinessId
    zone: _Zone
    type: DnsRecordType
    name: _Hostname
    content: _RecordContent
    ttl: _Ttl = 300
    proxied: bool = False
    comment: _Comment | None = None


class DeleteDnsRecordArgs(ToolArgs):
    business_id: BusinessId
    zone: _Zone
    record_id: OpaqueId


@dataclass(frozen=True, slots=True)
class CloudflareToolServices:
    cloudflare: CloudflarePort
    connection_status: ConnectionStatusPort


def build_cloudflare_tool_definitions(
    services: CloudflareToolServices,
) -> list[ToolDefinition[Any]]:
    return [
        ToolDefinition(
            name="get_cloudflare_connection_status",
            description=_STATUS_DESCRIPTION,
            args_model=GetCloudflareConnectionStatusArgs,
            tool_class=ToolClass.READ,
            handler=_get_connection_status(services),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="list_dns_zones",
            description=_LIST_ZONES_DESCRIPTION,
            args_model=ListDnsZonesArgs,
            tool_class=ToolClass.READ,
            handler=_list_dns_zones(services),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="list_dns_records",
            description=_LIST_RECORDS_DESCRIPTION,
            args_model=ListDnsRecordsArgs,
            tool_class=ToolClass.READ,
            handler=_list_dns_records(services),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="upsert_dns_record",
            description=_UPSERT_DESCRIPTION,
            args_model=UpsertDnsRecordArgs,
            tool_class=ToolClass.CATALOG_WRITE,
            handler=_upsert_dns_record(services),
            business_id_of=_by_business_id,
        ),
        ToolDefinition(
            name="delete_dns_record",
            description=_DELETE_DESCRIPTION,
            args_model=DeleteDnsRecordArgs,
            tool_class=ToolClass.CATALOG_WRITE,
            handler=_delete_dns_record(services),
            business_id_of=_by_business_id,
        ),
    ]


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)


async def _call_cloudflare[T](operation: Awaitable[T]) -> T:
    """Unico punto de traduccion de excepciones del conector (plan.md §8:
    "excepciones de dominio -> mapeador en presentacion"). Ninguna rama
    incluye el token ni un cuerpo crudo de Cloudflare -- los tres tipos que
    capturan la mensajean por su cuenta, ya sanitizado."""
    try:
        return await operation
    except CloudflareZoneNotAllowedError as exc:
        raise CloudflareZoneForbiddenError(str(exc)) from exc
    except (CloudflareZoneNotFoundError, CloudflareRecordAmbiguousError) as exc:
        raise ToolValidationError(str(exc)) from exc
    except (CloudflareApiError, CloudflareTransportError, CloudflareResponseTooLargeError) as exc:
        raise CloudflareUpstreamError(str(exc)) from exc


def _not_connected(sentinel: CloudflareNotConfigured) -> CloudflareNotConnectedError:
    return CloudflareNotConnectedError(
        "Cloudflare no esta conectado. Pide al propietario que lo conecte desde el panel "
        f"(Integraciones > Cloudflare) o cree un token en {sentinel.create_token_url}."
    )


def _get_connection_status(
    services: CloudflareToolServices,
) -> Handler[GetCloudflareConnectionStatusArgs, dict[str, object]]:
    async def handler(
        _args: GetCloudflareConnectionStatusArgs, _caller_scope: CallerScope
    ) -> dict[str, object]:
        status = await services.connection_status.execute()
        return {
            "connected": status.connected,
            "account_id": status.account_id,
            "zones": list(status.zones),
            "create_token_url": status.create_token_url,
            "required_permissions": list(status.required_permissions),
            "connected_at": (
                None if status.connected_at is None else status.connected_at.isoformat()
            ),
        }

    return handler


def _list_dns_zones(
    services: CloudflareToolServices,
) -> Handler[ListDnsZonesArgs, dict[str, object]]:
    async def handler(_args: ListDnsZonesArgs, _caller_scope: CallerScope) -> dict[str, object]:
        result = await _call_cloudflare(services.cloudflare.list_dns_zones())
        if isinstance(result, CloudflareNotConfigured):
            raise _not_connected(result)
        return {"configured": True, "zones": list(result)}

    return handler


def _list_dns_records(
    services: CloudflareToolServices,
) -> Handler[ListDnsRecordsArgs, dict[str, object]]:
    async def handler(args: ListDnsRecordsArgs, _caller_scope: CallerScope) -> dict[str, object]:
        result = await _call_cloudflare(
            services.cloudflare.list_dns_records(args.zone, record_type=args.type, name=args.name)
        )
        if isinstance(result, CloudflareNotConfigured):
            raise _not_connected(result)
        return {"configured": True, "records": list(result)}

    return handler


def _upsert_dns_record(
    services: CloudflareToolServices,
) -> Handler[UpsertDnsRecordArgs, dict[str, object]]:
    async def handler(args: UpsertDnsRecordArgs, _caller_scope: CallerScope) -> dict[str, object]:
        result = await _call_cloudflare(
            services.cloudflare.upsert_dns_record(
                args.zone,
                record_type=args.type,
                name=args.name,
                content=args.content,
                ttl=args.ttl,
                proxied=args.proxied,
                comment=args.comment,
            )
        )
        if isinstance(result, CloudflareNotConfigured):
            raise _not_connected(result)
        return {"configured": True, "record": result}

    return handler


def _delete_dns_record(
    services: CloudflareToolServices,
) -> Handler[DeleteDnsRecordArgs, dict[str, object]]:
    async def handler(args: DeleteDnsRecordArgs, _caller_scope: CallerScope) -> dict[str, object]:
        result = await _call_cloudflare(
            services.cloudflare.delete_dns_record(args.zone, args.record_id)
        )
        if isinstance(result, CloudflareNotConfigured):
            raise _not_connected(result)
        return {"configured": True, "deleted": True, "record": result}

    return handler
