from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from .domain import sample_due
from .fuel import fuel_report
from .lifecycle import end_observation, expire_observations, resolve_session
from .migrations import migrate_sample_ids
from .protocol import PAYLOADS, Hello, Lap, Vehicle, Wheel
from .secrets import read_secret
from .uuid7 import uuid7

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.environ.get("POSTGRES_DB", "stintara")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "stintara")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SCHEMA_FILE = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

SESSION_TIMEOUT_SECONDS = int(os.environ.get("SESSION_TIMEOUT_SECONDS", "120"))
if SESSION_TIMEOUT_SECONDS < 15:
    raise ValueError("SESSION_TIMEOUT_SECONDS must be at least 15")
active_agents: set[str] = set()
ingest_lock = asyncio.Lock()
logger = logging.getLogger("stintara.ingest")
MAX_INVALID_PAYLOADS = 10
INVALID_PAYLOAD_WINDOW_SECONDS = 60


def log_invalid(agent_key, kind, exc, code=None, reason=None):
    fields = {name for model in (*PAYLOADS.values(), Hello, Vehicle, Lap, Wheel) for name in model.model_fields}
    errors = []
    if isinstance(exc, ValidationError):
        for error in exc.errors(include_input=False, include_context=False, include_url=False)[:5]:
            errors.append({"field": [part if isinstance(part, int) or part in fields else "?"
                                      for part in error["loc"][:6]], "type": error["type"]})
    logger.warning(json.dumps({"event": "invalid_ingest_message", "agent_id": agent_key,
                              "message_type": kind, "error_class": type(exc).__name__,
                              "validation": errors, "close_code": code, "close_reason": reason}))


def vehicle_identity(row):
    # Legacy storage/API fields remain compatible. Pitwall mVehicleName is an
    # entry/livery label, not a reliable vehicle model or manufacturer.
    return {"entry_name": row.get("car_name"), "vehicle_class": row.get("car_class"),
            "vehicle_model": None, "manufacturer": None}


@asynccontextmanager
async def lifespan(app):
    await bootstrap()
    cleanup = asyncio.create_task(cleanup_sessions())
    try:
        yield
    finally:
        cleanup.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup
        if pool:
            await pool.close()


app = FastAPI(title="Stintara", version="0.1.0", lifespan=lifespan)
pool: asyncpg.Pool | None = None

# Per-agent runtime state. PostgreSQL remains the source of persisted history.
runtime: dict[str, dict[str, Any]] = {}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def decode_message(raw):
    def reject_nonfinite(value):
        raise ValueError("Non-finite JSON number")
    return json.loads(raw, parse_constant=reject_nonfinite)


async def create_db_pool() -> asyncpg.Pool:
    kwargs: dict[str, Any] = {"min_size": 1, "max_size": 10}
    if DATABASE_URL:
        return await asyncpg.create_pool(DATABASE_URL, **kwargs)
    return await asyncpg.create_pool(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        database=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        **kwargs,
    )


