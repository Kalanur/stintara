"""#46: an idle post-race provider state must not become a second Race session.

Messages are delivered in the order observed in production: LMU first reports SessionInfo of its empty
state (5 s placeholder duration), only then the reset scoring (clock 0, every vehicle at 0 laps).
"""
import pytest

from server.app import main
from tests.conftest import make_agent
from tests.test_convergence import roster, scoring

RACE = dict(minutes=82.6, laps=0)
PLACEHOLDER = dict(minutes=5 / 60, laps=0)


async def deliver(agent, payload, minutes=82.6, laps=0, track="Synthetic Circuit"):
    # Production persists the clock at 1 Hz; make every synthetic frame count.
    main.runtime.get(str(agent["id"]), {}).pop("last_persisted_scoring", None)
    info = {"track_name": track, "session_minutes": minutes, "session_laps": laps}
    await main.handle_message(agent, "lmu", "session_info", info)
    await main.handle_message(agent, "lmu", "scoring", payload)
    return main.runtime[str(agent["id"])]["session_id"]


def idle(clock=0.0):
    """Provider's empty post-race state: clock at zero, nobody has completed a lap."""
    return scoring([dict(v, total_laps=0) for v in roster()], clock=clock, kind="Race")


def running(clock, laps=39):
    return scoring([dict(v, total_laps=laps) for v in roster()], clock=clock, kind="Race")


async def start_race(agent, **limits):
    return await deliver(agent, running(100, laps=1), **(limits or RACE))


async def finish_race(agent, sid, clock=5331.6, driven=39, **limits):
    assert await deliver(agent, running(clock, laps=driven), **(limits or RACE)) == sid


async def sessions(db):
    async with db.acquire() as conn:
        return await conn.fetch("SELECT id,session_type,status,ended_at,session_minutes,session_laps "
                                "FROM sessions ORDER BY id")


async def row(db, sid):
    async with db.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM sessions WHERE id=$1", sid)


async def test_production_order_creates_no_tail_and_keeps_limits_and_standings(db):
    agent = await make_agent(db)
    sid = await start_race(agent)
    await finish_race(agent, sid)
    for _ in range(5):  # LMU keeps reporting the empty state until the player leaves
        assert await deliver(agent, idle(), **PLACEHOLDER) is None
    rows = await sessions(db)
    assert [r["id"] for r in rows] == [sid]
    assert rows[0]["status"] == "complete" and rows[0]["ended_at"] is not None
    assert rows[0]["session_minutes"] == 82.6  # the placeholder was never written to the finished race
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT max(total_laps) FROM session_participants WHERE session_id=$1", sid) == 39
    assert main.runtime[str(agent["id"])].get("session_id") is None
    # Runtime context stays current: the placeholder is what the provider reports now.
    assert main.runtime[str(agent["id"])]["session_minutes"] == pytest.approx(5 / 60)


async def test_race_completion_time_is_the_moment_of_the_idle_reset(db):
    agent = await make_agent(db)
    sid = await start_race(agent)
    await finish_race(agent, sid)
    last_seen = (await row(db, sid))["last_seen_at"]
    assert await deliver(agent, idle(), **PLACEHOLDER) is None
    assert await deliver(agent, idle(), **PLACEHOLDER) is None
    after = await row(db, sid)
    assert after["status"] == "complete" and after["ended_at"] >= last_seen
    assert after["last_seen_at"] == last_seen  # the idle frames never touch the finished session


async def test_mid_race_reset_with_placeholder_is_a_new_session_not_post_finish(db):
    agent = await make_agent(db)
    sid = await start_race(agent)
    await finish_race(agent, sid, clock=900.0, driven=8)  # 900 s of 4956 s: nowhere near the limit
    new = await deliver(agent, idle(), **PLACEHOLDER)
    assert new not in (None, sid)
    rows = {r["id"]: r for r in await sessions(db)}
    assert len(rows) == 2 and rows[sid]["status"] == "complete"
    assert rows[sid]["session_minutes"] == 82.6  # old limits stay with the old session
    assert rows[new]["session_minutes"] == pytest.approx(5 / 60)  # the new session takes the current SessionInfo


