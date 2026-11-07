"""Race-control normalization, spatial transport and the read-only track-map payload (all synthetic)."""
import math
import time

import pytest
from pydantic import ValidationError

from agent.pitwall import normalize
from server.app import main
from server.app.protocol import PAYLOADS, RaceControl, Scoring
from server.app.trackmap import track_map_payload
from tests.conftest import make_agent
from tests.track_fixtures import LENGTH, lap_samples


def status(flags=(11, 11, 11), phase=5, **extra):
    return {"type": "VehicleStatusUpdate", "sector_flags": list(flags), "game_phase": phase,
            "yellow_flag_state": 0, "player_flag": 0, "player_sector": 1, "safety_car_active": False,
            "player_under_yellow": False, "individual_phase": 5, **extra}


def rc(**kwargs):
    kind, payload = normalize(status(**kwargs))
    assert kind == "race_control"
    return RaceControl.model_validate(payload).model_dump()


def test_green_state_and_no_provider_leak():
    state = rc()
    assert state["phase"] == "green" and state["sector_flags"] == ["clear"] * 3
    assert state["safety_car_active"] is False and state["player_sector"] == 1
    assert set(state) == {"phase", "sector_flags", "safety_car_active", "player_sector", "diagnostics"}
    assert state["diagnostics"]["phase_raw"] == 5  # diagnostics only


@pytest.mark.parametrize("flags,expected", [
    ((1, 11, 11), ["local_yellow", "clear", "clear"]),
    ((11, 1, 11), ["clear", "local_yellow", "clear"]),
    ((11, 11, 1), ["clear", "clear", "local_yellow"]),
    ((1, 11, 1), ["local_yellow", "clear", "local_yellow"]),
    ((1, 1, 1), ["local_yellow"] * 3),
])
def test_local_yellow_comes_from_sector_flags_not_from_the_global_yellow_state(flags, expected):
    state = rc(flags=flags, yellow_flag_state=0, player_under_yellow=False)
    assert state["sector_flags"] == expected
    assert state["diagnostics"]["yellow_flag_state_raw"] == 0


@pytest.mark.parametrize("flags", [(2, 2, 2), (3, 2, 3), (7, 11, 11), (-1, 11, 11), (None, 11, 11)])
def test_unobserved_sector_flag_values_are_unknown_never_yellow(flags):
    state = rc(flags=flags, phase=8)
    assert "local_yellow" not in state["sector_flags"]
    assert state["sector_flags"][[i for i, v in enumerate(flags) if v not in (11, 1)][0]] == "unknown"
    assert state["diagnostics"]["sector_flags_raw"] == [v if type(v) is int else None for v in flags]


@pytest.mark.parametrize("phase,expected", [(0, "pre_session"), (3, "formation"), (5, "green"),
                                            (6, "full_course_yellow"), (7, "session_stopped"),
                                            (8, "session_over"), (1, "unknown"), (4, "unknown"),
                                            (99, "unknown"), (None, "unknown"), (True, "unknown")])
def test_game_phase_mapping_and_unknown_degradation(phase, expected):
    assert rc(phase=phase)["phase"] == expected


@pytest.mark.parametrize("flag,expected", [(True, True), (False, False), (None, None), (1, None), ("yes", None)])
def test_safety_car_is_a_strict_boolean(flag, expected):
    assert rc(safety_car_active=flag)["safety_car_active"] is expected


@pytest.mark.parametrize("raw,expected", [(1, 1), (2, 2), (0, 3), (-1, None), (7, None), (None, None)])
def test_player_sector_maps_provider_zero_to_the_last_sector(raw, expected):
    assert rc(player_sector=raw)["player_sector"] == expected


@pytest.mark.parametrize("flags", [None, "bad", [], [1], 5])
def test_malformed_sector_flag_lists_degrade_to_unknown(flags):
    kind, payload = normalize({**status(), "sector_flags": flags})
    assert kind == "race_control"
    assert RaceControl.model_validate(payload).sector_flags == ["unknown"] * 3


