import copy
import json

import httpx
import pytest

from server.app import main
from server.app.uuid7 import uuid7
from server.app.weather import normalize_weather
from tests.conftest import scoring_session


def synthetic_weather():
    return {"air_temp": 24, "track_temp": 41, "rain_intensity": 0, "cloudiness": .25,
            "avg_path_wetness": .1, "min_path_wetness": 0, "max_path_wetness": .2,
            "wind_speed_live": 3, "dark_cloud": .9, "wind_rel_deg": 42, "unknown": "<provider-field>",
            "forecast": [{"temperature": 24 + i, "rain_chance": i / 4, "humidity": 50,
                          "wind_speed": 2, "wind_direction": i, "sky_type": 123} for i in range(5)]}


@pytest.mark.parametrize("encoded", [False, True])
def test_valid_weather_whitelist_units_and_no_mutation(encoded):
    raw = synthetic_weather()
    before = copy.deepcopy(raw)
    result = normalize_weather(json.dumps(raw) if encoded else raw, "lmu")
    assert result["current"] == dict(air_temp_c=24, track_temp_c=41, rain_intensity=0, cloudiness=.25,
                                     wetness_avg=.1, wetness_min=0, wetness_max=.2, wind_speed_mps=3)
    assert result["forecast"]["timeline_complete"] is True
    assert [n["position_percent"] for n in result["forecast"]["nodes"]] == [0, 25, 50, 75, 100]
    assert result["forecast"]["nodes"][0] == dict(position_percent=0, temperature_c=24, rain_chance=0,
                                                  humidity_percent=50, wind_speed_mps=2, wind_direction="N")
    assert raw == before
    for key in ("unknown", "dark_cloud", "wind_rel_deg", "sky_type"):
        assert key not in json.dumps(result)


@pytest.mark.parametrize("raw", [None, {}, "null", "{broken", "[]", [], True, 7])
def test_unavailable_weather(raw):
    result = normalize_weather(raw, "lmu")
    assert all(value is None for value in result["current"].values())
    assert result["forecast"] == {"timeline_complete": False, "nodes": []}


def test_unknown_provider_needs_explicit_unit_mapping():
    assert normalize_weather(synthetic_weather(), "other") == normalize_weather(None, "lmu")


@pytest.mark.parametrize("invalid", [True, False, "24", float("nan"), float("inf"), -float("inf"), 10**400])
def test_invalid_numbers_everywhere(invalid):
    raw = {key: invalid for key in synthetic_weather() if key != "forecast"}
    raw["forecast"] = [{key: invalid for key in synthetic_weather()["forecast"][0]}]
    assert normalize_weather(raw, "lmu") == normalize_weather(None, "lmu")


@pytest.mark.parametrize("invalid", [-.01, 1.01])
def test_fractions_are_not_clamped(invalid):
    raw = dict.fromkeys(["rain_intensity", "cloudiness", "avg_path_wetness",
                         "min_path_wetness", "max_path_wetness"], invalid)
    raw["forecast"] = [{"temperature": 24, "rain_chance": invalid}]
    result = normalize_weather(raw, "lmu")
    assert all(value is None for value in result["current"].values())
    assert result["forecast"]["nodes"][0]["rain_chance"] is None


@pytest.mark.parametrize("humidity", [-1, 101])
def test_optional_forecast_values_and_negative_wind(humidity):
    result = normalize_weather({"air_temp": -300, "wind_speed_live": -1,
                                "forecast": [{"temperature": -300, "humidity": humidity, "wind_speed": -1}]}, "lmu")
    assert result["current"]["air_temp_c"] == -300  # No invented temperature threshold.
    assert result["current"]["wind_speed_mps"] is None
    assert result["forecast"]["nodes"] == [dict(position_percent=None, temperature_c=-300,
                                               rain_chance=None, humidity_percent=None,
                                               wind_speed_mps=None, wind_direction=None)]


