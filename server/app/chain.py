"""Persisted Practice/Qualifying/Race navigation (#43). Reads only PostgreSQL, never runtime state."""
from typing import Any

# Joined into the existing session query so the endpoint keeps its bounded query count.
CHAIN_COLUMNS = """s.race_id, r.event_id, p.id AS prev_id, p.session_type AS prev_type,
            n.id AS next_id, n.session_type AS next_type"""
CHAIN_JOINS = """LEFT JOIN races r ON r.id=s.race_id
            LEFT JOIN sessions p ON p.id=s.previous_session_id
            LEFT JOIN sessions n ON n.previous_session_id=s.id"""


def _ref(session_id, session_type) -> dict[str, Any] | None:
    return {"id": session_id, "session_type": session_type} if session_id else None


def pop_chain(data: dict[str, Any]) -> dict[str, Any]:
    """Replace the raw join columns of a session row by the public navigation contract."""
    prev_id, prev_type = data.pop("prev_id", None), data.pop("prev_type", None)
    next_id, next_type = data.pop("next_id", None), data.pop("next_type", None)
    data.setdefault("race_id", None)
    data["event_id"] = data.get("event_id")
    data["previous_session"] = _ref(prev_id, prev_type)
    data["next_session"] = _ref(next_id, next_type)
    return data
