"""Conservative LMU roster evidence; never persistent driver identity."""

import hashlib
import json
import math

MIN_SHARED_SLOTS = 5
MIN_SMALLER_OVERLAP = 0.8
CLOCK_TOLERANCE_SECONDS = 5.0
OBSERVATION_FRESH_SECONDS = 15.0
# Covers a roughly six-second transition skew without relaxing actual attachment.
TRANSITION_GRACE_SECONDS = 10.0


def namespace_key(provider, track, session_type):
    encoded = json.dumps([provider, track, session_type], ensure_ascii=True).encode()
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big", signed=True)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def clock_consistent(incoming, previous, age):
    return (finite(incoming) and finite(previous) and finite(age)
            and 0 <= age < OBSERVATION_FRESH_SECONDS
            and abs(incoming - (previous + age)) <= CLOCK_TOLERANCE_SECONDS)


def clock_reset(incoming, previous):
    return finite(incoming) and finite(previous) and incoming < previous - 2.0


def evidence_roster(rows, *, persisted=False):
    """Trim only; missing optional strings are empty. Duplicate slots invalidate evidence.

    A comparable entry needs a real driver string and some entry/team/class context.
    Adapter/persistence fallback names are not evidence. No aliases or case folding.
    """
    roster, seen = {}, set()
    keys = ("display_name", "team_name", "car_name", "car_class") if persisted else (
        "driver_name", "team_name", "car_name", "vehicle_class")
    for row in rows:
        slot = row.get("local_vehicle_id" if persisted else "id")
        if type(slot) is not int or slot < 0 or slot in seen:
            return None
        seen.add(slot)
        evidence = tuple(value.strip() if isinstance(value := row.get(key), str) else "" for key in keys)
        if evidence[0] and evidence[0] not in ("Unknown", f"Vehicle {slot}") and any(evidence[1:]):
            roster[slot] = evidence
    return roster


def matching_rosters(provider, incoming, persisted):
    if provider != "lmu":
        return False
    left = evidence_roster(incoming)
    right = evidence_roster(persisted, persisted=True)
    if not left or not right:
        return False
    shared = left.keys() & right.keys()
    if any(left[slot] != right[slot] for slot in shared):
        return False
    return len(shared) >= MIN_SHARED_SLOTS and len(shared) / min(len(left), len(right)) >= MIN_SMALLER_OVERLAP
