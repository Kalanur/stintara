"""File-local pseudonyms; unknown text remains private and requires review."""
from collections import Counter, defaultdict

from agent.pitwall import MESSAGE_TYPES

from .common import Parser, output_location, run_cli
from .format import WIRE, header, load, write

IDENTITY = {"driver_name": "Driver", "team_name": "Synthetic Team",
            "car_name": "Synthetic Entry", "track_name": "Synthetic Circuit"}
# Only fixed schema labels may be printed. Unknown keys can themselves contain secrets.
AUDIT_KEYS = set(IDENTITY) | {"vehicles", "drivers", "tires", "wheels", "weather", "type",
    "vehicle_class", "session_type", "plugin_version", "tire_compound_front_name", "tire_compound_rear_name",
    "finish_status", "fuel", "in_pits", "id", "position"}


def sanitize(metadata, records):
    mappings, audit = defaultdict(dict), Counter()

    def visit(value, path, key=None):
        if isinstance(value, str):
            if key in IDENTITY:
                mapping = mappings[key]
                if value not in mapping:
                    mapping[value] = f"{IDENTITY[key]} {len(mapping)+1:03d}"
                return mapping[value]
            if not (key == "type" and value in MESSAGE_TYPES):
                audit[path] += 1
            return value
        if isinstance(value, list):
            return [visit(item, path+"[]") for item in value]
        if isinstance(value, dict):
            result = {}
            for name, item in value.items():
                safe = name if name in AUDIT_KEYS else "[unknown]"
                if name not in AUDIT_KEYS:
                    # Keys are strings too, even when their values are numeric.
                    audit[path+".[unknown-key]"] += 1
                result[name] = visit(item, path+"."+safe, name)
            return result
        return value

    output = []
    for row in records:
        kind = row["message"].get("type")
        prefix = kind if isinstance(kind, str) and kind in MESSAGE_TYPES else "UnknownMessage"
        output.append(dict(record_type="message", t=row["t"], message=visit(row["message"], prefix)))
    return header("sanitized_real", WIRE), output, dict(sorted(audit.items()))


def sanitize_file(source, output):
    metadata, records = load(source, WIRE)
    result, sanitized, audit = sanitize(metadata, records)
    write(output_location(output, result["source"]), result, sanitized)
    print("Private sanitized capture; public review required.")
    for path, count in audit.items():
        print(f"Review path: {path} ({count} occurrences)")


def cli():
    parser = Parser(description="Pseudonymize a private capture; never overwrite input or claim public safety.")
    parser.add_argument("input")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run_cli(lambda: sanitize_file(args.input, args.output))


if __name__ == "__main__":
    cli()
