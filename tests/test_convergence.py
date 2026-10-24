import asyncio
import copy

import pytest

from server.app import main
from server.app.convergence import clock_consistent, matching_rosters, namespace_key
from server.app.lifecycle import expire_observations
from tests.conftest import make_agent


def roster(count=10, start=0):
    return [{"id": i, "driver_name": f"Synthetic Driver {i}", "team_name": "",
             "car_name": f"Synthetic Entry {i}", "vehicle_class": "GT3"} for i in range(start, start + count)]


def persisted(vehicles):
    return [dict(local_vehicle_id=v["id"], display_name=v.get("driver_name"),
                 team_name=v.get("team_name"), car_name=v.get("car_name"), car_class=v.get("vehicle_class"))
            for v in vehicles]


def scoring(vehicles=None, clock=100, player=0, kind="Practice"):
    return dict(session_type=kind, session_time=clock, player_vehicle_id=player,
                vehicles=roster() if vehicles is None else vehicles)


async def deliver(agent, payload=None, provider="lmu", track="Synthetic Circuit"):
    await main.handle_message(agent, provider, "session_info", {"track_name": track})
    await main.handle_message(agent, provider, "scoring", payload if payload is not None else scoring())
    return main.runtime[str(agent["id"])]["session_id"]


def test_roster_thresholds_subsets_normalization_and_no_mutation():
    raw = roster()
    before = copy.deepcopy(raw)
    assert matching_rosters("lmu", raw, persisted(roster(12)))
    assert matching_rosters("lmu", roster(5), persisted(raw))
    assert not matching_rosters("lmu", roster(4), persisted(raw))
    assert matching_rosters("lmu", roster(8) + roster(2, 30), persisted(raw))
    assert not matching_rosters("lmu", roster(7) + roster(3, 30), persisted(raw))
    trimmed = [{**v, "driver_name": " " + v["driver_name"] + " ", "team_name": None} for v in raw]
    assert matching_rosters("lmu", trimmed, persisted(raw))
    assert raw == before
    assert not matching_rosters("other", raw, persisted(raw))


@pytest.mark.parametrize("field", ["driver_name", "team_name", "car_name", "vehicle_class"])
def test_any_comparable_slot_conflict_rejects_even_with_high_overlap(field):
    vehicles = roster(20)
    vehicles[0][field] = "Different"
    assert not matching_rosters("lmu", vehicles, persisted(roster(20)))


@pytest.mark.parametrize("name", [None, "", "  ", "Unknown"])
def test_unknown_driver_and_name_only_fields_are_not_evidence(name):
    vehicles = [{**v, "driver_name": name} for v in roster()]
    assert not matching_rosters("lmu", vehicles, persisted(vehicles))
    names_only = [{"id": v["id"], "driver_name": v["driver_name"]} for v in roster()]
    assert not matching_rosters("lmu", names_only, persisted(names_only))


def test_duplicate_slots_missing_slots_and_case_changes_do_not_match():
    assert not matching_rosters("lmu", roster() + roster(1), persisted(roster()))
    assert not matching_rosters("lmu", roster(), persisted(roster() + roster(1)))
    assert not matching_rosters("lmu", [{**v, "id": None} for v in roster()], persisted(roster()))
    assert not matching_rosters("lmu", [{**v, "driver_name": v["driver_name"].lower()} for v in roster()], persisted(roster()))
    assert not matching_rosters("lmu", [], [])


@pytest.mark.parametrize("incoming,previous,age,expected", [
    (105, 100, 5, True), (110, 100, 5, True), (110.01, 100, 5, False),
    (99.99, 100, 5, False), (0, 100, 0, False), (100, 100, -1, False),
    (115, 100, 15, False), (114.9, 100, 14.9, True),
    (None, 100, 0, False), (True, 1, 0, False), (float("nan"), 100, 0, False),
    (100, float("inf"), 0, False), (100, None, 0, False),
])
def test_clock_gate(incoming, previous, age, expected):
    assert clock_consistent(incoming, previous, age) is expected


def test_namespace_lock_key_is_stable_and_unambiguous():
    assert namespace_key("lmu", "Synthetic", "Practice") == namespace_key("lmu", "Synthetic", "Practice")
    assert namespace_key("a|b", "c", "d") != namespace_key("a", "b|c", "d")
    assert namespace_key("lmu", "Synthetic", "Race") != namespace_key("lmu", "Synthetic", "Practice")


