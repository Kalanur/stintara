"""Versioned JSONL with payload-free validation errors and exclusive writes."""
import json
import math
from pathlib import Path

WIRE = "stintara-pitwall-capture"
PROTOCOL = "stintara-protocol-replay"
SOURCES = {"synthetic", "real", "sanitized_real"}
KINDS = {"connection", "session_info", "scoring", "lap_snapshots", "telemetry"}


class FormatError(ValueError):
    pass


def finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def header(source="synthetic", format=WIRE):
    result = dict(record_type="header", format=format, version=1, source=source)
    if source == "sanitized_real":
        result["public_review_required"] = True
    return result


def validate_header(value, expected=None):
    if not isinstance(value, dict) or value.get("record_type") != "header":
        raise FormatError("Missing header")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise FormatError("Unsupported version")
    if value.get("format") not in (WIRE, PROTOCOL) or (expected and value["format"] != expected):
        raise FormatError("Unsupported format")
    if not isinstance(value.get("source"), str) or value["source"] not in SOURCES:
        raise FormatError("Invalid source")
    if set(value) - {"record_type", "format", "version", "source", "public_review_required"}:
        raise FormatError("Unexpected header metadata")
    if ("public_review_required" in value and value["public_review_required"] is not True
            or value["source"] == "sanitized_real" and value.get("public_review_required") is not True):
        raise FormatError("Review marker required")
    return value


def validate_record(value, previous=0, format=WIRE):
    if not isinstance(value, dict) or set(value) != {"record_type", "t", "message"} or value["record_type"] != "message":
        raise FormatError("Invalid message record")
    if not finite(value["t"]) or value["t"] < 0 or value["t"] < previous:
        raise FormatError("Invalid or decreasing relative time")
    message = value["message"]
    if not isinstance(message, dict):
        raise FormatError("Message must be an object")
    if format == PROTOCOL:
        if (set(message) != {"protocol", "message_type", "payload"}
                or message.get("protocol") != "stintara-agent-v1"
                or not isinstance(message.get("message_type"), str)
                or message["message_type"] not in KINDS or not isinstance(message.get("payload"), dict)):
            raise FormatError("Invalid protocol envelope")
        reject_connection_metadata(message["payload"])
    return value


def reject_connection_metadata(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"token", "agent_token", "server_ws", "pitwall_ws", "authorization"}:
                raise FormatError("Connection metadata forbidden in protocol replay")
            reject_connection_metadata(item)
    elif isinstance(value, list):
        for item in value:
            reject_connection_metadata(item)


def decode(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        raise FormatError("Invalid JSON") from None


def encode(value):
    try:
        return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
    except (TypeError, ValueError, RecursionError):
        raise FormatError("Non-JSON or non-finite value") from None


def loads(text, expected=None):
    lines = text.splitlines()
    if not lines:
        raise FormatError("Missing header")
    metadata = validate_header(decode(lines[0]), expected)
    records, previous = [], 0
    for line in lines[1:]:
        row = validate_record(decode(line), previous, metadata["format"])
        encode(row)  # Reject non-finite JSON constants anywhere, without echoing input.
        records.append(row)
        previous = row["t"]
    return metadata, records


def dumps(metadata, records):
    validate_header(metadata)
    lines, previous = [encode(metadata)], 0
    for row in records:
        validate_record(row, previous, metadata["format"])
        lines.append(encode(row))
        previous = row["t"]
    return "".join(lines)


def load(path, expected=None):
    try:
        return loads(Path(path).read_text(encoding="utf-8"), expected)
    except (OSError, UnicodeError):
        raise FormatError("Cannot read capture") from None


def write(path, metadata, records):
    text = dumps(metadata, records)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
