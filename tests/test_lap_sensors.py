"""Provenance of tyre/fuel sensor fields in persisted lap checkpoints (#59). Synthetic data only.

Agent "owner" drives car 0, agent "observer" (another driver) drives car 1 and merely sees car 0. Pitwall reports
placeholders (wear 1.0, no sensors, default fuel) for every car except the one the observing agent drives.
"""
import asyncio
import json

import pytest

from server.app import main
from server.app.lap_sensors import accepted_sensors, owns_sensor_fields, stored_wheels_have_sensors
from tests.conftest import make_agent
from tests.test_convergence import deliver, roster, scoring

REAL = [dict(remaining=r, temp_inner=80.0, temp_mid=85.0, temp_outer=90.0, pressure=170.0) for r in (.9, .8, .85, .75)]
PLACEHOLDER = [dict(remaining=1.0)] * 4          # what the adapter hands over for a car without real tyre data


def checkpoint(car, lap=5, wheels=REAL, fuel=(41.5, 100.0), telemetry=True, **extra):
    snap = dict(id=car, total_laps=lap, last_lap_time=90.5, last_sector1=30.0, last_sector2=31.0, last_sector3=29.5,
                position=car + 1, num_pitstops=1, has_telemetry=telemetry, tire_compound_front_name="Medium",
                wheels=wheels, fuel_remaining=fuel[0] if fuel else None, fuel_capacity=fuel[1] if fuel else None,
                damage={"dent_severity": [0, 1]}, **extra)
    return dict(session_type="Race", session_time=100, drivers=[snap])


async def send(agent, payload):
    await main.handle_message(agent, "lmu", "lap_snapshots", payload)


async def field(db, track="Synthetic Circuit"):
    owner, observer = await make_agent(db, "owner"), await make_agent(db, "observer")
    sid = await deliver(owner, scoring(roster(), kind="Race", player=0), track=track)
    assert await deliver(observer, scoring(roster(), kind="Race", player=1), track=track) == sid
    return owner, observer, sid


async def lap_row(db, sid, car, lap=5):
    async with db.acquire() as conn:
        return await conn.fetchrow("""SELECT l.* FROM laps l JOIN session_participants p ON p.id=l.session_participant_id
            WHERE p.session_id=$1 AND p.local_vehicle_id=$2 AND l.lap_number=$3""", sid, car, lap)


def assert_owner_values(row):
    assert [row[f"tyre_remaining_{c}"] for c in ("fl", "fr", "rl", "rr")] == pytest.approx([.9, .8, .85, .75])
    assert (row["fuel_remaining_l"], row["fuel_capacity_l"], row["has_telemetry"]) == (41.5, 100.0, True)
    assert stored_wheels_have_sensors(row["tyre_snapshot"])


def assert_placeholder_free(row):
    assert all(row[f"tyre_remaining_{c}"] is None for c in ("fl", "fr", "rl", "rr"))
    assert row["tyre_snapshot"] is None and row["fuel_remaining_l"] is None and row["fuel_capacity_l"] is None
    assert row["has_telemetry"] is False


def test_ownership_requires_two_resolved_equal_identities():
    assert owns_sensor_fields("a", "a")
    assert not owns_sensor_fields("a", "b") and not owns_sensor_fields("a", None)
    assert not owns_sensor_fields(None, None) and not owns_sensor_fields(None, "a")


def test_accepted_sensors_need_ownership_flag_and_real_values():
    snap = checkpoint(0)["drivers"][0]
    assert accepted_sensors(snap, True)["fuel"] == (41.5, 100.0) and accepted_sensors(snap, True)["wheels"] is not None
    assert accepted_sensors(snap, False) == {"wheels": None, "fuel": None}
    assert accepted_sensors({**snap, "has_telemetry": False}, True) == {"wheels": None, "fuel": None}
    assert accepted_sensors({**snap, "wheels": PLACEHOLDER}, True)["wheels"] is None     # wear 1.0 alone proves nothing
    assert accepted_sensors({**snap, "wheels": REAL[:2]}, True)["wheels"] is not None   # short lists are padded
    for level, capacity in ((None, 100.0), (-1.0, 100.0), (40.0, 0.0), (float("nan"), 100.0), (40.0, None)):
        assert accepted_sensors({**snap, "fuel_remaining": level, "fuel_capacity": capacity}, True)["fuel"] is None
    # No heuristic on the level: exactly half a tank from the owner is a measurement.
    assert accepted_sensors({**snap, "fuel_remaining": 50.0, "fuel_capacity": 100.0}, True)["fuel"] == (50.0, 100.0)


