"""Bounded streaming diagnostics for v1 Pitwall wire captures (binary input)."""
import hashlib
import json
import math
from collections import Counter

from .format import WIRE, FormatError, finite, validate_header, validate_record

LINE_LIMIT = 32 * 1024 * 1024
TYPE_LIMIT = 128
FIELD_LIMIT = 256
SLOT_LIMIT = 256
EXAMPLE_LIMIT = 12
PLAYER_LAP_LIMIT = 256
PLAYER_PIT_LIMIT = 64
NAME_LIMIT = 96
VALUE_LIMIT = 16
MISSING = object()


def lines(stream, limit):
    """Drain oversized physical lines in chunks and yield one invalid marker."""
    while raw := stream.readline(limit + 1):
        if len(raw) > limit:
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(limit + 1)
            yield None
        else:
            yield raw


def reject_constant(_):
    raise ValueError("Non-finite JSON number")


def real(text):
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def short_name(value):
    return isinstance(value, str) and 0 < len(value) <= NAME_LIMIT


def sample(value):
    if isinstance(value, bool) or finite(value) or short_name(value):
        return value
    if isinstance(value, list) and len(value) <= VALUE_LIMIT:
        if all(isinstance(v, bool) or finite(v) or short_name(v) or v is None for v in value):
            return value.copy()
    return None


def measure(stats, value, t):
    if value is MISSING or value is None:
        key = "missing" if value is MISSING else "null"
        stats[key] = stats.get(key, 0) + 1
    elif finite(value):
        stats.setdefault("first_t", t)
        stats.setdefault("first_value", value)
        stats.update(last_t=t, last_value=value)
        stats["samples"] = stats.get("samples", 0) + 1
        stats["min"] = min(stats.get("min", value), value)
        stats["max"] = max(stats.get("max", value), value)
    else:
        stats["other"] = stats.get("other", 0) + 1


