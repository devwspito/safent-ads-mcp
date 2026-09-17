"""Real Runtime routing/publisher; replace only HTTPS destinations with ASGI."""

# ruff: noqa: PLC0415 -- Runtime is an optional explicit cross-repo test dependency.

import json
from datetime import UTC, datetime

import httpx


def runtime_driver(service, binding, enterprise_app, central, tmp_path):
    from importlib.resources import files

    from hermes.instance.association_store import InstanceAssociation, SQLiteAssociationStore
    from hermes.runtime.managed_ads_policy import ManagedAdsUnavailable, apply_signed_ads
    from hermes.runtime.managed_ads_transport import ManagedAdsTransport
    from hermes.shell_server.security.secrets import SecretsVault
    from safent_control.application.policy_publisher import PolicyPublisher

    from safent_ads.composition.managed_service import TOOL_MODELS

    assert json.loads(
        files("hermes.runtime").joinpath("managed_ads_tool_schemas.json").read_text()
    ) == {name: model.model_json_schema() for name, model in TOOL_MODELS.items()}

    instance_id = str(binding.instance_id)
    publisher = PolicyPublisher(repo=service.repo, keystore=service.keystore)
    publisher.publish(instance_id=instance_id)
    policy = service.repo.get_latest_policy(instance_id)
    envelope = {**policy, "payload": json.loads(policy["payload_json"])}
    envelope.pop("payload_json")
    store = SQLiteAssociationStore(
        db_path=tmp_path / "community.sqlite", vault=SecretsVault(master_key=b"s" * 32)
    )
    store.save(
        association=InstanceAssociation(
            instance_id=instance_id,
            tenant_id=str(binding.org_id),
            paired_at=datetime.now(UTC).isoformat(),
            cloud_endpoint="https://enterprise.invalid",
            signing_pubkey_hex=service.keystore.public_hex(org_id=str(binding.org_id)),
            license={},
            last_applied_version=0,
            state="active",
        ),
        instance_secret="synthetic-pairing-unused",
    )
    apply_signed_ads(store, json.dumps(envelope))

    async def post(url, bearer, body, *, forbidden):
        target = enterprise_app if url.startswith("https://enterprise.invalid/") else central
        async with httpx.AsyncClient(transport=httpx.ASGITransport(target)) as client:
            response = await client.post(
                url, json=body, headers={"Authorization": "Bearer " + bearer}
            )
        if response.status_code != 200:
            raise ManagedAdsUnavailable()
        assert all(secret not in response.text for secret in forbidden)
        return response.json()

    class Driver:
        async def post(self, path, *, json):
            result = await ManagedAdsTransport(store, post=post).call(
                str(binding.grant_id), path.rsplit("/", 1)[-1], json
            )
            return httpx.Response(200, json=result)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    return Driver()
