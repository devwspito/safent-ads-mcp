"""Immutable server-resolved authority snapshot; data, NOT human consent.

Only an admission adapter may source a binding. Parsing persisted/signed data
does not establish that its authority is still live (or that a human approved).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from uuid import UUID

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

MANAGED_ADS_CAPABILITIES = ("read", "propose", "approve", "execute")
_MAX_REMOTE_ACCOUNT_LENGTH = 128


def enterprise_account_ref(provider: AccountRef) -> AccountRef:
    """Decode ONE provider representation into the numeric EE claim contract.

    Meta accepts only act_<ASCII digits>; Google only ASCII digits. No alternate
    credential lookup, trimming, integer conversion or display-ID normalization.
    Business/connection and the exact digit sequence are retained.
    """
    raw = provider.external_account_id
    if not isinstance(raw, str) or not isinstance(provider.platform, PlatformCode):
        raise ValueError("managed_account_format_invalid")
    if provider.platform == PlatformCode.META:
        if not raw.startswith("act_"):
            raise ValueError("managed_account_format_invalid")
        raw = raw[4:]
    if not raw.isascii() or not raw.isdecimal() or not 1 <= len(raw) <= _MAX_REMOTE_ACCOUNT_LENGTH:
        raise ValueError("managed_account_format_invalid")
    return replace(provider, external_account_id=raw)


@dataclass(frozen=True, slots=True)
class ManagedAdsBinding:
    grant_id: UUID
    revision: int
    org_id: UUID
    user_id: UUID
    employee_id: UUID
    instance_id: UUID
    account: AccountRef
    resource_revision: int

    @property
    def provider_account(self) -> AccountRef:
        """Native account reference; claims and historical signatures stay numeric."""
        if self.account.platform == PlatformCode.META:
            return replace(
                self.account, external_account_id="act_" + self.account.external_account_id
            )
        return self.account

    def __post_init__(self) -> None:
        if not isinstance(self.account, AccountRef):
            raise ValueError("invalid_managed_binding")
        if any(
            not isinstance(value, UUID)
            for value in (
                self.grant_id,
                self.org_id,
                self.user_id,
                self.employee_id,
                self.instance_id,
                self.account.business_id,
                self.account.connection_id,
            )
        ):
            raise ValueError("invalid_managed_binding")
        if any(
            type(value) is not int or value < 1 for value in (self.revision, self.resource_revision)
        ):
            raise ValueError("invalid_managed_binding")
        remote = self.account.external_account_id
        if (
            not isinstance(self.account.platform, PlatformCode)
            or not isinstance(remote, str)
            or not remote.isascii()
            or not remote.isdecimal()
            or not 1 <= len(remote) <= _MAX_REMOTE_ACCOUNT_LENGTH
        ):
            raise ValueError("invalid_managed_binding")

    def validate_entity(self, entity: EntityRef) -> None:
        if (
            entity.platform != self.account.platform
            or entity.business_id != self.account.business_id
            or entity.connection_id != self.account.connection_id
            or (
                entity.level == EntityLevel.ACCOUNT
                and entity.external_id != self.provider_account.external_account_id
            )
        ):
            raise ValueError("managed_binding_scope_mismatch")

    def as_claims(self) -> dict[str, object]:
        return {
            "grant_id": str(self.grant_id),
            "revision": self.revision,
            "org_id": str(self.org_id),
            "user_id": str(self.user_id),
            "employee_id": str(self.employee_id),
            "instance_id": str(self.instance_id),
            "business_id": str(self.account.business_id),
            "platform": self.account.platform.value,
            "connection_id": str(self.account.connection_id),
            "external_account_id": self.account.external_account_id,
            "resource_revision": self.resource_revision,
            "role": "ads",
            "capabilities": list(MANAGED_ADS_CAPABILITIES),
        }

    @classmethod
    def from_claims(cls, value: object) -> ManagedAdsBinding:
        fields = {
            "grant_id",
            "revision",
            "org_id",
            "user_id",
            "employee_id",
            "instance_id",
            "business_id",
            "platform",
            "connection_id",
            "external_account_id",
            "resource_revision",
            "role",
            "capabilities",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("invalid_managed_binding")
        try:
            if value["role"] != "ads" or value["capabilities"] != list(MANAGED_ADS_CAPABILITIES):
                raise ValueError
            ids = {}
            for name in (
                "grant_id",
                "org_id",
                "user_id",
                "employee_id",
                "instance_id",
                "business_id",
                "connection_id",
            ):
                raw = value[name]
                if not isinstance(raw, str) or str(UUID(raw)) != raw:
                    raise ValueError
                ids[name] = UUID(raw)
            return cls(
                ids["grant_id"],
                value["revision"],
                ids["org_id"],
                ids["user_id"],
                ids["employee_id"],
                ids["instance_id"],
                AccountRef(
                    PlatformCode(value["platform"]),
                    value["external_account_id"],
                    ids["business_id"],
                    ids["connection_id"],
                ),
                value["resource_revision"],
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invalid_managed_binding") from exc


def binding_from_json(value: object) -> ManagedAdsBinding | None:
    """Nullable SQL/wire boundary; missing legacy context stays missing."""
    return None if value is None else ManagedAdsBinding.from_claims(value)


def binding_to_json(binding: ManagedAdsBinding | None) -> str | None:
    return (
        None
        if binding is None
        else json.dumps(binding.as_claims(), sort_keys=True, separators=(",", ":"))
    )
