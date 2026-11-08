import json
from unittest.mock import AsyncMock

import pytest

from agent import pitwall, stintara_agent
from tools.replay.compile import compile_capture
from tools.replay.format import dumps, header
from tools.replay.scenarios import record, scenario


def throttle_rows():
    rows = []
    for t in (0, .1, .21, .9, 1.01):
        rows.extend([record(t, "ScoringUpdate", session_type="Race", vehicles=[]),
                     record(t, "TelemetryUpdate", fuel=50-t),
                     record(t, "ConnectionStatus", game_connected=True)])
    return rows


async def test_compiler_matches_real_agent_forward_with_logical_time(monkeypatch):
    rows = throttle_rows()
    clock = iter(r["t"] for r in rows)
    monkeypatch.setattr(stintara_agent, "monotonic", lambda: next(clock))
    async def source():
        for row in rows:
            yield json.dumps(row["message"])
    uplink = AsyncMock()
    await stintara_agent.forward(source(), uplink)
    metadata, output, counts = compile_capture(header(), rows)
    assert [r["message"] for r in output] == [json.loads(c.args[0]) for c in uplink.send.await_args_list]
    assert [r["t"] for r in output if r["message"]["message_type"] == "scoring"] == [0, .21, .9]
    assert [r["t"] for r in output if r["message"]["message_type"] == "telemetry"] == [0, 1.01]
    assert counts["connection"] == 5
    assert counts["compiled"]+counts["ignored"] == counts["input"]
    assert "agent_hello" not in dumps(metadata, output)


@pytest.mark.parametrize("source", ["synthetic", "real", "sanitized_real"])
def test_normalizer_reused_ignored_messages_determinism_and_source(source):
    from tools.replay import compile
    assert compile.normalize is pitwall.normalize
    rows = scenario("session-transition")[1]+[record(3, "Unknown"), record(4, "ScoringUpdate", vehicles=None)]
    result = compile_capture(header(source), rows)
    assert result == compile_capture(header(source), rows)
    assert result[0]["source"] == source
    assert result[2]["ignored"] == 3
    assert [r["message"]["message_type"] for r in result[1]] == ["connection", "session_info", "session_info"]
    text = dumps(*result[:2])
    for key in ('"token"', '"server_ws"', '"agent_hello"'):
        assert key not in text


@pytest.mark.parametrize("key", ["token", "agent_token", "server_ws", "pitwall_ws", "authorization"])
def test_compiler_rejects_connection_metadata_without_disclosure(key):
    from tools.replay.format import FormatError

    rows = [record(0, "SessionInfo", track_name="Synthetic Circuit", weather={key: "private-test-marker"})]
    with pytest.raises(FormatError) as exc:
        compile_capture(header(), rows)
    assert "private-test-marker" not in str(exc.value)


def race_control_row(t, **overrides):
    payload = dict(game_phase=5, sector_flags=[11, 1, 11], safety_car_active=False, player_sector=2,
                   yellow_flag_state=0, player_flag=0)
    return record(t, "VehicleStatusUpdate", **dict(payload, **overrides))


def test_race_control_compiles_per_protocol_contract_and_preserves_order_and_time():
    from server.app.protocol import PAYLOADS

    rows = [record(0, "ConnectionStatus", game_connected=True),
            record(.5, "ScoringUpdate", session_type="Race", vehicles=[]),
            race_control_row(.5),
            record(.5, "AllDriversUpdate", session_type="Race", drivers=[]),
            record(1, "TelemetryUpdate", fuel=50)]
    metadata, output, counts = compile_capture(header(), rows)
    assert [(r["t"], r["message"]["message_type"]) for r in output] == [
        (0, "connection"), (.5, "scoring"), (.5, "race_control"), (.5, "lap_snapshots"), (1, "telemetry")]
    payload = output[2]["message"]["payload"]
    assert payload == pitwall.normalize(rows[2]["message"])[1]
    assert payload["sector_flags"] == ["clear", "local_yellow", "clear"]
    assert PAYLOADS["race_control"].model_validate(payload)
    assert counts["race_control"] == 1 and counts["compiled"] == 5 and counts["ignored"] == 0
    # Existing messages are unchanged by the presence of race control.
    assert compile_capture(header(), [r for i, r in enumerate(rows) if i != 2])[1] == [
        r for i, r in enumerate(output) if i != 2]


def test_race_control_unknown_values_are_preserved_as_unknown_not_dropped():
    _, output, counts = compile_capture(header(), [race_control_row(0, game_phase=99, sector_flags=[2, 3, 7])])
    payload = output[0]["message"]["payload"]
    assert counts["race_control"] == 1 and payload["phase"] == "unknown"
    assert payload["sector_flags"] == ["unknown"] * 3
    assert payload["diagnostics"]["phase_raw"] == 99 and payload["diagnostics"]["sector_flags_raw"] == [2, 3, 7]


async def test_race_control_throttle_matches_real_agent_forward(monkeypatch):
    rows = [race_control_row(t) for t in (0, .05, .1, .15, .21, .3)]
    clock = iter(r["t"] for r in rows)
    monkeypatch.setattr(stintara_agent, "monotonic", lambda: next(clock))
    async def source():
        for row in rows:
            yield json.dumps(row["message"])
    uplink = AsyncMock()
    await stintara_agent.forward(source(), uplink)
    _, output, counts = compile_capture(header(), rows)
    assert [r["message"] for r in output] == [json.loads(c.args[0]) for c in uplink.send.await_args_list]
    assert [r["t"] for r in output] == [0, .1, .21]
    assert counts["compiled"] + counts["ignored"] == counts["input"]


def test_protocol_replay_with_race_control_roundtrips_and_unknown_kind_still_rejected():
    from tools.replay.format import PROTOCOL, FormatError, loads

    _, output, _ = compile_capture(header(), [race_control_row(0)])
    text = dumps(header(format=PROTOCOL), output)
    assert loads(text, PROTOCOL)[1] == output
    bad = json.loads(json.dumps(output[0]))
    bad["message"]["message_type"] = "not_a_kind"
    with pytest.raises(FormatError):
        dumps(header(format=PROTOCOL), [bad])
