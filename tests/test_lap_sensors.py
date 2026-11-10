"""Unit tests of the lap sensor provenance rules (#59). Pure functions, no database."""
import json

import pytest

from server.app.lap_sensors import (
    accepted_sensors,
    checkpoint_compatible,
    completion_updates,
    fuel_valid,
    observe_crossing,
    owns_sensor_fields,
    stored_wheels_trusted,
)
from server.app.timing import field_status, wheels_trusted

REAL = [dict(remaining=r, temp_inner=80.0, temp_mid=85.0, temp_outer=90.0, pressure=170.0) for r in (.9, .8, .85, .75)]
PLACEHOLDER = [dict(remaining=1.0)] * 4


def snapshot(**extra):
    return {"has_telemetry": True, "wheels": REAL, "fuel_remaining": 41.5, "fuel_capacity": 100.0, **extra}


def test_ownership_requires_two_resolved_equal_identities():
    assert owns_sensor_fields("a", "a")
    assert not owns_sensor_fields("a", "b") and not owns_sensor_fields("a", None)
    assert not owns_sensor_fields(None, None) and not owns_sensor_fields(None, "a")


@pytest.mark.parametrize("wheels", [
    REAL[:1], REAL[:2], REAL[:3], [], None, PLACEHOLDER,
    [dict(temp_mid=85.0)] + REAL[1:],                      # a corner without remaining
    [dict(remaining=None, temp_mid=85.0)] + REAL[1:],
    [dict(remaining=1.1, temp_mid=85.0)] + REAL[1:], [dict(remaining=-.1, temp_mid=85.0)] + REAL[1:],
    [dict(remaining=float("nan"), temp_mid=85.0)] + REAL[1:],
    [dict(remaining=.5)] * 4,                              # complete remaining but not one sensor value
])
def test_incomplete_or_unproven_tyres_are_never_trusted(wheels):
    assert not wheels_trusted(wheels) and not stored_wheels_trusted(json.dumps(wheels))
    assert accepted_sensors(snapshot(wheels=wheels), True, True) == {"wheels": None, "fuel": None}
    assert field_status("Medium", wheels, None)["tyre_remaining_min"] is None   # display and persistence agree


def test_complete_real_tyres_are_trusted_by_display_and_persistence_alike():
    assert wheels_trusted(REAL) and stored_wheels_trusted(json.dumps(REAL))
    assert field_status("Medium", REAL, None)["tyre_remaining_min"] == .75
    assert wheels_trusted([dict(remaining=0.0, pressure=170.0)] * 4), "0.0 remaining is a real value"


@pytest.mark.parametrize("wheel", [dict(remaining=1.0, pressure=0.0, flat=True), dict(remaining=1.0, pressure=0.0, detached=True),
                                   dict(remaining=1.0, pressure=0.0, temp_inner=None, temp_mid=None, temp_outer=None)])
def test_zero_pressure_with_a_damage_marker_is_not_ownership_evidence(wheel):
    wheels = [wheel] * 4
    assert not wheels_trusted(wheels)
    assert accepted_sensors(snapshot(wheels=wheels, fuel_remaining=50.0), True, True) == {"wheels": None, "fuel": None}
    assert wheels_trusted([dict(wheel, temp_mid=85.0)] * 4), "a real temperature still proves a sensor"


def test_acceptance_needs_owner_crossing_flag_and_real_tyres():
    full = accepted_sensors(snapshot(), True, True)
    assert full["fuel"] == (41.5, 100.0) and len(full["wheels"]) == 4
    for owner, crossing, flag in ((False, True, True), (True, False, True), (True, True, False)):
        assert accepted_sensors(snapshot(has_telemetry=flag), owner, crossing) == {"wheels": None, "fuel": None}
    # Fuel is accepted only together with real tyre data (proof of the agent's own car), never on its own.
    assert accepted_sensors(snapshot(wheels=PLACEHOLDER), True, True) == {"wheels": None, "fuel": None}


@pytest.mark.parametrize("level,capacity,valid", [
    (0.0, 100.0, True), (50.0, 100.0, True), (100.0, 100.0, True), (100.9, 100.0, True),
    (101.5, 100.0, False), (150.0, 100.0, False), (-0.1, 100.0, False), (40.0, 0.0, False), (40.0, -5.0, False),
    (None, 100.0, False), (40.0, None, False), (float("nan"), 100.0, False), (float("inf"), 100.0, False),
    (40.0, float("inf"), False), (True, 100.0, False),
])
def test_fuel_plausibility(level, capacity, valid):
    assert fuel_valid(level, capacity) is valid
    assert (accepted_sensors(snapshot(fuel_remaining=level, fuel_capacity=capacity), True, True)["fuel"] is not None) is valid


def row(**extra):
    return {"lap_time": 90.5, "sector1": 30.0, "sector2": 31.0, "sector3": 29.5, "pitstops": 1, "tyre_snapshot": None,
            "fuel_remaining_l": None, "fuel_capacity_l": None, **extra}


