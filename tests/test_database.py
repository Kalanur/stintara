import json

import asyncpg
import pytest

from agent.pitwall import normalize
from server.app import main
from server.app.lifecycle import attach_observation, end_observation, expire_observations
from server.app.migrations import migrate_sample_ids
from server.app.uuid7 import uuid7
from tests.conftest import make_agent, scoring_session


async def test_identity_constraints_and_unresolved_participant(db):
    one, two = await make_agent(db), await make_agent(db, "two")
    async with db.acquire() as conn:
        for provider in ("racecontrol", "iracing"):
            await conn.execute("INSERT INTO driver_identities(id,driver_id,provider,external_id) VALUES($1,$2,$3,'123')",
                               uuid7(), one["driver_id"], provider)
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute("INSERT INTO driver_identities(id,driver_id,provider,external_id) VALUES($1,$2,'racecontrol','123')",
                               uuid7(), two["driver_id"])
        assert await conn.fetchval("SELECT count(*) FROM drivers WHERE display_name='Same Name'") == 2
        assert await conn.fetchval("SELECT count(*) FROM driver_identities WHERE driver_id=$1", one["driver_id"]) == 2
    agent, session_id, _ = await scoring_session(db, "three")
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT driver_id FROM session_participants WHERE session_id=$1 AND local_vehicle_id=8", session_id) is None
        assert await conn.fetchval("SELECT driver_id FROM session_participants WHERE session_id=$1 AND local_vehicle_id=7", session_id) == agent["driver_id"]


async def test_short_reconnect_survives_runtime_loss(db):
    agent, original, scoring = await scoring_session(db)
    main.runtime.clear()  # server restart / lost socket state
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Imola"})
    await main.handle_message(agent, "lmu", "scoring", {**scoring, "session_time": 110})
    assert main.runtime[str(agent["id"])]["session_id"] == original
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM sessions") == 1


@pytest.mark.parametrize("change", ["track", "type", "clock"])
async def test_new_real_session_stays_separate(db, change):
    agent, original, scoring = await scoring_session(db)
    if change == "track":
        await main.handle_message(agent, "lmu", "session_info", {"track_name": "Spa"})
    elif change == "type":
        scoring = {**scoring, "session_type": "Qualifying"}
    else:
        scoring = {**scoring, "session_time": 0}
    await main.handle_message(agent, "lmu", "scoring", scoring)
    assert main.runtime[str(agent["id"])]["session_id"] != original
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM sessions WHERE id=$1", original) == "complete"


async def test_multiple_observations_and_independent_disconnect(db):
    agent, session_id, scoring = await scoring_session(db)
    second = await make_agent(db, "two")
    async with db.acquire() as conn, conn.transaction():
        await attach_observation(conn, session_id, second["id"])
        await end_observation(conn, session_id, agent["id"])
        assert await conn.fetchval("SELECT status FROM sessions WHERE id=$1", session_id) == "live"
    await main.handle_message(second, "lmu", "session_info", {"track_name": "Imola"})
    await main.handle_message(second, "lmu", "scoring", {**scoring, "player_vehicle_id": 8})
    assert main.runtime[str(second["id"])]["session_id"] == session_id
    live = await main.get_live_rows()
    assert [row["slug"] for row in live] == ["two"]
    await main.finalize_agent_session(second)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM sessions WHERE id=$1", session_id) == "complete"


async def test_long_disconnect_hides_driver_and_closes_session(db):
    agent, session_id, scoring = await scoring_session(db)
    async with db.acquire() as conn:
        await conn.execute("UPDATE session_observations SET last_seen_at=now()-interval '121 seconds'")
        assert await main.get_live_rows() == []
        await expire_observations(conn, 120)
        assert await conn.fetchval("SELECT status FROM sessions WHERE id=$1", session_id) == "complete"
    main.runtime.clear()
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Imola"})
    await main.handle_message(agent, "lmu", "scoring", scoring)
    assert main.runtime[str(agent["id"])]["session_id"] != session_id


async def test_unrelated_agents_are_not_matched_by_track_or_name(db):
    _, first, _ = await scoring_session(db)
    _, second, _ = await scoring_session(db, "two")
    assert first != second


async def test_strategy_uuid_sampling_and_full_tyre_values(db):
    agent, session_id, _ = await scoring_session(db)
    telemetry = {"fuel": 42, "tires": [{"remaining": 1., "pressure": 170., "temp_inner": 80.,
                  "temp_mid": 90., "temp_outer": 100., "brake_temp": 450.}, {"remaining": 0.}]}
    for _ in range(10):
        await main.store_strategy_sample(agent, session_id, telemetry)
    main.runtime[str(agent["id"])].pop("last_sample_at")  # DB guard also survives reconnect
    await main.store_strategy_sample(agent, session_id, telemetry)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM strategy_samples") == 1
        row = await conn.fetchrow("SELECT * FROM strategy_samples")
        assert row["id"].version == 7
        assert row["tyre_remaining_fl"] == 1 and row["tyre_remaining_fr"] == 0
        assert row["tyre_temp_inner_fl_c"] == 80 and row["tyre_brake_temp_fl_c"] == 450
        await conn.execute("UPDATE strategy_samples SET recorded_at=now()-interval '2 seconds'")
    await main.store_strategy_sample(agent, session_id, telemetry)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM strategy_samples") == 2


