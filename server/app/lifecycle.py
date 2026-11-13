"""Provider-neutral session/observation persistence; no RaceControl inference."""
from . import session_meta
from .convergence import (
    OBSERVATION_FRESH_SECONDS,
    TRANSITION_GRACE_SECONDS,
    clock_consistent,
    clock_reset,
    finite,
    matching_rosters,
    namespace_key,
)
from .domain import same_session
from .uuid7 import uuid7

# Provider "empty session" state (clock at zero, nobody has a lap) that LMU reports after a race is over.
IDLE_CLOCK_SECONDS = 1.0


def idle_reset(scoring):
    vehicles = scoring.get("vehicles") or []
    clock = scoring.get("session_time")
    return bool(vehicles) and finite(clock) and 0 <= clock <= IDLE_CLOCK_SECONDS and all(
        type(v.get("total_laps")) is int and v["total_laps"] == 0 for v in vehicles)


async def race_ran_its_course(conn, previous):
    """The Race clock/laps reached the configured limit, so a later empty state is not a restart."""
    clock = previous["last_session_time"]
    if previous["session_type"] != "Race" or not finite(clock):
        return False
    # Only a bound end of the session clock counts (#20); an unknown end never claims the race is over.
    end, laps = previous["session_end_et_seconds"], session_meta.trusted_laps(previous)
    if finite(end) and end > 0 and clock >= end:
        return True
    if laps and laps > 0:
        done = await conn.fetchval("SELECT max(total_laps) FROM session_participants WHERE session_id=$1", previous["id"])
        return done is not None and done >= laps
    return False


SESSION_ORDER = {"Practice": 0, "Qualifying": 1, "Race": 2}
TRACK_LENGTH_TOLERANCE_M = 1.0


def chain_continues(previous, *, provider, track, track_length, session_type, fresh):
    """Only a directly observed, forward Practice -> Qualifying -> Race step on the same track links."""
    if not fresh or previous["simulator_provider"] != provider or previous["track_name"] != track:
        return False
    old, new = SESSION_ORDER.get(previous["session_type"]), SESSION_ORDER.get(session_type)
    if old is None or new is None or new <= old:
        return False  # same-type resets (restarts) and backwards steps never link
    left = previous["track_length_m"]
    return not (finite(left) and finite(track_length) and abs(left - track_length) > TRACK_LENGTH_TOLERANCE_M)


async def link_session_chain(conn, previous, session_id):
    """Caller holds the lock on ``previous``. Reuses events/races; creates an internal pair on first link."""
    if await conn.fetchval("SELECT 1 FROM sessions WHERE previous_session_id=$1", previous["id"]):
        return  # another observer already created the successor; never create a second chain branch
    race_id = previous["race_id"]
    if race_id is None:
        event_id, race_id = uuid7(), uuid7()
        await conn.execute("INSERT INTO events(id,simulator_provider,track_name) VALUES($1,$2,$3)",
                           event_id, previous["simulator_provider"], previous["track_name"])
        await conn.execute("INSERT INTO races(id,event_id,simulator_provider) VALUES($1,$2,$3)",
                           race_id, event_id, previous["simulator_provider"])
        await conn.execute("UPDATE sessions SET race_id=$2 WHERE id=$1", previous["id"], race_id)
    await conn.execute("UPDATE sessions SET race_id=$2,previous_session_id=$3 WHERE id=$1",
                       session_id, race_id, previous["id"])


async def complete_if_unobserved(conn, session_id):
    await conn.execute("""
        UPDATE sessions SET status='complete',ended_at=now()
        WHERE id=$1 AND status='live' AND NOT EXISTS (
            SELECT 1 FROM session_observations WHERE session_id=$1 AND ended_at IS NULL)
    """, session_id)


async def end_observation(conn, session_id, agent_id):
    # Serialize attach/end decisions on a shared session, including simultaneous stops.
    await conn.fetchval("SELECT id FROM sessions WHERE id=$1 FOR UPDATE", session_id)
    await conn.execute("""UPDATE session_observations SET ended_at=now()
        WHERE session_id=$1 AND agent_id=$2 AND ended_at IS NULL""", session_id, agent_id)
    await complete_if_unobserved(conn, session_id)


