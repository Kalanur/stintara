"""Adapter and CLI tests for the offline crossing-evidence tool, using only synthetic RAW-shaped data."""
import io
import json
from collections import Counter

import pytest

from tests.crossing_synthetic import candidates
from tools.crossing import cli
from tools.crossing.lmu_raw import peek_type, read
from tools.crossing.model import LinkObs, PhaseObs, ScoringObs, SessionMeta, TelemetryObs
from tools.replay.format import FormatError

SECRET = "Synthetic Driver Name"


def header(source="synthetic"):
    return json.dumps({"record_type": "header", "format": "stintara-pitwall-capture", "version": 1,
                       "source": source}) + "\n"


def record(t, message):
    return json.dumps({"record_type": "message", "t": t, "message": message}, separators=(",", ":")) + "\n"


def scoring(t, laps=1, **extra):
    vehicle = {"id": 7, "driver_name": SECRET, "team_name": "Team", "car_name": "Car", "total_laps": laps,
               "lap_start_et": 5.0, "lap_dist": 10.0, "pos_x": 1.0, "pos_y": 2.0, "pos_z": 3.0, "in_pits": False}
    vehicle.update(extra)
    return record(t, {"type": "ScoringUpdate", "session_type": "Race", "session_time": t, "num_vehicles": 1,
                      "vehicles": [vehicle], "player_vehicle_id": 7})


def telemetry(t):
    return record(t, {"type": "TelemetryUpdate", "speed_ms": 60.0, "current_et": t + 100,
                      "lap_start_et": 5.0, "position": {"x": 1.0, "y": 2.0, "z": 3.0},
                      "velocity": {"x": 60.0, "y": 0.0, "z": 0.5}})


def capture(*lines, source="synthetic"):
    return io.BytesIO((header(source) + "".join(lines)).encode())


def test_adapter_maps_ground_plane_and_never_copies_names():
    stats = Counter()
    events = list(read(capture(
        record(0.0, {"type": "SessionInfo", "track_name": " Track ", "track_length": 5000.0}),
        record(0.1, {"type": "VehicleStatusUpdate", "game_phase": 5}),
        record(0.2, {"type": "ConnectionStatus", "game_connected": False}),
        scoring(0.3), telemetry(0.4)), stats))
    kinds = [type(e) for e in events]
    assert kinds == [SessionMeta, PhaseObs, LinkObs, ScoringObs, TelemetryObs]
    meta, phase, link, score, tel = events
    assert meta.track_key == "Track" and phase.state == "green" and link.connected is False
    vehicle = score.vehicles[0]
    assert (vehicle.gx, vehicle.gy) == (1.0, 3.0) and score.player_key == 7           # x/z ground plane
    assert (tel.gx, tel.gy, tel.gz, tel.vx, tel.vy) == (1.0, 3.0, 2.0, 60.0, 0.5)
    assert SECRET not in repr(events) and "Team" not in repr(events)
    assert stats["source_header"] == "synthetic" and stats["records"] == 5


def test_adapter_skips_bulk_types_unparsed_and_counts_them():
    stats = Counter()
    bulk = '{"record_type":"message","t":0.5,"message":{"type":"AllDriversUpdate","drivers":[{{{not json'
    events = list(read(io.BytesIO((header() + bulk + "\n" + scoring(0.6)).encode()), stats))
    assert len(events) == 1 and stats["type:AllDriversUpdate"] == 1 and stats["invalid_lines"] == 0
    assert peek_type(bulk.encode()) == "AllDriversUpdate"


def test_adapter_counts_malformed_and_unusable_records_without_echoing_payloads():
    stats = Counter()
    events = list(read(capture(
        "not json\n",
        record(0.1, {"type": "SessionInfo", "track_name": "", "track_length": 0.0}),       # transient empty snapshot
        record(0.2, {"type": "TelemetryUpdate", "current_et": None, "position": {}}),
        '{"record_type":"message","t":NaN,"message":{"type":"ScoringUpdate"}}\n',
        record(0.3, {"type": "SomethingNew"}),
        scoring(0.4)), stats))
    assert len(events) == 1
    assert stats["invalid_lines"] == 2 and stats["unusable:SessionInfo"] == 1 and stats["unusable:TelemetryUpdate"] == 1
    assert stats["type:other"] == 1


