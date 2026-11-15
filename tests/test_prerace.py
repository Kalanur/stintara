import math

import httpx
import pytest

from server.app import main
from server.app.prerace import PACE_MARGIN, plan_prerace, prerace_evidence
from tests.conftest import make_agent, scoring_session


def timed(**extra):
    base = dict(duration_minutes=60.0, lap_time_s=120.0, fuel_per_lap_l=3.0, tank_capacity_l=100.0, ve_capable=False)
    return plan_prerace({**base, **extra})


def codes(plan):
    return {(i["code"], i["field"]) for i in plan["issues"]}


def test_timed_race_has_a_conservative_planning_bound_not_an_exact_count():
    plan = timed()
    distance = plan["distance"]
    assert distance["kind"] == "timed" and distance["approximate"] is True
    assert distance["laps_min"] == 30
    assert distance["laps_planning"] == math.ceil(3600 / (120 * (1 - PACE_MARGIN))) + 1 == 32
    # (planning laps + 1 reserve lap) * 3 L
    assert plan["status"] == "ready" and plan["start"] == {"fuel_l": 99.0, "ve_percent": None}
    assert plan["fuel"]["reserve_l"] == 3.0 and plan["fuel"]["laps_min_l"] == 90.0


def test_lap_limited_race_is_exact_and_ignores_pace():
    plan = timed(duration_minutes=None, race_laps=20, lap_time_s=None)
    assert plan["distance"] == dict(kind="laps", race_laps=20, duration_minutes=None, pace_s=None, laps_min=20,
                                    laps_planning=20, approximate=False)
    assert plan["status"] == "ready" and plan["start"]["fuel_l"] == 63.0


def test_missing_race_length_gives_no_recommendation():
    plan = timed(duration_minutes=None)
    assert plan["status"] == "incomplete" and plan["start"] is None and not plan["recommendation_available"]
    assert ("missing_race_length", "duration_minutes") in codes(plan)


def test_timed_race_without_pace_and_missing_consumption_are_reported_separately():
    plan = timed(lap_time_s=None, fuel_per_lap_l=None)
    assert plan["start"] is None
    assert {("missing_lap_time", "lap_time_s"), ("missing_fuel_per_lap", "fuel_per_lap_l")} <= codes(plan)
    assert plan["distance"]["laps_planning"] is None


@pytest.mark.parametrize("field,value", [
    ("lap_time_s", 0), ("lap_time_s", -90.0), ("lap_time_s", 5.0), ("lap_time_s", float("nan")),
    ("lap_time_s", float("inf")), ("lap_time_s", True), ("lap_time_s", 99999.0),
    ("duration_minutes", 0), ("duration_minutes", -5), ("race_laps", 0), ("race_laps", 2.5), ("race_laps", True),
    ("fuel_per_lap_l", 0), ("fuel_per_lap_l", -1.0), ("tank_capacity_l", 0), ("reserve_laps", -1.0),
    ("reserve_laps", 50.0), ("ve_capable", 1),
])
def test_invalid_values_are_rejected_never_silently_ignored(field, value):
    plan = timed(**{field: value}) if field != "race_laps" else timed(duration_minutes=None, race_laps=value)
    assert plan["status"] == "invalid_input" and plan["start"] is None
    assert ("invalid_value", field) in codes(plan)


def test_conflicting_race_length_is_not_resolved_by_guessing():
    plan = timed(race_laps=30)
    assert plan["status"] == "conflicting_input" and plan["start"] is None


def test_safety_reserve_is_explicit_and_additive():
    none, one, two = (timed(reserve_laps=r, tank_capacity_l=200.0) for r in (0.0, 1.0, 2.0))
    assert [p["start"]["fuel_l"] for p in (none, one, two)] == [96.0, 99.0, 102.0]
    assert two["reserve_laps"] == 2.0 and two["fuel"]["reserve_l"] == 6.0
    assert timed()["reserve_laps"] == 1.0   # default


def test_tank_too_small_means_stop_required_and_no_start_value():
    plan = timed(tank_capacity_l=60.0)
    assert plan["status"] == "stop_required" and plan["start"] is None and not plan["recommendation_available"]
    assert plan["fuel"]["single_stint_feasible"] is False and plan["fuel"]["shortfall_l"] == pytest.approx(39.0)
    assert plan["fuel"]["full_tank_laps"] == pytest.approx(20.0)


def test_unknown_tank_keeps_the_amount_but_marks_capacity_unchecked():
    plan = timed(tank_capacity_l=None)
    assert plan["status"] == "ready_capacity_unchecked" and plan["start"]["fuel_l"] == 99.0
    assert plan["fuel"]["capacity_checked"] is False and ("tank_capacity_unknown", "tank_capacity_l") in codes(plan)


def test_ve_capable_car_needs_ve_evidence_and_gets_no_fuel_only_advice():
    plan = timed(ve_capable=True)
    assert plan["status"] == "incomplete" and plan["start"] is None
    assert ("missing_ve_per_lap", "ve_per_lap_percent") in codes(plan)


