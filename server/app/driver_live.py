"""Own-driver focus block of /api/drivers/{slug}/live (Driver Live V2).

Pure functions over data the endpoint already holds, so they are unit-testable without a database. Unknown stays
unknown (None): nothing here estimates, interpolates or invents values, and no running sector time exists.
"""
from .timing import finite_number, lap_count, newest_state, project_timing
from .trackmap import SECTOR_SAMPLE_MAX_AGE_SECONDS, player_vehicle, public_race_control

LAP_TIME_TOLERANCE_S = 0.005   # provider times are single precision floats
FINISH_LABELS = {1: "FIN", 2: "DNF", 3: "DSQ"}


def positive(value):
    return value if finite_number(value) and value > 0 else None


def status_of(participant):
    """PIT/TRACK/FIN/DNF/DSQ with the semantics of session.html `participantStatus(p, true)`; else unknown."""
    finish = participant.get("finish_status")
    if type(finish) is int and finish in FINISH_LABELS:
        return FINISH_LABELS[finish]
    if participant.get("in_pits") is True:
        return "PIT"
    return "TRACK" if finish == 0 and participant.get("in_pits") is False else None


def current_lap_of(participant):
    """Completed + 1 while the car is still racing; unknown once finished/retired/disqualified or without a count."""
    completed = participant.get("total_laps")
    return completed + 1 if lap_count(completed) and participant.get("finish_status") == 0 else None


def own_player_sector(slug, session_id, local_vehicle_id, states, now):
    """Sector 1-3 only from fresh observations made by this driver's own agent in exactly this session.

    The observation must be of the driver's own car (the agent's player vehicle is the tracked participant), have
    fresh scoring and fresh race control, and the car must be on track. Several matching agents must agree.
    """
    seen = set()
    for state in states:
        if state.get("driver_slug") != slug or state.get("session_id") != session_id:
            continue
        scoring = state.get("scoring") or {}
        age = now - state.get("last_scoring_at", -1e9)
        vehicle = player_vehicle(scoring)
        control = public_race_control(state, now)
        if (vehicle is None or vehicle.get("id") != local_vehicle_id or not 0 <= age <= SECTOR_SAMPLE_MAX_AGE_SECONDS
                or vehicle.get("in_pits") is not False or not control or not control["fresh"]
                or control.get("player_sector") not in (1, 2, 3)):
            return None   # one non-matching own observation makes the answer ambiguous, not merely absent
        seen.add(control["player_sector"])
    return seen.pop() if len(seen) == 1 else None


def live_age(session_id, states, now):
    """Age in seconds of the newest scoring of this session, or None when nothing fresh is known."""
    state = newest_state(session_id, True, states, now)
    return round(now - state["last_scoring_at"], 3) if state else None


def focus_block(slug, session, participants, history, states, now):
    """Focus for the unique own participant; None when the own participant is missing or ambiguous.

    `participants` are the endpoint's participant dicts (already overlaid with live scoring); `history` is the
    stored last completed lap and last pit lap of that participant (a mapping or None).
    """
    own = [p for p in participants if p.get("is_stintara_driver")]
    if len(own) != 1 or not own[0].get("participant_id"):
        return None
    me = own[0]
    # Same conservative gap/interval semantics as the session view; computed on copies so existing fields stay.
    projected = [{**p, "id": p["participant_id"], "sector1": None, "sector2": None, "sector3": None}
                 for p in participants]
    project_timing(projected, session.get("session_type"))
    timed = next(p for p in projected if p["id"] == me["participant_id"])
    history = history or {}
    last = None
    completed = me.get("total_laps") if lap_count(me.get("total_laps")) else None
    stored_time = positive(history.get("lap_time"))
    live_time = positive(me.get("last_lap_time"))
    # Lap time, number and sectors must describe one lap. The stored row is used only when it is exactly the
    # lap the live scoring reports as last completed (and, when both times are known, agrees on the time);
    # a scoring ahead of or behind the stored checkpoint yields no sectors rather than a mixed lap.
    if (completed and history.get("lap_number") == completed and stored_time is not None
            and (live_time is None or abs(live_time - stored_time) <= LAP_TIME_TOLERANCE_S)):
        last = {"lap_number": completed, "lap_time": stored_time,
                "sector1": positive(history.get("sector1")), "sector2": positive(history.get("sector2")),
                "sector3": positive(history.get("sector3"))}
    pit = history.get("last_pit_lap")
    stops = me.get("pitstops")
    return {
        "participant_id": me["participant_id"],
        "status": status_of(me),
        "position": me["position"] if type(me.get("position")) is int and me["position"] > 0 else None,
        "gap_to_leader": timed["gap_to_leader"], "gap_laps": timed["gap_laps"], "interval": timed["interval"],
        "completed_laps": me["total_laps"] if lap_count(me.get("total_laps")) else None,
        "current_lap": current_lap_of(me),
        "last_lap": last,   # complete triple (number, time, sectors) of the scoring's last lap, else None
        "last_lap_number": completed if completed else None,
        "last_lap_time": live_time,
        "best_lap_time": positive(me.get("best_lap_time")),
        "pitstops": stops if lap_count(stops) else None,
        # First persisted completed-lap snapshot with an increased stop counter; not the exact pit entry/exit.
        "last_pit_lap": pit if type(pit) is int and pit > 0 and lap_count(stops) and stops > 0 else None,
        "player_sector": own_player_sector(slug, session["id"], me.get("local_vehicle_id"), states, now),
        "live_age_s": live_age(session["id"], states, now),
    }