async def attach_observation(conn, session_id, agent_id):
    """Internal, trusted association point. Never accept a client-supplied UUID."""
    live = await conn.fetchval("SELECT status='live' FROM sessions WHERE id=$1 FOR UPDATE", session_id)
    if not live:
        raise ValueError("Cannot observe a closed/missing session")
    await conn.execute("""
        INSERT INTO session_observations(id,session_id,agent_id,last_seen_at)
        VALUES($1,$2,$3,now()) ON CONFLICT(session_id,agent_id)
        DO UPDATE SET ended_at=NULL,last_seen_at=now()
    """, uuid7(), session_id, agent_id)


async def expire_observations(conn, timeout_seconds):
    rows = await conn.fetch("""
        SELECT DISTINCT session_id FROM session_observations
        WHERE ended_at IS NULL AND last_seen_at < now() - $1 * interval '1 second'
        ORDER BY session_id
    """, float(timeout_seconds))
    for row in rows:
        await conn.fetchval("SELECT id FROM sessions WHERE id=$1 FOR UPDATE", row["session_id"])
        await conn.execute("""UPDATE session_observations SET ended_at=now()
            WHERE session_id=$1 AND ended_at IS NULL
            AND last_seen_at < now() - $2 * interval '1 second'""", row["session_id"], float(timeout_seconds))
        await complete_if_unobserved(conn, row["session_id"])


async def cross_agent_candidates(conn, agent, provider, track, scoring):
    """Return strict matches and roster-only candidates from the same locked snapshot."""
    if provider != "lmu" or not finite(scoring.get("session_time")):
        return [], []
    candidates = await conn.fetch("""
        SELECT s.id FROM sessions s
        WHERE s.simulator_provider=$1 AND s.track_name=$2 AND s.session_type=$3 AND s.status='live'
        AND EXISTS (SELECT 1 FROM session_observations o WHERE o.session_id=s.id
            AND o.agent_id<>$4 AND o.ended_at IS NULL AND o.last_session_time IS NOT NULL
            AND o.last_seen_at > clock_timestamp() - $5 * interval '1 second')
        ORDER BY s.id FOR UPDATE OF s
    """, provider, track, scoring["session_type"], agent["id"], OBSERVATION_FRESH_SECONDS)
    matches, plausible = [], []
    for candidate in candidates:
        observations = await conn.fetch("""SELECT last_session_time,last_seen_at
            FROM session_observations WHERE session_id=$1 AND agent_id<>$2 AND ended_at IS NULL
            AND last_session_time IS NOT NULL ORDER BY last_seen_at DESC,id""", candidate["id"], agent["id"])
        now = await conn.fetchval("SELECT clock_timestamp()")
        observation = next((o for o in observations if finite(o["last_session_time"])
                            and 0 <= (now - o["last_seen_at"]).total_seconds() < OBSERVATION_FRESH_SECONDS), None)
        if observation is None:
            continue
        roster = await conn.fetch("""SELECT local_vehicle_id,display_name,team_name,car_name,car_class
            FROM session_participants WHERE session_id=$1""", candidate["id"])
        if matching_rosters(provider, scoring.get("vehicles", []), roster):
            plausible.append(candidate["id"])
            if clock_consistent(scoring["session_time"], observation["last_session_time"],
                                (now - observation["last_seen_at"]).total_seconds()):
                matches.append(candidate["id"])
    return matches, plausible


