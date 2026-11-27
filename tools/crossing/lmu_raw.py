"""LMU Pitwall RAW capture adapter: transport JSONL -> neutral observations. Streaming, one line at a time.

RAW means the Pitwall WebSocket JSON before any Stintara normalisation. This adapter is separate from
``agent.pitwall`` on purpose: it keeps raw values (no sentinel cleaning) and never copies driver, team or car names
into its output. Only fields the detector needs are extracted.

Wire facts used (observed in private captures, not a published contract):
* ``ScoringUpdate.vehicles[].total_laps`` is the lap counter; ``lap_start_et`` and ``session_time`` share one clock.
* ``TelemetryUpdate`` has ``current_et`` and ``lap_start_et`` but **no vehicle id**.
* Ground plane is ``(pos_x, pos_z)``; ``pos_y`` is height.
"""
import json
import math
from collections import Counter

from tools.replay.format import WIRE, FormatError, validate_header

from .model import LinkObs, PhaseObs, ScoringObs, SessionMeta, TelemetryObs, VehicleObs

LINE_LIMIT = 32 * 1024 * 1024
IGNORED_TYPES = frozenset({"AllDriversUpdate", "ElectronicsUpdate", "VersionInfo"})
PHASES = {0: "pre_session", 3: "formation", 5: "green", 6: "full_course_yellow", 7: "session_stopped",
          8: "session_over"}


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def integer(value):
    return value if type(value) is int else None


def reject_constant(_):
    raise ValueError("Non-finite JSON number")


def lines(stream):
    while raw := stream.readline(LINE_LIMIT + 1):
        if len(raw) > LINE_LIMIT:
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(LINE_LIMIT + 1)
            yield None
        else:
            yield raw


def peek_type(raw):
    """Cheap message-type sniff on the first bytes so ignored bulk messages are never parsed."""
    head = raw[:160]
    start = head.find(b'"message":{"type":"')
    if start < 0:
        return None
    start += len(b'"message":{"type":"')
    end = head.find(b'"', start)
    return head[start:end].decode("ascii", "replace") if end > 0 else None


def vehicle(raw):
    if not isinstance(raw, dict) or integer(raw.get("id")) is None:
        return None
    return VehicleObs(raw["id"], integer(raw.get("total_laps")), number(raw.get("lap_start_et")),
                      number(raw.get("lap_dist")), number(raw.get("pos_x")), number(raw.get("pos_z")),
                      raw["in_pits"] if isinstance(raw.get("in_pits"), bool) else None)


def convert(message, rec_t):
    """One parsed wire message -> neutral observation, or None. Malformed content yields None."""
    kind = message.get("type")
    if kind == "ScoringUpdate":
        session_time = number(message.get("session_time"))
        vehicles = message.get("vehicles")
        if session_time is None or not isinstance(vehicles, list):
            return None
        parsed = tuple(v for v in map(vehicle, vehicles) if v is not None)
        player = integer(message.get("player_vehicle_id"))
        session_kind = message.get("session_type")
        return ScoringObs(rec_t, session_time, session_kind if isinstance(session_kind, str) else None,
                          player if player is not None and player >= 0 else None, parsed)
    if kind == "TelemetryUpdate":
        position = message.get("position")
        if not isinstance(position, dict):
            return None
        et, gx, gy = number(message.get("current_et")), number(position.get("x")), number(position.get("z"))
        if et is None or gx is None or gy is None:
            return None
        velocity = message.get("velocity") if isinstance(message.get("velocity"), dict) else {}
        return TelemetryObs(rec_t, et, number(message.get("lap_start_et")), gx, gy, number(position.get("y")),
                            number(message.get("speed_ms")), number(velocity.get("x")), number(velocity.get("z")))
    if kind == "SessionInfo":
        name, length = message.get("track_name"), number(message.get("track_length"))
        if not isinstance(name, str) or not name.strip() or length is None or length <= 0:
            return None  # transient empty snapshots are not a layout statement
        return SessionMeta(rec_t, name.strip(), length)
    if kind == "VehicleStatusUpdate":
        return PhaseObs(rec_t, PHASES.get(integer(message.get("game_phase")), "unknown"))
    if kind == "ConnectionStatus":
        connected = message.get("game_connected")
        return LinkObs(rec_t, connected) if isinstance(connected, bool) else None
    return None


def read(stream, stats=None):
    """Yield observations from a binary RAW capture stream. ``stats`` (a Counter) receives input counts.

    Raises FormatError for a missing/invalid header or a non-wire format. Malformed records are counted and skipped.
    """
    stats = stats if stats is not None else Counter()
    iterator = lines(stream)
    first = next(iterator, None)
    if first is None:
        raise FormatError("Missing header")
    try:
        header = validate_header(json.loads(first, parse_constant=reject_constant), WIRE)
    except ValueError:
        raise FormatError("Invalid header") from None
    stats["source_header"] = header["source"]
    previous = 0.0
    for raw in iterator:
        if raw is None:
            stats["oversized_lines"] += 1
            continue
        sniffed = peek_type(raw)
        if sniffed in IGNORED_TYPES:  # bulk messages the detector never uses are counted, not parsed
            stats["records"] += 1
            stats["type:" + sniffed] += 1
            continue
        try:
            record = json.loads(raw, parse_constant=reject_constant)
            rec_t = number(record["t"])
            message = record["message"]
            if record["record_type"] != "message" or rec_t is None or rec_t < 0 or not isinstance(message, dict):
                raise ValueError
        except (ValueError, KeyError, TypeError, RecursionError):
            stats["invalid_lines"] += 1
            continue
        if rec_t < previous:
            stats["recorder_time_regression_lines"] += 1  # also surfaced to the detector as a continuity break
        previous = rec_t
        kind = message.get("type")
        stats["records"] += 1
        stats["type:" + (kind if kind in KNOWN else "other")] += 1
        event = convert(message, rec_t)
        if event is None:
            if kind in KNOWN:
                stats["unusable:" + kind] += 1
            continue
        yield event


KNOWN = frozenset({"ScoringUpdate", "TelemetryUpdate", "SessionInfo", "VehicleStatusUpdate", "ConnectionStatus"})