class Inspector:
    def __init__(self):
        self.events = {}
        self.types = Counter()
        self.fields = {}
        self.limits = Counter()
        self.ve = {key: {} for key in ("TelemetryUpdate.ve_available", "TelemetryUpdate.ve_history",
                                       "ElectronicsUpdate.virtual_energy", "ScoringUpdate.vehicles[].virtual_energy")}
        self.spatial = {}
        self.clocks = {}
        self.session = {}
        self.segment = 1
        self.player_report = {"lap_checkpoints": [], "pit_transitions": []}
        self.last_clock = {}
        self.reset_comparisons()

    def reset_comparisons(self):
        self.previous = {}
        self.latest_checkpoint = {}
        self.reset_player(None)
        self.length = None

    def reset_player(self, slot):
        self.player = slot
        self.local_ve = {}
        self.player_scoring = {}
        self.player_pit_values = {}
        self.player_stops = None

    def player_event(self, bucket, limit, point, **data):
        records = self.player_report[bucket]
        if len(records) >= limit:
            self.limits["omitted_player_" + bucket] += 1
            return
        t = point["t"]
        electronics = self.local_ve.get("electronics")
        records.append(dict(segment=self.segment, player_vehicle_id=self.player, t=t,
                            session_time=point["session_time"],
                            electronics_ve=dict(t=electronics[0], age_s=t-electronics[0],
                                                value=electronics[1]) if electronics else None,
                            **data))

    def player_sample(self, source, point):
        scoring = source == "ScoringUpdate.vehicles[]"
        previous = self.player_scoring
        if scoring:
            self.player_scoring = point
            if ("total_laps" in previous and "total_laps" in point
                    and point["total_laps"] > previous["total_laps"]):
                keys = ("total_laps", "lap_dist", "virtual_energy")
                self.player_event("lap_checkpoints", PLAYER_LAP_LIMIT, point,
                                  before={k: previous.get(k) for k in keys},
                                  after={k: point.get(k) for k in keys},
                                  in_pits=point.get("in_pits"), num_pitstops=self.player_stops)
        elif "num_pitstops" in point:
            self.player_stops = dict(t=point["t"], value=point["num_pitstops"])
        for field in ("in_pits", "num_pitstops"):
            if field not in point:
                continue
            key = (source, field)
            before = self.player_pit_values.get(key)
            after = point[field]
            if before is not None and before != after:
                self.player_event("pit_transitions", PLAYER_PIT_LIMIT, point,
                                  source=source, field=field, before=before, after=after,
                                  total_laps=point.get("total_laps", self.player_scoring.get("total_laps")),
                                  scoring_ve=self.player_scoring.get("virtual_energy"))
            self.player_pit_values[key] = after

    def event(self, name, t, **data):
        bucket = self.events.setdefault(name, {"count": 0, "examples": []})
        bucket["count"] += 1
        if len(bucket["examples"]) < EXAMPLE_LIMIT:
            bucket["examples"].append(dict(t=t, segment=self.segment, **data))

    def state(self, source, slot):
        key = (source, slot)
        if key not in self.previous and len(self.previous) >= SLOT_LIMIT:
            self.limits["untracked_context_samples"] += 1
            return {}
        return self.previous.setdefault(key, {})

    def metadata(self, m, kind, t):
        changes = []
        keys = ("track_name", "session_type") if kind in ("SessionInfo", "ScoringUpdate", "AllDriversUpdate") else ()
        for key in keys:
            value = m.get(key)
            if isinstance(value, str) and value.strip():
                # Keep identities out of the report and out of long-lived state.
                digest = hashlib.sha256(value.strip().encode("utf-8", "surrogatepass")).digest()
                if key in self.session and self.session[key] != digest:
                    changes.append(key)
                self.session[key] = digest
        if changes:
            self.segment += 1
            self.reset_comparisons()
            self.event("session_change", t, changed_fields=changes,
                       incoming_clock=m.get("session_time") if finite(m.get("session_time")) else None)
        if kind == "SessionInfo" and finite(m.get("track_length")):
            self.length = m["track_length"]
        clock = m.get("session_time")
        if finite(clock):
            measure(self.clocks.setdefault(kind, {}), clock, t)
            previous = self.last_clock.get(kind)
            if previous is not None and clock < previous[0]:
                self.event("clock_rollback", t, source=kind, previous=previous[0], current=clock,
                           previous_segment=previous[1])
                # A backwards clock is evidence of a discontinuity, not a session ID.
                if self.segment == previous[1]:
                    self.reset_comparisons()
            self.last_clock[kind] = (clock, self.segment)

    def observed(self, path, value, state, t, slot):
        if path not in self.fields and len(self.fields) >= FIELD_LIMIT:
            self.limits["untracked_field_samples"] += 1
            return
        stats = self.fields.setdefault(path, {"count": 0, "values": [], "changes": 0})
        stats["count"] += 1
        val = sample(value)
        if val is None and value is not None:
            stats["unrepresented"] = stats.get("unrepresented", 0) + 1
            return
        if finite(val):
            measure(stats, val, t)
        if not any(type(v) is type(val) and v == val for v in stats["values"]):
            if len(stats["values"]) < VALUE_LIMIT:
                stats["values"].append(val)
            else:
                stats["values_truncated"] = True
        if path in state and state[path] != val:
            stats["changes"] += 1
            if not finite(val) or "time" not in path.lower():
                self.event("field_transition", t, field=path, slot=slot, previous=state[path], current=val)
        state[path] = val

    def scan_fields(self, obj, prefix, state, t, slot, depth=0):
        for key, value in obj.items():
            if not short_name(key):
                continue
            lower = key.lower()
            path = prefix + "." + key
            if lower in ("num_pitstops", "in_pits") or any(token in lower for token in (
                "sector", "flag", "valid", "offtrack", "off_track", "track_limit",
                "cut", "penalt", "yellow", "safety_car", "game_phase",
            )):
                self.observed(path, value, state, t, slot)
            if depth < 4:
                if isinstance(value, dict):
                    self.scan_fields(value, path, state, t, slot, depth + 1)
                elif isinstance(value, list) and key not in ("vehicles", "drivers", "ve_history"):
                    for item in value:
                        if isinstance(item, dict):
                            # Unidentified array members do not form a time series.
                            self.scan_fields(item, path + "[]", {}, t, slot, depth + 1)

    def energy(self, source, value, t, state):
        stats = self.ve[source]
        measure(stats, value, t)
        previous = state.get("ve")
        if finite(value) and previous is not None and value != previous:
            stats["temporal_changes"] = stats.get("temporal_changes", 0) + 1
        if finite(value) and previous is not None and value > previous:
            self.event("ve_increase", t, source=source, previous=previous, current=value,
                       slot=state.get("slot"))
        state["ve"] = value if finite(value) else None

    def vehicle(self, row, source, slot, clock, t):
        state = self.state(source, slot) if slot is not None else {}
        last = state.get("sample", {})
        point = {k: row[k] for k in ("total_laps", "lap_dist", "pos_x", "pos_y", "pos_z", "num_pitstops",
                                      "virtual_energy", "sector") if finite(row.get(k))}
        if type(row.get("in_pits")) is bool:
            point["in_pits"] = row["in_pits"]
        point.update(t=t, session_time=clock)
        if self.player is not None and slot == self.player and source != "TelemetryUpdate":
            self.player_sample(source, point)
        state["slot"] = slot
        for k in ("lap_dist", "pos_x", "pos_y", "pos_z"):
            measure(self.spatial.setdefault(source, {}).setdefault(k, {}), row.get(k, MISSING), t)
        context = dict(source=source, slot=slot, before=last, after=point)
        if all(k in last and k in point for k in ("pos_x", "pos_y", "pos_z")):
            distance = math.dist([last[k] for k in ("pos_x", "pos_y", "pos_z")],
                                 [point[k] for k in ("pos_x", "pos_y", "pos_z")])
            if distance > 100:
                self.event("spatial_step_over_100", t, distance=distance if finite(distance) else None, **context)
        lap_advanced = "total_laps" in point and "total_laps" in last and point["total_laps"] > last["total_laps"]
        if lap_advanced:
            self.event("lap_advance", t, local_ve=self.local_ve.copy() if slot == self.player else {}, **context)
            if point.get("lap_dist") is not None and last.get("lap_dist") is not None:
                if point["lap_dist"] < last["lap_dist"]:
                    self.event("lap_distance_wrap", t, track_length=self.length, **context)
        elif self.length and self.length > 0 and "lap_dist" in last and "lap_dist" in point:
            if .9 * self.length <= last["lap_dist"] <= self.length and 0 <= point["lap_dist"] <= .1 * self.length:
                self.event("distance_wrap_candidate", t, track_length=self.length, **context)
        for key in ("num_pitstops", "in_pits"):
            if key in point and key in last and point[key] != last[key]:
                self.event(key + "_change", t, latest_driver_sample=self.latest_checkpoint.get(slot), **context)
        if source == "AllDriversUpdate.drivers[]" and slot is not None:
            if slot in self.latest_checkpoint or len(self.latest_checkpoint) < SLOT_LIMIT:
                self.latest_checkpoint[slot] = point
        if source == "ScoringUpdate.vehicles[]":
            self.energy(source + ".virtual_energy", row.get("virtual_energy", MISSING), t, state)
        self.scan_fields(row, source, state, t, slot)
        state["sample"] = point

    def message(self, m, t):
        kind = m["type"]
        if not short_name(kind) or (kind not in self.types and len(self.types) >= TYPE_LIMIT):
            kind = "<other>"
            self.limits["type_overflow_samples"] += 1
        self.types[kind] += 1
        self.metadata(m, kind, t)
        if kind == "ConnectionStatus" and m.get("game_connected") is False:
            self.reset_comparisons()
            self.last_clock.clear()
        clock = m.get("session_time") if finite(m.get("session_time")) else None
        if kind == "ScoringUpdate" and "player_vehicle_id" in m:
            slot = m["player_vehicle_id"]
            slot = slot if type(slot) is int and slot >= 0 else None
            if self.player != slot:
                self.reset_player(slot)
                self.previous.pop(("ElectronicsUpdate", None), None)
        state = self.state(kind, None)
        if kind == "TelemetryUpdate":
            flag = m.get("ve_available", MISSING)
            stats = self.ve["TelemetryUpdate.ve_available"]
            if type(flag) is bool:
                stats.setdefault("first_t", t)
                key = str(flag).lower()
                stats[key] = stats.get(key, 0) + 1
                stats.setdefault("first_" + key + "_t", t)
            else:
                measure(stats, flag, t)
            history = m.get("ve_history", MISSING)
            stats = self.ve["TelemetryUpdate.ve_history"]
            if isinstance(history, list):
                stats["arrays"] = stats.get("arrays", 0) + 1
                stats["max_length"] = max(stats.get("max_length", 0), len(history))
                for value in history:
                    measure(stats, value, t)
                if history and finite(history[-1]):
                    tail = stats.setdefault("tail", {})
                    measure(tail, history[-1], t)
                    previous = self.local_ve.get("history_tail")
                    if previous and history[-1] > previous[1]:
                        self.event("ve_increase", t, source="TelemetryUpdate.ve_history[-1]",
                                   previous=previous[1], current=history[-1], slot=self.player)
                    self.local_ve["history_tail"] = (t, history[-1])
            else:
                measure(stats, history, t)
        if kind == "ElectronicsUpdate":
            value = m.get("virtual_energy", MISSING)
            self.energy("ElectronicsUpdate.virtual_energy", value, t, state)
            if finite(value):
                self.local_ve["electronics"] = (t, value)
        if kind == "TelemetryUpdate":
            self.vehicle(m, kind, self.player, clock, t)
        else:
            self.scan_fields(m, kind, state, t, None)
        for source, key in (("ScoringUpdate", "vehicles"), ("AllDriversUpdate", "drivers")):
            rows = m.get(key)
            if kind == source and isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        slot = row.get("id")
                        self.vehicle(row, kind + "." + key + "[]", slot if type(slot) is int else None, clock, t)


