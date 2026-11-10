import time

import httpx

from server.app import main
from server.app.driver_live import current_lap_of, focus_block, own_player_sector, status_of
from tests.conftest import make_agent, scoring_session

SESSION = {"id": "S1", "session_type": "Race"}


def participant(pid, position, **extra):
    base = {"participant_id": pid, "local_vehicle_id": position, "position": position, "total_laps": 5,
            "last_lap_time": 90.0, "best_lap_time": 89.0, "gap_to_leader": 0.0 if position == 1 else 4.0 * position,
            "laps_behind_leader": 0, "in_pits": False, "pitstops": 0, "finish_status": 0, "is_stintara_driver": False}
    return {**base, **extra}


def state(slug="one", sector=2, vehicle_id=2, in_pits=False, age=0.2, control_age=0.2, session="S1", now=100.0):
    return {"driver_slug": slug, "session_id": session, "last_scoring_at": now - age,
            "scoring": {"player_vehicle_id": vehicle_id, "vehicles": [{"id": vehicle_id, "in_pits": in_pits}]},
            "race_control": {"player_sector": sector}, "race_control_at": now - control_age}


def test_status_semantics_follow_the_session_view():
    assert status_of({"finish_status": 1, "in_pits": True}) == "FIN"
    assert status_of({"finish_status": 2}) == "DNF" and status_of({"finish_status": 3}) == "DSQ"
    assert status_of({"finish_status": 0, "in_pits": True}) == "PIT"
    assert status_of({"finish_status": 0, "in_pits": False}) == "TRACK"
    assert status_of({"finish_status": 0, "in_pits": None}) is None
    assert status_of({"finish_status": 9, "in_pits": False}) is None      # unknown codes get no guessed label
    assert status_of({}) is None


def test_current_lap_only_while_racing_and_countable():
    assert current_lap_of({"total_laps": 4, "finish_status": 0}) == 5
    assert current_lap_of({"total_laps": 0, "finish_status": 0}) == 1
    for bad in ({"total_laps": 4, "finish_status": 1}, {"total_laps": 4, "finish_status": 2},
                {"total_laps": None, "finish_status": 0}, {"total_laps": -1, "finish_status": 0},
                {"total_laps": True, "finish_status": 0}, {"total_laps": 4}):
        assert current_lap_of(bad) is None


def test_player_sector_requires_own_fresh_matching_observation():
    now = 100.0
    assert own_player_sector("one", "S1", 2, [state(now=now)], now) == 2
    for bad in (state(slug="other", now=now), state(session="S2", now=now), state(vehicle_id=3, now=now),
                state(in_pits=True, now=now), state(age=1.5, now=now), state(control_age=6, now=now),
                state(sector=0, now=now), state(sector=None, now=now)):
        assert own_player_sector("one", "S1", 2, [bad], now) is None
    assert own_player_sector("one", "S1", 2, [], now) is None
    # A spectating agent of another driver never provides the sector, even when it sees the same session.
    assert own_player_sector("one", "S1", 2, [state(slug="two", sector=3, now=now)], now) is None
    # Several own observations must agree; one non-matching own observation makes the answer ambiguous.
    assert own_player_sector("one", "S1", 2, [state(sector=1, now=now), state(sector=1, now=now)], now) == 1
    assert own_player_sector("one", "S1", 2, [state(sector=1, now=now), state(sector=2, now=now)], now) is None
    assert own_player_sector("one", "S1", 2, [state(now=now), state(age=3, now=now)], now) is None


def test_focus_block_for_the_unique_own_participant():
    now = 100.0
    people = [participant("a", 1), participant("b", 2, is_stintara_driver=True, pitstops=1), participant("c", 3)]
    history = {"lap_number": 5, "lap_time": 90.0, "sector1": 30.0, "sector2": 31.0, "sector3": None, "last_pit_lap": 3}
    f = focus_block("one", SESSION, people, history, [state(now=now)], now)
    assert (f["participant_id"], f["status"], f["position"]) == ("b", "TRACK", 2)
    assert (f["completed_laps"], f["current_lap"], f["best_lap_time"]) == (5, 6, 89.0)
    assert f["last_lap"] == {"lap_number": 5, "lap_time": 90.0, "sector1": 30.0, "sector2": 31.0, "sector3": None}
    assert (f["last_lap_number"], f["last_lap_time"]) == (5, 90.0)
    assert (f["pitstops"], f["last_pit_lap"], f["player_sector"]) == (1, 3, 2)
    assert f["gap_to_leader"] == 8.0 and f["interval"] == 8.0
    assert [p["gap_to_leader"] for p in people] == [0.0, 8.0, 12.0], "existing participant fields stay untouched"