def test_unknown_ve_capability_is_not_treated_as_no_ve():
    plan = timed(ve_capable=None)
    assert plan["status"] == "incomplete" and plan["start"] is None
    assert ("ve_capability_unknown", "ve_capable") in codes(plan)


def test_ve_and_fuel_are_separate_resources_with_their_own_start_values():
    plan = timed(ve_capable=True, ve_per_lap_percent=2.5)
    assert plan["status"] == "ready"
    assert plan["start"] == {"fuel_l": 99.0, "ve_percent": 82.5}      # 33 laps * 2.5 %
    assert plan["energy"]["limiting_resource"] == "fuel" and plan["energy"]["single_stint_feasible"]
    # VE alone can make a single start load infeasible even when fuel fits.
    over = timed(ve_capable=True, ve_per_lap_percent=3.2)
    assert over["status"] == "stop_required" and over["start"] is None
    assert over["energy"]["shortfall_percent"] == pytest.approx(5.6) and over["fuel"]["single_stint_feasible"]
    assert timed(ve_capable=False, ve_per_lap_percent=2.0)["status"] == "conflicting_input"


def test_sources_are_echoed_only_for_supplied_inputs():
    plan = plan_prerace(dict(duration_minutes=45.0, lap_time_s=100.0, fuel_per_lap_l=2.0, ve_capable=False),
                        {"duration_minutes": "manual", "lap_time_s": "observed", "race_laps": "manual", "x": "manual"})
    assert plan["sources"] == {"duration_minutes": "manual", "lap_time_s": "observed"}


def test_plan_is_pure_and_does_not_mutate_its_input():
    inputs = dict(duration_minutes=60.0, lap_time_s=120.0, fuel_per_lap_l=3.0, ve_capable=False)
    before = dict(inputs)
    plan_prerace(inputs)
    assert inputs == before


# ------------------------------------------------------------------ evidence: suggestions only

def lap(n, time=100.0, fuel=None, **extra):
    return {"lap_number": n, "lap_time": time, "fuel_remaining_l": fuel, "pitstops": 0, "in_pits": False,
            "energy_remaining_fraction": None, **extra}


def laps(count=6, time=100.0, burn=2.5, start=80.0):
    return [lap(n, time + n * 0.1, start - burn * n) for n in range(1, count + 1)]


def test_qualifying_never_reports_race_rules_and_is_not_representative():
    session = dict(session_type="Qualify", session_laps=5, session_laps_confirmed=True,
                   session_duration_seconds=600.0)
    ev = prerace_evidence(session, laps(), dict(fuel_capacity_l=90.0, energy_available=False), "lmu")
    assert ev["race_rules"] == dict(available=False, race_laps=None, duration_minutes=None, source=None)
    assert ev["representative_of_race"] is None
    assert ev["fuel_per_lap"]["value"] == pytest.approx(2.5) and ev["tank_capacity_l"] == 90.0
    assert ev["lap_time"]["sample_count"] == 5 and ev["ve_capable"] is False


def test_race_rules_come_only_from_a_race_session_and_only_when_confirmed():
    race = dict(session_type="Race", session_laps=30, session_laps_confirmed=True, session_duration_seconds=None)
    assert prerace_evidence(race, [], {}, "lmu")["race_rules"] == dict(
        available=True, race_laps=30, duration_minutes=None, source="confirmed")
    timed_race = dict(race, session_laps=None, session_laps_confirmed=False, session_duration_seconds=5400.0)
    assert prerace_evidence(timed_race, [], {}, "lmu")["race_rules"]["duration_minutes"] == 90.0
    unconfirmed = dict(race, session_laps_confirmed=False, session_duration_seconds=None)
    assert prerace_evidence(unconfirmed, [], {}, "lmu")["race_rules"]["available"] is False


def test_pace_ignores_pit_laps_invalid_times_and_needs_three_clean_laps():
    rows = [lap(1, 100.0), lap(2, 300.0, in_pits=True), lap(3, float("nan")), lap(4, -1.0), lap(5, 101.0)]
    assert prerace_evidence(dict(session_type="Race"), rows, {}, "lmu")["lap_time"]["value"] is None
    rows.append(lap(6, 102.0))
    pace = prerace_evidence(dict(session_type="Race"), rows, {}, "lmu")["lap_time"]
    assert pace["value"] == 101.0 and pace["sample_count"] == 3


def test_ve_evidence_only_for_a_confirmed_ve_car_and_reported_in_percent():
    rows = [lap(n, 100.0, 80.0 - 2 * n, energy_remaining_fraction=1 - 0.03 * n) for n in range(1, 6)]
    ev = prerace_evidence(dict(session_type="Race"), rows, dict(energy_available=True), "lmu")
    assert ev["ve_capable"] is True and ev["ve_per_lap_percent"]["value"] == pytest.approx(3.0)
    unknown = prerace_evidence(dict(session_type="Race"), rows, {}, "lmu")
    assert unknown["ve_capable"] is None and unknown["ve_per_lap_percent"]["value"] is None