async def bootstrap() -> None:
    global pool, POSTGRES_PASSWORD
    agent_token = read_secret("BOOTSTRAP_AGENT_TOKEN", "BOOTSTRAP_AGENT_TOKEN_FILE")
    if "BOOTSTRAP_AGENT_TOKEN_FILE" not in os.environ:
        agent_token = agent_token.strip()  # Preserve environment-only behavior.
    if not agent_token.strip() or agent_token.strip().startswith(("change-", "replace-")):
        raise ValueError("Configure BOOTSTRAP_AGENT_TOKEN; default/placeholder tokens are rejected")
    if not DATABASE_URL:
        POSTGRES_PASSWORD = read_secret("POSTGRES_PASSWORD", "POSTGRES_PASSWORD_FILE", POSTGRES_PASSWORD)
        if not POSTGRES_PASSWORD or POSTGRES_PASSWORD.startswith(("change-", "replace-")):
            raise ValueError("Configure POSTGRES_PASSWORD or DATABASE_URL")
    driver_slug = os.environ.get("BOOTSTRAP_DRIVER_SLUG", "driver").strip().lower()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", driver_slug):
        raise ValueError("BOOTSTRAP_DRIVER_SLUG must contain lowercase letters, digits and hyphens")
    last_error: Exception | None = None
    for attempt in range(1, 31):
        try:
            pool = await create_db_pool()
            break
        except Exception as exc:
            last_error = exc
            if attempt == 30:
                raise
            await asyncio.sleep(2)
    if pool is None:
        raise RuntimeError("database connection failed") from last_error
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
            await migrate_sample_ids(conn)
        driver_name = os.environ.get("BOOTSTRAP_DRIVER_NAME", "Stintara Driver")
        driver_slug = os.environ.get("BOOTSTRAP_DRIVER_SLUG", "driver").strip().lower()
        agent_name = os.environ.get("BOOTSTRAP_AGENT_NAME", "gaming-pc")

        driver = await conn.fetchrow("SELECT id FROM drivers WHERE slug=$1", driver_slug)
        if not driver:
            driver_id = uuid7()
            await conn.execute(
                "INSERT INTO drivers(id,slug,display_name) VALUES($1,$2,$3)",
                driver_id, driver_slug, driver_name,
            )
        else:
            driver_id = driver["id"]
            await conn.execute("UPDATE drivers SET display_name=$2, updated_at=now() WHERE id=$1", driver_id, driver_name)

        agent = await conn.fetchrow("SELECT id FROM agents WHERE name=$1", agent_name)
        if not agent:
            await conn.execute(
                "INSERT INTO agents(id,driver_id,name,token_sha256) VALUES($1,$2,$3,$4)",
                uuid7(), driver_id, agent_name, token_hash(agent_token),
            )
        else:
            await conn.execute(
                "UPDATE agents SET driver_id=$2, token_sha256=$3, enabled=true WHERE id=$1",
                agent["id"], driver_id, token_hash(agent_token),
            )


async def cleanup_sessions():
    while True:
        async with ingest_lock:
            async with pool.acquire() as conn, conn.transaction():
                await expire_observations(conn, SESSION_TIMEOUT_SECONDS)
            now = time.monotonic()
            for key in list(runtime):
                if now - runtime[key].get("last_scoring_at", now) > SESSION_TIMEOUT_SECONDS:
                    runtime.pop(key, None)
        await asyncio.sleep(5)


@app.get("/health")
async def health() -> dict[str, str]:
    assert pool
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "version": "0.1.0"}


@app.get("/", response_class=HTMLResponse)
async def frontpage() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/drivers/{slug}/live", response_class=HTMLResponse)
async def driver_live_page(slug: str) -> HTMLResponse:
    html = (STATIC_DIR / "live.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__DRIVER_SLUG__", json.dumps(slug).replace("<", "\\u003c")))


async def get_live_rows(slug: str | None = None) -> list[dict[str, Any]]:
    assert pool
    query = """
        SELECT s.id AS session_id, s.session_type, s.track_name, s.track_length_m,
               s.started_at, d.slug, d.display_name,
               sp.position, sp.total_laps, sp.last_lap_time, sp.best_lap_time,
               sp.car_name, sp.car_class, sp.in_pits
          FROM sessions s
          JOIN session_participants sp ON sp.session_id=s.id
          JOIN drivers d ON d.id=sp.driver_id
         WHERE s.status='live' AND EXISTS (SELECT 1 FROM session_observations o JOIN agents a ON a.id=o.agent_id
             WHERE o.session_id=s.id AND a.driver_id=sp.driver_id AND o.ended_at IS NULL
             AND o.last_seen_at > now() - interval '15 seconds')
    """
    args: list[Any] = []
    if slug is not None:
        query += " AND d.slug=$1"
        args.append(slug.lower())
    query += " ORDER BY s.started_at DESC"
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
    result = [dict(r) for r in rows]
    for row in result:
        for state in runtime.values():
            if state.get("session_id") == row["session_id"] and state.get("driver_slug") == row["slug"]:
                scoring = state.get("scoring", {})
                vehicle = next((v for v in scoring.get("vehicles", []) if v["id"] == scoring.get("player_vehicle_id")), None)
                if vehicle:
                    for key in ("position", "total_laps", "last_lap_time", "best_lap_time", "car_name", "in_pits"):
                        row[key] = vehicle.get(key)
    for row in result:
        row.update(vehicle_identity(row))
    return result


@app.get("/api/live")
async def api_live() -> list[dict[str, Any]]:
    return await get_live_rows()


@app.get("/sessions/{session_id}/live", response_class=HTMLResponse)
async def session_live_page(session_id: UUID) -> HTMLResponse:
    html = (STATIC_DIR / "session.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__SESSION_ID__", json.dumps(str(session_id))))