def stored(lap, time_=90.0):
    return {"lap_number": lap, "lap_time": time_, "sector1": 30.0, "sector2": 31.0, "sector3": 29.0, "last_pit_lap": None}


def test_sectors_belong_only_to_the_lap_the_scoring_reports():
    people = [participant("a", 1), participant("b", 2, is_stintara_driver=True, total_laps=6, last_lap_time=91.5)]
    # Scoring ahead of the stored checkpoint (stored lap 5, scoring says lap 6 completed): no sectors, live time kept.
    f = focus_block("one", SESSION, people, stored(5), [], 100.0)
    assert f["last_lap"] is None and (f["last_lap_number"], f["last_lap_time"]) == (6, 91.5)
    # Scoring behind the stored checkpoint (stored lap 7, scoring still at 6): also none.
    assert focus_block("one", SESSION, people, stored(7), [], 100.0)["last_lap"] is None
    # Same lap number but a different time is not the same lap record.
    assert focus_block("one", SESSION, people, stored(6, 95.0), [], 100.0)["last_lap"] is None
    # Exact match (within single-precision noise) yields the complete triple.
    f = focus_block("one", SESSION, people, stored(6, 91.5004), [], 100.0)
    assert f["last_lap"]["lap_number"] == 6 and f["last_lap"]["sector2"] == 31.0
    # Unknown live time with a matching lap number: the stored time is used, never mixed with another lap.
    people[1]["last_lap_time"] = None
    f = focus_block("one", SESSION, people, stored(6, 91.5), [], 100.0)
    assert f["last_lap"]["lap_time"] == 91.5 and f["last_lap_time"] is None
    # No stored lap at all, or no completed lap yet: nothing is claimed.
    assert focus_block("one", SESSION, people, None, [], 100.0)["last_lap"] is None
    people[1]["total_laps"] = 0
    f = focus_block("one", SESSION, people, stored(0), [], 100.0)
    assert f["last_lap"] is None and f["last_lap_number"] is None


def test_focus_unknowns_and_ambiguity_never_invent_values():
    now = 100.0
    me = participant("b", 2, is_stintara_driver=True, total_laps=None, best_lap_time=-1, in_pits=None, pitstops=None,
                     gap_to_leader=None)
    f = focus_block("one", SESSION, [participant("a", 1), me], {"lap_number": 0}, [], now)
    assert f["status"] is None and f["completed_laps"] is None and f["current_lap"] is None
    assert f["best_lap_time"] is None and f["last_lap"] is None and f["last_pit_lap"] is None
    assert f["gap_to_leader"] is None and f["interval"] is None and f["player_sector"] is None
    assert f["live_age_s"] is None
    # last_pit_lap without any recorded stop is not reported; zero/invalid sectors become unknown.
    me = participant("b", 2, is_stintara_driver=True, pitstops=0)
    me["last_lap_time"] = 80.0
    f = focus_block("one", SESSION, [me], {"lap_number": 5, "lap_time": 80.0, "sector1": 0, "sector2": -2,
                                          "sector3": float("nan"), "last_pit_lap": 2}, [], now)
    assert f["last_pit_lap"] is None
    assert (f["last_lap"]["sector1"], f["last_lap"]["sector2"], f["last_lap"]["sector3"]) == (None, None, None)
    # An ambiguous (two own participants) or missing own participant yields no focus at all.
    two = [participant("a", 1, is_stintara_driver=True), participant("b", 2, is_stintara_driver=True)]
    assert focus_block("one", SESSION, two, None, [], now) is None
    assert focus_block("one", SESSION, [participant("a", 1)], None, [], now) is None


def test_finished_retired_and_disqualified_have_no_current_lap_and_stable_status():
    for code, label in ((1, "FIN"), (2, "DNF"), (3, "DSQ")):
        me = participant("b", 2, is_stintara_driver=True, finish_status=code, in_pits=True)
        f = focus_block("one", SESSION, [me], None, [], 100.0)
        assert (f["status"], f["current_lap"], f["completed_laps"]) == (label, None, 5)


