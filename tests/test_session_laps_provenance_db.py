"""#20 follow-up (PostgreSQL): a lap limit counts only with provenance (``session_laps_confirmed``).

Rows written before the binding rule keep their stored value untouched but are unverified: a placeholder such as
``session_laps=999`` must neither block a confirmed end nor appear as a race length. merge(), session_timing() and
race_ran_its_course() share one trust rule (``session_meta.trusted_laps``). Synthetic data only.
"""
import time

from server.app import main, session_meta
from tests.conftest import make_agent
from tests.test_session_tail import END, deliver, idle, row, running, sessions, start_race

TRACK = "Synthetic Circuit"
UNKNOWN = dict(minutes=0, laps=0)


async def legacy_session(db, laps=999, **limits):
    """A live Race whose lap value predates the provenance rule: stored, but unverified."""
    agent = await make_agent(db)
    sid = await start_race(agent, **(limits or UNKNOWN))
    async with db.acquire() as conn:
        await conn.execute("UPDATE sessions SET session_laps=$2,session_laps_confirmed=false WHERE id=$1", sid, laps)
    return agent, sid


async def timing(sid):
    return (await main.api_session_live(sid))["timing"]


async def test_legacy_placeholder_never_appears_as_a_race_length(db):
    agent, sid = await legacy_session(db)
    stored = await row(db, sid)
    assert stored["session_laps"] == 999 and stored["session_laps_confirmed"] is False  # value untouched
    assert (await timing(sid))["configured_laps"] is None


async def test_confirmed_end_binds_despite_a_legacy_placeholder_and_leaves_the_value_alone(db):
    agent, sid = await legacy_session(db)
    assert await deliver(agent, running(300.0, laps=3), minutes=82.6, laps=0) == sid
    bound = await row(db, sid)
    assert bound["session_end_et_seconds"] == END
    assert (bound["session_laps"], bound["session_laps_confirmed"]) == (999, False)  # history is not rewritten
    shown = await timing(sid)
    assert shown["end_et_seconds"] == END and shown["configured_laps"] is None


async def test_newly_confirmed_lap_limit_replaces_the_legacy_value(db):
    agent, sid = await legacy_session(db, laps=999)
    assert await deliver(agent, running(300.0, laps=3), minutes=0, laps=20) == sid
    bound = await row(db, sid)
    assert (bound["session_laps"], bound["session_laps_confirmed"], bound["session_end_et_seconds"]) == (20, True, None)
    assert (await timing(sid))["configured_laps"] == 20


async def test_new_lap_limited_race_is_confirmed_and_unchanged_in_behaviour(db):
    agent = await make_agent(db)
    limits = dict(minutes=0, laps=20)
    sid = await start_race(agent, **limits)
    bound = await row(db, sid)
    assert (bound["session_laps"], bound["session_laps_confirmed"]) == (20, True)
    assert (await timing(sid))["configured_laps"] == 20
    assert await deliver(agent, running(1500.0, laps=20), **limits) == sid
    assert await deliver(agent, idle(), minutes=0, laps=0) is None  # finished lap race: post-finish idle, no 2nd row
    rows = await sessions(db)
    assert len(rows) == 1 and rows[0]["status"] == "complete"


async def test_legacy_lap_value_does_not_make_a_reset_post_finish(db):
    """race_ran_its_course() must not judge with an unverified value: the reset is a new session."""
    agent, sid = await legacy_session(db, laps=5)
    assert await deliver(agent, running(900.0, laps=5), **UNKNOWN) == sid  # five laps "reached" the unverified value
    new = await deliver(agent, idle(), **UNKNOWN)
    assert new not in (None, sid)
    rows = {r["id"]: r for r in await sessions(db)}
    assert len(rows) == 2 and rows[sid]["session_laps"] == 5  # the old row keeps its value, it is just not trusted


async def test_the_same_reset_is_post_finish_when_the_lap_limit_is_confirmed(db):
    agent, sid = await legacy_session(db, laps=5)
    async with db.acquire() as conn:
        await conn.execute("UPDATE sessions SET session_laps_confirmed=true WHERE id=$1", sid)
    assert await deliver(agent, running(900.0, laps=5), **UNKNOWN) == sid
    assert await deliver(agent, idle(), **UNKNOWN) is None
    rows = await sessions(db)
    assert len(rows) == 1 and rows[0]["status"] == "complete"


async def test_upgrade_adds_the_flag_idempotently_and_never_rewrites_history(db):
    agent, sid = await legacy_session(db, laps=20)  # stands in for a historic row
    async with db.acquire() as conn:
        await conn.execute("ALTER TABLE sessions DROP COLUMN session_laps_confirmed")  # schema before the upgrade
        before = await conn.fetchrow("SELECT session_laps,session_end_et_seconds,session_duration_seconds,status "
                                     "FROM sessions WHERE id=$1", sid)
        for _ in range(2):  # additive and idempotent
            await conn.execute(main.SCHEMA_FILE.read_text())
        after = await conn.fetchrow("SELECT session_laps,session_end_et_seconds,session_duration_seconds,status,"
                                    "session_laps_confirmed FROM sessions WHERE id=$1", sid)
    assert {k: after[k] for k in before.keys()} == dict(before)  # historic values are untouched
    assert after["session_laps_confirmed"] is False  # ... but unverified until proven otherwise
    assert (await timing(sid))["configured_laps"] is None


class Observer:
    """One agent's runtime state, binding SessionInfo claims to a session that another agent also observes."""

    def __init__(self, session_id):
        self.session_id, self.state = session_id, dict(provider="lmu", track_name=TRACK, session_id=session_id)

    async def claim(self, *, minutes=0.0, laps=0, clock=300.0):
        now = time.monotonic()
        self.state.update(race_control={"phase": "green"}, race_control_at=now)
        session_meta.observe(self.state, "lmu", {"track_name": TRACK, "session_minutes": minutes, "session_laps": laps},
                             None, now)
        candidate = self.state.pop("meta_candidate")
        await main.bind_session_meta(self.state, candidate, self.session_id,
                                     dict(session_time=clock, session_type="Race"), False, None)


async def test_two_observers_converge_on_one_confirmed_end_with_a_legacy_placeholder(db):
    _, sid = await legacy_session(db)
    a, b = Observer(sid), Observer(sid)
    await a.claim(minutes=82.6)
    await b.claim(minutes=82.6)
    assert (await row(db, sid))["session_end_et_seconds"] is None  # one bracketed claim per observer is not enough
    await a.claim(minutes=82.6, clock=301.0)
    await b.claim(minutes=82.6, clock=301.0)  # second observer repeats: already known, nothing changes
    bound = await row(db, sid)
    assert bound["session_end_et_seconds"] == END
    assert (bound["session_laps"], bound["session_laps_confirmed"]) == (999, False)
    await a.claim(minutes=90.0, clock=302.0)
    await a.claim(minutes=90.0, clock=303.0)
    assert (await row(db, sid))["session_end_et_seconds"] == END  # write-once across observers


async def test_two_observers_confirm_a_lap_limit_once(db):
    _, sid = await legacy_session(db, laps=999)
    a, b = Observer(sid), Observer(sid)
    for observer in (a, a, b, b):
        await observer.claim(laps=20)
    assert (await row(db, sid))["session_laps"] == 20
    for _ in range(2):
        await b.claim(laps=30)  # a later claim never replaces a confirmed lap limit
    bound = await row(db, sid)
    assert (bound["session_laps"], bound["session_laps_confirmed"]) == (20, True)