@pytest.mark.parametrize("code,expected", list(enumerate(["N", "NE", "E", "SE", "S", "SW", "W", "NW"])) +
                         [(-1, None), (8, None), (1.5, None), (True, None), ("1", None), (None, None)])
def test_forecast_cardinal_direction(code, expected):
    node = normalize_weather({"forecast": [{"temperature": 24, "wind_direction": code}]}, "lmu")["forecast"]["nodes"][0]
    assert node["wind_direction"] == expected


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4, 6])
def test_incomplete_or_oversized_forecast_preserves_order_without_positions(count):
    raw = {"forecast": [{"temperature": 24 + i} for i in range(count)]}
    result = normalize_weather(raw, "lmu")["forecast"]
    assert result["timeline_complete"] is False
    assert [n["temperature_c"] for n in result["nodes"]] == list(range(24, 24 + count))
    assert all(n["position_percent"] is None for n in result["nodes"])


@pytest.mark.parametrize("bad", [None, True, "bad", [], {}, {"sky_type": 1}, {"temperature": float("nan")}])
def test_malformed_forecast_nodes_do_not_shift_timeline(bad):
    nodes = synthetic_weather()["forecast"]
    nodes[2] = bad
    result = normalize_weather({"forecast": nodes}, "lmu")["forecast"]
    assert not result["timeline_complete"]
    assert [n["temperature_c"] for n in result["nodes"]] == [24, 25, 27, 28]
    assert all(n["position_percent"] is None for n in result["nodes"])
    assert not normalize_weather({"forecast": synthetic_weather()["forecast"] + [bad]}, "lmu")["forecast"]["timeline_complete"]


@pytest.mark.parametrize("forecast", [None, {}, "bad", True])
def test_invalid_forecast_container(forecast):
    assert normalize_weather({"forecast": forecast}, "lmu")["forecast"]["nodes"] == []


async def test_api_weather_latest_snapshot_shared_and_stale_read_only(db):
    agent, sid, _ = await scoring_session(db, "synthetic-weather")
    raw = synthetic_weather()
    # Use the established synthetic session's track so SessionInfo updates that session.
    track = main.runtime[str(agent["id"])]["persisted_track"]
    await main.handle_message(agent, "lmu", "session_info", {"track_name": track, "weather": raw})
    raw["air_temp"] = 25
    await main.handle_message(agent, "lmu", "session_info", {"track_name": track, "weather": raw})
    expected = normalize_weather(raw, "lmu")
    async with db.acquire() as conn:
        before = await conn.fetchval("SELECT weather FROM sessions WHERE id=$1", sid)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost") as client:
        spectator = (await client.get(f"/api/sessions/{sid}/live")).json()
        driver = (await client.get(f"/api/drivers/{agent['slug']}/live")).json()
        assert spectator["live"] and driver["live"]
        assert spectator["session"]["weather"] == driver["session"]["weather"] == expected
        async with db.acquire() as conn:
            await conn.execute("UPDATE session_observations SET last_seen_at=now()-interval '16 seconds' WHERE session_id=$1", sid)
        stale = (await client.get(f"/api/sessions/{sid}/live")).json()
        assert not stale["live"] and stale["session"]["weather"] == expected
        async with db.acquire() as conn:
            await conn.execute("UPDATE sessions SET status='complete' WHERE id=$1", sid)
        ended = (await client.get(f"/api/sessions/{sid}/live")).json()
        assert not ended["live"] and ended["session"]["weather"] == expected
    async with db.acquire() as conn:
        after = await conn.fetchval("SELECT weather FROM sessions WHERE id=$1", sid)
    assert before == after and json.loads(after) == raw


async def test_weather_renderer_is_included_in_both_pages():
    for response in (await main.driver_live_page("synthetic"), await main.session_live_page(uuid7())):
        assert b"__WEATHER_UI__" not in response.body
        assert b"function weatherWidget" in response.body