async def test_lap_snapshot_idempotence_damage_and_no_live_regression(db):
    agent, session_id, scoring = await scoring_session(db)
    payload = {"session_type": "Race", "session_time": 100, "drivers": [
        {"id": 7, "total_laps": 1, "last_lap_time": 90., "position": 4, "has_telemetry": True,
         "wheels": [{"remaining": 1., "temp_inner": 80.}], "damage": {"dent_severity": [1, 0]}}]}
    await main.handle_message(agent, "lmu", "lap_snapshots", payload)
    await main.handle_message(agent, "lmu", "lap_snapshots", payload)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM laps") == 1
        row = await conn.fetchrow("SELECT * FROM laps")
        assert row["id"].version == 7 and row["tyre_remaining_fl"] == 1
        assert json.loads(row["damage"])["dent_severity"] == [1, 0]
        assert await conn.fetchval("SELECT position FROM session_participants WHERE local_vehicle_id=7") == 1


async def test_initial_lap_snapshot_waits_for_scoring(db):
    agent = await make_agent(db)
    await main.handle_message(agent, "lmu", "lap_snapshots", {"session_type": "Race", "session_time": 100,
        "drivers": [{"id": 7, "total_laps": 1, "last_lap_time": 90}]})
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Imola"})
    await main.handle_message(agent, "lmu", "scoring", {"session_type": "Race", "session_time": 100,
        "player_vehicle_id": 7, "vehicles": [{"id": 7, "driver_name": "Name"}]})
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM laps") == 1


async def test_sample_id_migration_preserves_rows(db):
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("DROP TABLE strategy_samples")
        await conn.execute("CREATE TABLE strategy_samples(id bigserial PRIMARY KEY,fuel_l double precision)")
        await conn.execute("INSERT INTO strategy_samples(fuel_l) VALUES(42),(41)")
        await migrate_sample_ids(conn)
        await migrate_sample_ids(conn)
        rows = await conn.fetch("SELECT * FROM strategy_samples ORDER BY fuel_l")
        assert [r["fuel_l"] for r in rows] == [41, 42]
        assert all(r["id"].version == 7 for r in rows)


async def test_split_races_are_distinct_and_provider_scoped(db):
    async with db.acquire() as conn:
        event = uuid7()
        await conn.execute("INSERT INTO events(id,simulator_provider,external_event_id) VALUES($1,'example','event-1')", event)
        for split in (1, 2):
            await conn.execute("INSERT INTO races(id,event_id,simulator_provider,external_race_id,split_number) VALUES($1,$2,'example',$3,$4)",
                               uuid7(), event, f"race-{split}", split)
        assert await conn.fetchval("SELECT count(*) FROM races WHERE event_id=$1", event) == 2
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute("INSERT INTO races(id,simulator_provider,external_race_id) VALUES($1,'example','race-1')", uuid7())


async def test_invalid_telemetry_is_not_persisted(db):
    agent, session_id, _ = await scoring_session(db)
    main.runtime[str(agent["id"])]["telemetry_valid"] = False
    await main.store_strategy_sample(agent, session_id, {"fuel": 42})
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM strategy_samples") == 0


async def test_live_cache_updates_without_writing_every_scoring_packet(db):
    agent, session_id, scoring = await scoring_session(db)
    changed = {**scoring, "session_time": 100.1,
               "vehicles": [{**scoring["vehicles"][0], "position": 5}, scoring["vehicles"][1]]}
    await main.handle_message(agent, "lmu", "scoring", changed)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT position FROM session_participants WHERE local_vehicle_id=7") == 1
    assert (await main.get_live_rows())[0]["position"] == 5
    live = await main.api_driver_live(agent["slug"])
    assert next(p for p in live["participants"] if p["is_stintara_driver"])["position"] == 5


async def test_normalized_sentinels_are_null_in_storage_and_entry_api(db):
    agent, session_id, scoring = await scoring_session(db)
    scoring["vehicles"][0].update(car_name="Example Entry #7", vehicle_class="GT3", team_name="Example Team")
    await main.upsert_participants(agent, session_id, scoring)
    _, payload = normalize({"type": "TelemetryUpdate", "fuel": 42, "tires": [
        {"wear": .987, "pressure": 0, "temp_inner": -273.15, "temp_mid": -273.15, "temp_outer": -273.15}]})
    await main.store_strategy_sample(agent, session_id, payload)
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM strategy_samples")
        assert row["tyre_pressure_fl_kpa"] is None and row["tyre_temp_inner_fl_c"] is None
        assert row["tyre_remaining_fl"] == .987 and row["fuel_l"] == 42
    data = await main.api_driver_live(agent["slug"])
    participant = next(p for p in data["participants"] if p["is_stintara_driver"])
    assert participant["entry_name"] == participant["car_name"] == "Example Entry #7"
    assert participant["vehicle_class"] == "GT3" and participant["team_name"] == "Example Team"
    assert participant["vehicle_model"] is None and participant["manufacturer"] is None