def test_transport_rejects_invented_states_and_old_payloads_stay_valid():
    assert "race_control" in PAYLOADS
    with pytest.raises(ValidationError):
        RaceControl.model_validate({"phase": "chequered_cthulhu"})
    with pytest.raises(ValidationError):
        RaceControl.model_validate({"sector_flags": ["clear", "clear", "mauve"]})
    old = Scoring.model_validate({"session_type": "Race", "vehicles": [{"id": 1, "driver_name": "Synthetic"}]})
    vehicle = old.vehicles[0]
    assert (vehicle.lap_dist, vehicle.pos_x, vehicle.pos_y, vehicle.pos_z) == (None, None, None, None)


def test_scoring_spatial_fields_are_normalized_and_nonfinite_values_become_unknown():
    kind, payload = normalize({"type": "ScoringUpdate", "session_type": "Race", "vehicles": [
        {"id": 1, "lap_dist": 12.5, "pos_x": 1.0, "pos_y": 2.0, "pos_z": 3.0},
        {"id": 2, "lap_dist": float("nan"), "pos_x": float("inf"), "pos_y": True, "pos_z": "3"},
        {"id": 3}]})
    assert kind == "scoring"
    first, second, third = payload["vehicles"]
    assert (first["lap_dist"], first["pos_x"], first["pos_z"]) == (12.5, 1.0, 3.0)
    assert all(second[key] is None for key in ("lap_dist", "pos_x", "pos_y", "pos_z"))
    assert all(third[key] is None for key in ("lap_dist", "pos_x", "pos_y", "pos_z"))
    validated = Scoring.model_validate(payload)
    assert validated.vehicles[1].pos_x is None and validated.vehicles[0].pos_y == 2.0


def test_map_payload_without_runtime_state_is_a_safe_unknown():
    result = track_map_payload({}, [], 100.0)
    assert result["geometry"]["state"] == "unknown" and result["vehicles"] == [] and result["race_control"] is None


def test_map_payload_flags_stale_vehicles_pit_cars_and_drops_invalid_positions():
    now = 1000.0
    state = {"last_scoring_at": now - 1, "scoring": {"vehicles": [
        {"id": 1, "pos_x": 10.123, "pos_z": -5.0, "in_pits": False, "position": 1},
        {"id": 2, "pos_x": 20.0, "pos_z": 7.0, "in_pits": True, "position": 2},
        {"id": 3, "pos_x": None, "pos_z": 1.0, "in_pits": False, "position": 3},
        {"id": 4, "pos_x": math.nan, "pos_z": 1.0, "in_pits": False, "position": 4}]},
        "race_control": RaceControl().model_dump(), "race_control_at": now - 1}
    participants = [{"local_vehicle_id": 1, "id": "p1", "display_name": "Synthetic A"}]
    fresh = track_map_payload(state, participants, now)
    assert [v["position"] for v in fresh["vehicles"]] == [1, 2]
    assert fresh["vehicles"][0]["participant_id"] == "p1" and fresh["vehicles"][1]["participant_id"] is None
    assert fresh["vehicles"][1]["in_pits"] is True and fresh["vehicles_fresh"] is True
    assert fresh["race_control"]["fresh"] is True
    stale = track_map_payload(state, participants, now + 30)
    assert stale["vehicles_fresh"] is False and stale["race_control"]["fresh"] is False


async def start(db, track="Synthetic Circuit", session_type="Race", length=LENGTH):
    agent = await make_agent(db, "synthetic-engineer")
    await main.handle_message(agent, "lmu", "session_info", {"track_name": track, "track_length": length})
    return agent


def scoring(sample, session_type="Race", time_s=100.0):
    ld, x, z, pits = sample
    return Scoring.model_validate(dict(session_type=session_type, session_time=time_s, player_vehicle_id=7,
                                       vehicles=[dict(id=7, driver_name="Synthetic Driver", position=1, total_laps=1,
                                                      in_pits=pits, lap_dist=ld, pos_x=x, pos_y=0.0, pos_z=z),
                                                 dict(id=8, driver_name="Synthetic Rival", position=2, total_laps=1,
                                                      in_pits=False, lap_dist=ld - 50, pos_x=x + 5, pos_y=0.0,
                                                      pos_z=z)])).model_dump()


async def drive(agent, samples, session_type="Race", with_sectors=False):
    for sample in samples:
        await main.handle_message(agent, "lmu", "scoring", scoring(sample, session_type))
        if with_sectors:
            sector = 1 if sample[0] % LENGTH < 1000 else 2 if sample[0] % LENGTH < 2000 else 3
            await main.handle_message(agent, "lmu", "race_control", RaceControl(
                phase="green", player_sector=sector).model_dump())


