from copy import deepcopy

import pytest

from server.app import main
from server.app.timing import newest_scoring, project_timing, session_timing
from server.app.uuid7 import uuid7
from tests.conftest import scoring_session


def field():
    return [dict(id=uuid7(), local_vehicle_id=i, position=i, total_laps=5, laps_behind_leader=0,
                 gap_to_leader=gap) for i, gap in enumerate((0, 14.3, 14.8, 16.1), 1)]


def test_race_gap_and_adjacent_intervals():
    rows = field()
    project_timing(rows, "Race")
    assert [p["gap_to_leader"] for p in rows] == [0, 14.3, 14.8, 16.1]
    assert rows[0]["interval"] is None
    assert rows[2]["interval"] == pytest.approx(.5)
    assert rows[3]["interval"] == pytest.approx(1.3)
    assert rows[2]["interval_preceding_id"] == rows[1]["id"]


@pytest.mark.parametrize("gap", [-1, float("nan"), float("inf"), None, True])
def test_invalid_gaps_remain_unknown(gap):
    rows = field()
    rows[2]["gap_to_leader"] = gap
    project_timing(rows, "Race")
    assert rows[2]["gap_to_leader"] is None
    assert rows[2]["interval"] is rows[3]["interval"] is None


@pytest.mark.parametrize("change", [dict(position=7), dict(position=2), dict(total_laps=4),
    dict(total_laps=None), dict(laps_behind_leader=1), dict(laps_behind_leader=None),
    dict(laps_behind_leader=-1), dict(gap_to_leader=10)])
def test_incomparable_neighbors_have_no_interval(change):
    rows = field()
    rows[2].update(change)
    project_timing(rows, "Race")
    assert rows[2]["interval"] is None


def test_lap_deficit_precedes_time_and_leader_contradiction_is_unknown():
    rows = field()
    rows[2].update(laps_behind_leader=2, gap_to_leader=-9)
    rows[0]["gap_to_leader"] = 4
    project_timing(rows, "Race")
    assert rows[2]["gap_laps"] == 2 and rows[2]["gap_to_leader"] is None
    assert rows[2]["interval"] is rows[3]["interval"] is None
    assert rows[0]["gap_to_leader"] is None


@pytest.mark.parametrize("kind", ["Practice", "Qualifying", None])
def test_nonrace_unknown(kind):
    rows = field()
    project_timing(rows, kind)
    assert all(p["gap_to_leader"] is p["gap_laps"] is p["interval"] is None for p in rows)


def test_projection_copies_and_never_mixes_stored_gap_into_runtime():
    sid, rows = uuid7(), field()
    original = deepcopy(rows)
    states = [dict(session_id=sid, last_scoring_at=99, scoring=dict(session_time=123, vehicles=[
        dict(id=2, position=2, total_laps=5, time_behind_leader=4, laps_behind_leader=0)]))]
    output = main.spectator_participants(rows, sid, True, states, 100, "Race")
    assert rows == original
    assert next(p for p in output if p["local_vehicle_id"] == 2)["gap_to_leader"] == 4
    assert all(p["gap_to_leader"] is None for p in output if p["local_vehicle_id"] != 2)
    assert all(p["interval"] is None for p in output)


def test_session_clock_newest_fresh_same_session_and_configuration():
    sid = uuid7()
    def state(id, at, elapsed):
        return dict(session_id=id, last_scoring_at=at, scoring=dict(session_time=elapsed))
    states = [state(sid, 98, 3), state(sid, 99, 4), state(uuid7(), 100, 5), state(sid, 101, 6)]
    scoring = newest_scoring(sid, True, states, 100)
    assert scoring["session_time"] == 4
    fixed = dict(session_type="Race", session_laps=20, session_laps_confirmed=True, session_minutes=0)
    assert session_timing(fixed, scoring) == dict(elapsed_seconds=4, configured_laps=20, end_et_seconds=None,
                                                  remaining_seconds=None, configured_duration_seconds=None)
    timed = session_timing(dict(fixed, session_end_et_seconds=1934, session_duration_seconds=1800), scoring)
    assert timed["configured_laps"] is None  # a known end takes precedence over a lap value
    assert (timed["end_et_seconds"], timed["remaining_seconds"], timed["configured_duration_seconds"]) == (1934, 1930, 1800)
    # The end is not a duration: without a derived duration it stays unknown.
    assert session_timing(dict(fixed, session_end_et_seconds=1934), scoring)["configured_duration_seconds"] is None
    assert session_timing(dict(fixed, session_end_et_seconds=3), scoring)["remaining_seconds"] == 0  # clamped
    for live, at in ((False, 99), (True, 85), (True, 80)):
        result = newest_scoring(sid, live, [state(sid, at, 10)], 100)
        assert session_timing(fixed, result)["elapsed_seconds"] is None
    for elapsed in (None, -1, float("inf")):
        assert session_timing(fixed, dict(session_time=elapsed))["elapsed_seconds"] is None
    assert session_timing(fixed, dict(session_time=0))["elapsed_seconds"] == 0


