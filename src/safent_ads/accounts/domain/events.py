"""Eventos de dominio de `accounts` (plan.md §5, data-model.md §Eventos de
dominio). Cada uno hereda los tres campos obligatorios de `DomainEvent`
(`business_id`, `occurred_at`, `cycle_id`)."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.accounts.domain.budget import Budget
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.shared.events import DomainEvent
from safent_ads.shared.ids import EntityRef, PlatformCode


@dataclass(frozen=True, kw_only=True)
class PlatformAccountThrottled(DomainEvent):
    account_ref: AccountRef


@dataclass(frozen=True, kw_only=True)
class PlatformAccountSuspended(DomainEvent):
    account_ref: AccountRef
    reason: str


@dataclass(frozen=True, kw_only=True)
class PlatformAccountConnected(DomainEvent):
    """El propietario completo el flujo OAuth "Conectar" (o pego un System
    User token de Meta) y la cuenta quedo enlazada a una credencial
    valida."""

    account_ref: AccountRef
    credential_ref_id: str


@dataclass(frozen=True, kw_only=True)
class CredentialInvalidated(DomainEvent):
    """Una `PlatformCredential` dejo de ser utilizable: revocada por el
    propietario o rechazada por la plataforma (threat-model.md C-21)."""

    credential_ref_id: str
    platform: PlatformCode
    reason: str


@dataclass(frozen=True, kw_only=True)
class AdEntityDrifted(DomainEvent):
    entity_ref: EntityRef
    expected_hash: PlatformStateHash
    observed_hash: PlatformStateHash


@dataclass(frozen=True, kw_only=True)
class AdEntityBudgetChanged(DomainEvent):
    entity_ref: EntityRef
    previous_budget: Budget | None
    new_budget: Budget


@dataclass(frozen=True, kw_only=True)
class LearningPhaseEntered(DomainEvent):
    entity_ref: EntityRef


@dataclass(frozen=True, kw_only=True)
class LearningPhaseCompleted(DomainEvent):
    entity_ref: EntityRef
