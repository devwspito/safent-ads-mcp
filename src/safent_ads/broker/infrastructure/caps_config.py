"""Carga `config/caps.yaml`: los topes duros que `ads-broker` aplica el
mismo, independientes de la base de datos (T066, plan.md §3.3: "un fichero
de topes duros que la API no puede escribir"; threat-model.md C-17: "topes
diario/mensual sobre el ledger de cambios aplicados... mas contador por
entidad y dia"; plan.md §15 D-A1: valores por defecto de autonomia).

Fail-closed a proposito: un fichero ausente, invalido, o una cuenta sin
entrada propia no produce un `CapsConfig` vacio ni topes permisivos por
omision -- lanza `CapsConfigError`. La unica via de exito es un fichero bien
formado (parseo identico a `rules/domain/rule_catalog_schema.py`).

Seam para una tarea futura: `resolve()` es el punto que `execute_write`
(broker/presentation/dispatcher.py) debera consultar antes de reenviar una
escritura a `GoogleAdsAdapter`/`MetaAdsAdapter`. Hoy nadie la llama todavia
-- F2 la conecta junto con `ExecutionChokepoint` (plan.md §6)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from safent_ads.broker.domain.account_key import (
    InvalidPlatformAccountIdError,
    canonical_platform_account_id,
)
from safent_ads.shared.errors import InfrastructureError

_MAX_STEP_PCT_CEILING = 100.0

# Techo de todo importe en unidad menor (spec 008 `data-model.md`
# §`SpendEnvelope`): JSON admite enteros ilimitados y la aritmetica del
# ledger no. 10^12 centimos son 10.000 millones de euros -- ningun sobre
# real se acerca, y un desbordamiento deja de ser representable.
MAX_MINOR_AMOUNT: Final = 10**12

_CURRENCY_PATTERN: Final = r"^[A-Z]{3}$"


class CapsConfigError(InfrastructureError):
    """`config/caps.yaml` ausente, con YAML invalido, con un esquema
    incorrecto, o sin entrada para la cuenta pedida."""


class CapsDefaults(BaseModel):
    """Umbrales de autonomia D-A1 (plan.md §15) aplicados a toda cuenta que
    no los sobreescriba explicitamente. Los topes monetarios
    (`daily_cap_minor`, etc.) no tienen aqui un valor razonable -- son
    siempre obligatorios por cuenta."""

    model_config = ConfigDict(extra="forbid")

    max_step_pct: float = Field(gt=0, le=_MAX_STEP_PCT_CEILING)
    max_changes_per_day: int = Field(gt=0)
    autonomy_enabled: bool


class _AccountCapsOverride(BaseModel):
    """Entrada tal cual aparece bajo `accounts:` en el YAML. Los campos de
    comportamiento son opcionales: ausentes, se toman de `defaults`."""

    model_config = ConfigDict(extra="forbid")

    daily_cap_minor: int = Field(ge=0)
    monthly_cap_minor: int = Field(ge=0)
    floor_minor: int = Field(ge=0)
    ceiling_minor: int = Field(ge=0)
    max_step_pct: float | None = Field(default=None, gt=0, le=_MAX_STEP_PCT_CEILING)
    max_changes_per_day: int | None = Field(default=None, gt=0)
    autonomy_enabled: bool | None = None

    @model_validator(mode="after")
    def _floor_not_above_ceiling(self) -> _AccountCapsOverride:
        if self.floor_minor > self.ceiling_minor:
            raise ValueError(
                f"floor_minor > ceiling_minor: {self.floor_minor} > {self.ceiling_minor}"
            )
        return self


class AccountCaps(BaseModel):
    """Topes resueltos para una `platform_account_id`: ya fusionados con
    `defaults`, sin campos opcionales -- lo que consume `resolve()`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    daily_cap_minor: int
    monthly_cap_minor: int
    floor_minor: int
    ceiling_minor: int
    max_step_pct: float
    max_changes_per_day: int
    autonomy_enabled: bool


