"""Binding of SessionInfo metadata to a confirmed session (#20).

A SessionInfo message carries no session identity, and around a session change it may describe the session
that is ending or the one that is starting. It is therefore never persisted when it arrives. It becomes a
*candidate* that is committed only when the next scoring frame of the same stream resolves to the very same
session ("bracketing"), nothing indicates a change in between, and the same claim has been seen twice. Anything
else is dropped: unknown stays unknown.

Provider semantics are explicit. For LMU/Pitwall ``session_minutes`` is the session END ON THE SESSION CLOCK
(minutes of elapsed time, the same clock as ``session_time``), not a configured duration: 2.5 for a 2 minute
practice that starts at 30 s, 10.5 for a 10 minute qualifying, 82.6 for an 80 minute race with a 2.6 minute
pre-race period. During the pre-race period it is a countdown (1.07, 0, 2.23) and jumps to the real end when
the race goes green (32.23). Other providers make no such promise, so they never produce an end clock here.
"""
import math

from .trackmap import RACE_CONTROL_FRESH_SECONDS

BIND_MAX_AGE_SECONDS = 5.0       # a candidate older than this is no longer evidence for the current frame
CONFIRMATIONS = 2                # identical bracketed claims required before a value is accepted
TIMED_PHASES = ("green", "full_course_yellow")
PRE_PHASES = ("pre_session", "formation")
MIN_DURATION_SECONDS = 60.0
PRE_CLOCK_TOLERANCE_SECONDS = 5.0  # the pre-race end must coincide with the clock at which the race goes green
END_EPSILON_SECONDS = 0.5


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def provider_end_clock_seconds(provider, payload):
    """Session end on the session clock in seconds, or None when the provider defines no such value."""
    if provider != "lmu":
        return None
    minutes = payload.get("session_minutes")
    return minutes * 60 if finite(minutes) and minutes > 0 else None


def lap_limit(payload):
    laps = payload.get("session_laps")
    return laps if type(laps) is int and 0 < laps < 2**31 - 1 else None


def phase_of(state, now):
    """Fresh simulator-neutral race-control phase, else None."""
    control = state.get("race_control")
    age = now - state.get("race_control_at", float("-inf"))
    if not isinstance(control, dict) or not 0 <= age <= RACE_CONTROL_FRESH_SECONDS:
        return None
    return control.get("phase")


def observe(state, provider, payload, source_epoch, now):
    """Remember a SessionInfo claim together with what was known when it arrived. Persists nothing."""
    state["meta_candidate"] = dict(
        session_id=state.get("session_id"), track=payload["track_name"], end_et=provider_end_clock_seconds(provider, payload),
        laps=lap_limit(payload), epoch=source_epoch, at=now, phase=phase_of(state, now))


def confirm(state, *, provider, resolved_session_id, scoring, changed, source_epoch, now):
    """Resolve the pending candidate against the scoring frame that has just been resolved.

    Returns the accepted values (a dict with any of end_et, duration, laps) or None. Always consumes the candidate.
    """
    candidate = state.pop("meta_candidate", None)
    if not candidate:
        return None
    bracketed = (resolved_session_id is not None and candidate["session_id"] == resolved_session_id
                 and not changed and candidate["epoch"] == source_epoch
                 and candidate["track"] == state.get("track_name")
                 and 0 <= now - candidate["at"] <= BIND_MAX_AGE_SECONDS)
    if not bracketed:
        state.pop("meta_seen", None)
        return None
    claim = (resolved_session_id, candidate["end_et"], candidate["laps"])
    seen = state.get("meta_seen")
    count = seen["count"] + 1 if seen and seen["claim"] == claim else 1
    state["meta_seen"] = dict(claim=claim, count=count)
    clock = scoring.get("session_time")
    if candidate["phase"] in PRE_PHASES and candidate["end_et"] is not None and finite(clock):
        # Pre-race countdown: never an end, only the reference point for deriving the race duration later.
        state["meta_pre"] = dict(session_id=resolved_session_id, end_et=candidate["end_et"], clock=clock)
    accepted = {}
    if count < CONFIRMATIONS:
        return None
    end = candidate["end_et"]
    if candidate["laps"] is not None and end is None:
        accepted["laps"] = candidate["laps"]  # a simultaneous time end wins: the lap value may be a placeholder
    if (end is not None and candidate["phase"] in TIMED_PHASES and phase_of(state, now) in TIMED_PHASES
            and finite(clock) and end > clock):
        accepted["end_et"] = end
        pre = state.get("meta_pre")
        if (scoring.get("session_type") == "Race" and pre and pre["session_id"] == resolved_session_id
                and abs(pre["end_et"] - pre["clock"]) <= PRE_CLOCK_TOLERANCE_SECONDS
                and end - pre["end_et"] >= MIN_DURATION_SECONDS):
            accepted["duration"] = round(end - pre["end_et"], 3)
    return accepted or None


def merge(row, accepted):
    """New (end_et, duration, laps) for a session row given accepted values.

    Write-once per dimension: a limit that is already known is never replaced by a later claim, because the
    legitimate flow (pre-race countdown -> green) never accepts a value before the real end is known. A session
    is limited by time or by laps, never both, so the first confirmed dimension wins.
    """
    end, duration, laps = row["session_end_et_seconds"], row["session_duration_seconds"], row["session_laps"]
    laps_known = laps is not None and laps > 0
    if "end_et" in accepted and end is None and not laps_known:
        end = accepted["end_et"]
        duration = accepted.get("duration", duration)
    elif "duration" in accepted and duration is None and end is not None and abs(accepted["end_et"] - end) <= END_EPSILON_SECONDS:
        duration = accepted["duration"]
    if "laps" in accepted and not laps_known and end is None:
        laps = accepted["laps"]
    return end, duration, laps