# Freshness describes the observation feed, not whether a race is finished.
SESSION_LIVE_SQL = """s.status='live' AND EXISTS (
    SELECT 1 FROM session_observations o WHERE o.session_id=s.id
    AND o.ended_at IS NULL AND o.last_seen_at > now() - interval '15 seconds')"""


@app.get("/api/sessions/live")
async def api_live_sessions() -> list[dict[str, Any]]:
    assert pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT s.id, s.track_name, s.session_type, s.simulator_provider, s.started_at,
                   (SELECT count(*) FROM session_participants p WHERE p.session_id=s.id) AS participant_count
            FROM sessions s WHERE {SESSION_LIVE_SQL} ORDER BY s.started_at DESC, s.id
        """)
    return [dict(row) for row in rows]


def spectator_participants(rows, session_id, live, states, now):
    """Overlay one newest fresh observation; never combine different sessions by names."""
    participants = [dict(row) for row in rows]
    candidates = [state for state in states if live and state.get("session_id") == session_id
                  and now - state.get("last_scoring_at", 0) < 15 and state.get("scoring")]
    state = max(candidates, key=lambda s: s["last_scoring_at"], default={})
    vehicles = {v["id"]: v for v in state.get("scoring", {}).get("vehicles", [])}
    for p in participants:
        vehicle = vehicles.get(p["local_vehicle_id"])
        if vehicle:
            for key in ("position", "total_laps", "last_lap_time", "best_lap_time", "in_pits"):
                p[key] = vehicle.get(key)
        p.update(vehicle_identity(p))
        # Gap/interval interpretation is unverified. Keep raw ingestion unchanged.
        p.update(gap_to_leader=None, interval=None)
    return sorted(participants, key=lambda p: (p["position"] is None,
                                              p["position"] or 0, str(p["id"])))


@app.get("/api/sessions/{session_id}/live")
async def api_session_live(session_id: UUID) -> dict[str, Any]:
    assert pool
    async with pool.acquire() as conn:
        session = await conn.fetchrow(f"""SELECT s.id, s.track_name, s.session_type,
            s.simulator_provider, s.started_at, s.last_seen_at, s.status,
            s.session_laps, s.session_minutes, ({SESSION_LIVE_SQL}) AS live
            FROM sessions s WHERE s.id=$1""", session_id)
        if not session:
            raise HTTPException(404, "session not found")
        participants = await conn.fetch("""
            SELECT p.id, p.driver_id, d.slug AS driver_slug, p.local_vehicle_id,
                   p.display_name, p.team_name, p.car_name, p.car_class,
                   CASE WHEN p.provider='lmu' THEN NULL ELSE p.car_number END AS car_number,
                   p.position, p.total_laps, p.last_lap_time, p.best_lap_time, p.in_pits
            FROM session_participants p LEFT JOIN drivers d ON d.id=p.driver_id
            WHERE p.session_id=$1
        """, session_id)
    data = dict(session)
    live = data.pop("live")
    return {"session": data, "live": live,
            "participants": spectator_participants(participants, session_id, live,
                                                    runtime.values(), time.monotonic())}


@app.get("/api/sessions/{session_id}/participants/{participant_id}/fuel")
async def api_participant_fuel(session_id: UUID, participant_id: UUID) -> dict[str, Any]:
    assert pool
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        exists = await conn.fetchval("""SELECT p.id FROM session_participants p
            JOIN sessions s ON s.id=p.session_id WHERE p.id=$1 AND s.id=$2""", participant_id, session_id)
        if not exists:
            raise HTTPException(404, "session participant not found")
        checkpoints = await conn.fetch("""SELECT lap_number,fuel_remaining_l,pitstops,in_pits
            FROM laps WHERE session_participant_id=$1 ORDER BY lap_number""", participant_id)
        strategy = await conn.fetchrow("""SELECT fuel_l,fuel_capacity_l,recorded_at,
            fuel_avg_consumption_lap,fuel_laps_remaining,fuel_stint_number,fuel_stint_laps,fuel_stint_consumption_l
            FROM strategy_samples WHERE session_participant_id=$1 ORDER BY recorded_at DESC,id DESC LIMIT 1""",
                                       participant_id)
        now = await conn.fetchval("SELECT clock_timestamp()")
    return {"session_id": session_id, "participant_id": participant_id,
            **fuel_report(checkpoints, strategy, now)}


@app.get("/api/drivers/{slug}/live")
async def api_driver_live(slug: str) -> dict[str, Any]:
    assert pool
    async with pool.acquire() as conn:
        driver = await conn.fetchrow("SELECT id,slug,display_name FROM drivers WHERE slug=$1", slug.lower())
        if not driver:
            raise HTTPException(404, "driver not found")
        session = await conn.fetchrow(
            """
            SELECT s.* FROM sessions s
            JOIN session_participants sp ON sp.session_id=s.id
            WHERE sp.driver_id=$1 AND s.status='live' AND EXISTS (SELECT 1 FROM session_observations o JOIN agents a ON a.id=o.agent_id
             WHERE o.session_id=s.id AND a.driver_id=sp.driver_id AND o.ended_at IS NULL
             AND o.last_seen_at > now() - interval '15 seconds')
            ORDER BY s.started_at DESC LIMIT 1
            """, driver["id"]
        )
        if not session:
            return {"driver": dict(driver), "live": False}
        participants = await conn.fetch(
            """
            SELECT id AS participant_id,local_vehicle_id,display_name,team_name,car_name,car_class,
                   CASE WHEN provider='lmu' THEN NULL ELSE car_number END AS car_number,
                   position,total_laps,last_lap_time,best_lap_time,gap_to_leader,
                   laps_behind_leader,in_pits,pitstops,finish_status,has_telemetry,
                   CASE WHEN driver_id=$2 THEN true ELSE false END AS is_stintara_driver
            FROM session_participants
            WHERE session_id=$1
            ORDER BY position NULLS LAST, display_name
            """, session["id"], driver["id"]
        )
        strategy = await conn.fetchrow(
            """
            SELECT ss.* FROM strategy_samples ss
            JOIN session_participants sp ON sp.id=ss.session_participant_id
            WHERE sp.session_id=$1 AND sp.driver_id=$2
            ORDER BY ss.recorded_at DESC LIMIT 1
            """, session["id"], driver["id"]
        )
    participants = [dict(p) for p in participants]
    for state in runtime.values():
        if state.get("session_id") == session["id"] and time.monotonic() - state.get("last_scoring_at", 0) < 15:
            by_vehicle = {v["id"]: v for v in state.get("scoring", {}).get("vehicles", [])}
            for participant in participants:
                vehicle = by_vehicle.get(participant["local_vehicle_id"])
                if vehicle:
                    for key in ("position", "total_laps", "last_lap_time", "best_lap_time", "in_pits"):
                        participant[key] = vehicle.get(key)
                    participant["gap_to_leader"] = vehicle.get("time_behind_leader")
    participants.sort(key=lambda p: p["position"] if p["position"] is not None else 9999)
    for participant in participants:
        participant.update(vehicle_identity(participant))
    return {
        "driver": dict(driver),
        "live": True,
        "session": dict(session),
        "participants": participants,
        "strategy": dict(strategy) if strategy else None,
    }


async def authenticate_agent(token: str) -> asyncpg.Record | None:
    assert pool
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT a.id,a.name,a.driver_id,d.slug,d.display_name
            FROM agents a JOIN drivers d ON d.id=a.driver_id
            WHERE a.token_sha256=$1 AND a.enabled=true
            """, token_hash(token)
        )


