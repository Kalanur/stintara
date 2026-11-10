"""Lap sensor provenance against PostgreSQL (#59). Synthetic data only.

Agent "owner" drives car 0, agent "observer" (another driver) drives car 1 and merely sees car 0. Pitwall reports
placeholders (wear 1.0, no sensors, a default fuel level) for every car except the one the agent drives, and it
freezes a snapshot per lap. An agent that connects mid-lap gets a first-seen snapshot with later readings under the
same lap number; the server accepts sensor values only for laps whose start it watched.
"""
import asyncio
import json

import pytest

from server.app import main
from tests.conftest import make_agent
from tests.test_convergence import deliver, roster, scoring

REAL = [dict(remaining=r, temp_inner=80.0, temp_mid=85.0, temp_outer=90.0, pressure=170.0) for r in (.9, .8, .85, .75)]
PLACEHOLDER = [dict(remaining=1.0)] * 4
clock = iter(range(1000, 100000))


def checkpoint(car, lap=5, wheels=REAL, fuel=(41.5, 100.0), telemetry=True, lap_time=90.5, stops=1, **extra):
    snap = dict(id=car, total_laps=lap, last_lap_time=lap_time, last_sector1=30.0, last_sector2=31.0, last_sector3=29.5,
                position=car + 1, num_pitstops=stops, has_telemetry=telemetry, tire_compound_front_name="Medium",
                wheels=wheels, fuel_remaining=fuel[0] if fuel else None, fuel_capacity=fuel[1] if fuel else None,
                damage={"dent_severity": [0, 1]}, **extra)
    return dict(session_type="Race", session_time=100, drivers=[snap])


async def send(agent, payload):
    # The envelope clock must match the agent's scoring clock (server-side 5 s plausibility check).
    payload["session_time"] = main.runtime[str(agent["id"])]["scoring"]["session_time"]
    await main.handle_message(agent, "lmu", "lap_snapshots", payload)


async def frame(agent, player, laps):
    """One scoring frame of the agent whose player car (and only that car) has completed `laps` laps."""
    payload = scoring(roster(), kind="Race", player=player, clock=next(clock))
    for vehicle in payload["vehicles"]:
        vehicle["total_laps"] = laps if vehicle["id"] == player else 0
    await main.handle_message(agent, "lmu", "scoring", payload)


async def start_lap(agent, player, lap):
    """The agent watches its car cross into `lap` (lap-1 -> lap completed laps)."""
    await frame(agent, player, lap - 1)
    await frame(agent, player, lap)


async def field(db, track="Synthetic Circuit"):
    owner, observer = await make_agent(db, "owner"), await make_agent(db, "observer")
    sid = await deliver(owner, scoring(roster(), kind="Race", player=0), track=track)
    assert await deliver(observer, scoring(roster(), kind="Race", player=1), track=track) == sid
    return owner, observer, sid


async def lap_row(db, sid, car, lap=5):
    async with db.acquire() as conn:
        return await conn.fetchrow("""SELECT l.* FROM laps l JOIN session_participants p ON p.id=l.session_participant_id
            WHERE p.session_id=$1 AND p.local_vehicle_id=$2 AND l.lap_number=$3""", sid, car, lap)


async def flag(db, sid, car):
    async with db.acquire() as conn:
        return await conn.fetchval("SELECT has_telemetry FROM session_participants WHERE session_id=$1 "
                                   "AND local_vehicle_id=$2", sid, car)


def assert_owner_values(row):
    assert [row[f"tyre_remaining_{c}"] for c in ("fl", "fr", "rl", "rr")] == pytest.approx([.9, .8, .85, .75])
    assert (row["fuel_remaining_l"], row["fuel_capacity_l"], row["has_telemetry"]) == (41.5, 100.0, True)


def assert_unknown(row):
    assert all(row[f"tyre_remaining_{c}"] is None for c in ("fl", "fr", "rl", "rr"))
    assert row["tyre_snapshot"] is None and row["fuel_remaining_l"] is None and row["fuel_capacity_l"] is None
    assert row["has_telemetry"] is False