def test_two_participants_in_different_splits_cannot_share_evidence():
    split_a = prerace_evidence(dict(session_type="Race"), laps(burn=2.0), dict(fuel_capacity_l=80.0), "lmu")
    split_b = prerace_evidence(dict(session_type="Race"), laps(burn=3.0, time=110.0), dict(fuel_capacity_l=95.0), "lmu")
    assert split_a["fuel_per_lap"]["value"] == pytest.approx(2.0)
    assert split_b["fuel_per_lap"]["value"] == pytest.approx(3.0)
    assert split_a["tank_capacity_l"] == 80.0 and split_b["tank_capacity_l"] == 95.0
    assert split_a["lap_time"]["value"] != split_b["lap_time"]["value"]


def test_qualifying_to_race_transition_keeps_manual_inputs_and_evidence_separate():
    manual = dict(duration_minutes=45.0, lap_time_s=118.0, fuel_per_lap_l=3.1, tank_capacity_l=90.0, ve_capable=False)
    before = plan_prerace(manual, {"duration_minutes": "manual"})
    quali = prerace_evidence(dict(session_type="Qualify", session_laps=None), laps(), {}, "lmu")
    race = prerace_evidence(dict(session_type="Race", session_laps=None, session_laps_confirmed=False,
                                 session_duration_seconds=2700.0), [], {}, "lmu")
    assert quali["race_rules"]["available"] is False and race["race_rules"]["duration_minutes"] == 45.0
    # Evidence never edits the plan; the same manual inputs still produce the same plan in the race session.
    assert plan_prerace(manual, {"duration_minutes": "manual"}) == before
    assert before["sources"] == {"duration_minutes": "manual"}


# ------------------------------------------------------------------ API (needs PostgreSQL)

QUERY = "duration_minutes=60&lap_time_s=120&fuel_per_lap_l=3&tank_capacity_l=100&ve_capable=false"


async def get(path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost") as client:
        return await client.get(path)


async def test_api_plan_without_any_live_session_and_unknown_driver(db):
    agent = await make_agent(db, "idle-driver")
    data = (await get(f"/api/drivers/{agent['slug']}/prerace?{QUERY}")).json()
    assert data["plan"]["status"] == "ready" and data["evidence"] is None and data["context"] is None
    assert (await get(f"/api/drivers/nobody/prerace?{QUERY}")).status_code == 404
    assert (await get(f"/api/drivers/{agent['slug']}/prerace?lap_time_s=abc")).status_code == 422
    nan = await get(f"/api/drivers/{agent['slug']}/prerace?duration_minutes=nan&race_laps=3")
    assert nan.json()["plan"]["status"] == "invalid_input"


async def test_api_two_agents_in_different_splits_stay_isolated_and_nothing_is_written(db):
    a, session_a, _ = await scoring_session(db, "split-a")
    b, session_b, _ = await scoring_session(db, "split-b")
    assert session_a != session_b
    tables = ("laps", "strategy_samples", "sessions")
    async with db.acquire() as conn:
        for agent, session, burn, capacity in ((a, session_a, 2.0, 80.0), (b, session_b, 3.0, 95.0)):
            mine = await conn.fetchval("SELECT id FROM session_participants WHERE session_id=$1 AND driver_id=$2",
                                       session, agent["driver_id"])
            for n in range(1, 7):
                await conn.execute("""INSERT INTO laps(id,session_participant_id,lap_number,lap_time,fuel_remaining_l,
                    pitstops,in_pits) VALUES(gen_random_uuid(),$1,$2,$3,$4,0,false)""", mine, n, 100.0 + n,
                                   70 - burn * n)
            await conn.execute("""INSERT INTO strategy_samples(id,session_participant_id,recorded_at,fuel_l,
                fuel_capacity_l,energy_available) VALUES(gen_random_uuid(),$1,now(),10,$2,false)""", mine, capacity)
        before = [await conn.fetch(f"SELECT * FROM {t} ORDER BY id") for t in tables]
    first = (await get(f"/api/drivers/{a['slug']}/prerace")).json()
    second = (await get(f"/api/drivers/{b['slug']}/prerace")).json()
    assert first["context"]["session_id"] == str(session_a) and second["context"]["session_id"] == str(session_b)
    assert first["evidence"]["fuel_per_lap"]["value"] == pytest.approx(2.0)
    assert second["evidence"]["fuel_per_lap"]["value"] == pytest.approx(3.0)
    assert first["evidence"]["tank_capacity_l"] == 80.0 and second["evidence"]["tank_capacity_l"] == 95.0
    # The suggestion is never applied: without explicit inputs the plan stays incomplete.
    assert first["plan"]["status"] == "incomplete" and first["plan"]["start"] is None
    async with db.acquire() as conn:
        after = [await conn.fetch(f"SELECT * FROM {t} ORDER BY id") for t in tables]
    assert before == after


async def test_pages_embed_the_planner_script_and_leave_no_placeholder():
    for path in ("/drivers/someone/live", f"/sessions/{'0' * 8}-0000-0000-0000-{'0' * 12}/live"):
        body = (await get(path)).text
        assert "const PreRaceUI" in body and "__PRERACE_UI__" not in body