@pytest.mark.parametrize("first_count,second_count", [(10, 12), (12, 10)])
async def test_same_split_union_preserves_participant_uuids_and_driver_identity(db, first_count, second_count):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    first = await deliver(a, scoring(roster(first_count)))
    async with db.acquire() as conn:
        original = {p["local_vehicle_id"]: p["id"] for p in await conn.fetch("SELECT id,local_vehicle_id FROM session_participants")}
    second = await deliver(b, scoring(roster(second_count), player=1))
    assert first == second
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM sessions") == 1
        assert await conn.fetchval("SELECT count(*) FROM session_observations WHERE ended_at IS NULL") == 2
        participants = await conn.fetch("SELECT * FROM session_participants")
        assert len(participants) == 12
        by_slot = {p["local_vehicle_id"]: p for p in participants}
        assert all(by_slot[slot]["id"] == value for slot, value in original.items())
        assert by_slot[0]["driver_id"] == a["driver_id"]
        assert by_slot[1]["driver_id"] == b["driver_id"]
        assert all(by_slot[i]["driver_id"] is None for i in range(2, 12))
        assert await conn.fetchval("SELECT count(*) FROM drivers") == 2
        assert await conn.fetchval("SELECT count(*) FROM driver_identities") == 0
    # A different authenticated driver cannot take over an already resolved slot.
    c = await make_agent(db, "three")
    assert await deliver(c) == first
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT driver_id FROM session_participants WHERE local_vehicle_id=0") == a["driver_id"]
        assert await conn.fetchval("SELECT count(*) FROM drivers") == 3
        assert await conn.fetchval("SELECT count(*) FROM driver_identities") == 0


@pytest.mark.parametrize("case", ["disjoint", "conflict", "small", "clock", "null_clock", "provider", "track", "type", "names_only"])
async def test_incompatible_or_insufficient_evidence_keeps_sessions_separate(db, case):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    first = await deliver(a)
    payload, options = scoring(player=1), {}
    if case == "disjoint":
        payload["vehicles"] = roster(10, 30)
    elif case == "conflict":
        payload["vehicles"][0]["driver_name"] = "Different Synthetic Driver"
    elif case == "small":
        payload["vehicles"] = roster(4)
    elif case in ("clock", "null_clock"):
        payload["session_time"] = 0 if case == "clock" else None
    elif case == "provider":
        options["provider"] = "other"
    elif case == "track":
        options["track"] = "Other Synthetic Circuit"
    elif case == "type":
        payload["session_type"] = "Race"
    elif case == "names_only":
        payload["vehicles"] = [{"id": v["id"], "driver_name": v["driver_name"]} for v in roster()]
    assert await deliver(b, payload, **options) != first
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM sessions") == 2


@pytest.mark.parametrize("case", ["stale", "future", "null_clock", "nonfinite_clock", "ended", "complete"])
async def test_candidate_requires_fresh_active_finite_observation(db, case):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    first = await deliver(a)
    queries = {
        "stale": "UPDATE session_observations SET last_seen_at=now()-interval '16 seconds'",
        "future": "UPDATE session_observations SET last_seen_at=now()+interval '10 seconds'",
        "null_clock": "UPDATE session_observations SET last_session_time=NULL",
        "nonfinite_clock": "UPDATE session_observations SET last_session_time='NaN'::float8",
        "ended": "UPDATE session_observations SET ended_at=now()",
        "complete": "UPDATE sessions SET status='complete'",
    }
    async with db.acquire() as conn:
        await conn.execute(queries[case])
    assert await deliver(b, scoring(player=1)) != first


async def test_multiple_matching_candidates_do_not_guess_and_same_agent_stays_first(db):
    a, b, c = [await make_agent(db, name) for name in ("one", "two", "three")]
    first = await deliver(a)
    second = await deliver(b, scoring(clock=200))
    assert second != first
    async with db.acquire() as conn:
        await conn.execute("UPDATE session_observations SET last_session_time=100,last_seen_at=now()")
    third = await deliver(c)
    assert third not in (first, second)
    main.runtime.pop(str(a["id"]))
    assert await deliver(a) == first  # Same-agent observation wins even with multiple matching candidates.


@pytest.mark.parametrize("disjoint", [False, True])
async def test_concurrent_initial_rosters_are_visible_before_other_resolution(db, monkeypatch, disjoint):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    entered, release = asyncio.Event(), asyncio.Event()
    original = main.upsert_participants_on_connection

    async def delayed(conn, agent, sid, payload, provider):
        if agent["id"] == a["id"]:
            entered.set()
            await release.wait()
        await original(conn, agent, sid, payload, provider)

    monkeypatch.setattr(main, "upsert_participants_on_connection", delayed)
    task_a = asyncio.create_task(deliver(a))
    await asyncio.wait_for(entered.wait(), 5)
    task_b = asyncio.create_task(deliver(b, scoring(roster(12, 30 if disjoint else 0), player=1)))
    try:
        # A is paused after resolution but before its roster is persisted. B must wait on the DB lock.
        for _ in range(100):
            async with db.acquire() as conn:
                waiting = await conn.fetchval("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted")
            if waiting:
                break
            await asyncio.sleep(.01)
        assert waiting > 0 and not task_b.done()
    finally:
        release.set()
        first, second = await asyncio.wait_for(asyncio.gather(task_a, task_b), 10)
    assert (first != second) is disjoint
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM sessions") == (2 if disjoint else 1)
        assert await conn.fetchval("SELECT count(*) FROM session_observations") == 2
        assert await conn.fetchval("SELECT count(*) FROM session_participants") == (22 if disjoint else 12)