@pytest.mark.parametrize("order", ["owner_first", "observer_first"])
async def test_both_arrival_orders_end_with_the_owners_values_and_shared_facts(db, order):
    owner, observer, sid = await field(db)
    seen = checkpoint(0, wheels=PLACEHOLDER, fuel=(50.0, 100.0))          # the observer's placeholder view of car 0
    own = checkpoint(0)
    first, second = ((owner, own), (observer, seen)) if order == "owner_first" else ((observer, seen), (owner, own))
    await send(*first)
    await send(*second)
    row = await lap_row(db, sid, 0)
    assert_owner_values(row)
    assert (row["lap_time"], row["sector1"], row["sector3"], row["position"], row["pitstops"]) == (90.5, 30.0, 29.5, 1, 1)
    assert row["compound_front"] == "Medium"
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM laps") == 1


async def test_observer_alone_persists_facts_but_no_placeholder_sensors(db):
    owner, observer, sid = await field(db)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER, fuel=(50.0, 100.0)))
    row = await lap_row(db, sid, 0)
    assert_placeholder_free(row)
    assert (row["lap_time"], row["sector2"], row["compound_front"], row["position"]) == (90.5, 31.0, "Medium", 1)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT has_telemetry FROM session_participants WHERE local_vehicle_id=0") is False
    # Even sensor-looking values from a non-owner are not accepted, and the other car is its owner's data.
    await send(observer, checkpoint(0, lap=6, wheels=REAL))
    assert_placeholder_free(await lap_row(db, sid, 0, 6))
    await send(observer, checkpoint(1, lap=5))
    assert_owner_values(await lap_row(db, sid, 1))


async def test_owner_telemetry_is_idempotent_and_never_overwritten_by_null_or_placeholders(db):
    owner, observer, sid = await field(db)
    await send(owner, checkpoint(0))
    before = dict(await lap_row(db, sid, 0))
    await send(owner, checkpoint(0))                                                    # same checkpoint again
    await send(owner, checkpoint(0, wheels=PLACEHOLDER, fuel=None))                     # garage-like placeholder
    await send(owner, checkpoint(0, telemetry=False, wheels=[]))
    await send(owner, checkpoint(0, wheels=[dict(remaining=.1, temp_inner=20.0)] * 4, fuel=(5.0, 100.0)))  # valid but later
    await send(observer, checkpoint(0, wheels=PLACEHOLDER, fuel=(50.0, 100.0)))
    assert dict(await lap_row(db, sid, 0)) == before


async def test_owner_repairs_only_missing_or_unusable_values(db):
    owner, observer, sid = await field(db)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    async with db.acquire() as conn:   # a row as written before this fix: placeholder tyres, plausible-looking fuel
        await conn.execute("""UPDATE laps SET tyre_snapshot=$1::jsonb,tyre_remaining_fl=1,tyre_remaining_fr=1,
            tyre_remaining_rl=1,tyre_remaining_rr=1,fuel_remaining_l=60,fuel_capacity_l=120,has_telemetry=true""",
                           json.dumps(PLACEHOLDER))
    await send(owner, checkpoint(0))
    row = await lap_row(db, sid, 0)
    assert [row[f"tyre_remaining_{c}"] for c in ("fl", "fr", "rl", "rr")] == pytest.approx([.9, .8, .85, .75])
    assert (row["fuel_remaining_l"], row["fuel_capacity_l"]) == (60, 120), "a stored fuel value is never replaced"
    # Fuel arrives later than tyres: a missing fuel value is completed, valid tyres stay.
    await send(owner, checkpoint(0, lap=6, fuel=None))
    await send(observer, checkpoint(0, lap=6, wheels=PLACEHOLDER, fuel=(50.0, 100.0)))
    partial = await lap_row(db, sid, 0, 6)
    assert partial["fuel_remaining_l"] is None and partial["has_telemetry"] is True
    await send(owner, checkpoint(0, lap=6, wheels=REAL[:1] * 4, fuel=(33.0, 100.0)))
    done = await lap_row(db, sid, 0, 6)
    assert done["fuel_remaining_l"] == 33.0
    assert done["tyre_remaining_fl"] == pytest.approx(.9) and done["tyre_remaining_rr"] == pytest.approx(.75), "the first valid tyres are kept"


