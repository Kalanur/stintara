import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agent.pitwall import normalize
from agent.stintara_agent import forward
from server.app.domain import same_session, sample_due
from server.app.main import decode_message, driver_live_page
from server.app.protocol import Scoring, Telemetry
from server.app.uuid7 import uuid7


def test_uuid7_version_timestamp_and_uniqueness():
    ids = [uuid7() for _ in range(1000)]
    assert len(set(ids)) == 1000
    assert all(value.version == 7 for value in ids)
    assert ids[-1].int >> 80 >= ids[0].int >> 80


@pytest.mark.parametrize("remaining", [0.0, 1.0, 0.65])
def test_native_remaining_is_not_inverted(remaining):
    kind, payload = normalize({"type": "TelemetryUpdate", "tires": [{"wear": remaining, "brake_temp": 500}]})
    assert kind == "telemetry"
    assert Telemetry.model_validate(payload).tires[0].remaining == remaining
    assert payload["tires"][0]["brake_temp"] == 500
    assert "wear" not in payload["tires"][0]


def test_lap_normalization_and_unavailable_telemetry():
    _, payload = normalize({"type": "AllDriversUpdate", "drivers": [
        {"has_telemetry": True, "last_sector1": 30, "last_sector2": 65,
         "wheels": [{"wear": 1.0, "surface_temp": [80, 90, 100]}], "dent_severity": [1, 0]},
        {"has_telemetry": False, "wheels": [{"wear": 0}], "fuel_remaining": -1},
    ]})
    first, second = payload["drivers"]
    assert first["last_sector2"] == 35
    assert first["wheels"][0]["remaining"] == 1
    assert first["wheels"][0]["temp_outer"] == 100
    assert first["damage"]["dent_severity"] == [1, 0]
    assert second["wheels"] == [] and second["fuel_remaining"] is None


def test_nonfinite_value_is_unavailable():
    _, payload = normalize({"type": "TelemetryUpdate", "fuel_laps_remaining": float("inf")})
    assert payload["fuel_laps_remaining"] is None
    json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize("change", [
    {"session_type": "Qualifying"}, {"track_name": "Spa"},
    {"session_time": 0}, {"provider": "iracing"},
])
def test_different_sessions_do_not_merge(change):
    previous = {"status": "live", "simulator_provider": "lmu", "session_type": "Race",
                "track_name": "Imola", "last_session_time": 100}
    current = dict(provider="lmu", session_type="Race", track_name="Imola", session_time=101)
    assert same_session(previous, **current)
    current.update(change)
    assert not same_session(previous, **current)


def test_sampling_interval():
    assert sample_due(None, 0)
    assert not sample_due(10, 10.999)
    assert sample_due(10, 11)


def test_invalid_ingest_payload_is_rejected():
    with pytest.raises(ValidationError):
        Scoring.model_validate({"session_type": "Race", "vehicles": [{"id": "oops"}]})
    with pytest.raises(ValidationError):
        Telemetry.model_validate({"tires": [{"remaining": 9}]})


async def test_agent_rate_limit_and_malformed_frame(monkeypatch):
    async def frames():
        yield "not json"
        yield "[]"
        for i in range(6):
            yield json.dumps({"type": "TelemetryUpdate", "fuel": 50 - i})
    clock = iter([0, .2, .4, .6, .8, 1.01])
    monkeypatch.setattr("agent.stintara_agent.monotonic", lambda: next(clock))
    uplink = AsyncMock()
    await forward(frames(), uplink)
    assert uplink.send.await_count == 2


def test_nonfinite_json_cannot_enter_untyped_metadata():
    with pytest.raises(ValueError):
        decode_message('{"weather":{"temperature":NaN}}')


async def test_driver_slug_cannot_break_out_of_script():
    response = await driver_live_page('</script><script>alert(1)</script>')
    assert b'const slug = "</script>' not in response.body
    assert b'\\u003c/script>' in response.body