def test_adapter_rejects_bad_headers_and_other_formats():
    for text in ("", "not json\n", json.dumps({"record_type": "header", "format": "stintara-protocol-replay",
                                               "version": 1, "source": "synthetic"}) + "\n"):
        with pytest.raises(FormatError):
            list(read(io.BytesIO(text.encode())))


def test_adapter_keeps_negative_player_as_no_player_and_surfaces_recorder_regression():
    stats = Counter()
    line = record(0.5, {"type": "ScoringUpdate", "session_type": "Race", "session_time": 1.0, "num_vehicles": 0,
                        "vehicles": [], "player_vehicle_id": -1})
    events = list(read(capture(line, record(0.2, {"type": "VehicleStatusUpdate", "game_phase": 5})), stats))
    assert events[0].player_key is None and stats["recorder_time_regression_lines"] == 1


# ---------------------------------------------------------------- CLI
def write_capture(path, stream_events, source="synthetic"):
    """Serialise the neutral synthetic stream back to wire-shaped records (test-only inverse of the adapter)."""
    lines = [header(source)]
    for e in stream_events:
        if isinstance(e, SessionMeta):
            lines.append(record(e.rec_t, {"type": "SessionInfo", "track_name": e.track_key,
                                          "track_length": e.track_length}))
        elif isinstance(e, ScoringObs):
            vehicles = [{"id": v.key, "total_laps": v.laps, "lap_start_et": v.lap_start_et, "lap_dist": v.lap_dist,
                         "pos_x": v.gx, "pos_z": v.gy, "pos_y": 0.0, "in_pits": v.in_pits, "driver_name": SECRET}
                        for v in e.vehicles]
            lines.append(record(e.rec_t, {"type": "ScoringUpdate", "session_type": e.session_kind,
                                          "session_time": e.session_time, "vehicles": vehicles,
                                          "player_vehicle_id": e.player_key}))
        elif isinstance(e, TelemetryObs):
            lines.append(record(e.rec_t, {"type": "TelemetryUpdate", "current_et": e.et, "lap_start_et": e.lap_start_et,
                                          "speed_ms": e.speed, "position": {"x": e.gx, "y": 0.0, "z": e.gy},
                                          "velocity": {"x": e.vx, "y": 0.0, "z": e.vy}}))
        elif isinstance(e, PhaseObs):
            lines.append(record(e.rec_t, {"type": "VehicleStatusUpdate", "game_phase": 5}))
    path.write_text("".join(lines), encoding="utf-8")


def test_cli_end_to_end_report_is_versioned_and_diagnostic(tmp_path, capsys):
    from tests.crossing_synthetic import build
    source = tmp_path / "synthetic-capture.jsonl"
    write_capture(source, build((10.03, 20.03, 30.03), lateral=[0.0, 0.5, -0.5]))
    report = cli.analyse(source)
    assert report["schema"] == "crossing-evidence-diagnostic-v1" and report["report_schema"] == cli.REPORT_SCHEMA
    assert report["input"]["source_header"] == "synthetic" and report["input"]["type:ScoringUpdate"] > 0
    assert len(candidates(report)) == 3 and report["counts"]["distinct_candidate_lap_crossings"] == 3
    output = tmp_path / "report.json"
    cli.run(source, output)
    stored = json.loads(output.read_text(encoding="utf-8"))
    assert stored["schema"] == report["schema"] and SECRET not in output.read_text(encoding="utf-8")
    printed = capsys.readouterr().out
    assert "DIAGNOSTIC EVIDENCE ONLY" in printed and "no start/finish anchor" in printed
    assert "accepted marker" not in printed.replace("never an accepted", "")
    with pytest.raises(FileExistsError):
        cli.write_report(stored | {"input": {"source_header": "synthetic"}}, output)        # exclusive create


def test_cli_refuses_real_derived_output_outside_the_private_data_directory(tmp_path):
    from tests.crossing_synthetic import build
    source = tmp_path / "real-looking.jsonl"
    write_capture(source, build((10.0,)), source="real")
    with pytest.raises(SystemExit):
        cli.run_cli(lambda: cli.run(source, tmp_path / "leak.json"))
    assert not (tmp_path / "leak.json").exists()
