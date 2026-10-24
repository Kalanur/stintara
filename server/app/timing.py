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


def newest_scoring(session_id, live, states, now):
    candidates = [s for s in states if live and s.get("session_id") == session_id
                  and 0 <= now - s.get("last_scoring_at", 0) < 15 and s.get("scoring")]
    return max(candidates, key=lambda s: s["last_scoring_at"], default={}).get("scoring", {})


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


def session_timing(session, scoring):
    elapsed = scoring.get("session_time")
    minutes, laps = session.get("session_minutes"), session.get("session_laps")
    duration = minutes if nonnegative(minutes) and minutes > 0 else None
    # Positive duration takes precedence; a simultaneous lap value may be a projection/sentinel.
    distance = laps if session.get("session_type") == "Race" and not duration and lap_count(laps) and laps > 0 else None
    return dict(elapsed_seconds=elapsed if nonnegative(elapsed) else None,
                configured_laps=distance, configured_minutes=duration)