def test_checkpoint_compatibility_requires_the_same_lap_record():
    base = {"last_lap_time": 90.5, "last_sector1": 30.0, "last_sector2": 31.0, "last_sector3": 29.5, "num_pitstops": 1}
    assert checkpoint_compatible(row(), base)
    assert checkpoint_compatible(row(), {**base, "last_lap_time": 90.5003})
    assert checkpoint_compatible(row(sector2=None), {**base, "last_sector2": 31.7}), "unknown on one side is not a conflict"
    for change in ({"last_lap_time": 92.0}, {"last_lap_time": None}, {"last_sector3": 30.5}, {"num_pitstops": 2}):
        assert not checkpoint_compatible(row(), {**base, **change})


def test_completion_fills_only_what_is_missing_and_never_overwrites_valid_values():
    both = accepted_sensors(snapshot(), True, True)
    plan = completion_updates(row(), both)
    assert plan["fuel_remaining_l"] == 41.5 and plan["fuel_capacity_l"] == 100.0 and "tyre_snapshot" in plan
    assert completion_updates(row(tyre_snapshot=json.dumps(REAL), fuel_remaining_l=30.0, fuel_capacity_l=100.0), both) == {}
    # Unusable stored tyres (placeholder, short) are replaced; trusted ones are kept.
    for bad in (json.dumps(PLACEHOLDER), json.dumps(REAL[:1]), "not json", None):
        assert "tyre_snapshot" in completion_updates(row(tyre_snapshot=bad, fuel_remaining_l=30.0, fuel_capacity_l=100.0), both)
    # Missing or unusable capacity next to a valid stored level: only the capacity is completed.
    for capacity in (None, 0.0, -1.0):
        assert completion_updates(row(tyre_snapshot=json.dumps(REAL), fuel_remaining_l=40.0, fuel_capacity_l=capacity), both) \
            == {"fuel_capacity_l": 100.0}
    # ... unless the stored level is inconsistent with the incoming capacity; then the pair is replaced as a whole.
    plan = completion_updates(row(tyre_snapshot=json.dumps(REAL), fuel_remaining_l=140.0, fuel_capacity_l=None), both)
    assert plan == {"fuel_remaining_l": 41.5, "fuel_capacity_l": 100.0}
    # A stored level above its own capacity is unusable and replaced.
    assert completion_updates(row(tyre_snapshot=json.dumps(REAL), fuel_remaining_l=150.0, fuel_capacity_l=100.0), both) \
        == {"fuel_remaining_l": 41.5, "fuel_capacity_l": 100.0}
    # Nothing accepted, nothing written: placeholders and NULL never reach the row.
    assert completion_updates(row(), {"wheels": None, "fuel": None}) == {}


def frame(laps, player=0, clock=100.0, kind="Race", other=3):
    return {"player_vehicle_id": player, "session_type": kind, "session_time": clock,
            "vehicles": [{"id": 0, "total_laps": laps}, {"id": 1, "total_laps": other}]}


def test_crossing_is_remembered_only_for_an_observed_increment_without_discontinuity():
    seen = {}
    observe_crossing(seen, {}, None, frame(4), 10.0)
    assert seen == {}                                                   # first frame: nothing observed yet
    observe_crossing(seen, frame(4), 10.0, frame(4, clock=101), 10.5)
    assert seen == {}
    observe_crossing(seen, frame(4, clock=101), 10.5, frame(5, clock=102), 11.0)
    assert seen == {0: [5]}
    observe_crossing(seen, frame(5, clock=102), 11.0, frame(6, clock=103), 11.5)
    assert seen == {0: [5, 6]}
    for n in range(7, 12):
        observe_crossing(seen, frame(n - 1, clock=100 + n), 12.0, frame(n, clock=101 + n), 12.5)
    assert seen[0] == [8, 9, 10, 11], "bounded memory"


@pytest.mark.parametrize("case", ["gap", "clock_back", "other_player", "other_type", "jump", "lap_back", "unknown_player"])
def test_any_discontinuity_forgets_observed_crossings(case):
    seen = {0: [5]}
    previous, current, gap = frame(5, clock=110), frame(6, clock=111), 0.5
    if case == "gap":
        gap = 6.0
    elif case == "clock_back":
        current = frame(6, clock=50)
    elif case == "other_player":
        current = frame(6, player=1, clock=111)
    elif case == "other_type":
        current = frame(6, kind="Qualifying", clock=111)
    elif case == "jump":
        current = frame(7, clock=111)
    elif case == "lap_back":
        current = frame(4, clock=111)
    else:
        previous, current = frame(5, player=-1, clock=110), frame(6, player=-1, clock=111)
    observe_crossing(seen, previous, 20.0, current, 20.0 + gap)
    assert seen == {}
