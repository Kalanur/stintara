"""Inspect a Pitwall JSONL capture with bounded memory."""
import json
from pathlib import Path

from .common import Parser, output_location, run_cli
from .inspector import inspect_stream


def inspect(path, output=None):
    with Path(path).open("rb") as stream:
        report = inspect_stream(stream)
    if output:
        destination = output_location(output, report["source"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, allow_nan=False, indent=2)
            stream.write("\n")
    duration = report["duration"]
    elapsed = f"{duration:.1f}s" if duration is not None else "unknown"
    print(f"Messages: {report['valid_records']} valid, {report['invalid_records']} invalid; "
          f"duration {elapsed}; {report['segments']} observed segment(s).")
    print("Most frequent types:", json.dumps(sorted(report["types"].items(), key=lambda pair: -pair[1])[:8]))
    for source, stats in report["ve"].items():
        print(source, json.dumps(stats))
    print("Diagnostic event counts:", json.dumps({k: v["count"] for k, v in report["events"].items()}))


def cli():
    parser = Parser(description=__doc__)
    parser.add_argument("capture")
    parser.add_argument("--output", help="New JSON report path (under data/ for real-derived reports)")
    args = parser.parse_args()
    run_cli(lambda: inspect(args.capture, args.output))


if __name__ == "__main__":
    cli()
