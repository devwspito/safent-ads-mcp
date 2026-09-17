"""Account-scoped Meta identifiers: act_<account>/<node>; no ambient context."""

import re

from safent_ads.broker.platforms.errors import CredentialNotConnectedError

_SCOPED = re.compile(r"^(act_[0-9]+)(?:/([0-9]+))?$")


def split_meta_scope(reference: str) -> tuple[str, str]:
    match = _SCOPED.fullmatch(reference)
    if match is None:
        raise CredentialNotConnectedError("Meta requiere act_<cuenta>/<entidad>")
    account_id, node_id = match.groups()
    return account_id, node_id or account_id


def scoped_meta_id(account_id: str, node_id: str) -> str:
    account, _ = split_meta_scope(account_id)
    if not node_id.isascii() or not node_id.isdigit():
        raise CredentialNotConnectedError("id Meta invalido")
    return f"{account}/{node_id}"