async def current_or_create_session(agent, payload):
    state = runtime.setdefault(str(agent["id"]), {})
    async with pool.acquire() as conn, conn.transaction():
        session_id = await resolve_session(conn, agent, state, payload, SESSION_TIMEOUT_SECONDS)
    if session_id != state.get("session_id"):
        for key in ("last_sample_at", "last_persisted_scoring", "scoring"):
            state.pop(key, None)
    state["session_id"] = session_id
    state["session_type"] = payload["session_type"]
    return session_id


async def upsert_participants(agent: asyncpg.Record, session_id: Any, payload: dict[str, Any]) -> None:
    assert pool
    player_vehicle_id = payload.get("player_vehicle_id")
    vehicles = payload.get("vehicles") or []
    async with pool.acquire() as conn:
        for v in vehicles:
            local_id = int(v.get("id", -1))
            driver_id = agent["driver_id"] if local_id == player_vehicle_id else None
            await conn.execute(
                """
                INSERT INTO session_participants(
                    id,session_id,driver_id,provider,local_vehicle_id,display_name,team_name,car_name,car_class,car_number,
                    position,total_laps,last_lap_time,best_lap_time,gap_to_leader,laps_behind_leader,in_pits,last_seen_at)
                VALUES($1,$2,$3,$17,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,now())
                ON CONFLICT(session_id,local_vehicle_id) DO UPDATE SET
                    driver_id=COALESCE(session_participants.driver_id,EXCLUDED.driver_id),
                    team_name=EXCLUDED.team_name,car_name=EXCLUDED.car_name,
                    car_class=EXCLUDED.car_class,car_number=EXCLUDED.car_number,position=EXCLUDED.position,
                    total_laps=EXCLUDED.total_laps,last_lap_time=EXCLUDED.last_lap_time,best_lap_time=EXCLUDED.best_lap_time,
                    gap_to_leader=EXCLUDED.gap_to_leader,laps_behind_leader=EXCLUDED.laps_behind_leader,
                    in_pits=EXCLUDED.in_pits,last_seen_at=now()
                """,
                uuid7(), session_id, driver_id, local_id, v.get("driver_name") or f"Vehicle {local_id}",
                v.get("team_name"), v.get("car_name"), v.get("vehicle_class"), v.get("car_number"),
                v.get("position"), v.get("total_laps", 0), v.get("last_lap_time"), v.get("best_lap_time"),
                v.get("time_behind_leader"), v.get("laps_behind_leader"), bool(v.get("in_pits", False)), runtime[str(agent["id"])]["provider"],
            )
        await conn.execute("UPDATE sessions SET last_seen_at=now() WHERE id=$1", session_id)


