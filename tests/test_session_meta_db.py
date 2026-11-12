"""#20 end to end (PostgreSQL): session limits follow the confirmed session, never the previous one.

Synthetic data only. Message order and values follow a real LMU Qualifying -> Race change: the Race scoring frame
arrives first, the Race SessionInfo about 0.8 s later, and the pre-race countdown (1.07 / 0 / 2.23 minutes) only
becomes the real end (32.23 minutes) when the race goes green.
"""
import pytest

from server.app import main
from server.app.uuid7 import uuid7
from tests.conftest import make_agent
from tests.test_convergence import roster, scoring

TRACK = "Synthetic Circuit"
QUALI_END = 10.5 * 60
RACE_END = 32.2333333 * 60


def frame(clock, kind="Race", laps=0):
    return scoring([dict(v, total_laps=laps) for v in roster()], clock=clock, kind=kind)


async def send(agent, clock, *, kind="Race", laps=0, minutes=None, session_laps=0, phase=None, info_first=True):
    """One scoring frame, optionally with a SessionInfo before it (or after it for the delayed order)."""
    key = str(agent["id"])
    main.runtime.get(key, {}).pop("last_persisted_scoring", None)  # production persists at 1 Hz
    if phase:
        await main.handle_message(agent, "lmu", "race_control", {"phase": phase})
    info = {"track_name": TRACK, "session_minutes": minutes, "session_laps": session_laps}
    if info_first:
        await main.handle_message(agent, "lmu", "session_info", info)
    await main.handle_message(agent, "lmu", "scoring", frame(clock, kind, laps))
    if not info_first:
        await main.handle_message(agent, "lmu", "session_info", info)
    return main.runtime[key].get("session_id")


async def limits(db, sid):
    async with db.acquire() as conn:
        return await conn.fetchrow(
            "SELECT session_type,session_end_et_seconds,session_duration_seconds,session_laps,session_minutes,status "
            "FROM sessions WHERE id=$1", sid)


async def qualifying(agent):
    sid = await send(agent, 300.0, kind="Qualifying", minutes=10.5, phase="green")
    for i in range(1, 4):
        await send(agent, 300.0 + i, kind="Qualifying", minutes=10.5, phase="green")
    return sid


async def race_from_qualifying(agent, *, bind=True):
    """The observed change: stale 10.5, then Race scoring, then the countdown, then green."""
    quali = await qualifying(agent)
    race = await send(agent, 0.0, minutes=10.5, phase="pre_session")  # stale value, new session
    assert race and race != quali
    # Delayed order: scoring first, the Race SessionInfo (1.07) after it.
    await send(agent, 2.8, minutes=1.0666667, phase="pre_session", info_first=False)
    await send(agent, 67.0, minutes=0.0, phase="formation")
    await send(agent, 134.4, minutes=2.2333333, phase="formation")
    if bind:
        for clock in (135.6, 136.6, 137.6, 138.6):
            await send(agent, clock, minutes=32.2333333, phase="green")
    return quali, race


async def test_race_never_inherits_the_qualifying_end_and_derives_its_own(db):
    agent = await make_agent(db)
    quali, race = await race_from_qualifying(agent, bind=False)
    assert (await limits(db, quali))["session_end_et_seconds"] == pytest.approx(QUALI_END)
    before = await limits(db, race)  # countdown values and the stale 10.5 are all behind us: nothing bound yet
    assert before["session_end_et_seconds"] is None and before["session_duration_seconds"] is None
    for clock in (135.6, 136.6, 137.6, 138.6):
        await send(agent, clock, minutes=32.2333333, phase="green")
    after = await limits(db, race)
    assert after["session_end_et_seconds"] == pytest.approx(RACE_END)
    assert after["session_duration_seconds"] == pytest.approx(1800.0)  # the 30 minute race, derived at green
    assert (await limits(db, quali))["session_end_et_seconds"] == pytest.approx(QUALI_END)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM sessions") == 2
        assert await conn.fetchval("SELECT count(*) FROM sessions WHERE session_end_et_seconds=$1", QUALI_END) == 1
    assert (await limits(db, race))["session_minutes"] is None  # the legacy column is no longer written