@pytest.mark.parametrize("order", ["owner_first", "observer_first"])
async def test_both_arrival_orders_end_with_the_owners_values_and_shared_facts(db, order):
    owner, observer, sid = await field(db)
    await start_lap(owner, 0, 5)
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
    assert_unknown(row)
    assert (row["lap_time"], row["sector2"], row["compound_front"], row["position"]) == (90.5, 31.0, "Medium", 1)
    assert await flag(db, sid, 0) is False
    await start_lap(observer, 1, 5)
    await send(observer, checkpoint(0, lap=6, wheels=REAL))                  # sensor-looking values, wrong agent
    assert_unknown(await lap_row(db, sid, 0, 6))


async def test_first_seen_snapshot_without_an_observed_lap_start_gets_no_sensor_values(db):
    owner, observer, sid = await field(db)
    await frame(owner, 0, 4)                              # the agent connected mid-lap: lap 4 completed, start unseen
    await frame(owner, 0, 4)
    await send(owner, checkpoint(0, lap=4))               # first-seen snapshot with later readings, same lap number
    assert_unknown(await lap_row(db, sid, 0, 4))
    await frame(owner, 0, 5)                              # now the start of lap 5 is watched
    await send(owner, checkpoint(0, lap=5))
    assert_owner_values(await lap_row(db, sid, 0))
    await send(owner, checkpoint(0, lap=4))               # the unobserved lap stays unknown even when repeated
    assert_unknown(await lap_row(db, sid, 0, 4))
    # Observer first, owner connected mid-lap: the observer's row is not completed from the first-seen snapshot ...
    await send(observer, checkpoint(0, lap=7, wheels=PLACEHOLDER))
    await send(owner, checkpoint(0, lap=7))
    assert_unknown(await lap_row(db, sid, 0, 7))
    # ... but is when the owner watched the lap start before the checkpoint repeats.
    await start_lap(owner, 0, 7)
    await send(owner, checkpoint(0, lap=7))
    assert_owner_values(await lap_row(db, sid, 0, 7))


async def test_scoring_silence_forgets_observed_laps(db):
    owner, observer, sid = await field(db)
    await start_lap(owner, 0, 5)
    main.runtime[str(owner["id"])]["last_scoring_at"] -= 30     # Pitwall restart: no scoring for a while
    await frame(owner, 0, 5)
    await send(owner, checkpoint(0))
    assert_unknown(await lap_row(db, sid, 0))


@pytest.mark.parametrize("wheels", [REAL[:1], REAL[:2], REAL[:3], [dict(temp_mid=85.0)] + REAL[1:],
                                    [dict(remaining=None, temp_mid=85.0)] + REAL[1:], [dict(remaining=.5)] * 4])
async def test_incomplete_tyres_never_block_a_later_complete_set(db, wheels):
    owner, observer, sid = await field(db)
    await start_lap(owner, 0, 5)
    await send(owner, checkpoint(0, wheels=wheels, fuel=None))
    row = await lap_row(db, sid, 0)
    assert row["tyre_snapshot"] is None and row["tyre_remaining_fl"] is None and row["has_telemetry"] is False
    await send(owner, checkpoint(0))                                       # the complete set of the same checkpoint
    assert_owner_values(await lap_row(db, sid, 0))


async def test_valid_matched_tyres_are_never_replaced(db):
    owner, observer, sid = await field(db)
    await start_lap(owner, 0, 5)
    await send(owner, checkpoint(0))
    before = dict(await lap_row(db, sid, 0))
    for payload in (checkpoint(0), checkpoint(0, wheels=PLACEHOLDER, fuel=None), checkpoint(0, telemetry=False, wheels=[]),
                    checkpoint(0, wheels=[dict(remaining=.1, temp_inner=20.0)] * 4, fuel=(5.0, 100.0)),
                    checkpoint(0, wheels=REAL[:1], fuel=(5.0, 100.0))):
        await send(owner, payload)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER, fuel=(50.0, 100.0)))
    assert dict(await lap_row(db, sid, 0)) == before


@pytest.mark.parametrize("change", [dict(lap_time=92.0), dict(lap_time=None), dict(last_sector1=31.5), dict(stops=2)])
async def test_a_contradicting_checkpoint_of_the_same_lap_number_is_not_combined(db, change):
    owner, observer, sid = await field(db)
    await start_lap(owner, 0, 5)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    snap = checkpoint(0, **{k: v for k, v in change.items() if k != "last_sector1"})
    if "last_sector1" in change:
        snap["drivers"][0]["last_sector1"] = change["last_sector1"]
    await send(owner, snap)
    row = await lap_row(db, sid, 0)
    assert_unknown(row)
    assert (row["lap_time"], row["sector1"], row["pitstops"]) == (90.5, 30.0, 1), "first writer's facts stay"
    await send(owner, checkpoint(0))                                       # the matching checkpoint still completes
    assert_owner_values(await lap_row(db, sid, 0))