async def store_lap_snapshots(agent: asyncpg.Record, session_id: Any, payload: dict[str, Any]) -> None:
    assert pool
    drivers = payload.get("drivers") or []
    async with pool.acquire() as conn:
        for d in drivers:
            local_id = int(d.get("id", -1))
            p = await conn.fetchrow(
                "SELECT id,driver_id FROM session_participants WHERE session_id=$1 AND local_vehicle_id=$2",
                session_id, local_id,
            )
            if not p:
                continue
            wheels = d.get("wheels") or [{}, {}, {}, {}]
            while len(wheels) < 4:
                wheels.append({})
            has_tel = bool(d.get("has_telemetry", False))
            lap_no = int(d.get("total_laps") or 0)
            if lap_no <= 0 or not d.get("last_lap_time") or d["last_lap_time"] <= 0:
                continue
            # LMU mWear is tread remaining: 1.0=new, 0.0=destroyed. Preserve that explicit meaning.
            await conn.execute(
                """
                INSERT INTO laps(
                    id,session_participant_id,lap_number,position,lap_time,sector1,sector2,sector3,
                    gap_to_leader,gap_ahead,fuel_remaining_l,fuel_capacity_l,compound_front,compound_rear,
                    tyre_remaining_fl,tyre_remaining_fr,tyre_remaining_rl,tyre_remaining_rr,in_pits,pitstops,finish_status,has_telemetry,tyre_snapshot,damage)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23::jsonb,$24::jsonb)
                ON CONFLICT(session_participant_id,lap_number) DO NOTHING
                """,
                uuid7(), p["id"], lap_no, d.get("position"), d.get("last_lap_time"),
                d.get("last_sector1"), d.get("last_sector2"), d.get("last_sector3"), d.get("gap_to_leader"),
                d.get("gap_ahead"), d.get("fuel_remaining"), d.get("fuel_capacity"),
                d.get("tire_compound_front_name"), d.get("tire_compound_rear_name"),
                wheels[0].get("remaining"), wheels[1].get("remaining"), wheels[2].get("remaining"), wheels[3].get("remaining"),
                bool(d.get("in_pits", False)), d.get("num_pitstops", 0), d.get("finish_status", 0), has_tel,
                json.dumps(wheels) if has_tel else None, json.dumps(d.get("damage")) if has_tel else None,
            )