async def test_stale_pre_race_reference_binds_the_end_but_leaves_the_duration_unknown(db):
    """Codex P2 (#65): last countdown at clock 64, race green at 134, end 1934 must not store 1870 s."""
    agent = await make_agent(db)
    await qualifying(agent)
    race = await send(agent, 0.0, minutes=10.5, phase="pre_session")
    await send(agent, 64.0, minutes=64 / 60, phase="formation")
    await send(agent, 130.0, phase="formation")  # later pre-race metadata is missing
    for clock in (134.0, 135.0, 136.0, 137.0):
        await send(agent, clock, minutes=32.2333333, phase="green")
    bound = await limits(db, race)
    assert bound["session_end_et_seconds"] == pytest.approx(RACE_END)
    assert bound["session_duration_seconds"] is None


async def test_api_timing_separates_configured_duration_from_remaining_time(db):
    agent = await make_agent(db)
    _, race = await race_from_qualifying(agent)
    timing = (await main.api_session_live(race))["timing"]
    assert timing["end_et_seconds"] == pytest.approx(RACE_END)
    assert timing["configured_duration_seconds"] == pytest.approx(1800.0)
    assert timing["elapsed_seconds"] == 138.6
    assert timing["remaining_seconds"] == pytest.approx(RACE_END - 138.6)
    assert "configured_minutes" not in timing


async def test_mid_race_reset_is_not_post_finish_with_the_bound_end(db):
    """With the stale qualifying value (630 s) a reset at clock 800 would have looked like a finished race (#46)."""
    agent = await make_agent(db)
    _, race = await race_from_qualifying(agent)
    assert await send(agent, 800.0, laps=12, minutes=32.2333333, phase="green") == race
    new = await send(agent, 0.0, minutes=0.0833333, phase="pre_session")  # idle, empty state
    assert new not in (None, race)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM sessions WHERE session_type='Race'") == 2


async def test_race_that_ran_to_the_bound_end_is_post_finish_idle(db):
    agent = await make_agent(db)
    _, race = await race_from_qualifying(agent)
    assert await send(agent, 1950.0, laps=36, minutes=32.2333333, phase="session_over") == race
    assert await send(agent, 0.0, minutes=0.0833333, phase="pre_session") is None  # no second Race row
    assert await send(agent, 0.0, minutes=0.0833333, phase="pre_session") is None
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM sessions WHERE session_type='Race'") == 1
    assert (await limits(db, race))["status"] == "complete"


async def test_unknown_end_never_claims_that_the_race_is_over(db):
    agent = await make_agent(db)
    race = await send(agent, 300.0, minutes=0.0, phase="green")
    for i in range(1, 4):
        await send(agent, 300.0 + i, minutes=0.0, phase="green")
    assert (await limits(db, race))["session_end_et_seconds"] is None
    assert await send(agent, 5000.0, laps=60, minutes=0.0, phase="green") == race
    new = await send(agent, 0.0, minutes=0.0833333, phase="pre_session")
    assert new not in (None, race)  # conservative: a new session, not a suppressed one
    timing = (await main.api_session_live(race))["timing"]
    assert timing["end_et_seconds"] is None and timing["remaining_seconds"] is None
    assert timing["configured_duration_seconds"] is None


async def test_no_green_phase_means_no_end(db):
    agent = await make_agent(db)
    race = await send(agent, 300.0, minutes=32.2333333)  # race control never reported
    for i in range(1, 4):
        await send(agent, 300.0 + i, minutes=32.2333333)
    assert (await limits(db, race))["session_end_et_seconds"] is None