async def test_api_adds_focus_for_the_own_participant_only_and_keeps_existing_fields(db):
    agent, sid, _ = await scoring_session(db, "focus-driver")
    async with db.acquire() as conn:
        mine = await conn.fetchval("SELECT id FROM session_participants WHERE session_id=$1 AND driver_id=$2",
                                   sid, agent["driver_id"])
        for n in range(1, 5):
            await conn.execute("""INSERT INTO laps(id,session_participant_id,lap_number,lap_time,sector1,sector2,sector3,
                pitstops) VALUES(gen_random_uuid(),$1,$2,$3,30.0,31.0,$4,$5)""", mine, n, 90.0 + n, 29.0 + n,
                               1 if n >= 3 else 0)
    state_ = main.runtime[str(agent["id"])]
    state_["scoring"]["vehicles"][0]["in_pits"] = False
    state_["last_scoring_at"] = state_["race_control_at"] = time.monotonic()
    state_["race_control"] = {"player_sector": 3}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost") as client:
        data = (await client.get(f"/api/drivers/{agent['slug']}/live")).json()
    focus = data["focus"]
    assert focus["participant_id"] == str(mine) and focus["current_lap"] == 2 and focus["status"] == "TRACK"
    # Scoring says 1 completed lap while laps 1-4 are stored: the stored lap 4 must not be shown as the last lap.
    assert focus["last_lap"] is None and focus["last_lap_number"] == 1
    async with db.acquire() as conn:
        await conn.execute("UPDATE session_participants SET total_laps=4,last_lap_time=94.0 WHERE id=$1", mine)
    state_["scoring"]["vehicles"][0].update(total_laps=4, last_lap_time=94.0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost") as client:
        matched = (await client.get(f"/api/drivers/{agent['slug']}/live")).json()["focus"]
    assert matched["last_lap"]["lap_number"] == 4 and matched["last_lap"]["sector3"] == 33.0
    # The participant's own stop counter is 0 (not yet reported by a lap snapshot), so no last stop is reported.
    assert focus["last_pit_lap"] is None
    assert focus["player_sector"] == 3
    assert {"participants", "strategy", "session", "driver"} <= set(data)
    other = next(p for p in data["participants"] if not p["is_stintara_driver"])
    assert "sector1" not in other and "last_pit_lap" not in other


async def test_api_spectating_agent_of_another_driver_supplies_no_sector(db):
    agent, sid, _ = await scoring_session(db, "focus-driver")
    spectator = await make_agent(db, "spectator")
    main.runtime[str(spectator["id"])] = dict(
        driver_slug="spectator", session_id=sid, last_scoring_at=time.monotonic(),
        scoring=main.runtime[str(agent["id"])]["scoring"], race_control={"player_sector": 2},
        race_control_at=time.monotonic())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost") as client:
        data = (await client.get(f"/api/drivers/{agent['slug']}/live")).json()
    assert data["focus"]["player_sector"] is None


async def test_last_pit_lap_is_the_first_stored_checkpoint_after_a_counter_increase(db):
    agent, sid, _ = await scoring_session(db, "focus-driver")
    async with db.acquire() as conn:
        mine = await conn.fetchval("SELECT id FROM session_participants WHERE session_id=$1 AND driver_id=$2",
                                   sid, agent["driver_id"])

        async def last_pit(counters):
            await conn.execute("DELETE FROM laps WHERE session_participant_id=$1", mine)
            for lap, stops in counters.items():
                await conn.execute("INSERT INTO laps(id,session_participant_id,lap_number,pitstops) "
                                   "VALUES(gen_random_uuid(),$1,$2,$3)", mine, lap, stops)
            return (await conn.fetchrow(main.OWN_LAP_HISTORY_SQL, mine))["last_pit_lap"]

        assert await last_pit({1: 0, 2: 0, 3: 1, 4: 1, 5: 1}) == 3
        assert await last_pit({1: 0, 2: 1, 3: 1, 4: 2, 5: 2}) == 4, "the latest increase wins"
        # A missing checkpoint delays the detection: the stop happened before lap 4 but is first seen at lap 5.
        assert await last_pit({1: 0, 2: 0, 5: 1}) == 5
        # A counter already above zero at the first stored checkpoint has no earlier baseline and is not a detection.
        assert await last_pit({3: 1, 4: 1}) is None
        assert await last_pit({1: 0, 2: 0}) is None