async def test_laps_limited_restart_with_placeholder_laps_is_not_post_finish(db):
    agent = await make_agent(db)
    limits = dict(minutes=0, laps=20)
    sid = await start_race(agent, **limits)
    await finish_race(agent, sid, clock=900.0, driven=8, **limits)
    new = await deliver(agent, idle(), minutes=0, laps=1)
    assert new not in (None, sid)
    assert len(await sessions(db)) == 2 and (await row(db, sid))["session_laps"] == 20


async def test_laps_limited_finish_is_post_finish_idle(db):
    agent = await make_agent(db)
    limits = dict(minutes=0, laps=20)
    sid = await start_race(agent, **limits)
    await finish_race(agent, sid, clock=1500.0, driven=20, **limits)
    assert await deliver(agent, idle(), minutes=0, laps=0) is None
    rows = await sessions(db)
    assert len(rows) == 1 and rows[0]["session_laps"] == 20 and rows[0]["status"] == "complete"


async def test_unknown_session_limits_remain_unknown_after_creation(db):
    agent = await make_agent(db)
    sid = await start_race(agent, minutes=0, laps=0)  # no positive limit known yet
    assert await deliver(agent, running(300.0, laps=3), minutes=90.0, laps=0) == sid
    assert (await row(db, sid))["session_minutes"] in (None, 0)  # deliberately conservative: no session identity
    assert main.runtime[str(agent["id"])]["session_minutes"] == 90.0
    assert main.runtime[str(agent["id"])].get("session_meta_pending") is None  # consumed by that resolution


async def test_known_limit_is_stable_but_runtime_follows_session_info(db):
    agent = await make_agent(db)
    sid = await start_race(agent)
    assert await deliver(agent, running(300.0, laps=3), minutes=90.0, laps=0) == sid
    assert (await row(db, sid))["session_minutes"] == 82.6  # limits are immutable after INSERT
    assert main.runtime[str(agent["id"])]["session_minutes"] == 90.0


async def interleave(db, clock, driven, limits, info, reset_info=None):
    """A runs; a SessionInfo of the next phase arrives; one more old frame of A; then the reset scoring."""
    agent = await make_agent(db)
    sid = await start_race(agent, **limits)
    await finish_race(agent, sid, clock=clock, driven=driven, **limits)
    assert await deliver(agent, running(clock + 0.1, laps=driven), **info) == sid  # resolves to A, carries next info
    return agent, sid, await deliver(agent, idle(), **(reset_info or info))


async def test_interleaving_mid_race_keeps_limit_and_separates(db):
    agent, sid, new = await interleave(db, 900.0, 8, RACE, PLACEHOLDER)
    assert new not in (None, sid)
    rows = {r["id"]: r for r in await sessions(db)}
    assert len(rows) == 2 and rows[sid]["session_minutes"] == 82.6 and rows[sid]["status"] == "complete"
    assert rows[new]["session_minutes"] == pytest.approx(5 / 60)  # the new session takes the current SessionInfo


async def test_interleaving_finished_race_stays_one_session(db):
    agent, sid, new = await interleave(db, 5331.6, 39, RACE, PLACEHOLDER)
    assert new is None
    rows = await sessions(db)
    assert len(rows) == 1 and rows[0]["session_minutes"] == 82.6 and rows[0]["status"] == "complete"
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT max(total_laps) FROM session_participants WHERE session_id=$1", sid) == 39


LAPS = dict(minutes=0, laps=20)


@pytest.mark.parametrize("info", [dict(minutes=0, laps=1), dict(minutes=5 / 60, laps=0)])
async def test_interleaving_lap_limit_mid_race(db, info):  # second case: lap race + time placeholder (cross-dimension)
    agent, sid, new = await interleave(db, 900.0, 8, LAPS, info)
    assert new not in (None, sid)
    rows = {r["id"]: r for r in await sessions(db)}
    assert len(rows) == 2 and (rows[sid]["session_laps"], rows[sid]["session_minutes"]) == (20, 0)
    assert rows[new]["session_minutes"] == pytest.approx(info["minutes"])  # the new session takes the current info


@pytest.mark.parametrize("info", [dict(minutes=0, laps=0), dict(minutes=0, laps=1), dict(minutes=5 / 60, laps=0)])
async def test_interleaving_lap_limit_finished(db, info):
    agent, sid, new = await interleave(db, 1500.0, 20, LAPS, info)
    assert new is None
    rows = await sessions(db)
    assert len(rows) == 1 and rows[0]["status"] == "complete"
    assert (rows[0]["session_laps"], rows[0]["session_minutes"]) == (20, 0)


