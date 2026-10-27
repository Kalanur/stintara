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
    assert (p["pitstops"], p["finish_status"], p["has_telemetry"]) == (1, finish, True)
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