class CapsResolverPort(Protocol):
    """Lo UNICO que `WriteAuthorizationPipeline` necesita de los topes.
    `CapsConfig` lo satisface estructuralmente (solo fichero, comportamiento
    de hoy) y `EffectiveCapsResolver` tambien (fichero + estado del panel,
    recortado al sobre en CADA llamada). Declarado aqui, junto a los tipos
    que devuelve, para no invertir la dependencia: `broker/platforms/
    write_pipeline.py` ya importaba de este modulo."""

    def resolve(self, platform_account_id: str) -> AccountCaps: ...


class SpendEnvelope(BaseModel):
    """El *sobre de gasto* (spec 008 `plan.md` §5, `data-model.md`): hasta
    donde puede repartir el panel. Root-only, porque vive en el mismo
    `config/caps.yaml` que `ads-api` no puede escribir.

    Los SIETE campos son obligatorios a proposito: un sobre declarado a
    medias seria un defecto permisivo por omision, justo lo que el resto de
    este sistema rechaza. Y no hay valor por defecto para ninguno -- un
    sobre "razonable" de fabrica es la misma clase de defecto.

    `strict=True`: `3.0` no es 3, `"3"` no es 3 y `true` no es 1. Los
    importes son enteros en unidad menor de `currency`, con cota superior
    porque JSON no la tiene.

    Por que acota tambien el techo y el suelo (revision T027, C1):
    `max_daily_cap_minor`/`max_monthly_cap_minor` acotan el DELTA ACUMULADO
    de subidas aplicadas, no el presupuesto en pie -- un presupuesto alto
    fijado una vez sigue gastando cada dia sin mas escrituras. El unico
    tope del importe absoluto es `ceiling_minor`, y en una cuenta
    solo-panel el `min(fichero, panel)` no existe."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_daily_cap_minor: int = Field(ge=0, le=MAX_MINOR_AMOUNT)
    max_monthly_cap_minor: int = Field(ge=0, le=MAX_MINOR_AMOUNT)
    max_ceiling_minor: int = Field(ge=0, le=MAX_MINOR_AMOUNT)
    min_floor_minor: int = Field(ge=0, le=MAX_MINOR_AMOUNT)
    max_accounts: int = Field(ge=1)
    max_cap_changes_per_day: int = Field(ge=1)
    currency: str = Field(pattern=_CURRENCY_PATTERN)

    @model_validator(mode="after")
    def _bounds_are_coherent(self) -> SpendEnvelope:
        if self.max_monthly_cap_minor < self.max_daily_cap_minor:
            raise ValueError(
                "max_monthly_cap_minor < max_daily_cap_minor: "
                f"{self.max_monthly_cap_minor} < {self.max_daily_cap_minor}"
            )
        if self.min_floor_minor > self.max_ceiling_minor:
            raise ValueError(
                "min_floor_minor > max_ceiling_minor: "
                f"{self.min_floor_minor} > {self.max_ceiling_minor}"
            )
        if self.max_daily_cap_minor > self.max_ceiling_minor:
            raise ValueError(
                "max_daily_cap_minor > max_ceiling_minor: "
                f"{self.max_daily_cap_minor} > {self.max_ceiling_minor}"
            )
        return self


class CapsConfig(BaseModel):
    """Raiz de `config/caps.yaml` ya validada."""

    model_config = ConfigDict(extra="forbid")

    defaults: CapsDefaults
    accounts: dict[str, _AccountCapsOverride] = Field(default_factory=dict)
    # Ausente = el panel NO puede fijar ningun tope: comportamiento de hoy,
    # intacto. Una instancia que no lo declara no cambia en nada.
    panel_managed: SpendEnvelope | None = None

    @model_validator(mode="after")
    def _file_keys_are_canonical_when_the_panel_can_write(self) -> CapsConfig:
        """Solo con sobre declarado. El estado del panel se guarda bajo la
        clave canonica; si una entrada del fichero usara otra forma del
        mismo id, `min(fichero, panel)` no se aplicaria y el operador veria
        un tope que no es el que se aplica. Se falla al arrancar, nombrando
        la clave, en vez de resolver mal en silencio. Sin `panel_managed`
        no se comprueba nada: el fichero sigue admitiendo cualquier clave,
        byte a byte como hoy."""
        if self.panel_managed is None:
            return self
        for key in self.accounts:
            try:
                canonical = canonical_platform_account_id(key)
            except InvalidPlatformAccountIdError as exc:
                raise ValueError(f"accounts: clave de cuenta invalida {key!r}: {exc}") from exc
            if canonical != key:
                raise ValueError(
                    f"accounts: con panel_managed declarado la clave {key!r} debe escribirse "
                    f"en su forma canonica {canonical!r}"
                )
        return self

    def resolve(self, platform_account_id: str) -> AccountCaps:
        """Topes de una cuenta con los campos de comportamiento ausentes
        rellenados desde `defaults`. `CapsConfigError` si la cuenta no
        tiene entrada -- sin autonomia implicita para cuentas no
        configuradas."""
        override = self.accounts.get(platform_account_id)
        if override is None:
            raise CapsConfigError(f"cuenta sin topes configurados: {platform_account_id!r}")
        return AccountCaps(
            daily_cap_minor=override.daily_cap_minor,
            monthly_cap_minor=override.monthly_cap_minor,
            floor_minor=override.floor_minor,
            ceiling_minor=override.ceiling_minor,
            max_step_pct=_first_not_none(override.max_step_pct, self.defaults.max_step_pct),
            max_changes_per_day=_first_not_none(
                override.max_changes_per_day, self.defaults.max_changes_per_day
            ),
            autonomy_enabled=_first_not_none(
                override.autonomy_enabled, self.defaults.autonomy_enabled
            ),
        )


def _first_not_none[T](value: T | None, fallback: T) -> T:
    return fallback if value is None else value


def parse_caps_config(raw_yaml: str) -> CapsConfig:
    """Pura: sin tocar el sistema de ficheros (mismo patron que
    `rule_catalog_schema.parse_catalog`)."""
    try:
        document = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise CapsConfigError(f"YAML invalido: {exc}") from exc
    if not isinstance(document, dict):
        raise CapsConfigError(f"la raiz debe ser un mapeo YAML, no {type(document).__name__}")
    try:
        return CapsConfig.model_validate(document)
    except (TypeError, ValueError) as exc:
        raise CapsConfigError(f"esquema de topes invalido: {exc}") from exc


@dataclass(frozen=True, slots=True)
class HardCapsStatus:
    caps_digest: str
    accounts_count: int


@dataclass(frozen=True, slots=True)
class LoadedCapsConfig:
    caps: CapsConfig
    status: HardCapsStatus


def load_caps_snapshot(path: Path) -> LoadedCapsConfig:
    """Digest and policy come from the SAME bytes, read once at startup.

    A later file replacement cannot make an old broker claim the new policy.
    No paths, amounts, account identifiers or credential material enter status.
    """
    if not path.is_file():
        raise CapsConfigError(f"fichero de topes no encontrado: {path}")
    raw = path.read_bytes()
    try:
        caps = parse_caps_config(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise CapsConfigError("caps_invalid_encoding") from exc
    return LoadedCapsConfig(
        caps=caps,
        status=HardCapsStatus(hashlib.sha256(raw).hexdigest(), len(caps.accounts)),
    )


def load_caps_config(path: Path) -> CapsConfig:
    """Unico punto que toca el sistema de ficheros. `path` normalmente
    `BrokerSettings.hard_caps_file` (`ADS_BROKER_HARD_CAPS_FILE`,
    `/etc/ads-broker/caps.yaml` en compose.yaml)."""
    return load_caps_snapshot(path).caps
