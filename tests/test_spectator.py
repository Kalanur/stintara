from contextlib import asynccontextmanager

import httpx
import pytest

from server.app import main
from server.app.lifecycle import attach_observation
from server.app.uuid7 import uuid7
from tests.conftest import make_agent, scoring_session


def participant(position=1):
    return dict(id=uuid7(), local_vehicle_id=7, display_name="Same Name", driver_id=None,
                position=position, car_name="Synthetic entry", car_class="GT3", total_laps=2)


def test_projection_preserves_unresolved_duplicates_and_source_positions():
    session = uuid7()
    rows = [participant(2), participant(2), participant(None)]
    result = main.spectator_participants(rows, session, True, [], 100)
    assert len(result) == 3
    assert [p["position"] for p in result] == [2, 2, None]
    assert all(p["driver_id"] is None and p["display_name"] == "Same Name" for p in result)
    assert all(p["gap_to_leader"] is None and p["interval"] is None for p in result)
    assert all(p["vehicle_model"] is None and p["entry_name"] == "Synthetic entry" for p in result)
    assert "entry_name" not in rows[0]


@pytest.mark.parametrize("live", [True, False])
def test_projection_uses_newest_fresh_same_session_only(live):
    session = uuid7()
    def state(sid, at, position):
        return dict(session_id=sid, last_scoring_at=at, scoring={"vehicles": [
            {"id": 7, "position": position, "total_laps": 3, "in_pits": True,
             "time_behind_leader": -4}]})
    states = [state(session, 99, 3), state(session, 98, 4), state(uuid7(), 100, 5),
              state(session, 80, 6)]
    p = main.spectator_participants([participant()], session, live, states, 100)[0]
    assert p["position"] == (3 if live else 1)
    assert p["gap_to_leader"] is None
    assert main.spectator_participants([participant()], session, True, states[2:], 100)[0]["position"] == 1


class FakePool:
    def __init__(self, session):
        self.session = session
        self.queries = []

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetchrow(self, query, session_id):
        self.queries.append(query)
        return self.session

    async def fetch(self, query, *args):
        self.queries.append(query)
        return [participant()]