@pytest.mark.parametrize("info", [dict(minutes=82.6, laps=1), dict(minutes=0, laps=1)])
async def test_interleaving_time_limit_mid_race_with_lap_placeholder(db, info):
    agent, sid, new = await interleave(db, 900.0, 8, RACE, info)
    assert new not in (None, sid)
    rows = {r["id"]: r for r in await sessions(db)}
    assert len(rows) == 2 and (rows[sid]["session_minutes"], rows[sid]["session_laps"]) == (82.6, 0)
    assert rows[new]["session_laps"] == 1


@pytest.mark.parametrize("info", [dict(minutes=82.6, laps=1), dict(minutes=0, laps=1)])
async def test_interleaving_time_limit_finished_with_lap_placeholder(db, info):
    agent, sid, new = await interleave(db, 5331.6, 39, RACE, info)
    assert new is None
    rows = await sessions(db)
    assert len(rows) == 1 and rows[0]["status"] == "complete"
    assert (rows[0]["session_minutes"], rows[0]["session_laps"]) == (82.6, 0)


async def test_session_info_alone_does_not_touch_the_persisted_row(db):
    agent = await make_agent(db)
    sid = await start_race(agent)
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Synthetic Circuit", "session_minutes": 7})
    assert main.runtime[str(agent["id"])]["session_minutes"] == 7  # runtime context is immediate
    assert (await row(db, sid))["session_minutes"] == 82.6  # persistence waits for scoring to confirm the session


async def test_genuine_new_race_after_idle_reset_takes_its_own_session_info(db):
    agent = await make_agent(db)
    sid = await start_race(agent)
    await finish_race(agent, sid)
    assert await deliver(agent, idle(), **PLACEHOLDER) is None
    new = await deliver(agent, idle(clock=5.0), **RACE)
    assert new and new != sid
    assert await deliver(agent, running(30, laps=1), **RACE) == new
    rows = {r["id"]: r for r in await sessions(db)}
    assert len(rows) == 2 and rows[sid]["session_minutes"] == rows[new]["session_minutes"] == 82.6


@pytest.mark.parametrize("case", ["idle_not_all_zero", "wrong_session_type", "other_track"])
async def test_real_resets_still_separate_sessions(db, case):
    agent = await make_agent(db)
    sid = await start_race(agent)
    await finish_race(agent, sid)
    if case == "idle_not_all_zero":
        partial = [dict(v, total_laps=0) for v in roster()]
        partial[0]["total_laps"] = 1
        new = await deliver(agent, scoring(partial, clock=0.0, kind="Race"), **PLACEHOLDER)
    elif case == "wrong_session_type":  # Race -> Qualifying is a real transition, not an idle Race state
        new = await deliver(agent, scoring([dict(v, total_laps=0) for v in roster()], clock=0.0, kind="Qualifying"),
                            **PLACEHOLDER)
    else:
        new = await deliver(agent, idle(), track="Other Circuit", **PLACEHOLDER)
    assert new not in (None, sid)
    assert len(await sessions(db)) == 2 and (await row(db, sid))["session_minutes"] == 82.6


async def test_server_restart_in_idle_state_does_not_resurrect_or_merge(db):
    agent = await make_agent(db)
    sid = await start_race(agent)
    await finish_race(agent, sid)
    assert await deliver(agent, idle(), **PLACEHOLDER) is None
    main.runtime.clear()  # restart: suppression is runtime-only, A stays complete and untouched
    assert await deliver(agent, idle(), **PLACEHOLDER) not in (None, sid)
    rows = await sessions(db)
    assert len(rows) == 2 and {r["status"] for r in rows if r["id"] == sid} == {"complete"}
    assert (await row(db, sid))["session_minutes"] == 82.6


async def test_two_observers_idle_reset_by_one_keeps_shared_race_live_for_other(db):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    sid = await start_race(a)
    assert await deliver(b, running(100, laps=1), **RACE) == sid
    await finish_race(a, sid)
    assert await deliver(b, running(5331.6), **RACE) == sid
    assert await deliver(a, idle(), **PLACEHOLDER) is None
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM sessions WHERE id=$1", sid) == "live"
        assert await conn.fetchval(
            "SELECT count(*) FROM session_observations WHERE session_id=$1 AND ended_at IS NULL", sid) == 1
    assert len(await sessions(db)) == 1 and (await row(db, sid))["session_minutes"] == 82.6