async def test_owner_repairs_rows_with_placeholder_tyres_but_keeps_valid_fuel(db):
    owner, observer, sid = await field(db)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    async with db.acquire() as conn:   # a row as written before this change: placeholder tyres, plausible fuel
        await conn.execute("""UPDATE laps SET tyre_snapshot=$1::jsonb,tyre_remaining_fl=1,tyre_remaining_fr=1,
            tyre_remaining_rl=1,tyre_remaining_rr=1,fuel_remaining_l=60,fuel_capacity_l=120,has_telemetry=true""",
                           json.dumps(PLACEHOLDER))
    await start_lap(owner, 0, 5)
    await send(owner, checkpoint(0))
    row = await lap_row(db, sid, 0)
    assert [row[f"tyre_remaining_{c}"] for c in ("fl", "fr", "rl", "rr")] == pytest.approx([.9, .8, .85, .75])
    assert (row["fuel_remaining_l"], row["fuel_capacity_l"]) == (60, 120), "a valid stored fuel pair is never replaced"


@pytest.mark.parametrize("capacity", [None, 0.0])
async def test_missing_or_zero_capacity_is_completed_next_to_a_valid_level(db, capacity):
    owner, observer, sid = await field(db)
    await start_lap(owner, 0, 5)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    async with db.acquire() as conn:
        await conn.execute("UPDATE laps SET fuel_remaining_l=40, fuel_capacity_l=$1", capacity)
    await send(owner, checkpoint(0, fuel=(40.0, 100.0)))
    row = await lap_row(db, sid, 0)
    assert (row["fuel_remaining_l"], row["fuel_capacity_l"]) == (40, 100)


@pytest.mark.parametrize("level,capacity,stored", [(0.0, 100.0, True), (50.0, 100.0, True), (100.0, 100.0, True),
                                                   (150.0, 100.0, False), (40.0, 0.0, False), (40.0, None, False),
                                                   (-1.0, 100.0, False)])
async def test_fuel_plausibility_when_persisting(db, level, capacity, stored):
    owner, observer, sid = await field(db)
    await start_lap(owner, 0, 5)
    await send(owner, checkpoint(0, fuel=(level, capacity)))
    row = await lap_row(db, sid, 0)
    assert row["tyre_snapshot"] is not None, "tyres are independent of the fuel pair"
    assert (row["fuel_remaining_l"] == level) is stored
    if not stored:
        assert row["fuel_remaining_l"] is None and row["fuel_capacity_l"] is None


async def test_concurrent_writers_converge_on_one_row_with_owner_values(db):
    owner, observer, sid = await field(db)
    for lap in range(1, 6):
        await start_lap(owner, 0, lap)
        await asyncio.gather(send(observer, checkpoint(0, lap=lap, wheels=PLACEHOLDER, fuel=(50.0, 100.0))),
                             send(owner, checkpoint(0, lap=lap)), send(observer, checkpoint(0, lap=lap, wheels=PLACEHOLDER)))
    for lap in range(1, 6):
        assert_owner_values(await lap_row(db, sid, 0, lap))
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM laps") == 5


async def test_completions_queued_on_one_row_lock_serialise_and_stay_consistent(db):
    owner, observer, sid = await field(db)
    await start_lap(owner, 0, 5)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    async with db.acquire() as holder:
        transaction = holder.transaction()
        await transaction.start()
        await holder.fetchrow("SELECT 1 FROM laps WHERE lap_number=5 FOR UPDATE")
        # Two owner checkpoints (one complete, one contradicting) queue up behind the held row lock.
        first = asyncio.create_task(send(owner, checkpoint(0)))
        second = asyncio.create_task(send(owner, checkpoint(0, lap_time=93.0, fuel=(7.0, 100.0))))
        third = asyncio.create_task(send(owner, checkpoint(0)))
        await asyncio.sleep(.3)
        assert not (first.done() and second.done() and third.done()), "writers wait for the lock"
        await transaction.commit()
    await asyncio.gather(first, second, third)
    assert_owner_values(await lap_row(db, sid, 0))
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM laps") == 1


