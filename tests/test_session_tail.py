"""#46: an idle post-race provider state must not become a second Race session."""
import pytest

from server.app import main
from tests.conftest import make_agent
from tests.test_convergence import roster, scoring

MINUTES = {}  # per-test configured duration; the real SessionInfo repeats it every second


async def deliver(agent, payload, track="Synthetic Circuit"):
    # Production persists the clock at 1 Hz; make every synthetic frame count.
    main.runtime.get(str(agent["id"]), {}).pop("last_persisted_scoring", None)
    info = {"track_name": track, "session_minutes": MINUTES.get("minutes", 82.6), "session_laps": MINUTES.get("laps", 0)}
    await main.handle_message(agent, "lmu", "session_info", info)
    await main.handle_message(agent, "lmu", "scoring", payload)
    return main.runtime[str(agent["id"])]["session_id"]


def idle(vehicles=None, clock=0.0):
    """Provider's empty post-race state: clock at zero, nobody has completed a lap."""
    return scoring([dict(v, total_laps=0) for v in (vehicles or roster())], clock=clock, kind="Race")


def running(clock, laps=39):
    return scoring([dict(v, total_laps=laps) for v in roster()], clock=clock, kind="Race")


async def start_race(agent):
    return await deliver(agent, running(100, laps=1))


async def finish_race(agent, sid, clock=5331.6):
    assert await deliver(agent, running(clock)) == sid


@pytest.fixture(autouse=True)
def reset_minutes():
    MINUTES.clear()


async def sessions(db):
    async with db.acquire() as conn:
        return await conn.fetch("SELECT id,session_type,status,ended_at FROM sessions ORDER BY id")


async def test_idle_post_race_state_does_not_create_second_session(db):
    agent = await make_agent(db)
    sid = await start_race(agent)
    await finish_race(agent, sid)
    for _ in range(5):  # LMU keeps reporting the empty state until the player leaves
        assert await deliver(agent, idle()) is None
    rows = await sessions(db)
    assert [r["id"] for r in rows] == [sid]
    assert rows[0]["status"] == "complete" and rows[0]["ended_at"] is not None
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT max(total_laps) FROM session_participants WHERE session_id=$1", sid) == 39
    assert main.runtime[str(agent["id"])].get("session_id") is None


async def test_race_completion_time_is_the_moment_of_the_idle_reset(db):
    agent = await make_agent(db)
    sid = await start_race(agent)
    await finish_race(agent, sid)
    async with db.acquire() as conn:
        last_seen = await conn.fetchval("SELECT last_seen_at FROM sessions WHERE id=$1", sid)
    assert await deliver(agent, idle()) is None
    assert await deliver(agent, idle()) is None
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT status,ended_at,last_seen_at FROM sessions WHERE id=$1", sid)
    assert row["status"] == "complete" and row["ended_at"] >= last_seen
    assert row["last_seen_at"] == last_seen  # the idle frames never touch the finished session


async def test_genuine_new_race_after_idle_reset_is_a_new_session(db):
    agent = await make_agent(db)
    sid = await start_race(agent)
    await finish_race(agent, sid)
    assert await deliver(agent, idle()) is None
    new = await deliver(agent, idle(clock=5.0))
    assert new and new != sid
    assert await deliver(agent, running(30, laps=1)) == new
    assert len(await sessions(db)) == 2


async def test_laps_limited_race_finishing_by_laps_is_recognised(db):
    agent = await make_agent(db)
    MINUTES.update(minutes=0, laps=20)
    sid = await deliver(agent, running(100, laps=1))
    assert await deliver(agent, running(1500, laps=20)) == sid
    assert await deliver(agent, idle()) is None
    assert len(await sessions(db)) == 1


@pytest.mark.parametrize("case", ["mid_race_restart", "idle_not_all_zero", "wrong_session_type", "other_track"])
async def test_real_resets_still_separate_sessions(db, case):
    agent = await make_agent(db)
    sid = await start_race(agent)
    if case == "mid_race_restart":  # clock was nowhere near the configured duration
        assert await deliver(agent, running(900, laps=8)) == sid
        assert await deliver(agent, idle()) not in (None, sid)
    elif case == "idle_not_all_zero":
        await finish_race(agent, sid)
        partial = [dict(v, total_laps=0) for v in roster()]
        partial[0]["total_laps"] = 1
        assert await deliver(agent, scoring(partial, clock=0.0, kind="Race")) not in (None, sid)
    elif case == "wrong_session_type":  # Race -> Qualifying is a real transition, not an idle Race state
        await finish_race(agent, sid)
        assert await deliver(agent, scoring([dict(v, total_laps=0) for v in roster()], clock=0.0, kind="Qualifying")) not in (None, sid)
    else:
        await finish_race(agent, sid)
        assert await deliver(agent, idle(), track="Other Circuit") not in (None, sid)
    assert len(await sessions(db)) == 2


async def test_server_restart_in_idle_state_does_not_resurrect_or_merge(db):
    agent = await make_agent(db)
    sid = await start_race(agent)
    await finish_race(agent, sid)
    assert await deliver(agent, idle()) is None
    main.runtime.clear()  # restart: suppression is runtime-only, A stays complete and untouched
    assert await deliver(agent, idle()) not in (None, sid)
    rows = await sessions(db)
    assert len(rows) == 2 and {r["status"] for r in rows if r["id"] == sid} == {"complete"}


async def test_two_observers_idle_reset_by_one_keeps_shared_race_live_for_other(db):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    sid = await start_race(a)
    assert await deliver(b, running(100, laps=1)) == sid
    await finish_race(a, sid)
    assert await deliver(b, running(5331.6)) == sid
    assert await deliver(a, idle()) is None
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM sessions WHERE id=$1", sid) == "live"
        assert await conn.fetchval("SELECT count(*) FROM session_observations WHERE session_id=$1 AND ended_at IS NULL", sid) == 1
    assert len(await sessions(db)) == 1
