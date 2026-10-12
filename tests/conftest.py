import os

import asyncpg
import pytest
import pytest_asyncio

from server.app import main
from server.app.uuid7 import uuid7


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL database")
    schema = "test_" + uuid7().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(url, min_size=1, max_size=3, server_settings={"search_path": schema})
    main.pool = pool
    main.runtime.clear()
    main.active_agents.clear()
    try:
        async with pool.acquire() as conn:
            await conn.execute(main.SCHEMA_FILE.read_text())
        yield pool
    finally:
        main.runtime.clear()
        main.pool = None
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def make_agent(pool, name="one", display_name="Same Name"):
    driver_id, agent_id = uuid7(), uuid7()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO drivers(id,slug,display_name) VALUES($1,$2,$3)", driver_id, name, display_name)
        await conn.execute("INSERT INTO agents(id,driver_id,name,token_sha256) VALUES($1,$2,$3,$4)",
                           agent_id, driver_id, name, main.token_hash("test-token-" + name))
    return {"id": agent_id, "driver_id": driver_id, "slug": name, "display_name": display_name}


async def scoring_session(pool, name="one"):
    agent = await make_agent(pool, name)
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Imola"})
    scoring = {"session_type": "Race", "session_time": 100.0, "player_vehicle_id": 7,
               "vehicles": [{"id": 7, "driver_name": "Same Name", "position": 1, "total_laps": 1},
                            {"id": 8, "driver_name": "Same Name", "position": 2, "total_laps": 1}]}
    await main.handle_message(agent, "lmu", "scoring", scoring)
    return agent, main.runtime[str(agent["id"])]["session_id"], scoring
