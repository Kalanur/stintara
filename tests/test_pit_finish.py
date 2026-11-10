"""Synthetic accepted-snapshot and persisted-counter evidence only."""
import pytest

from server.app import main
from server.app.protocol import LapSnapshots
from tests.conftest import make_agent


async def setup_field(db):
    agent = await make_agent(db, "synthetic-engineer")
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Synthetic Circuit"})
    scoring = dict(session_type="Race", session_time=100, player_vehicle_id=7,
                   vehicles=[dict(id=7, driver_name="Synthetic Driver", position=1, total_laps=28,
                                  in_pits=False, time_behind_leader=0, laps_behind_leader=0)])
    await main.handle_message(agent, "lmu", "scoring", scoring)
    return agent, main.runtime[str(agent["id"])]["session_id"], scoring


async def snapshot(agent, lap=10, stops=0, finish=0, lap_time=90, **context):
    payload = LapSnapshots.model_validate(dict(session_type="Race", session_time=100, drivers=[dict(
        id=7, total_laps=lap, num_pitstops=stops, finish_status=finish, last_lap_time=lap_time,
        has_telemetry=True, in_pits=True, position=9)], **context)).model_dump()
    await main.handle_message(agent, "lmu", "lap_snapshots", payload)


@pytest.mark.parametrize("finish", [0, 1, 2, 3, 99])
@pytest.mark.parametrize("lap", [0, 28])
async def test_snapshot_state_without_valid_lap_and_scoring_remains_authoritative(db, finish, lap):
    agent, sid, _ = await setup_field(db)
    await snapshot(agent, lap=lap, lap_time=None, stops=1, finish=finish)
    data = await main.api_session_live(sid)
    p = data["participants"][0]
    # has_telemetry means trusted own-agent sensor data was accepted; this snapshot carries none (#59).
    assert (p["pitstops"], p["finish_status"], p["has_telemetry"]) == (1, finish, False)
    assert p["last_pit_lap"] is None
    assert p["in_pits"] is False and p["position"] == 1
    assert p["gap_to_leader"] == 0
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM laps") == 0
        stored = await conn.fetchrow("SELECT in_pits,position FROM session_participants")
        assert not stored["in_pits"] and stored["position"] == 1
        await conn.execute("UPDATE session_observations SET last_seen_at=now()-interval '16 seconds'")
    stale = await main.api_session_live(sid)
    assert not stale["live"] and stale["participants"][0]["finish_status"] == finish


async def test_counter_monotonic_and_duplicate_lap_does_not_block_finish_update(db):
    agent, sid, _ = await setup_field(db)
    await snapshot(agent, stops=2)
    await snapshot(agent, stops=2, finish=1)  # Same completed lap, no new INSERT.
    assert (await main.api_session_live(sid))["participants"][0]["finish_status"] == 1
    await snapshot(agent, lap=9, stops=1, finish=1)
    await snapshot(agent, lap=10, stops=-1, finish=1)
    p = (await main.api_session_live(sid))["participants"][0]
    assert p["pitstops"] == 2
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM laps WHERE lap_number=10") == 1


async def test_proven_transitions_and_cached_counter_dips(db):
    agent, sid, _ = await setup_field(db)
    for lap, stops, expected in [(10,0,None),(11,0,None),(12,1,12),(13,1,12),
                                 (14,0,12),(15,1,12),(27,2,27),(28,2,27)]:
        await snapshot(agent, lap, stops)
        p = (await main.api_session_live(sid))["participants"][0]
        assert p["last_pit_lap"] == expected
    assert p["pitstops"] == 2


async def test_join_with_positive_counter_requires_a_later_proven_increase(db):
    agent, sid, _ = await setup_field(db)
    for lap in (10,11):
        await snapshot(agent, lap, 1)
        assert (await main.api_session_live(sid))["participants"][0]["last_pit_lap"] is None
    await snapshot(agent, 13, 2)
    assert (await main.api_session_live(sid))["participants"][0]["last_pit_lap"] == 13


@pytest.mark.parametrize("case", ["clock", "type", "disconnect"])
async def test_rejected_snapshots_cannot_update_participant_state(db, case):
    agent, sid, _ = await setup_field(db)
    payload = dict(session_type="Race", session_time=100, drivers=[dict(
        id=7, total_laps=0, num_pitstops=3, finish_status=3, has_telemetry=True)])
    if case == "clock":
        payload["session_time"] = 80
    elif case == "type":
        payload["session_type"] = "Qualifying"
    else:
        await main.handle_message(agent, "lmu", "connection", dict(game_connected=False,telemetry_valid=False))
    await main.handle_message(agent, "lmu", "lap_snapshots", payload)
    p = (await main.api_session_live(sid))["participants"][0]
    assert p["pitstops"] == 0 and p["finish_status"] == 0 and not p["has_telemetry"]


