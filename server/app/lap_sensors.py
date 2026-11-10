"""Provenance and quality of player-only lap sensor fields (#59).

LMU only delivers real tyre and fuel telemetry for the car the observing agent drives; for every other car the
provider reports placeholders (wear 1.0 without sensors, a default fuel level). A lap checkpoint's tyre and fuel
values are therefore persisted only when ALL of the following hold:

* ownership: the writing agent's server-side driver is the participant's resolved driver, AND the snapshot itself
  carries complete, real tyre telemetry (the physical fingerprint of the agent's own car). A stale or duplicated
  `player_vehicle_id` mapping can attach a driver to a wrong slot, but never produces real tyre sensors there;
* checkpoint time: the agent's own scoring stream was observed crossing into that lap (see `observe_crossing`).
  Pitwall freezes a snapshot per lap, but also builds one when it first sees a car (connect or restart mid-lap),
  carrying later readings under the same lap number; those cannot be told apart from the snapshot alone, so a
  lap whose start was not observed gets no sensor values (fail closed);
* quality: complete four-corner tyres; fuel level 0..capacity (small tolerance) with a positive capacity.

Everything else a checkpoint carries (timing, sectors, position, pit state, compound) stays open to every
observer. Damage is deliberately not handled here (experimental, unchanged).
"""
import json

from .timing import finite_number, wheels_trusted

FUEL_OVER_CAPACITY_TOLERANCE = 1.01     # provider floats and rounded capacities; 0 %, 50 % and 100 % stay valid
TIME_TOLERANCE_S = 0.005                # provider times are single precision
CROSSING_MAX_GAP_S = 5.0                # a longer scoring silence (restart, reconnect) forgets observed crossings
CROSSING_MEMORY = 4


def owns_sensor_fields(agent_driver_id, participant_driver_id):
    """Both identities resolved and equal. Never by name, vehicle id or position."""
    return agent_driver_id is not None and participant_driver_id is not None and agent_driver_id == participant_driver_id


def fuel_valid(level, capacity):
    return (finite_number(level) and finite_number(capacity) and capacity > 0
            and 0 <= level <= capacity * FUEL_OVER_CAPACITY_TOLERANCE)


def accepted_sensors(snapshot, owner, crossing_observed):
    """Player-only values of one checkpoint that may be persisted: {"wheels", "fuel"}, each None if unsupported."""
    accepted = {"wheels": None, "fuel": None}
    if not (owner and crossing_observed and snapshot.get("has_telemetry")):
        return accepted
    wheels = snapshot.get("wheels")
    if not wheels_trusted(wheels):
        return accepted          # no complete real tyre data: not proven to be the agent's own car either
    accepted["wheels"] = list(wheels)
    level, capacity = snapshot.get("fuel_remaining"), snapshot.get("fuel_capacity")
    if fuel_valid(level, capacity):
        accepted["fuel"] = (level, capacity)
    return accepted


def stored_wheels_trusted(value):
    """Is a stored `laps.tyre_snapshot` (jsonb text or list) complete, real tyre data?"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    return isinstance(value, list) and wheels_trusted(value)


def _close(a, b):
    return finite_number(a) and finite_number(b) and abs(a - b) <= TIME_TOLERANCE_S


def checkpoint_compatible(row, snapshot):
    """May a snapshot complete this stored row? Only when both describe the same lap record.

    Lap time must agree; sectors and the stop counter must agree wherever both sides know them. Anything else is
    a different checkpoint of the same lap number and is never combined with the stored row.
    """
    if not _close(row["lap_time"], snapshot.get("last_lap_time")):
        return False
    if row["pitstops"] != snapshot.get("num_pitstops", 0):
        return False
    for key in ("sector1", "sector2", "sector3"):
        stored, incoming = row[key], snapshot.get("last_" + key)
        if stored is not None and incoming is not None and not _close(stored, incoming):
            return False
    return True


def completion_updates(row, accepted):
    """Column updates that complete a stored row from an accepted checkpoint; never overwrites a valid value."""
    updates = {}
    wheels = accepted["wheels"]
    if wheels is not None and not stored_wheels_trusted(row["tyre_snapshot"]):
        for corner, wheel in zip(("fl", "fr", "rl", "rr"), wheels):
            updates[f"tyre_remaining_{corner}"] = wheel.get("remaining")
        updates["tyre_snapshot"] = json.dumps(wheels)
    if accepted["fuel"] is not None and not fuel_valid(row["fuel_remaining_l"], row["fuel_capacity_l"]):
        level, capacity = accepted["fuel"]
        stored_level, stored_capacity = row["fuel_remaining_l"], row["fuel_capacity_l"]
        # A usable stored level is kept; a missing or unusable capacity is completed when it is consistent with it.
        capacity_missing = not (finite_number(stored_capacity) and stored_capacity > 0)
        if (capacity_missing and finite_number(stored_level) and stored_level >= 0
                and stored_level <= capacity * FUEL_OVER_CAPACITY_TOLERANCE):
            updates["fuel_capacity_l"] = capacity
        else:
            updates["fuel_remaining_l"], updates["fuel_capacity_l"] = level, capacity
    return updates


def observe_crossing(crossings, previous, previous_at, scoring, now):
    """Remember laps whose start this agent watched on its own car: the player vehicle went from N-1 to N laps.

    `crossings` maps local vehicle id -> list of lap numbers. Any discontinuity (silence longer than
    CROSSING_MAX_GAP_S, another session type, a clock that went back, another player vehicle, a lap count that
    does anything but increase by one) forgets everything, so a reconnect or restart mid-lap leaves that lap
    unobserved.
    """
    player = scoring.get("player_vehicle_id")
    if (previous_at is None or now - previous_at > CROSSING_MAX_GAP_S or not previous or player is None
            or previous.get("player_vehicle_id") != player
            or previous.get("session_type") != scoring.get("session_type")
            or (finite_number(previous.get("session_time")) and finite_number(scoring.get("session_time"))
                and scoring["session_time"] < previous["session_time"])):
        crossings.clear()
        return

    def laps(frame):
        vehicle = next((v for v in frame.get("vehicles", []) if v.get("id") == player), None)
        value = vehicle.get("total_laps") if vehicle else None
        return value if type(value) is int and value >= 0 else None
    before, after = laps(previous), laps(scoring)
    if before is None or after is None or after < before or after > before + 1:
        crossings.clear()
    elif after == before + 1:
        known = crossings.setdefault(player, [])
        known.append(after)
        del known[:-CROSSING_MEMORY]