async def test_api_sectors_latest_lap_gap_clock_and_raw_data_unchanged(db):
    agent, sid, scoring = await scoring_session(db, "synthetic-timing")
    scoring["vehicles"][0].update(time_behind_leader=0, laps_behind_leader=0)
    scoring["vehicles"][1].update(time_behind_leader=3.5, laps_behind_leader=0)
    # Both completed counts must match before deriving an interval.
    scoring["vehicles"][1]["total_laps"] = 1
    async with db.acquire() as conn:
        pid = await conn.fetchval("SELECT id FROM session_participants WHERE session_id=$1 AND local_vehicle_id=7", sid)
        await conn.execute("UPDATE sessions SET session_laps=20,session_laps_confirmed=true WHERE id=$1", sid)
        for lap, s1, s2, s3 in ((2, 31, 32, None), (1, 25, 26, 27)):
            await conn.execute("""INSERT INTO laps(id,session_participant_id,lap_number,sector1,sector2,sector3)
                VALUES($1,$2,$3,$4,$5,$6)""", uuid7(), pid, lap, s1, s2, s3)
        before = await conn.fetch("SELECT * FROM laps ORDER BY id")
    data = await main.api_session_live(sid)
    p = next(p for p in data["participants"] if p["id"] == pid)
    assert (p["sector_lap_number"], p["sector1"], p["sector2"], p["sector3"]) == (2, 31, 32, None)
    other = next(p for p in data["participants"] if p["id"] != pid)
    assert other["sector1"] is None and other["sector_lap_number"] is None
    assert other["interval"] == 3.5
    assert p["total_laps"] == 1  # Provider completed laps, not +1 or snapshot substitution.
    assert data["timing"] == dict(elapsed_seconds=100, configured_laps=20, end_et_seconds=None,
                                  remaining_seconds=None, configured_duration_seconds=None)
    async with db.acquire() as conn:
        assert before == await conn.fetch("SELECT * FROM laps ORDER BY id")
    main.runtime[str(agent["id"])]["last_scoring_at"] -= 16
    assert (await main.api_session_live(sid))["timing"]["elapsed_seconds"] is None


def test_sector_invalid_values_not_reconstructed():
    rows = field()
    rows[0].update(sector1=-1, sector2=float("inf"), sector3=0)
    project_timing(rows, "Race")
    assert all(rows[0][key] is None for key in ("sector1", "sector2", "sector3"))


# --- Interval reliability (#68 / #10) ---------------------------------------------------------------------
# Three private LMU captures showed `mTimeBehindLeader == 0` (observed, not guaranteed) for every car with a
# positive lap deficit. Two lapped cars therefore carry no subtractable leader gap: any interval would be a
# fabricated 0.0 s. These tests pin that the interval stays unknown and says why.

def lapped_field():
    """P1/P2 on the lead lap, P3..P5 one lap down (time reported as 0.0), P6 two laps down."""
    spec = ((0, 0, 0.0), (0, 0, 11.2), (4, 1, 0.0), (4, 1, 0.0), (4, 1, 0.0), (3, 2, 0.0))
    return [dict(id=uuid7(), local_vehicle_id=i, position=i, total_laps=laps, laps_behind_leader=deficit,
                 gap_to_leader=gap) for i, (laps, deficit, gap) in enumerate(spec, 1)]


