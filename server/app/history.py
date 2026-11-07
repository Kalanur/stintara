"""Persisted-only session history. Reads PostgreSQL only; never runtime or live overlays."""
from __future__ import annotations

from typing import Any

HISTORY_DEFAULT_LIMIT = 50
HISTORY_MAX_LIMIT = 100
RECENT_RESULTS_LIMIT = 5
FINISHED = 1  # LMU finish_status: 1 = finished (FIN)
STATUSES = ("live", "complete")
FILTER_OPTION_LIMIT = 500

# Winner: position=1 must be unique within the session, so ``n`` is checked in Python.
# Stintara driver: exactly one participant of the session has a driver_id (``n``).
_SELECT = """
SELECT s.id, s.simulator_provider, s.session_type, s.track_name, s.status,
       s.started_at, s.ended_at,
       (SELECT count(*) FROM session_participants p WHERE p.session_id=s.id) AS participant_count,
       w.id AS w_id, w.display_name AS w_name, w.position AS w_position,
       w.finish_status AS w_finish, w.n AS w_n,
       k.id AS k_id, k.slug AS k_slug, k.driver_name AS k_name, k.position AS k_position,
       k.finish_status AS k_finish, k.n AS k_n
FROM sessions s
LEFT JOIN LATERAL (
    SELECT p.id, p.display_name, p.position, p.finish_status, count(*) OVER () AS n
    FROM session_participants p WHERE p.session_id=s.id AND p.position=1 LIMIT 1
) w ON true
LEFT JOIN LATERAL (
    SELECT p.id, d.slug, d.display_name AS driver_name, p.position, p.finish_status,
           count(*) OVER () AS n
    FROM session_participants p JOIN drivers d ON d.id=p.driver_id
    WHERE p.session_id=s.id LIMIT 1
) k ON true
"""

RECENT_ORDER = "s.ended_at DESC, s.id DESC"
HISTORY_ORDER = "s.started_at DESC, s.id DESC"


def serialize(row) -> dict[str, Any]:
    """Stable response contract. Result fields are only set when persisted data is unambiguous."""
    winner = None
    if (row["status"] == "complete" and row["session_type"] == "Race" and row["w_n"] == 1
            and row["w_finish"] == FINISHED):
        winner = {"participant_id": row["w_id"], "display_name": row["w_name"],
                  "position": row["w_position"], "finish_status": row["w_finish"]}
    driver = None
    if row["k_n"] == 1:
        driver = {"participant_id": row["k_id"], "driver_slug": row["k_slug"],
                  "display_name": row["k_name"], "position": row["k_position"],
                  "finish_status": row["k_finish"]}
    return {"id": row["id"], "simulator_provider": row["simulator_provider"],
            "session_type": row["session_type"], "track_name": row["track_name"],
            "status": row["status"],
            "started_at": row["started_at"], "ended_at": row["ended_at"],
            "participant_count": row["participant_count"],
            "winner": winner, "stintara_driver": driver}


async def query_sessions(conn, *, session_type=None, provider=None, track=None, status=None,
                         limit=HISTORY_DEFAULT_LIMIT, offset=0, recent=False) -> dict[str, Any]:
    """One parameterised query for Home (recent=True) and /history; fetches limit+1 for has_more."""
    if status is not None and status not in STATUSES:
        raise ValueError("status must be one of: " + ", ".join(STATUSES))
    if not 1 <= limit <= HISTORY_MAX_LIMIT or offset < 0:
        raise ValueError("limit must be 1..%d and offset >= 0" % HISTORY_MAX_LIMIT)
    where: list[str] = []
    args: list[Any] = []
    if recent:  # completed races with a real completion time; live ones never qualify
        status, session_type = "complete", "Race"
        where.append("s.ended_at IS NOT NULL")
    for column, value in (("s.session_type", session_type), ("s.simulator_provider", provider),
                          ("s.track_name", track), ("s.status", status)):
        if value is not None:
            args.append(value)
            where.append(f"{column}=${len(args)}")
    args.extend([limit + 1, offset])
    sql = (_SELECT + (" WHERE " + " AND ".join(where) if where else "")
           + f" ORDER BY {RECENT_ORDER if recent else HISTORY_ORDER}"
           + f" LIMIT ${len(args) - 1} OFFSET ${len(args)}")
    rows = await conn.fetch(sql, *args)
    return {"items": [serialize(r) for r in rows[:limit]], "limit": limit, "offset": offset,
            "has_more": len(rows) > limit}


async def filter_options(conn) -> dict[str, list[str]]:
    async def distinct(column, extra=""):
        rows = await conn.fetch(f"SELECT DISTINCT {column} AS v FROM sessions {extra} ORDER BY 1 LIMIT $1",
                                FILTER_OPTION_LIMIT)
        return [r["v"] for r in rows]
    return {"providers": await distinct("simulator_provider"),
            "tracks": await distinct("track_name", "WHERE track_name IS NOT NULL"),
            "session_types": await distinct("session_type")}