async def test_reconnects_keep_provenance_from_the_database_and_forget_unseen_laps(db):
    owner, observer, sid = await field(db)
    await start_lap(owner, 0, 5)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    main.runtime.clear()                                    # server restart or lost sockets
    assert await deliver(owner, scoring(roster(), kind="Race", player=0, clock=next(clock))) == sid
    await send(owner, checkpoint(0))                        # lap 5 start was watched before the restart, not after
    assert_unknown(await lap_row(db, sid, 0))
    await start_lap(owner, 0, 6)
    assert await deliver(observer, scoring(roster(), kind="Race", player=1, clock=next(clock))) == sid
    await send(observer, checkpoint(0, lap=6, wheels=PLACEHOLDER))
    await send(owner, checkpoint(0, lap=6))
    assert_owner_values(await lap_row(db, sid, 0, 6))
    main.runtime.clear()
    assert await deliver(observer, scoring(roster(), kind="Race", player=1, clock=next(clock))) == sid
    await send(observer, checkpoint(0, lap=6, wheels=PLACEHOLDER, fuel=(50.0, 100.0)))
    assert_owner_values(await lap_row(db, sid, 0, 6))
    await send(observer, checkpoint(0, lap=7, wheels=PLACEHOLDER))
    assert_unknown(await lap_row(db, sid, 0, 7))


async def test_unresolved_participant_never_gets_sensor_values(db):
    owner, observer, sid = await field(db)
    await start_lap(owner, 0, 5)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT driver_id FROM session_participants WHERE local_vehicle_id=5") is None
    for agent in (owner, observer):
        await send(agent, checkpoint(5, wheels=REAL, fuel=(20.0, 100.0)))
    row = await lap_row(db, sid, 5)
    assert_unknown(row)
    assert (row["lap_time"], row["compound_front"]) == (90.5, "Medium")


async def test_other_drivers_agent_never_completes_a_car_it_does_not_own(db):
    owner, observer, sid = await field(db)
    await start_lap(observer, 1, 5)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    await send(observer, checkpoint(0, wheels=REAL, fuel=(41.5, 100.0)))      # same lap, plausible values, wrong owner
    assert_unknown(await lap_row(db, sid, 0))
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT driver_id FROM session_participants WHERE local_vehicle_id=0") == owner["driver_id"]


async def test_stale_player_vehicle_id_cannot_turn_foreign_placeholders_into_owner_data(db):
    """Frame sequence reported by the review: player_vehicle_id 1 (stale), then the real 0."""
    agent = await make_agent(db, "stale-owner")
    sid = await deliver(agent, scoring(roster(), kind="Race", player=1, clock=100))
    main.runtime[str(agent["id"])].pop("last_persisted_scoring")      # production persists identity at 1 Hz
    await main.handle_message(agent, "lmu", "scoring", scoring(roster(), kind="Race", player=0, clock=101))
    async with db.acquire() as conn:   # the existing COALESCE mapping keeps the stale slot: two slots, one driver
        owned = {r["local_vehicle_id"] for r in await conn.fetch(
            "SELECT local_vehicle_id FROM session_participants WHERE driver_id=$1", agent["driver_id"])}
    assert owned == {0, 1}
    await start_lap(agent, 0, 5)
    # Slot 1 is a foreign car: placeholders with the default fuel, even if the stale frames also "saw" laps of it.
    await send(agent, checkpoint(1, wheels=PLACEHOLDER, fuel=(50.0, 100.0)))
    await send(agent, checkpoint(1, lap=6, wheels=PLACEHOLDER, fuel=(50.0, 100.0)))
    for lap in (5, 6):
        assert_unknown(await lap_row(db, sid, 1, lap))
    assert await flag(db, sid, 1) is False
    # The real own car still works (complete real tyres are the proof of ownership).
    await send(agent, checkpoint(0))
    assert_owner_values(await lap_row(db, sid, 0))
    # The correct driver of slot 1 cannot take the slot over: the existing identity rules refuse a different
    # driver on a resolved slot (no session is resolved for that agent). Re-assigning participants would be an
    # identity decision, deliberately not made here (docs/ARCHITECTURE.md).
    other = await make_agent(db, "real-driver-of-slot-1")
    now = main.runtime[str(agent["id"])]["scoring"]["session_time"]
    assert await deliver(other, scoring(roster(), kind="Race", player=1, clock=now)) != sid
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT driver_id FROM session_participants WHERE session_id=$1 "
                                   "AND local_vehicle_id=1", sid) == agent["driver_id"]


