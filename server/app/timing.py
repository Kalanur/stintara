"""Conservative spectator presentation; raw provider persistence is unchanged."""
import math
from collections import Counter


def nonnegative(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def lap_count(value):
    return type(value) is int and value >= 0


def position(value):
    return type(value) is int and value > 0


def newest_state(session_id, live, states, now):
    candidates = [s for s in states if live and s.get("session_id") == session_id
                  and 0 <= now - s.get("last_scoring_at", 0) < 15 and s.get("scoring")]
    return max(candidates, key=lambda s: s["last_scoring_at"], default={})


def newest_scoring(session_id, live, states, now):
    return newest_state(session_id, live, states, now).get("scoring", {})


def race_gap(participant, race):
    if not race or not position(participant.get("position")):
        return None, None
    deficit, gap = participant.get("laps_behind_leader"), participant.get("gap_to_leader")
    if deficit is not None and not lap_count(deficit):
        return None, None
    if participant["position"] == 1 and (deficit or not nonnegative(gap) or gap != 0):
        return None, None
    if deficit:
        return None, deficit
    return (gap if nonnegative(gap) else None), None


def preceding_interval(previous, current):
    if (not previous or current["position"] != previous["position"] + 1
            or not lap_count(current.get("total_laps"))
            or not lap_count(previous.get("total_laps"))
            or current["total_laps"] != previous["total_laps"]
            or current.get("laps_behind_leader") != 0 or previous.get("laps_behind_leader") != 0
            or not nonnegative(current.get("gap_to_leader"))
            or not nonnegative(previous.get("gap_to_leader"))
            or current["gap_to_leader"] < previous["gap_to_leader"]):
        return None
    return current["gap_to_leader"] - previous["gap_to_leader"]


def lap_ms(value):
    """Lap time as integer milliseconds for comparison; provider floats are single precision."""
    return round(value * 1000) if nonnegative(value) and value > 0 else None


def classify_lap_times(participants):
    """Live-timing colour semantics for one session's participants, from provider times only.

    best_lap_class: "overall" only for the single fastest Best of the session (all classes together);
    every other Best, including ordinary personal bests, stays None (neutral). A tie for the fastest at
    millisecond precision is attributed to nobody. last_lap_class: "overall" when Last is that unique
    session best, "personal" when Last equals the driver's own Best (not the session best), "slower" when
    Last is slower than Best, None when either is unknown or Last is faster than Best (inconsistent mix).
    The provider reports no lap validity, so there is no invalid class; sectors are never classified.
    """
    bests = {id(p): lap_ms(p.get("best_lap_time")) for p in participants}
    known = [v for v in bests.values() if v is not None]
    fastest = min(known, default=None)
    unique = fastest is not None and known.count(fastest) == 1
    for p in participants:
        best, last = bests[id(p)], lap_ms(p.get("last_lap_time"))
        overall = unique and best == fastest
        p["best_lap_class"] = "overall" if overall else None
        p["last_lap_class"] = (None if best is None or last is None or last < best
                               else "slower" if last > best else "overall" if overall else "personal")


def project_timing(participants, session_type):
    counts = Counter(p.get("position") for p in participants if position(p.get("position")))
    unique = {p["position"]: p for p in participants if position(p.get("position")) and counts[p["position"]] == 1}
    race = session_type == "Race"
    for p in participants:
        p["gap_to_leader"], p["gap_laps"] = race_gap(p, race)
        p.update(interval=None, interval_preceding_id=None)
        for sector in ("sector1", "sector2", "sector3"):
            value = p.get(sector)
            p[sector] = value if nonnegative(value) and value > 0 else None
    if race:
        for pos, p in unique.items():
            previous = unique.get(pos-1)
            interval = preceding_interval(previous, p)
            if interval is not None:
                p.update(interval=interval, interval_preceding_id=previous["id"])
    classify_lap_times(participants)


def session_timing(session, scoring):
    elapsed = scoring.get("session_time")
    minutes, laps = session.get("session_minutes"), session.get("session_laps")
    duration = minutes if nonnegative(minutes) and minutes > 0 else None
    # Positive duration takes precedence; a simultaneous lap value may be a projection/sentinel.
    distance = laps if session.get("session_type") == "Race" and not duration and lap_count(laps) and laps > 0 else None
    return dict(elapsed_seconds=elapsed if nonnegative(elapsed) else None,
                configured_laps=distance, configured_minutes=duration)


SENSOR_KEYS = ("temp_inner", "temp_mid", "temp_outer", "carcass_temp", "brake_temp", "pressure")


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def field_status(compound, wheels, dents):
    """Compact tyre/damage view of one checkpoint or snapshot; unknown stays None, nothing is guessed.

    LMU mWear is tyre remaining (1.0 new .. 0.0 destroyed), so the field value is the worst corner.
    Pitwall only has real tyre telemetry for the player's car: for every other car mWear is a constant 1.0
    next to sentinel (-273.15) temperatures. Remaining tyre is therefore trusted only when at least one
    wheel carries a real sensor value; otherwise it is unknown, never "new".
    dent_severity is one provider integer per body region (0 = undamaged; 0/1 observed in captures).
    """
    wheels = [w for w in wheels or [] if isinstance(w, dict)]
    corners = [w.get("remaining") for w in wheels]
    sensors = any(finite_number(w.get(k)) for w in wheels for k in SENSOR_KEYS)
    trusted = len(corners) == 4 and sensors and all(finite_number(v) and 0 <= v <= 1 for v in corners)
    regions = [v for v in dents if type(v) is int and v >= 0] if isinstance(dents, list) else []
    known_dents = bool(regions) and len(regions) == len(dents)
    return {"tyre_compound": compound if isinstance(compound, str) and compound.strip() else None,
            "tyre_remaining_min": min(corners) if trusted else None,
            "damage_regions": sum(1 for v in regions if v > 0) if known_dents else None,
            "damage_max": max(regions) if known_dents else None}