def test_lapped_cars_with_equal_deficit_get_no_fabricated_interval():
    rows = lapped_field()
    project_timing(rows, "Race")
    assert [p["gap_laps"] for p in rows] == [None, None, 1, 1, 1, 2]
    assert [p["gap_to_leader"] for p in rows][:2] == [0.0, 11.2]
    assert all(p["gap_to_leader"] is None for p in rows[2:])
    assert [p["interval"] for p in rows][2:] == [None] * 4
    assert all(p["interval_preceding_id"] is None for p in rows[2:])
    assert [p["interval_reason"] for p in rows] == ["leader", None, "lapped", "lapped", "lapped", "lapped"]


def test_first_lapped_car_behind_lead_lap_car_is_lapped_not_a_time():
    rows = lapped_field()
    project_timing(rows, "Race")
    assert rows[1]["interval"] == pytest.approx(11.2) and rows[1]["interval_reason"] is None
    assert rows[2]["interval"] is None and rows[2]["interval_reason"] == "lapped"


@pytest.mark.parametrize("change, reason", [
    (dict(position=7), "position"), (dict(position=2), "position"),
    (dict(total_laps=4), "laps"), (dict(total_laps=None), "laps"),
    (dict(laps_behind_leader=1), "lapped"), (dict(laps_behind_leader=None), "gap"),
    (dict(laps_behind_leader=-1), "gap"), (dict(gap_to_leader=10), "gap"),
    (dict(gap_to_leader=-3.2), "gap"), (dict(gap_to_leader=float("nan")), "gap"),
    (dict(gap_to_leader=None), "gap")])
def test_unknown_interval_carries_a_reason(change, reason):
    rows = field()
    rows[2].update(change)
    project_timing(rows, "Race")
    assert rows[2]["interval"] is None and rows[2]["interval_preceding_id"] is None
    assert rows[2]["interval_reason"] == reason


def test_negative_gap_behind_leader_is_unknown_never_absolute():
    """#10: a car physically ahead of a pitting P1 reports a negative time; it is neither abs()ed nor subtracted."""
    rows = field()
    rows[1]["gap_to_leader"] = -4.4
    project_timing(rows, "Race")
    assert rows[1]["gap_to_leader"] is None
    assert rows[1]["interval"] is None and rows[2]["interval"] is None
    assert rows[1]["interval_reason"] == rows[2]["interval_reason"] == "gap"


def test_non_monotonic_adjacent_gaps_stay_unknown():
    rows = field()
    rows[2]["gap_to_leader"] = 14.0   # behind P3 by position but with a smaller leader gap than P2 (14.3)
    project_timing(rows, "Race")
    assert rows[2]["interval"] is None and rows[2]["interval_reason"] == "gap"


def test_leader_lap_interval_unchanged_and_reason_cleared():
    rows = field()
    project_timing(rows, "Race")
    assert [p["interval_reason"] for p in rows] == ["leader", None, None, None]


def test_duplicate_positions_have_no_interval_and_say_so():
    rows = field()
    rows[3]["position"] = 3
    project_timing(rows, "Race")
    assert rows[2]["interval"] is rows[3]["interval"] is None
    assert rows[2]["interval_reason"] == rows[3]["interval_reason"] == "position"


def test_nonrace_interval_reason():
    rows = field()
    project_timing(rows, "Qualifying")
    assert all(p["interval_reason"] == "not_race" for p in rows)


def test_spectator_projection_exposes_interval_reason_for_lapped_live_scoring():
    sid, rows = uuid7(), lapped_field()
    live = [dict(id=p["local_vehicle_id"], position=p["position"], total_laps=p["total_laps"],
                 time_behind_leader=p["gap_to_leader"], laps_behind_leader=p["laps_behind_leader"]) for p in rows]
    states = [dict(session_id=sid, last_scoring_at=99, scoring=dict(session_time=123, vehicles=live))]
    out = {p["local_vehicle_id"]: p for p in main.spectator_participants(rows, sid, True, states, 100, "Race")}
    assert out[2]["interval"] == pytest.approx(11.2) and out[2]["interval_reason"] is None
    assert all(out[i]["interval"] is None and out[i]["interval_reason"] == "lapped" for i in (3, 4, 5, 6))
    assert [out[i]["gap_laps"] for i in (3, 4, 5, 6)] == [1, 1, 1, 2]