async def test_concurrent_writers_converge_on_one_row_with_owner_values(db):
    owner, observer, sid = await field(db)
    for lap in range(1, 9):
        await asyncio.gather(send(observer, checkpoint(0, lap=lap, wheels=PLACEHOLDER, fuel=(50.0, 100.0))),
                             send(owner, checkpoint(0, lap=lap)), send(observer, checkpoint(0, lap=lap, wheels=PLACEHOLDER)))
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM laps l JOIN session_participants p ON p.id=l.session_participant_id "
                                   "WHERE p.local_vehicle_id=0") == 8
    for lap in range(1, 9):
        assert_owner_values(await lap_row(db, sid, 0, lap))


async def test_reconnects_keep_provenance_from_the_database_not_runtime_state(db):
    owner, observer, sid = await field(db)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    main.runtime.clear()                                    # server restart or lost sockets
    assert await deliver(owner, scoring(roster(), kind="Race", player=0, clock=101)) == sid
    await send(owner, checkpoint(0))
    assert_owner_values(await lap_row(db, sid, 0))
    main.runtime.clear()
    assert await deliver(observer, scoring(roster(), kind="Race", player=1, clock=102)) == sid
    await send(observer, checkpoint(0, wheels=PLACEHOLDER, fuel=(50.0, 100.0)))
    assert_owner_values(await lap_row(db, sid, 0))
    await send(observer, checkpoint(0, lap=6, wheels=PLACEHOLDER))
    assert_placeholder_free(await lap_row(db, sid, 0, 6))


async def test_unresolved_participant_never_gets_sensor_values(db):
    owner, observer, sid = await field(db)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT driver_id FROM session_participants WHERE local_vehicle_id=5") is None
    for agent in (owner, observer):
        await send(agent, checkpoint(5, wheels=REAL, fuel=(20.0, 100.0)))
    row = await lap_row(db, sid, 5)
    assert_placeholder_free(row)
    assert (row["lap_time"], row["compound_front"]) == (90.5, "Medium")
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT driver_id FROM session_participants WHERE local_vehicle_id=5") is None


async def test_other_drivers_agent_never_completes_a_car_it_does_not_own(db):
    owner, observer, sid = await field(db)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    await send(observer, checkpoint(0, wheels=REAL, fuel=(41.5, 100.0)))      # same lap, plausible values, wrong owner
    assert_placeholder_free(await lap_row(db, sid, 0))
    async with db.acquire() as conn:
        mine = await conn.fetchval("SELECT driver_id FROM session_participants WHERE local_vehicle_id=0")
    assert mine == owner["driver_id"] != observer["driver_id"]


async def test_separate_races_with_the_same_local_vehicle_ids_stay_separate(db):
    owner, observer, first = await field(db, track="Synthetic Circuit")
    other = await make_agent(db, "other-split")
    second = await deliver(other, scoring(roster(), kind="Race", player=0), track="Another Synthetic Circuit")
    assert second != first
    await send(owner, checkpoint(0))
    assert_owner_values(await lap_row(db, first, 0))
    assert await lap_row(db, second, 0) is None
    await send(other, checkpoint(0, wheels=REAL[:2] * 2, fuel=(10.0, 80.0)))
    assert (await lap_row(db, second, 0))["fuel_remaining_l"] == 10.0
    assert_owner_values(await lap_row(db, first, 0))                       # the first race is untouched
    async with db.acquire() as conn:   # one driver in two races: each participant keeps its own checkpoints
        assert await conn.fetchval("SELECT count(DISTINCT session_id) FROM session_participants WHERE driver_id=$1",
                                   other["driver_id"]) == 1


async def test_damage_handling_is_unchanged_experimental_and_observer_independent(db):
    owner, observer, sid = await field(db)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    assert json.loads((await lap_row(db, sid, 0))["damage"]) == {"dent_severity": [0, 1]}
    await send(owner, checkpoint(0, **{}))
    assert json.loads((await lap_row(db, sid, 0))["damage"]) == {"dent_severity": [0, 1]}


async def test_participant_has_telemetry_follows_accepted_owner_telemetry_only(db):
    owner, observer, sid = await field(db)

    async def flag(car):
        async with db.acquire() as conn:
            return await conn.fetchval("SELECT has_telemetry FROM session_participants WHERE session_id=$1 "
                                       "AND local_vehicle_id=$2", sid, car)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    assert await flag(0) is False
    await send(owner, checkpoint(0))
    assert await flag(0) is True
    await send(observer, checkpoint(0, lap=6, wheels=PLACEHOLDER, fuel=(50.0, 100.0)))
    assert await flag(0) is True, "a non-owner observation cannot flip the flag"
    await send(owner, checkpoint(0, lap=7, wheels=PLACEHOLDER, fuel=None))
    assert await flag(0) is False, "the owner's own snapshot without usable telemetry is accepted state too"
