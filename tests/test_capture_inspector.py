import io
import json
from pathlib import Path

import pytest

from tools.replay.format import FormatError, header
from tools.replay.inspect import inspect
from tools.replay.inspector import EXAMPLE_LIMIT, FIELD_LIMIT, TYPE_LIMIT, inspect_stream


def line(record):
    return json.dumps(record).encode() + b"\n"


def message(t, kind, **fields):
    return line(dict(record_type="message", t=t, message={"type": kind, **fields}))


def analyze(*records):
    return inspect_stream(io.BytesIO(line(header()) + b"".join(records)))


def test_ve_spatial_sectors_pits_and_clock_evidence():
    def vehicle(laps, distance, x, sector, stops, ve):
        return dict(id=7, total_laps=laps, lap_dist=distance, pos_x=x, pos_y=0, pos_z=0,
                    sector=sector, num_pitstops=stops, in_pits=bool(stops), virtual_energy=ve)

    rows = [
        message(0, "SessionInfo", track_name="Synthetic A", track_length=1000),
        message(1, "ScoringUpdate", session_time=50, player_vehicle_id=7, vehicles=[
            vehicle(2, 998, 3, 3, 0, .3), dict(id=8, pos_x=9000, pos_y=0, pos_z=0)]),
        message(2, "TelemetryUpdate", ve_available=True, ve_history=None),
        message(3, "ElectronicsUpdate", virtual_energy=.31),
        message(4, "AllDriversUpdate", session_time=51, drivers=[
            dict(id=7, total_laps=3, num_pitstops=0, last_sector2=62)]),
        message(5, "VehicleStatusUpdate", yellow_flag_state=3, sector_flags=[0, 2, 0],
                game_phase=4, player_under_yellow=True, player_sector=2, safety_car_active=False),
        message(6, "ScoringUpdate", player_vehicle_id=7, session_time=52,
                vehicles=[dict(vehicle(3, 2, 800, 1, 1, .8), lap_invalid=True)]),
        message(7, "VehicleStatusUpdate", yellow_flag_state=0, sector_flags=[0, 0, 0]),
        message(8, "SessionInfo", track_name="Synthetic B"),
        message(9, "ScoringUpdate", session_time=1, vehicles=[vehicle(0, 0, 0, 1, 0, .9)]),
        message(10, "TelemetryUpdate", ve_available=False, ve_history=[.4, .3]),
        message(11, "UnrecognizedSynthetic"),
    ]
    r = analyze(*rows)
    assert (r["lines"], r["valid_records"], r["invalid_records"]) == (13, 12, 0)
    assert (r["first_t"], r["last_t"], r["duration"], r["segments"]) == (0, 11, 11, 2)
    assert r["types"]["UnrecognizedSynthetic"] == 1
    assert r["rates_hz"]["ScoringUpdate"] == pytest.approx(3/11)
    assert r["ve"]["TelemetryUpdate.ve_available"]["first_true_t"] == 2
    history = r["ve"]["TelemetryUpdate.ve_history"]
    assert (history["null"], history["min"], history["max"], history["first_t"]) == (1, .3, .4, 10)
    assert r["ve"]["ElectronicsUpdate.virtual_energy"]["min"] == .31
    assert r["ve"]["ScoringUpdate.vehicles[].virtual_energy"]["max"] == .9
    events = r["events"]
    assert events["ve_increase"]["count"] == 1
    assert events["spatial_step_over_100"]["count"] == 1
    assert events["lap_distance_wrap"]["examples"][0]["before"]["lap_dist"] == 998
    assert events["lap_advance"]["examples"][0]["local_ve"]["electronics"] == (3, .31)
    assert events["num_pitstops_change"]["examples"][0]["latest_driver_sample"]["num_pitstops"] == 0
    assert events["in_pits_change"]["count"] == 1
    assert events["clock_rollback"]["examples"][0]["previous_segment"] == 1
    assert events["clock_rollback"]["examples"][0]["segment"] == 2
    assert r["fields"]["ScoringUpdate.vehicles[].sector"]["changes"] == 1
    assert r["fields"]["VehicleStatusUpdate.sector_flags"]["changes"] == 1
    assert r["fields"]["VehicleStatusUpdate.yellow_flag_state"]["values"] == [3, 0]
    assert r["fields"]["ScoringUpdate.vehicles[].lap_invalid"]["values"] == [True]
    assert r["fields"]["AllDriversUpdate.drivers[].last_sector2"]["min"] == 62
    assert "off_track" not in json.dumps(r)


def test_streaming_reader_recovers_at_next_physical_line():
    class BoundedReader(io.BytesIO):
        def read(self, *args):
            pytest.fail("whole-file read")

        def readlines(self, *args):
            pytest.fail("whole-file readlines")

        def readline(self, size=-1):
            assert 0 < size <= 257
            return super().readline(size)

    raw = line(header()) + b"\xff\n" + b"x"*900 + b"\n{malformed\n" + message(0, "TelemetryUpdate")
    r = inspect_stream(BoundedReader(raw), line_limit=256)
    assert (r["invalid_records"], r["valid_records"], r["lines"]) == (3, 1, 5)
    assert r["invalid_line_examples"] == [2, 3, 4]
    assert r["ve"]["TelemetryUpdate.ve_history"]["missing"] == 1
    assert r["rates_hz"]["TelemetryUpdate"] is None
    with pytest.raises(FormatError):
        inspect_stream(io.BytesIO(b"not a header\n"))


def test_report_cardinality_is_bounded():
    rows = [message(i, "ScoringUpdate", vehicles=[dict(id=i, sector=i, pos_x=i)]) for i in range(400)]
    rows += [message(401+i, f"Synthetic{i}", **{f"cut_{j}": j for j in range(4)}) for i in range(200)]
    r = analyze(*rows, *([b"broken\n"]*30))
    assert len(r["types"]) <= TYPE_LIMIT + 1
    assert len(r["fields"]) <= FIELD_LIMIT
    assert len(r["invalid_line_examples"]) == EXAMPLE_LIMIT
    assert all(r["limits_hit"][key] > 0 for key in
               ("untracked_context_samples", "untracked_field_samples", "type_overflow_samples"))


def test_cli_output_is_versioned_exclusive_and_private(tmp_path, capsys):
    capture = tmp_path / "synthetic.jsonl"
    output = tmp_path / "report.json"
    capture.write_bytes(line(header()) + message(2, "Synthetic"))
    inspect(capture, output)
    assert "1 valid" in capsys.readouterr().out
    with output.open() as stream:
        assert json.load(stream)["format"] == "capture-report-v1"
    with pytest.raises(FileExistsError):
        inspect(capture, output)
    capture.write_bytes(line(header("real")))
    with pytest.raises(ValueError):
        inspect(capture, Path(tmp_path, "private.json"))