async def test_separate_splits_of_one_event_on_the_same_track_stay_separate(db):
    owner, observer, first = await field(db, track="Synthetic Circuit")
    other = await make_agent(db, "other-split")
    roster_two = roster()
    for vehicle in roster_two:
        vehicle["driver_name"] = f"Other Split Driver {vehicle['id']}"
    second = await deliver(other, scoring(roster_two, kind="Race", player=0), track="Synthetic Circuit")
    assert second != first                                    # conflicting rosters: no convergence on one track
    await start_lap(owner, 0, 5)
    await send(owner, checkpoint(0))
    assert_owner_values(await lap_row(db, first, 0))
    assert await lap_row(db, second, 0) is None
    await start_lap(other, 0, 5)
    await send(other, checkpoint(0, wheels=REAL[:1] * 4, fuel=(10.0, 80.0), lap_time=95.0))
    mine = await lap_row(db, second, 0)
    assert (mine["lap_time"], mine["fuel_remaining_l"]) == (95.0, 10.0)
    assert_owner_values(await lap_row(db, first, 0))          # the first split is untouched
    # The observer of split one cannot reach split two's participant and vice versa.
    await send(observer, checkpoint(0, wheels=PLACEHOLDER, fuel=(50.0, 100.0)))
    assert (await lap_row(db, second, 0))["fuel_remaining_l"] == 10.0


async def test_damage_handling_is_unchanged_experimental_and_observer_independent(db):
    owner, observer, sid = await field(db)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    assert json.loads((await lap_row(db, sid, 0))["damage"]) == {"dent_severity": [0, 1]}
    await start_lap(owner, 0, 5)
    await send(owner, checkpoint(0))
    assert json.loads((await lap_row(db, sid, 0))["damage"]) == {"dent_severity": [0, 1]}


async def test_has_telemetry_contract_per_lap_and_per_participant(db):
    """Lap: trusted sensor group accepted for that lap. Participant: trusted own-agent data was ever accepted."""
    owner, observer, sid = await field(db)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    assert await flag(db, sid, 0) is False and (await lap_row(db, sid, 0))["has_telemetry"] is False
    await start_lap(owner, 0, 5)
    await send(owner, checkpoint(0))
    assert await flag(db, sid, 0) is True and (await lap_row(db, sid, 0))["has_telemetry"] is True
    await send(observer, checkpoint(0, lap=6, wheels=PLACEHOLDER, fuel=(50.0, 100.0)))
    assert await flag(db, sid, 0) is True, "a non-owner observation cannot change the flag"
    await start_lap(owner, 0, 7)
    await send(owner, checkpoint(0, lap=7, wheels=PLACEHOLDER, fuel=None))
    assert await flag(db, sid, 0) is True, "later placeholders do not reset it: it is not a freshness flag"
    assert (await lap_row(db, sid, 0, 7))["has_telemetry"] is False, "per lap: nothing was accepted for lap 7"
    assert await flag(db, sid, 1) is False
    # Consumers: the API exposes the participant flag as stored.
    live = await main.api_session_live(sid)
    assert {p["local_vehicle_id"]: p["has_telemetry"] for p in live["participants"]}[0] is True


async def test_a_failure_between_insert_and_completion_is_repaired_by_the_next_checkpoint(db, monkeypatch):
    owner, observer, sid = await field(db)
    await start_lap(owner, 0, 5)
    await send(observer, checkpoint(0, wheels=PLACEHOLDER))
    real = main.complete_lap_sensors

    async def failing(*args, **kwargs):
        raise RuntimeError("connection lost")
    monkeypatch.setattr(main, "complete_lap_sensors", failing)
    with pytest.raises(RuntimeError):
        await send(owner, checkpoint(0))
    row = await lap_row(db, sid, 0)
    assert_unknown(row)
    assert (row["lap_time"], row["sector1"]) == (90.5, 30.0)
    monkeypatch.setattr(main, "complete_lap_sensors", real)
    await send(owner, checkpoint(0))                                       # Pitwall repeats the frozen snapshot
    assert_owner_values(await lap_row(db, sid, 0))