async def test_http_routes_validation_not_found_and_offline_field(monkeypatch):
    sid = uuid7()
    monkeypatch.setattr(main, "pool", FakePool(dict(id=sid, live=False, status="ended")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost") as client:
        response = await client.get(f"/api/sessions/{sid}/live")
        assert response.status_code == 200
        assert len(main.pool.queries) == 2
        assert "LEFT JOIN LATERAL" in main.pool.queries[1]
        assert response.json()["live"] is False
        assert response.json()["participants"][0]["driver_id"] is None
        assert (await client.get("/api/sessions/not-a-uuid/live")).status_code == 422
        page = await client.get(f"/sessions/{sid}/live")
        assert page.status_code == 200 and "__SESSION_ID__" not in page.text
        assert str(sid) in page.text
        monkeypatch.setattr(main, "pool", FakePool(None))
        assert (await client.get(f"/api/sessions/{sid}/live")).status_code == 404


async def test_database_session_list_field_separation_and_staleness(db):
    _, first, _ = await scoring_session(db, "one")
    _, second, _ = await scoring_session(db, "two")
    observer = await make_agent(db, "observer")
    async with db.acquire() as conn, conn.transaction():
        await attach_observation(conn, first, observer["id"])
    listed = await main.api_live_sessions()
    assert {s["id"] for s in listed} == {first, second}
    assert all(s["participant_count"] == 2 for s in listed)
    data = await main.api_session_live(first)
    assert data["live"] and len(data["participants"]) == 2
    assert sum(p["driver_id"] is None for p in data["participants"]) == 1
    assert len({p["id"] for p in data["participants"]}) == 2
    assert all(p["car_number"] is None for p in data["participants"])
    async with db.acquire() as conn:
        await conn.execute("UPDATE session_observations SET last_seen_at=now()-interval '16 seconds' WHERE session_id=$1", first)
    assert {s["id"] for s in await main.api_live_sessions()} == {second}
    stale = await main.api_session_live(first)
    assert not stale["live"] and len(stale["participants"]) == 2
    async with db.acquire() as conn:
        await conn.execute("UPDATE sessions SET status='complete' WHERE id=$1", second)
    assert await main.api_live_sessions() == []


async def test_driver_focus_id_is_actual_participant_not_equal_name(db):
    agent, session_id, _ = await scoring_session(db, "synthetic")
    data = await main.api_driver_live(agent["slug"])
    assert data["live"] is True and data["session"]["id"] == session_id
    assert data["driver"]["id"] == agent["driver_id"]
    assert data["strategy"] is None
    assert len(data["participants"]) == 2
    assert len({p["display_name"] for p in data["participants"]}) == 1
    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT id,driver_id,local_vehicle_id FROM session_participants WHERE session_id=$1", session_id)
    by_local = {row["local_vehicle_id"]: row for row in rows}
    legacy_fields = {"local_vehicle_id", "display_name", "team_name", "car_name", "car_class", "car_number",
                     "position", "total_laps", "last_lap_time", "best_lap_time", "gap_to_leader",
                     "laps_behind_leader", "in_pits", "pitstops", "finish_status", "has_telemetry",
                     "is_stintara_driver", "entry_name", "vehicle_class", "vehicle_model", "manufacturer"}
    for p in data["participants"]:
        assert set(p) == legacy_fields | {"participant_id"}
        row = by_local[p["local_vehicle_id"]]
        assert p["participant_id"] == row["id"]
        assert p["is_stintara_driver"] == (row["driver_id"] == agent["driver_id"])
    assert by_local[8]["driver_id"] is None
    assert by_local[7]["driver_id"] == agent["driver_id"]
    assert len({p["participant_id"] for p in data["participants"]}) == 2


def wheels(*remaining, sensor=80.0):
    return [dict(remaining=r, temp_mid=sensor) for r in remaining]


def test_field_status_remaining_tyre_semantics_and_unknowns():
    from server.app.timing import field_status
    # mWear is tyre remaining (1.0 new): the field value is the worst (minimum) corner, never inverted.
    assert field_status("Medium", wheels(1.0, 0.8, 0.9, 0.95), [0] * 8) == dict(
        tyre_compound="Medium", tyre_remaining_min=0.8, damage_regions=0, damage_max=0)
    assert field_status(None, wheels(1.0, 0.5, None, 1.0), None) == dict(
        tyre_compound=None, tyre_remaining_min=None, damage_regions=None, damage_max=None)
    assert field_status(" ", wheels(1, 1, 1, 1), [0, 1, 0, 1, 1, 0, 0, 0])["damage_regions"] == 3
    assert field_status("Wet", wheels(1, 1, 1, float("nan")), [0, True])["tyre_remaining_min"] is None
    assert field_status("Wet", [], [0, -1])["damage_regions"] is None
    assert field_status("Wet", None, None)["tyre_remaining_min"] is None


def test_field_status_ignores_provider_placeholder_wear_without_sensors():
    from server.app.timing import field_status
    # Non-player cars: mWear constant 1.0, all sensors sentinel (normalized to None) -> unknown, not "new".
    placeholder = [dict(remaining=1.0, temp_inner=None, temp_mid=None, temp_outer=None,
                        carcass_temp=None, brake_temp=None, pressure=None) for _ in range(4)]
    result = field_status("Medium", placeholder, [0, 0, 0, 0, 0, 0, 0, 0])
    assert result["tyre_remaining_min"] is None
    assert result["tyre_compound"] == "Medium" and result["damage_regions"] == 0


def test_projection_field_status_prefers_fresh_snapshot_over_checkpoint_and_stays_unknown():
    import json
    session = uuid7()
    stored = dict(participant(1), compound_front="Soft", tyre_snapshot=json.dumps(wheels(0.5, 0.5, 0.5, 0.5)),
                  lap_damage='{"dent_severity": [0, 0, 0, 0, 0, 0, 0, 0]}', sector1=30.0, sector2=40.0, sector3=20.0)
    p = main.spectator_participants([dict(stored)], session, True, [], 100)[0]
    assert (p["tyre_compound"], p["tyre_remaining_min"], p["damage_regions"]) == ("Soft", 0.5, 0)
    assert (p["sector1"], p["sector2"], p["sector3"]) == (30.0, 40.0, 20.0)
    snap = {"has_telemetry": True, "tire_compound_front_name": "Hard", "wheels": wheels(0.9, 0.9, 0.9, 0.9),
            "damage": {"dent_severity": [0, 2, 0, 0, 0, 0, 0, 0]}}
    live = dict(session_id=session, last_scoring_at=99, snapshots_at=99, snapshots={7: snap},
                scoring={"vehicles": [{"id": 7, "position": 1, "total_laps": 2}]})
    p = main.spectator_participants([dict(stored)], session, True, [live], 100)[0]
    assert (p["tyre_compound"], p["tyre_remaining_min"], p["damage_regions"], p["damage_max"]) == ("Hard", 0.9, 1, 2)
    stale = dict(live, snapshots_at=10)
    assert main.spectator_participants([dict(stored)], session, True, [stale], 100)[0]["tyre_compound"] == "Soft"
    # No checkpoint and no snapshot: everything unknown, no guessed values, timing sectors unknown too.
    bare = main.spectator_participants([participant(1)], session, True, [], 100)[0]
    assert bare["tyre_compound"] is bare["tyre_remaining_min"] is bare["damage_regions"] is None
    assert bare.get("sector1") is None