async def test_lap_limited_race_binds_laps_without_an_end(db):
    agent = await make_agent(db)
    race = await send(agent, 100.0, minutes=0.0, session_laps=20)
    for i in range(1, 4):
        await send(agent, 100.0 + i, minutes=0.0, session_laps=20)
    row = await limits(db, race)
    assert row["session_laps"] == 20 and row["session_end_et_seconds"] is None
    assert (await main.api_session_live(race))["timing"]["configured_laps"] == 20


async def test_second_observer_cannot_replace_a_known_end(db):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    _, race = await race_from_qualifying(a)
    assert await send(b, 140.0, laps=1, minutes=40.0, phase="green") == race
    for i in range(1, 4):
        await send(b, 140.0 + i, laps=1, minutes=40.0, phase="green")  # wrong claim, bracketed and repeated
    assert (await limits(db, race))["session_end_et_seconds"] == pytest.approx(RACE_END)  # write-once


async def test_reconnect_starts_cold_and_binds_again_only_after_confirmation(db):
    agent = await make_agent(db)
    await main.handle_message(agent, "lmu", "connection", {"game_connected": True, "telemetry_valid": True})
    race = await send(agent, 300.0, minutes=32.2333333, phase="green")
    for i in range(1, 4):
        await send(agent, 300.0 + i, minutes=32.2333333, phase="green")
    assert (await limits(db, race))["session_end_et_seconds"] == pytest.approx(RACE_END)
    await main.handle_message(agent, "lmu", "connection", {"game_connected": False, "telemetry_valid": True})
    await main.handle_message(agent, "lmu", "connection", {"game_connected": True, "telemetry_valid": True})
    new = await send(agent, 400.0, minutes=40.0, phase="green")  # cold start: no earlier resolved session
    assert new != race
    assert (await limits(db, new))["session_end_et_seconds"] is None
    await send(agent, 401.0, minutes=40.0, phase="green")
    assert (await limits(db, new))["session_end_et_seconds"] is None  # a single bracketed claim
    await send(agent, 402.0, minutes=40.0, phase="green")
    assert (await limits(db, new))["session_end_et_seconds"] == pytest.approx(2400.0)
    assert (await limits(db, race))["session_end_et_seconds"] == pytest.approx(RACE_END)


async def test_source_epoch_change_between_info_and_frame_does_not_bind(db):
    agent = await make_agent(db)
    key = str(agent["id"])
    await main.handle_message(agent, "lmu", "race_control", {"phase": "green"}, 1)
    await main.handle_message(agent, "lmu", "session_info", {"track_name": TRACK, "session_minutes": 32.2333333}, 1)
    await main.handle_message(agent, "lmu", "scoring", frame(300.0), 1)
    race = main.runtime[key]["session_id"]
    for clock in (301.0, 302.0):
        main.runtime[key].pop("last_persisted_scoring", None)
        await main.handle_message(agent, "lmu", "session_info", {"track_name": TRACK, "session_minutes": 32.2333333}, 1)
        await main.handle_message(agent, "lmu", "scoring", frame(clock), 2)  # Pitwall reconnected in between
    assert (await limits(db, race))["session_end_et_seconds"] is None


async def test_legacy_rows_keep_their_values_but_are_never_trusted(db):
    legacy = uuid7()
    async with db.acquire() as conn:
        await conn.execute("""INSERT INTO sessions(id,simulator_provider,session_type,track_name,session_minutes)
            VALUES($1,'lmu','Race',$2,10.5)""", legacy, TRACK)
        await conn.execute(main.SCHEMA_FILE.read_text())  # additive and idempotent, twice
        await conn.execute(main.SCHEMA_FILE.read_text())
    row = await limits(db, legacy)
    assert row["session_minutes"] == 10.5 and row["session_end_et_seconds"] is None  # no backfill, no guessing
    timing = (await main.api_session_live(legacy))["timing"]
    assert timing["end_et_seconds"] is None and timing["remaining_seconds"] is None