async def resolve_session(conn, agent, state, scoring, timeout_seconds):
    # Caller supplies a transaction-local state copy and publishes it only after commit.
    pending = state.pop("pending_convergence", None)
    invalidated = state.pop("convergence_invalidated", False)
    provider = state["provider"]
    track = state.get("track_name")
    if not track:
        return None  # wait for source context; don't create an Unknown-track session
    # Held until the caller commits the initial participant roster as well as resolution.
    await conn.execute("SELECT pg_advisory_xact_lock($1)", namespace_key(provider, track, scoring["session_type"]))
    # Serializes session creation for this agent, including after a server restart.
    await conn.fetchval("SELECT id FROM agents WHERE id=$1 FOR UPDATE", agent["id"])
    rows = await conn.fetch("""
        SELECT s.*,o.last_session_time,o.last_seen_at AS observation_seen
        FROM sessions s JOIN session_observations o ON o.session_id=s.id
        WHERE o.agent_id=$1 AND o.ended_at IS NULL AND s.status='live'
        ORDER BY s.started_at DESC FOR UPDATE OF s,o
    """, agent["id"])
    session_id = None
    now = await conn.fetchval("SELECT clock_timestamp()")
    namespace = (provider, track, scoring["session_type"])
    reset = (state.get("persisted_track") == track and state.get("session_type") == scoring["session_type"]
             and clock_reset(scoring.get("session_time"), state.get("scoring", {}).get("session_time")))
    reset = reset or any(previous["simulator_provider"] == provider and previous["track_name"] == track
                         and previous["session_type"] == scoring["session_type"]
                         and clock_reset(scoring.get("session_time"), previous["last_session_time"])
                         for previous in rows)
    # #46: after a finished Race the provider reports an empty session (clock 0, no laps for anyone).
    # That is not a new race and must not become a second Race row. Complete the finished session
    # now (ended_at stays the moment of the reset) and create nothing until the state shows progress.
    held = state.get("post_finish_idle")
    if held:
        if held == namespace and idle_reset(scoring):
            return None
        state.pop("post_finish_idle", None)
    elif reset and idle_reset(scoring):
        finished = [p for p in rows if p["simulator_provider"] == provider and p["track_name"] == track
                    and p["session_type"] == scoring["session_type"] and await race_ran_its_course(conn, p)]
        if finished:
            for previous in rows:
                await end_observation(conn, previous["id"], agent["id"])
            state["post_finish_idle"] = namespace
            return None
    if pending:
        invalidated = (invalidated or pending["namespace"] != namespace
                       or not 0 <= (now - pending["started_at"]).total_seconds() < TRANSITION_GRACE_SECONDS
                       or clock_reset(scoring.get("session_time"), pending["last_clock"]))
    for previous in rows:
        fresh = (now - previous["observation_seen"]).total_seconds() < timeout_seconds
        if session_id is None and not reset and fresh and same_session(
            previous, provider=provider, session_type=scoring["session_type"],
            track_name=track, session_time=scoring.get("session_time"),
        ):
            session_id = previous["id"]
        else:
            await end_observation(conn, previous["id"], agent["id"])
    if session_id is None and not reset and not invalidated:
        matches, plausible = await cross_agent_candidates(conn, agent, provider, track, scoring)
        # Once waiting, candidate changes/ambiguity must fail closed, even if one
        # candidate now passes the clock gate. Never restart the deadline.
        safe_retry = not pending or plausible == [pending["candidate_id"]]
        if safe_retry and len(matches) == 1:
            session_id = matches[0]
        if session_id is not None:
            await attach_observation(conn, session_id, agent["id"])
        elif safe_retry and not matches and len(plausible) == 1:
            state["pending_convergence"] = dict(
                namespace=namespace, candidate_id=plausible[0],
                started_at=pending["started_at"] if pending else now,
                last_clock=scoring["session_time"],
            )
            return None
    if session_id is None:
        session_id = uuid7()
        await conn.execute("""
            INSERT INTO sessions(id,simulator_provider,session_type,track_name,
                track_length_m,game_version,weather)
            VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)
        """, session_id, provider, scoring["session_type"], track,
            state.get("track_length_m"), state.get("game_version"), state.get("weather_json", "{}"))
        await attach_observation(conn, session_id, agent["id"])
        # Exactly one live session of this agent was just left by a genuine transition (#43).
        if len(rows) == 1 and chain_continues(
                rows[0], provider=provider, track=track, track_length=state.get("track_length_m"),
                session_type=scoring["session_type"],
                fresh=(now - rows[0]["observation_seen"]).total_seconds() < timeout_seconds):
            await link_session_chain(conn, rows[0], session_id)
    await conn.execute("""
        UPDATE session_observations SET last_seen_at=clock_timestamp(),last_session_time=$3
        WHERE session_id=$1 AND agent_id=$2
    """, session_id, agent["id"], scoring.get("session_time"))
    await conn.execute("UPDATE sessions SET last_seen_at=now() WHERE id=$1", session_id)
    return session_id
