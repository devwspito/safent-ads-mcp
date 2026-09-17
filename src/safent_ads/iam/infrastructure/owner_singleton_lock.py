"""Advisory lock shared by the two paths that can create the FIRST owner of
an installation (spec 002b threat-model.md C-59): the Safent bridge
(`sql_owner_bridge_repository.py`) and Google federated login
(`sql_owner_federated_identity_repository.py`).

With an EMPTY `owners` table, `SELECT ... FOR UPDATE` locks no row -- there
is nothing to lock yet. Two distinct authorized identities racing on that
empty table (two authorized emails, or a bridge assertion racing a
federated login) can both pass the `SELECT` and both `INSERT`, breaking
"at most one owner per deployment" -- `owners.email` is `UNIQUE`, but the
two paths write DIFFERENT emails (the bridge's fixed synthetic address vs.
the real Google address), so that constraint alone never collides.

A single `pg_advisory_xact_lock`, taken by BOTH repositories as the very
first statement of `resolve_for_subject()`, serializes the whole
read-or-create-or-bind sequence regardless of which path arrives first.
One key, one constant, imported by both -- never copied."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

# hashtext() returns int4; pg_advisory_xact_lock accepts it as bigint.
# Transaction-scoped (`_xact_`): Postgres releases it on commit/rollback,
# never orphaned by a crashed connection.
LOCK_SOLE_OWNER_SQL: TextClause = text(
    "SELECT pg_advisory_xact_lock(hashtext('ads-owner-singleton'))"
)