async def test_new_session_starts_with_independent_pit_finish_state(db):
    agent, sid, scoring = await setup_field(db)
    await snapshot(agent, stops=2, finish=3)
    await main.handle_message(agent, "lmu", "scoring", {**scoring, "session_time": 0})
    new_sid = main.runtime[str(agent["id"])]["session_id"]
    assert new_sid != sid
    await snapshot(agent, stops=2, finish=3)  # Old session clock must be rejected.
    p = (await main.api_session_live(new_sid))["participants"][0]
    assert p["pitstops"] == 0 and p["finish_status"] == 0 and p["last_pit_lap"] is None


async def test_full_field_sectors_tyre_and_damage_live_and_from_checkpoint(db):
    agent, sid, scoring = await setup_field(db)
    wheels = [dict(remaining=r, temp_mid=85.0) for r in (0.9, 0.7, 0.8, 0.85)]  # LMU mWear: tyre remaining, 1.0 = new
    # The agent watched the car start this lap (27 -> 28), so its checkpoint is a crossing snapshot (#59).
    for laps in (27, 28):
        await main.handle_message(agent, "lmu", "scoring", {**scoring, "session_time": 100 + laps - 27,
            "vehicles": [{**scoring["vehicles"][0], "total_laps": laps}]}, "pitwall-connection-1")
    payload = LapSnapshots.model_validate(dict(session_type="Race", session_time=100, drivers=[dict(
        id=7, total_laps=28, last_lap_time=122.9, last_sector1=36.9, last_sector2=50.6, last_sector3=35.4,
        has_telemetry=True, tire_compound_front_name="Medium", wheels=wheels,
        damage={"dent_severity": [0, 1, 0, 0, 1, 0, 0, 0]})])).model_dump()
    await main.handle_message(agent, "lmu", "lap_snapshots", payload, "pitwall-connection-1")
    p = (await main.api_session_live(sid))["participants"][0]
    assert (p["sector1"], p["sector2"], p["sector3"]) == (36.9, 50.6, 35.4)
    assert (p["tyre_compound"], p["tyre_remaining_min"], p["damage_regions"], p["damage_max"]) == ("Medium", 0.7, 2, 1)
    # Without a fresh snapshot the persisted completed-lap checkpoint gives the same view.
    main.runtime[str(agent["id"])].pop("snapshots")
    p = (await main.api_session_live(sid))["participants"][0]
    assert (p["tyre_compound"], p["tyre_remaining_min"], p["damage_regions"]) == ("Medium", 0.7, 2)
    assert (p["sector1"], p["sector3"]) == (36.9, 35.4)


async def test_full_field_unknown_sectors_and_no_telemetry_stay_unknown(db):
    agent, sid, _ = await setup_field(db)
    await snapshot(agent, lap=11, lap_time=90)
    p = (await main.api_session_live(sid))["participants"][0]
    assert p["sector1"] is p["sector2"] is p["sector3"] is None
    assert p["tyre_compound"] is None and p["tyre_remaining_min"] is None


async def test_full_field_tyre_remaining_unknown_for_placeholder_telemetry(db):
    from agent.pitwall import wheel
    agent, sid, _ = await setup_field(db)
    # Pitwall placeholder for cars without real tyre telemetry: wear 1.0, sentinel temperatures.
    placeholder = dict(wear=1.0, surface_temp=[-273.15] * 3, carcass_temp=-273.15, pressure=0.0)
    payload = LapSnapshots.model_validate(dict(session_type="Race", session_time=100, drivers=[dict(
        id=7, total_laps=28, last_lap_time=122.9, has_telemetry=True, tire_compound_front_name="Medium",
        wheels=[dict(remaining=1.0)] * 4, damage={"dent_severity": [0] * 8})])).model_dump()
    payload["drivers"][0]["wheels"] = [wheel(dict(placeholder), True) for _ in range(4)]
    await main.handle_message(agent, "lmu", "lap_snapshots", payload)
    p = (await main.api_session_live(sid))["participants"][0]
    assert p["tyre_compound"] == "Medium" and p["tyre_remaining_min"] is None and p["damage_regions"] == 0
