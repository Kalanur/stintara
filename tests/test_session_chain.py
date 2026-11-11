"""#43: persisted Practice -> Qualifying -> Race chain built only from direct same-agent transitions."""
import pytest

from server.app import history, main
from tests.conftest import make_agent
from tests.test_convergence import roster, scoring

TRACK = "Synthetic Circuit"


async def at(agent, kind, clock=100, track=TRACK, laps=0, minutes=None):
    vehicles = [dict(v, total_laps=laps) for v in roster()]
    info = {"track_name": track}
    if minutes is not None:
        info["session_minutes"] = minutes
    # With a limit the info/scoring pair is repeated: session limits are bound only after a bracketed,
    # repeated SessionInfo while the race is green (#20).
    for _ in range(3 if minutes is not None else 1):
        if minutes is not None:
            await main.handle_message(agent, "lmu", "race_control", {"phase": "green"})
        await main.handle_message(agent, "lmu", "session_info", info)
        main.runtime.get(str(agent["id"]), {}).pop("last_persisted_scoring", None)  # production persists at 1 Hz
        await main.handle_message(agent, "lmu", "scoring", scoring(vehicles, clock=clock, player=1, kind=kind))
    return main.runtime[str(agent["id"])]["session_id"]


async def nav(sid):
    s = (await main.api_session_live(sid))["session"]
    return {k: s[k] for k in ("event_id", "race_id", "previous_session", "next_session")}


async def counts(db):
    async with db.acquire() as conn:
        return {t: await conn.fetchval(f"SELECT count(*) FROM {t}") for t in ("sessions", "races", "events")}


async def test_practice_qualifying_race_chain_uses_existing_event_race_model(db):
    agent = await make_agent(db)
    p, q, r = await at(agent, "Practice", 500), await at(agent, "Qualifying", 5), await at(agent, "Race", 3)
    assert len({p, q, r}) == 3
    a, b, c = await nav(p), await nav(q), await nav(r)
    assert a["race_id"] == b["race_id"] == c["race_id"] and a["event_id"] == b["event_id"] == c["event_id"]
    assert a["event_id"] is not None
    assert a["previous_session"] is None and a["next_session"] == {"id": q, "session_type": "Qualifying"}
    assert b["previous_session"] == {"id": p, "session_type": "Practice"}
    assert b["next_session"] == {"id": r, "session_type": "Race"}
    assert c["previous_session"] == {"id": q, "session_type": "Qualifying"} and c["next_session"] is None
    assert await counts(db) == {"sessions": 3, "races": 1, "events": 1}  # no second event structure


async def test_chain_survives_restart_and_schema_reapplication(db):
    agent = await make_agent(db)
    p, q = await at(agent, "Practice", 500), await at(agent, "Qualifying", 5)
    before = await nav(q)
    main.runtime.clear()
    async with db.acquire() as conn:
        await conn.execute(main.SCHEMA_FILE.read_text())  # additive and idempotent
    assert await nav(q) == before and before["previous_session"]["id"] == p
    assert (await nav(p))["next_session"]["id"] == q


async def test_missing_successor_is_never_invented(db):
    agent = await make_agent(db)
    q = await at(agent, "Qualifying", 700)
    await main.finalize_agent_session(agent)
    assert (await nav(q))["next_session"] is None
    assert await counts(db) == {"sessions": 1, "races": 0, "events": 0}


@pytest.mark.parametrize("case", ["same_type_reset", "backwards", "other_track", "stale_observation", "unknown_type"])
async def test_non_chain_transitions_stay_unlinked(db, case):
    agent = await make_agent(db)
    first_kind = {"same_type_reset": "Race", "backwards": "Race", "other_track": "Qualifying",
                  "stale_observation": "Qualifying", "unknown_type": "Warmup"}[case]
    first = await at(agent, first_kind, 900)
    if case == "stale_observation":
        async with db.acquire() as conn:
            await conn.execute("UPDATE session_observations SET last_seen_at=now()-interval '10 minutes'")
    if case == "backwards":
        second = await at(agent, "Practice", 3)
    elif case == "other_track":
        second = await at(agent, "Race", 3, track="Other Circuit")
    else:
        second = await at(agent, "Race", 3)
    assert second != first
    for sid in (first, second):
        view = await nav(sid)
        assert view["previous_session"] is None and view["next_session"] is None and view["race_id"] is None
    assert await counts(db) == {"sessions": 2, "races": 0, "events": 0}


async def test_other_agent_without_observed_transition_is_not_linked(db):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    q = await at(a, "Qualifying", 700)
    r = await at(b, "Race", 3)  # same track, but no directly observed transition
    assert r != q and (await nav(r))["previous_session"] is None and (await nav(q))["next_session"] is None


async def test_two_observers_converge_without_duplicate_chains(db):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    p = await at(a, "Practice", 500)
    assert await at(b, "Practice", 500) == p
    q = await at(a, "Qualifying", 5)
    assert await at(b, "Qualifying", 5) == q
    assert await counts(db) == {"sessions": 2, "races": 1, "events": 1}
    assert (await nav(p))["next_session"]["id"] == q
    assert (await nav(q))["previous_session"]["id"] == p


async def test_second_successor_is_refused_and_concurrent_agents_stay_unlinked(db):
    from server.app.lifecycle import link_session_chain
    from server.app.uuid7 import uuid7
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    p = await at(a, "Practice", 500)
    assert await at(b, "Practice", 500) == p
    q1 = await at(a, "Qualifying", 5)
    assert await at(b, "Qualifying", 400) is None  # not clock-consistent: waits, creates no duplicate chain
    other = uuid7()
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("INSERT INTO sessions(id,simulator_provider,session_type,track_name) VALUES($1,'lmu','Qualifying',$2)",
                           other, TRACK)
        previous = await conn.fetchrow("SELECT * FROM sessions WHERE id=$1", p)
        await link_session_chain(conn, previous, other)  # P already has q1 as successor
    assert (await nav(other))["previous_session"] is None and (await nav(p))["next_session"]["id"] == q1
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM sessions WHERE previous_session_id=$1", p) == 1


async def test_post_race_idle_state_is_not_a_race_to_race_link(db):
    """#46 on top of #43: the idle state creates no session, hence no Race -> Race transition."""
    agent = await make_agent(db)
    await at(agent, "Practice", 500)
    await at(agent, "Qualifying", 5)
    race = await at(agent, "Race", 3, laps=1, minutes=1)
    assert await at(agent, "Race", 300, laps=5, minutes=1) == race  # clock past the configured minute
    assert await at(agent, "Race", 0.0, laps=0, minutes=5 / 60) is None  # placeholder SessionInfo first
    assert (await counts(db))["sessions"] == 3 and (await nav(race))["next_session"] is None


async def test_history_rows_expose_persisted_previous_session_only(db):
    agent = await make_agent(db)
    q, r = await at(agent, "Qualifying", 700), await at(agent, "Race", 3)
    async with db.acquire() as conn:
        items = {str(i["id"]): i for i in (await history.query_sessions(conn))["items"]}
    assert items[str(r)]["previous_session"] == {"id": q, "session_type": "Qualifying"}
    assert items[str(q)]["previous_session"] is None and items[str(r)]["race_id"] is not None
