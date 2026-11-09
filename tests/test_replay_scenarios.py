from pathlib import Path

from server.app.fuel import consumption_estimate
from server.app.protocol import PAYLOADS
from tools.replay.compile import compile_capture
from tools.replay.format import dumps, load
from tools.replay.scenarios import scenario

FIXTURE = Path(__file__).parent / "fixtures/replay/demo-race.jsonl"


def test_demo_matches_generator_and_production_adapter_contracts():
    metadata, rows = scenario()
    assert metadata["source"] == "synthetic"
    assert FIXTURE.read_bytes() == dumps(metadata, rows).encode()
    assert load(FIXTURE) == (metadata, rows)
    assert dumps(*scenario()) == dumps(metadata, rows)
    assert all(isinstance(r["message"], dict) for r in rows)
    assert [r["t"] for r in rows] == sorted(r["t"] for r in rows)
    _, compiled, counts = compile_capture(metadata, rows)
    assert counts["ignored"] == 0
    validated = []
    for row in compiled:
        message = row["message"]
        validated.append((message["message_type"], PAYLOADS[message["message_type"]].model_validate(message["payload"]).model_dump()))
    scoring = [p for k, p in validated if k == "scoring"]
    assert len(scoring[0]["vehicles"]) == len({v["id"] for v in scoring[0]["vehicles"]}) == 6
    assert all(v["driver_name"].startswith("Driver ") for v in scoring[0]["vehicles"])
    assert scoring[0]["vehicles"][0]["position"] != scoring[-1]["vehicles"][0]["position"]
    laps = [p["drivers"][0] for k, p in validated if k == "lap_snapshots"]
    assert any(lap["in_pits"] for lap in laps) and not laps[-1]["in_pits"]
    assert any(b["fuel_remaining"] > a["fuel_remaining"] for a, b in zip(laps, laps[1:]))
    checkpoints = [dict(lap_number=p["total_laps"], fuel_remaining_l=p["fuel_remaining"],
                        pitstops=p["num_pitstops"], in_pits=p["in_pits"]) for p in laps]
    estimate = consumption_estimate(checkpoints)
    assert estimate["estimate"]["consumption_l_per_lap"] == 3
    assert estimate["estimate"]["sample_count"] == 5
    assert {k for k, _ in validated} == {"connection", "session_info", "scoring", "lap_snapshots", "telemetry"}
    assert len(FIXTURE.read_bytes()) < 40000


def test_latency_probe_scenario_is_deterministic_synthetic_and_unique_per_scoring_message():
    from tools.replay.compile import compile_capture
    metadata, rows = scenario("latency-probe")
    assert metadata["source"] == "synthetic"
    assert dumps(metadata, rows) == dumps(*scenario("latency-probe"))
    scoring = [r["message"] for r in rows if r["message"]["type"] == "ScoringUpdate"]
    xs = [m["vehicles"][0]["pos_x"] for m in scoring]
    assert len(xs) == len(set(xs)) > 1000                      # one unique x per source message
    assert all("Driver" in v["driver_name"] for m in scoring[:3] for v in m["vehicles"])
    _, compiled, counts = compile_capture(metadata, rows)
    # The compiler applies the agent's 0.2 s scoring throttle: about 5 Hz survive from 14 Hz.
    assert 4 <= counts["scoring"] / 90 <= 6
    times = [r["t"] for r in compiled if r["message"]["message_type"] == "scoring"]
    assert min(b - a for a, b in zip(times, times[1:])) >= 0.2 - 1e-9
