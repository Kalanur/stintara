"""Compile using the production adapter and a relative logical clock."""
from collections import Counter

from agent.pitwall import normalize

from .common import Parser, output_location, run_cli
from .format import PROTOCOL, WIRE, header, load, validate_record, write


def compile_capture(metadata, records):
    output, last = [], {}
    counts = Counter(input=len(records), compiled=0, ignored=0)
    for row in records:
        try:
            normalized = normalize(row["message"])
        except (TypeError, ValueError, AttributeError, KeyError):
            normalized = None
        if normalized is None:
            counts["ignored"] += 1
            continue
        kind, payload = normalized
        interval = {"scoring": .2, "telemetry": 1.0}.get(kind, 0)
        if kind in last and row["t"] - last[kind] < interval:
            counts["ignored"] += 1
            continue
        compiled = dict(record_type="message", t=row["t"], message=dict(
            protocol="stintara-agent-v1", message_type=kind, payload=payload))
        validate_record(compiled, format=PROTOCOL)
        output.append(compiled)
        last[kind] = row["t"]
        counts["compiled"] += 1
        counts[kind] += 1
    return header(metadata["source"], PROTOCOL), output, dict(counts)


def compile_file(source, output):
    metadata, records = load(source, WIRE)
    result, compiled, counts = compile_capture(metadata, records)
    write(output_location(output, result["source"]), result, compiled)
    print(counts)


def cli():
    parser = Parser(description="Compile private captures or synthetic fixtures with logical agent throttling.")
    parser.add_argument("input")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run_cli(lambda: compile_file(args.input, args.output))


if __name__ == "__main__":
    cli()
