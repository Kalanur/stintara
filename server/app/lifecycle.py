"""Provider-neutral session/observation persistence; no RaceControl inference."""
from .domain import same_session
from .uuid7 import uuid7


async def complete_if_unobserved(conn, session_id):
    await conn.execute("""
        UPDATE sessions SET status='complete',ended_at=now()
        WHERE id=$1 AND status='live' AND NOT EXISTS (
            SELECT 1 FROM session_observations WHERE session_id=$1 AND ended_at IS NULL)
    """, session_id)


async def end_observation(conn, session_id, agent_id):
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
        UPDATE session_observations SET ended_at=now()
        WHERE ended_at IS NULL AND last_seen_at < now() - $1 * interval '1 second'
        RETURNING session_id
    """, float(timeout_seconds))
    for row in rows:
        await complete_if_unobserved(conn, row["session_id"])


async def resolve_session(conn, agent, state, scoring, timeout_seconds):
    provider = state["provider"]
    track = state.get("track_name")
    if not track:
        return None  # wait for source context; don't create an Unknown-track session
    # Serializes session creation for this agent, including after a server restart.
    await conn.fetchval("SELECT id FROM agents WHERE id=$1 FOR UPDATE", agent["id"])
    rows = await conn.fetch("""
        SELECT s.*,o.last_session_time,o.last_seen_at AS observation_seen
        FROM sessions s JOIN session_observations o ON o.session_id=s.id
        WHERE o.agent_id=$1 AND o.ended_at IS NULL AND s.status='live'
        ORDER BY s.started_at DESC FOR UPDATE OF s,o
    """, agent["id"])
    session_id = None
    now = await conn.fetchval("SELECT now()")
    for previous in rows:
        fresh = (now - previous["observation_seen"]).total_seconds() < timeout_seconds
        if session_id is None and fresh and same_session(
            previous, provider=provider, session_type=scoring["session_type"],
            track_name=track, session_time=scoring.get("session_time"),
        ):
            session_id = previous["id"]
        else:
            await end_observation(conn, previous["id"], agent["id"])
    if session_id is None:
        session_id = uuid7()
        await conn.execute("""
            INSERT INTO sessions(id,simulator_provider,session_type,track_name,
                track_length_m,game_version,session_laps,session_minutes,weather)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
        """, session_id, provider, scoring["session_type"], track,
            state.get("track_length_m"), state.get("game_version"),
            state.get("session_laps"), state.get("session_minutes"), state.get("weather_json", "{}"))
        await attach_observation(conn, session_id, agent["id"])
    await conn.execute("""
        UPDATE session_observations SET last_seen_at=now(),last_session_time=$3
        WHERE session_id=$1 AND agent_id=$2
    """, session_id, agent["id"], scoring.get("session_time"))
    await conn.execute("UPDATE sessions SET last_seen_at=now() WHERE id=$1", session_id)
    return session_id