async def test_failed_initial_roster_rolls_back_resolution(db, monkeypatch):
    a = await make_agent(db)

    async def fail(*args):
        raise RuntimeError("synthetic persistence failure")

    monkeypatch.setattr(main, "upsert_participants_on_connection", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        await deliver(a)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM sessions") == 0
        assert await conn.fetchval("SELECT count(*) FROM session_observations") == 0
    assert not main.runtime[str(a["id"])].get("session_id")


async def test_converged_runtime_loss_independent_disconnect_and_rejoin(db):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    sid = await deliver(a)
    assert await deliver(b, scoring(player=1)) == sid
    main.runtime.clear()
    assert await deliver(a, scoring(clock=101)) == sid
    await main.finalize_agent_session(a)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM sessions WHERE id=$1", sid) == "live"
    assert await deliver(a, scoring(clock=101)) == sid
    await main.finalize_agent_session(a)
    await main.finalize_agent_session(b)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM sessions WHERE id=$1", sid) == "complete"
        assert await conn.fetchval("SELECT count(*) FROM session_observations WHERE ended_at IS NULL") == 0


async def test_simultaneous_disconnects_complete_shared_session(db):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    sid = await deliver(a)
    assert await deliver(b, scoring(player=1)) == sid
    await asyncio.wait_for(asyncio.gather(main.finalize_agent_session(a), main.finalize_agent_session(b)), 10)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM sessions WHERE id=$1", sid) == "complete"


@pytest.mark.parametrize("runtime_loss", [False, True])
async def test_small_meaningful_reset_cannot_rejoin_still_running_session(db, runtime_loss):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    sid = await deliver(a)
    assert await deliver(b, scoring(player=1)) == sid
    if runtime_loss:
        main.runtime.pop(str(a["id"]))
    # Three seconds is inside cross-agent tolerance but violates same-agent reset semantics.
    assert await deliver(a, scoring(clock=97)) != sid
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM sessions WHERE id=$1", sid) == "live"
        assert await conn.fetchval("SELECT count(*) FROM session_observations WHERE session_id=$1 AND ended_at IS NULL", sid) == 1


async def test_clock_compensates_wall_time_and_uses_freshest_suitable_observer(db):
    a, b, c = [await make_agent(db, name) for name in ("one", "two", "three")]
    sid = await deliver(a)
    assert await deliver(b, scoring(player=1)) == sid
    async with db.acquire() as conn:
        await conn.execute("UPDATE session_observations SET last_session_time=90,last_seen_at=now()-interval '10 seconds' WHERE agent_id=$1", a["id"])
        await conn.execute("UPDATE session_observations SET last_session_time=NULL WHERE agent_id=$1", b["id"])
    assert await deliver(c, scoring(player=2)) == sid


async def test_freshest_valid_clock_cannot_be_bypassed_by_older_matching_clock(db):
    a, b, c = [await make_agent(db, name) for name in ("one", "two", "three")]
    sid = await deliver(a)
    assert await deliver(b, scoring(player=1)) == sid
    async with db.acquire() as conn:
        await conn.execute("UPDATE session_observations SET last_session_time=99,last_seen_at=now()-interval '1 second' WHERE agent_id=$1", a["id"])
        await conn.execute("UPDATE session_observations SET last_session_time=200,last_seen_at=now() WHERE agent_id=$1", b["id"])
    assert await deliver(c, scoring(player=2)) != sid


async def test_timeout_of_one_observer_keeps_converged_session_live(db):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")
    sid = await deliver(a)
    assert await deliver(b, scoring(player=1)) == sid
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE session_observations SET last_seen_at=now()-interval '121 seconds' WHERE agent_id=$1", a["id"])
        await expire_observations(conn, 120)
        assert await conn.fetchval("SELECT status FROM sessions WHERE id=$1", sid) == "live"
        assert await conn.fetchval("SELECT count(*) FROM session_observations WHERE ended_at IS NULL") == 1
        await conn.execute("UPDATE session_observations SET last_seen_at=now()-interval '121 seconds'")
        await expire_observations(conn, 120)
        assert await conn.fetchval("SELECT status FROM sessions WHERE id=$1", sid) == "complete"
