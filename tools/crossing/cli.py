"""Local, read-only diagnostic CLI: ``python -m tools.crossing <capture.jsonl> [--output report.json]``.

Reads one v1 Pitwall RAW capture, never writes next to it and never touches Stintara, the agent or a database. The
optional report is exclusive-create and, for non-synthetic captures, must stay under the ignored ``data/`` directory.
Nothing printed or written here is an accepted start/finish marker.
"""
import json
from collections import Counter
from pathlib import Path

from tools.replay.common import Parser, output_location, run_cli

from .detector import Config, Detector
from .lmu_raw import read

REPORT_SCHEMA = "crossing-evidence-report-v1"


def analyse(path, config=None):
    """Stream one capture through the adapter and detector; returns the full report dictionary."""
    stats = Counter()
    detector = Detector(config or Config())
    with Path(path).open("rb") as stream:
        for event in read(stream, stats):
            detector.feed(event)
    detector.finish()
    report = detector.report()
    report["report_schema"] = REPORT_SCHEMA
    report["input"].update({k: v for k, v in sorted(stats.items())})
    return report


def render(report):
    counts, interval = report["counts"], report["interval"]
    filtered, raw = report["dispersion"]["filtered"], report["dispersion"]["unfiltered"]

    def line(label, block):
        if not block or not block.get("n"):
            return f"  {label}: n=0"
        return (f"  {label}: n={block['n']} median={block['median']:.3f} p90={block['p90']:.3f} "
                f"max={block['max']:.3f}")

    out = ["DIAGNOSTIC EVIDENCE ONLY - no start/finish anchor is computed or accepted.",
           f"Schema {report['schema']} | source header: {report['input'].get('source_header')}",
           "Input: " + json.dumps({k: v for k, v in report["input"].items() if k.startswith(("type:", "records"))}),
           f"Wrap candidates {counts['wrap_candidates']} "
           f"(with counter increase {counts['wrap_with_counter_increase']}, "
           f"without {counts['wrap_without_counter_increase']}); "
           f"counter increases {counts['counter_increases']} "
           f"(outside wrap list {counts['counter_increase_without_wrap']}); anomalies "
           f"{json.dumps(counts['counter_anomalies'])}",
           f"Resolved {counts['transitions_resolved']}; diagnostic candidates {counts['diagnostic_candidates']}; "
           f"distinct candidate lap crossings {counts['distinct_candidate_lap_crossings']}; "
           f"lap_start_et arriving after the counter message {counts['late_lap_start_et_updates']} "
           f"{json.dumps(counts['late_lap_start_et_by_source'])}",
           "Rejections (first reason): " + json.dumps(report["rejections"]["first_reason"]),
           "Rejections (any reason):   " + json.dumps(report["rejections"]["any_reason"]),
           "Interval width [s] (candidates) -> " + line("", interval["width_s_candidates"]).strip(),
           "Interval travel [m] (candidates) -> " + line("", interval["travel_m_candidates"]).strip(),
           "Radial dispersion [m]:", line("unfiltered", raw["radial_m"]), line("filtered", filtered["radial_m"])]
    for axis in ("longitudinal_m", "lateral_m"):
        out.append(f"{axis} (absolute):")
        for label, block in (("unfiltered", raw), ("filtered", filtered)):
            out.append(line(label, block[axis]))
    provenance = report["provenance"]
    out += ["Provenance: source_epoch=" + str(provenance["source"]["source_epoch"])
            + f" ({provenance['source']['source_epoch_status']}); vehicle: {provenance['vehicle']['status']}"
            + f" {json.dumps(provenance['vehicle']['position_correlation'])};"
            + f" layout: {provenance['layout']['status']}, changes {provenance['layout']['layout_changes']}",
            f"Continuity: {provenance['continuity']['segments']} derived diagnostic segment(s) (not connection "
            f"epochs); breaks {json.dumps(provenance['continuity']['break_reasons'])}"]
    return "\n".join(out)


def write_report(report, output):
    destination = output_location(output, report["input"].get("source_header", "unknown"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, allow_nan=False, indent=2)
        stream.write("\n")


def run(path, output=None):
    report = analyse(path)
    if output:
        write_report(report, output)
    print(render(report))


def cli():
    parser = Parser(description=__doc__)
    parser.add_argument("capture")
    parser.add_argument("--output", help="New JSON report path (under data/ for real-derived captures)")
    args = parser.parse_args()
    run_cli(lambda: run(args.capture, args.output))


if __name__ == "__main__":
    cli()