async def session_id_of(agent):
    return main.runtime[str(agent["id"])]["session_id"]


async def test_end_to_end_learns_closed_track_and_exposes_vehicles_sectors_and_yellow(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40) * 1 + lap_samples(start=2.0, end=LENGTH + 40),
                with_sectors=True)
    flags = ["clear", "local_yellow", "clear"]
    await main.handle_message(agent, "lmu", "race_control",
                              RaceControl(phase="green", sector_flags=flags, player_sector=2).model_dump())
    live = await main.api_session_live(await session_id_of(agent))
    track = live["track_map"]
    assert track["geometry"]["closed"] and track["geometry"]["state"] == "complete"
    assert 20 <= len(track["geometry"]["points"]) <= 700
    assert track["geometry"]["sector_boundaries_m"] == pytest.approx([1000.0, 2000.0], abs=15)
    assert set(track["geometry"]["point_sectors"]) == {1, 2, 3}
    assert track["race_control"]["sector_flags"] == flags and track["race_control"]["fresh"]
    assert len(track["vehicles"]) == 2 and track["vehicles_fresh"] is True
    assert all(v["participant_id"] for v in track["vehicles"])
    assert [p["position"] for p in live["participants"]] == [1, 2]  # existing payload unchanged


async def test_race_control_is_available_while_the_track_is_still_learning(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=1500.0, end=1800.0))
    await main.handle_message(agent, "lmu", "race_control", RaceControl(
        phase="full_course_yellow", sector_flags=["unknown", "clear", "clear"]).model_dump())
    track = (await main.api_session_live(await session_id_of(agent)))["track_map"]
    assert track["geometry"]["state"] == "learning" and track["geometry"]["progress"] is None
    assert track["race_control"]["phase"] == "full_course_yellow" and track["vehicles"]


async def test_session_change_keeps_geometry_for_the_same_track_but_not_for_another(db):
    agent = await start(db, session_type="Practice")
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40), session_type="Practice")
    assert main.runtime[str(agent["id"])]["track_model"].complete
    await drive(agent, lap_samples(start=1500.0, end=1520.0), session_type="Race")  # Practice -> Race, no lap needed
    sid = await session_id_of(agent)
    assert (await main.api_session_live(sid))["track_map"]["geometry"]["closed"] is True
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Other Circuit", "track_length": LENGTH})
    other = main.runtime[str(agent["id"])]["track_model"]
    assert not other.complete and other is not main.track_store.get(("lmu", "Synthetic Circuit", round(LENGTH)), LENGTH)


async def test_geometry_survives_a_simulator_reconnect_within_the_server_process(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40))
    await main.handle_message(agent, "lmu", "connection", {"game_connected": False, "telemetry_valid": True})
    await main.handle_message(agent, "lmu", "connection", {"game_connected": True, "telemetry_valid": True})
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Synthetic Circuit", "track_length": LENGTH})
    assert main.runtime[str(agent["id"])]["track_model"].complete


async def test_old_agent_without_spatial_fields_or_race_control_still_works(db):
    agent = await start(db)
    legacy = Scoring.model_validate(dict(session_type="Race", session_time=100, player_vehicle_id=7, vehicles=[
        dict(id=7, driver_name="Synthetic Driver", position=1, total_laps=3)])).model_dump()
    await main.handle_message(agent, "lmu", "scoring", legacy)
    track = (await main.api_session_live(await session_id_of(agent)))["track_map"]
    assert track["vehicles"] == [] and track["race_control"] is None
    assert track["geometry"]["state"] == "learning" and track["geometry"]["points"] == []


async def test_map_state_is_read_only_runtime_state(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=300.0))
    async with db.acquire() as conn:
        tables = ["laps", "strategy_samples", "session_participants", "sessions"]
        before = [await conn.fetch(f"SELECT * FROM {t} ORDER BY id") for t in tables]
    time.sleep(0)
    await main.api_session_live(await session_id_of(agent))
    async with db.acquire() as conn:
        assert before == [await conn.fetch(f"SELECT * FROM {t} ORDER BY id") for t in tables]
