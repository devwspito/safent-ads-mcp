"""IAM fixtures do not TRUNCATE through account/audit FKs in shared databases."""

from uuid import uuid4

import pytest
from tests.conftest import _recreate_database, alembic_upgrade, to_alembic_dsn, with_database


@pytest.fixture(scope="module")
def isolated_iam_database_url(postgres_container) -> str:
    base = postgres_container.get_connection_url()
    name = f"ads_iam_{uuid4().hex}"
    _recreate_database(base, name)
    url = with_database(to_alembic_dsn(base), name)
    alembic_upgrade(url)
    return url
