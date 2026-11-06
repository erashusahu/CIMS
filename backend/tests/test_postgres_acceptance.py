import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.skipif(not os.getenv("CIMS_TEST_DATABASE_URL"), reason="Set CIMS_TEST_DATABASE_URL to an isolated PostgreSQL test database")


@pytest.mark.asyncio
async def test_migrations_and_tenant_rls():
    engine = create_async_engine(os.environ["CIMS_TEST_DATABASE_URL"])
    async with engine.begin() as conn:
        migration = await conn.execute(text("SELECT version_num FROM alembic_version"))
        assert migration.scalar_one() == "0001_foundation"
        await conn.execute(text("SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-000000000001', true)"))
        visible = await conn.execute(text("SELECT count(*) FROM businesses WHERE id='00000000-0000-0000-0000-000000000002'"))
        assert visible.scalar_one() == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_audit_table_has_append_only_trigger():
    engine = create_async_engine(os.environ["CIMS_TEST_DATABASE_URL"])
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname IN ('audit_logs_append_only','audit_logs_no_truncate') AND NOT tgisinternal"))
        assert result.scalar_one() == 2
    await engine.dispose()
