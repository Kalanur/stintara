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