async def store_strategy_sample(agent, session_id, payload):
    state = runtime[str(agent["id"])]
    now = time.monotonic()
    if not state.get("telemetry_valid", True) or not sample_due(state.get("last_sample_at"), now):
        return
    async with pool.acquire() as conn, conn.transaction():
        participant = await conn.fetchval(
            "SELECT id FROM session_participants WHERE session_id=$1 AND driver_id=$2 LIMIT 1",
            session_id, agent["driver_id"],
        )
        if not participant:
            return
        # Database timestamp check also enforces sampling after reconnect/restart.
        latest = await conn.fetchval("SELECT max(recorded_at) FROM strategy_samples WHERE session_participant_id=$1", participant)
        db_now = await conn.fetchval("SELECT now()")
        if latest and (db_now - latest).total_seconds() < 1:
            return
        fields = {
            "id": uuid7(), "session_participant_id": participant,
            "session_time": payload.get("current_et"), "speed_ms": payload.get("speed_ms"),
            "rpm": payload.get("rpm"), "gear": payload.get("gear"), "fuel_l": payload.get("fuel"),
            "fuel_capacity_l": payload.get("fuel_capacity"),
            "fuel_avg_consumption_lap": payload.get("fuel_avg_consumption"),
            "fuel_laps_remaining": payload.get("fuel_laps_remaining"),
            "fuel_stint_number": payload.get("fuel_stint_number"), "fuel_stint_laps": payload.get("fuel_stint_laps"),
            "fuel_stint_consumption_l": payload.get("fuel_stint_consumption"),
            "water_temp_c": payload.get("water_temp"), "oil_temp_c": payload.get("oil_temp"),
        }
        tires = (payload.get("tires") or []) + [{}, {}, {}, {}]
        for corner, tire in zip(("fl", "fr", "rl", "rr"), tires):
            fields[f"tyre_remaining_{corner}"] = tire.get("remaining")
            fields[f"tyre_pressure_{corner}_kpa"] = tire.get("pressure")
            temps = [tire.get(f"temp_{part}") for part in ("inner", "mid", "outer")]
            temps = [t for t in temps if t is not None]
            fields[f"tyre_temp_{corner}_c"] = sum(temps) / len(temps) if temps else None
            for name in ("temp_inner", "temp_mid", "temp_outer", "brake_temp"):
                fields[f"tyre_{name}_{corner}_c"] = tire.get(name)
        # Column names come exclusively from the whitelist above.
        columns = ','.join(fields)
        placeholders = ','.join(f'${i}' for i in range(1, len(fields) + 1))
        await conn.execute(f"INSERT INTO strategy_samples({columns}) VALUES({placeholders})", *fields.values())
    state["last_sample_at"] = now


async def finalize_agent_session(agent):
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch("SELECT session_id FROM session_observations WHERE agent_id=$1 AND ended_at IS NULL", agent["id"])
        for row in rows:
            await end_observation(conn, row["session_id"], agent["id"])
    runtime.pop(str(agent["id"]), None)


async def handle_message(agent, provider, kind, payload):
    state = runtime.setdefault(str(agent["id"]), {})
    state.update(provider=provider, driver_slug=agent["slug"])
    if kind == "connection":
        state["game_version"] = payload.get("plugin_version")
        state["telemetry_valid"] = payload["telemetry_valid"]
        state["game_connected"] = payload["game_connected"]
        if not payload["game_connected"]:
            await finalize_agent_session(agent)
            runtime[str(agent["id"])] = {"provider": provider, "game_connected": False}
        return
    if kind == "session_info":
        state.update(track_name=payload["track_name"], track_length_m=payload.get("track_length"),
                     session_laps=payload.get("session_laps"), session_minutes=payload.get("session_minutes"),
                     weather_json=json.dumps(payload.get("weather", {})))
        if state.get("session_id") and state.get("persisted_track") == payload["track_name"]:
            async with pool.acquire() as conn:
                await conn.execute("""UPDATE sessions SET track_length_m=$2,session_laps=$3,
                    session_minutes=$4,weather=$5::jsonb WHERE id=$1 AND status='live'""",
                    state["session_id"], state.get("track_length_m"), state.get("session_laps"),
                    state.get("session_minutes"), state["weather_json"])
        return
    if state.get("game_connected") is False:
        return
    if kind == "scoring":
        # Receive at 2-5 Hz; only persist current scoring at 1 Hz (no scoring history).
        now = time.monotonic()
        previous = state.get("scoring", {})
        changed = (previous.get("session_type") != payload["session_type"]
                   or state.get("persisted_track") != state.get("track_name")
                   or (previous.get("session_time") is not None and payload.get("session_time") is not None
                       and payload["session_time"] < previous["session_time"] - 2))
        if changed or sample_due(state.get("last_persisted_scoring"), now):
            session_id = await current_or_create_session(agent, payload)
            if not session_id:
                return
            await upsert_participants(agent, session_id, payload)
            state["last_persisted_scoring"] = now
            state["persisted_track"] = state.get("track_name")
            async with pool.acquire() as conn:
                await conn.execute("UPDATE agents SET last_seen_at=now() WHERE id=$1", agent["id"])
        state["scoring"] = payload
        state["last_scoring_at"] = now
        pending = state.pop("pending_laps", None)
        if pending:
            await handle_message(agent, provider, "lap_snapshots", pending)
        return
    session_id = state.get("session_id")
    if not session_id:
        if kind == "lap_snapshots":
            state["pending_laps"] = payload
        return
    if kind == "lap_snapshots":
        scoring = state.get("scoring", {})
        elapsed, lap_elapsed = scoring.get("session_time"), payload.get("session_time")
        if payload["session_type"] != state.get("session_type"):
            return
        if elapsed is not None and lap_elapsed is not None and abs(elapsed - lap_elapsed) > 5:
            return  # cached snapshot from another session/reconnect
        await store_lap_snapshots(agent, session_id, payload)
    elif kind == "telemetry" and time.monotonic() - state.get("last_scoring_at", 0) < 15:
        if state.get("track_name") != state.get("persisted_track"):
            return
        elapsed = state.get("scoring", {}).get("session_time")
        if elapsed is not None and payload.get("current_et") is not None and payload["current_et"] < elapsed - 2:
            return
        await store_strategy_sample(agent, session_id, payload)