def inspect_stream(stream, *, line_limit=LINE_LIMIT):
    """Analyze an open binary stream. A valid wire header is required."""
    if type(line_limit) is not int or not 0 < line_limit <= LINE_LIMIT:
        raise ValueError("Invalid capture line limit")
    records = lines(stream, line_limit)
    first = next(records, None)
    if first is None:
        raise FormatError("Missing or oversized header")
    try:
        header = validate_header(json.loads(first), WIRE)
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise FormatError("Invalid capture header") from None
    analysis = Inspector()
    first_t = last_t = None
    invalid = valid = 0
    bad_lines = []
    for number, raw in enumerate(records, 2):
        try:
            if raw is None:
                raise FormatError("Oversized line")
            row = validate_record(json.loads(raw, parse_constant=reject_constant, parse_float=real), last_t or 0)
            m = row["message"]
            if not isinstance(m.get("type"), str) or not m["type"]:
                raise FormatError("Missing message type")
        except (ValueError, UnicodeError, RecursionError):
            invalid += 1
            if len(bad_lines) < EXAMPLE_LIMIT:
                bad_lines.append(number)
            continue
        valid += 1
        last_t = row["t"]
        if first_t is None:
            first_t = last_t
        analysis.message(m, last_t)
    duration = last_t - first_t if valid else None
    return dict(format="capture-report-v1", source=header["source"], lines=1 + valid + invalid,
                valid_records=valid, invalid_records=invalid, invalid_line_examples=bad_lines,
                first_t=first_t, last_t=last_t, duration=duration, segments=int(bool(valid)) + analysis.segment - 1,
                types=dict(analysis.types), rates_hz={k: n / duration if duration and finite(n / duration)
                                                     else None for k, n in analysis.types.items()},
                session_time=analysis.clocks, ve=analysis.ve, spatial=analysis.spatial, fields=analysis.fields,
                events=analysis.events, player=analysis.player_report, limits_hit=dict(analysis.limits))
