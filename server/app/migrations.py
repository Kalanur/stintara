"""Transactional compatibility step for the pre-test V0.1 schema."""
from .uuid7 import uuid7


async def migrate_sample_ids(conn):
    data_type = await conn.fetchval("""
        SELECT data_type FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name='strategy_samples' AND column_name='id'
    """)
    if data_type != "bigint":
        return
    # No V0.1 table references sample IDs. Custom foreign keys cause a safe rollback.
    await conn.execute("LOCK TABLE strategy_samples IN ACCESS EXCLUSIVE MODE")
    await conn.execute("ALTER TABLE strategy_samples ADD COLUMN migration_uuid uuid")
    for row in await conn.fetch("SELECT id FROM strategy_samples"):
        await conn.execute("UPDATE strategy_samples SET migration_uuid=$2 WHERE id=$1", row["id"], uuid7())
    await conn.execute("ALTER TABLE strategy_samples ALTER COLUMN id DROP DEFAULT")
    await conn.execute("ALTER TABLE strategy_samples ALTER COLUMN id TYPE uuid USING migration_uuid")
    await conn.execute("ALTER TABLE strategy_samples DROP COLUMN migration_uuid")