@app.websocket("/ws/ingest")
async def ingest(ws: WebSocket):
    await ws.accept()
    agent_key = None
    kind = None
    invalid_payloads = deque()
    try:
        hello = Hello.model_validate(decode_message(await asyncio.wait_for(ws.receive_text(), timeout=10)))
        agent = await authenticate_agent(hello.token)
        if not agent:
            log_invalid(None, None, ValueError(), 1008, "invalid token")
            await ws.close(code=1008, reason="invalid token")
            return
        key = str(agent["id"])
        if key in active_agents:
            log_invalid(key, None, ValueError(), 1008, "agent already connected")
            await ws.close(code=1008, reason="agent already connected")
            return
        agent_key = key
        active_agents.add(key)
        await ws.send_json({"type": "agent_welcome", "driver_slug": agent["slug"], "version": "0.1.0"})
        while True:
            kind = None
            raw = await ws.receive_text()
            if len(raw) > 2 * 1024 * 1024:
                log_invalid(agent_key, None, ValueError(), 1009, "message too large")
                await ws.close(code=1009, reason="message too large")
                return
            message = decode_message(raw)
            candidate = message.get("message_type") if isinstance(message, dict) else None
            kind = candidate if isinstance(candidate, str) and candidate in PAYLOADS else None
            if not isinstance(message, dict) or message.get("protocol") != "stintara-agent-v1":
                raise ValueError("invalid protocol")
            if kind is None:
                raise ValueError("unsupported message")
            if not isinstance(message.get("payload"), dict):
                raise TypeError("payload must be an object")
            try:
                payload = PAYLOADS[kind].model_validate(message["payload"]).model_dump()
            except ValidationError as exc:
                # Only authenticated, recognized provider data can be dropped.
                # A rolling budget cannot be reset by interleaving good messages.
                if kind not in ("scoring", "telemetry", "lap_snapshots"):
                    raise
                now = time.monotonic()
                while invalid_payloads and now - invalid_payloads[0] >= INVALID_PAYLOAD_WINDOW_SECONDS:
                    invalid_payloads.popleft()
                invalid_payloads.append(now)
                fatal = len(invalid_payloads) >= MAX_INVALID_PAYLOADS
                reason = "too many invalid payloads" if fatal else None
                log_invalid(agent_key, kind, exc, 1008 if fatal else None, reason)
                if fatal:
                    await ws.close(code=1008, reason=reason)
                    return
                continue
            async with ingest_lock:
                await handle_message(agent, hello.simulator, kind, payload)
    except WebSocketDisconnect:
        pass
    except (asyncio.TimeoutError, json.JSONDecodeError, ValidationError, ValueError, TypeError, KeyError) as exc:
        log_invalid(agent_key, kind, exc, 1008, "invalid Stintara message")
        await ws.close(code=1008, reason="invalid Stintara message")
    finally:
        if agent_key:
            active_agents.discard(agent_key)
        # Preserve observation for short outages; cleanup closes it after timeout.
