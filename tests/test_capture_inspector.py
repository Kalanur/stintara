import io
import json
from pathlib import Path

import pytest

from tools.replay.format import FormatError, header
from tools.replay.inspect import inspect
from tools.replay.inspector import (
    EXAMPLE_LIMIT,
    FIELD_LIMIT,
    PLAYER_LAP_LIMIT,
    PLAYER_PIT_LIMIT,
    TYPE_LIMIT,
    inspect_stream,
)


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


def scoring(t, player, *rows):
    return message(t, "ScoringUpdate", session_time=t+10, player_vehicle_id=player, vehicles=list(rows))


def test_player_correlates_only_observed_slot_and_timestamped_samples():
    r = analyze(
        scoring(1, 7, dict(id=7, total_laps=3, lap_dist=990, virtual_energy=.5, in_pits=False)),
        message(2, "AllDriversUpdate", drivers=[dict(id=7, num_pitstops=1)]),
        message(3, "ElectronicsUpdate", virtual_energy=.42),
        message(4, "AllDriversUpdate", session_time=14, drivers=[
            dict(id=7, num_pitstops=2, total_laps=3, driver_name="Synthetic private identity"),
            dict(id=8, num_pitstops=9)]),
        scoring(5, 7, dict(id=8, total_laps=10), dict(id=7, total_laps=4, lap_dist=2,
                virtual_energy=.4, in_pits=True, team_name="Synthetic private identity")),
    )["player"]
    electronics = dict(t=3, age_s=2, value=.42)
    assert r["lap_checkpoints"] == [dict(segment=1, player_vehicle_id=7, t=5, session_time=15,
        before=dict(total_laps=3, lap_dist=990, virtual_energy=.5),
        after=dict(total_laps=4, lap_dist=2, virtual_energy=.4), electronics_ve=electronics,
        in_pits=True, num_pitstops=dict(t=4, value=2))]
    count, pits = r["pit_transitions"]
    assert count == dict(segment=1, player_vehicle_id=7, t=4, session_time=14,
        source="AllDriversUpdate.drivers[]", field="num_pitstops", before=1, after=2,
        total_laps=3, scoring_ve=.5, electronics_ve=dict(t=3, age_s=1, value=.42))
    assert pits == dict(segment=1, player_vehicle_id=7, t=5, session_time=15,
        source="ScoringUpdate.vehicles[]", field="in_pits", before=False, after=True,
        total_laps=4, scoring_ve=.4, electronics_ve=electronics)
    assert "Synthetic private identity" not in json.dumps(r)


def test_player_switch_and_session_change_start_fresh_comparisons():
    r = analyze(
        message(0, "SessionInfo", track_name="Synthetic A"),
        scoring(1, 1, dict(id=1, total_laps=3), dict(id=2, total_laps=2, in_pits=True)),
        message(2, "ElectronicsUpdate", virtual_energy=.8),
        message(3, "AllDriversUpdate", drivers=[dict(id=2, num_pitstops=2)]),
        scoring(4, 2, dict(id=2, total_laps=5, in_pits=False)),
        scoring(5, 2, dict(id=2, total_laps=6)),
        message(6, "ElectronicsUpdate", virtual_energy=.2),
        message(7, "AllDriversUpdate", drivers=[dict(id=2, num_pitstops=4)]),
        message(8, "SessionInfo", track_name="Synthetic B"),
        scoring(9, 2, dict(id=2, total_laps=8, in_pits=True)),
        scoring(10, 2, dict(id=2, total_laps=9)),
    )["player"]
    assert r["pit_transitions"] == []
    assert [(p["segment"], p["player_vehicle_id"], p["t"], p["electronics_ve"], p["num_pitstops"])
            for p in r["lap_checkpoints"]] == [(1, 2, 5, None, None), (2, 2, 10, None, None)]


def test_player_buckets_have_independent_capture_wide_limits():
    n = PLAYER_LAP_LIMIT + 3
    r = analyze(*(scoring(i, 1, dict(id=1, total_laps=i, in_pits=bool(i % 2))) for i in range(n+1)))
    for bucket, cap in (("lap_checkpoints", PLAYER_LAP_LIMIT), ("pit_transitions", PLAYER_PIT_LIMIT)):
        assert [p["t"] for p in r["player"][bucket]] == list(range(1, cap+1))
        assert r["limits_hit"]["omitted_player_" + bucket] == n-cap


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
