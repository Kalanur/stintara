"""Provenance of player-only lap sensor fields (#59).

LMU only delivers real tyre and fuel telemetry for the car the observing agent drives; for every other car the
provider reports placeholders (wear 1.0 without sensors, a default fuel level). A lap checkpoint's tyre and fuel
values are therefore persisted only when the writing agent's server-side driver is the participant's resolved
driver. Everything else a checkpoint carries (timing, sectors, position, pit state, compound) stays open to every
observer. Damage is deliberately not handled here (experimental, unchanged).
"""
import json

from .timing import finite_number, tyre_sensors_present


def owns_sensor_fields(agent_driver_id, participant_driver_id):
    """Both identities resolved and equal. Never by name, vehicle id or position."""
    return agent_driver_id is not None and participant_driver_id is not None and agent_driver_id == participant_driver_id


def accepted_sensors(snapshot, owner):
    """Player-only values of one checkpoint that may be persisted: {"wheels", "fuel"} entries, each None if unsupported.

    Tyre values need a real sensor reading (a wear of 1.0 alone is indistinguishable from the placeholder); fuel
    needs a finite non-negative level and a positive capacity. No heuristic on the fuel level itself.
    """
    accepted = {"wheels": None, "fuel": None}
    if not owner or not snapshot.get("has_telemetry"):
        return accepted
    wheels = [w if isinstance(w, dict) else {} for w in (snapshot.get("wheels") or [])][:4]
    wheels += [{}] * (4 - len(wheels))
    if tyre_sensors_present(wheels) and all(w.get("remaining") is None or finite_number(w["remaining"]) for w in wheels):
        accepted["wheels"] = wheels
    level, capacity = snapshot.get("fuel_remaining"), snapshot.get("fuel_capacity")
    if finite_number(level) and finite_number(capacity) and level >= 0 and capacity > 0:
        accepted["fuel"] = (level, capacity)
    return accepted


def stored_wheels_have_sensors(value):
    """Does a stored `laps.tyre_snapshot` (jsonb text or list) hold a real sensor reading?"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    return isinstance(value, list) and tyre_sensors_present(value)
