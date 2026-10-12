"""Small simulator-independent lifecycle and sampling decisions."""


def same_session(previous, *, provider, session_type, track_name, session_time):
    if previous["status"] != "live" or previous["simulator_provider"] != provider:
        return False
    if previous["session_type"] != session_type or previous["track_name"] != track_name:
        return False
    elapsed = previous["last_session_time"]
    # Permit small clock jitter; a backwards jump means a restart/new session.
    return elapsed is None or session_time is None or session_time >= elapsed - 2.0


def sample_due(last_sample, now, interval=1.0):
    return last_sample is None or now - last_sample >= interval
