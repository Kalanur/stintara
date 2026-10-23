import json

import pytest

from agent.pitwall import normalize
from tools.replay import common
from tools.replay.compile import compile_capture
from tools.replay.format import header, load, write
from tools.replay.sanitize import sanitize, sanitize_file
from tools.replay.scenarios import record


@pytest.mark.parametrize("blank", ["", " ", "\t\r\n "])
def test_blank_session_info_survives_sanitization_and_is_ignored_by_compiler(blank):
    rows = [record(0, "SessionInfo", track_name="Private Circuit One"),
            record(1, "SessionInfo", track_name=blank),
            record(2, "SessionInfo", track_name="Private Circuit One")]
    metadata, sanitized, _ = sanitize(header("real"), rows)
    assert sanitized[1]["message"]["track_name"] == blank
    assert normalize(sanitized[1]["message"]) is None
    _, compiled, counts = compile_capture(metadata, sanitized)
    assert [row["t"] for row in compiled] == [0, 2]
    assert counts["ignored"] == 1
    assert [row["message"]["payload"]["track_name"] for row in compiled] == ["Synthetic Circuit 001"] * 2


@pytest.mark.parametrize("field,prefix", [("driver_name", "Driver"), ("team_name", "Synthetic Team"),
                                         ("car_name", "Synthetic Entry"), ("track_name", "Synthetic Circuit")])
def test_blank_identity_values_preserved_without_consuming_pseudonyms(field, prefix):
    values = ["", " \t\r\n", "Private Label One", " ", "Private Label Two", "Private Label One"]
    rows = [record(0, "ScoringUpdate", vehicles=[{field: value} for value in values])]
    result = sanitize(header("real"), rows)
    assert result == sanitize(header("real"), rows)
    assert [v[field] for v in result[1][0]["message"]["vehicles"]] == [
        "", " \t\r\n", f"{prefix} 001", " ", f"{prefix} 002", f"{prefix} 001"]
    assert result[2] == {}


def private_rows():
    vehicle = dict(id=7, driver_name="Private Driver One", team_name="Private Team One", car_name="Private Entry One",
                   position=2, total_laps=4, fuel_remaining=42.5, num_pitstops=1, in_pits=True,
                   gap_to_leader=-3.5, finish_status=2, wheels=[dict(wear=.8)], nullable=None,
                   vehicle_class="Private Unknown Class", **{"Private Unknown Key": "Private Unknown Value"})
    return [record(0, "SessionInfo", track_name="Private Circuit One"),
            record(1, "ScoringUpdate", vehicles=[vehicle, dict(vehicle, driver_name="Private Driver Two")]),
            record(2, "AllDriversUpdate", drivers=[vehicle])]


def test_identity_mapping_preserves_telemetry_and_audit_is_values_free():
    rows = private_rows()
    original = json.dumps(rows)
    metadata, output, audit = sanitize(header("real"), rows)
    assert json.dumps(rows) == original  # no mutation
    assert metadata == header("sanitized_real") and metadata["public_review_required"]
    first, second = output[1]["message"]["vehicles"]
    assert first["driver_name"] == "Driver 001" and second["driver_name"] == "Driver 002"
    assert first == output[2]["message"]["drivers"][0]
    assert first["team_name"] == "Synthetic Team 001" and first["car_name"] == "Synthetic Entry 001"
    assert output[0]["message"]["track_name"] == "Synthetic Circuit 001"
    for key in ("id", "position", "total_laps", "fuel_remaining", "num_pitstops", "in_pits", "gap_to_leader",
                "finish_status", "wheels", "nullable"):
        assert first[key] == rows[1]["message"]["vehicles"][0][key]
    for value in ("Private Driver One", "Private Driver Two", "Private Team One", "Private Entry One", "Private Circuit One"):
        assert value not in json.dumps(output)
    assert [r["t"] for r in output] == [0, 1, 2]
    assert audit["ScoringUpdate.vehicles[].vehicle_class"] == 2
    assert "Private" not in json.dumps(audit)
    assert sanitize(header("real"), rows) == (metadata, output, audit)
    # Mapping restarts per file; it is not a hash or a cross-session identity.
    assert sanitize(header(), [rows[2]])[1][0]["message"]["drivers"][0]["driver_name"] == "Driver 001"


def test_private_paths_exclusive_write_no_mapping_and_safe_audit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(common, "PRIVATE_ROOT", tmp_path)
    raw, output = tmp_path/"raw.jsonl", tmp_path/"sanitized.jsonl"
    write(raw, header("real"), private_rows())
    original = raw.read_bytes()
    sanitize_file(raw, output)
    assert load(output)[0]["source"] == "sanitized_real"
    assert set(p.name for p in tmp_path.iterdir()) == {"raw.jsonl", "sanitized.jsonl"}
    assert "Private Driver" not in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        sanitize_file(raw, raw)
    assert raw.read_bytes() == original
    with pytest.raises(ValueError):
        common.output_location(tmp_path.parent/"outside.jsonl", "sanitized_real")
