"""Vehicles-only refresh (#55): runtime-only, session-scoped, ordered, no PostgreSQL per poll. Synthetic data only."""
import asyncio
import json
import time
from uuid import UUID

import pytest

from server.app import main
from server.app.trackmap import SERVER_EPOCH, VEHICLE_REFRESH_MS
from server.app.uuid7 import uuid7
from tests.conftest import make_agent
from tests.test_convergence import deliver, roster


class NoDatabase:
    """Stands in for the pool: any use of PostgreSQL by the endpoint is a failure and is counted."""
    acquired = 0

    def acquire(self, *args, **kwargs):
        self.acquired += 1
        raise AssertionError("the vehicles endpoint must not query PostgreSQL")


@pytest.fixture
def runtime_only(monkeypatch):
    guard = NoDatabase()
    monkeypatch.setattr(main, "pool", guard)
    main.runtime.clear()
    yield guard
    main.runtime.clear()


def field(count=40, start=0.0):
    return [dict(id=i + 1, position=i + 1, pos_x=start + i * 10.0, pos_z=i * 3.0, in_pits=i == 5) for i in range(count)]


def put(key, session_id, vehicles, age=0.0, seq=1, identities=True, index_session=None):
    """Install one agent's runtime state as the websocket handler would have left it."""
    names = {v["id"]: {"id": uuid7(), "display_name": f"Same Name {v['id'] % 3}"} for v in vehicles}
    main.runtime[key] = {"session_id": session_id, "last_scoring_at": time.monotonic() - age, "scoring_seq": seq,
                         "scoring": {"vehicles": vehicles},
                         **({"participant_index": {"session_id": index_session or session_id, "by_local": names}}
                            if identities else {})}
    return names


async def call(session_id):
    response = await main.api_session_vehicles(session_id)
    assert response.headers["cache-control"] == "no-store"
    return json.loads(response.body)


async def test_forty_vehicles_come_from_runtime_without_any_database_access(runtime_only):
    sid = uuid7()
    names = put("a", sid, field(40), age=0.12, seq=7)
    body = await call(sid)
    assert body["live"] is True and body["session_id"] == str(sid)
    assert (body["seq"], body["server_epoch"], body["vehicles_fresh"]) == (7, SERVER_EPOCH, True)
    assert 0.1 <= body["vehicles_age_s"] < 1.0
    assert len(body["vehicles"]) == 40
    assert {v["participant_id"] for v in body["vehicles"]} == {str(n["id"]) for n in names.values()}
    first = body["vehicles"][0]
    assert set(first) == {"participant_id", "x", "z", "in_pits", "position", "name"}
    assert (first["x"], first["z"], first["position"]) == (0.0, 0.0, 1)
    assert runtime_only.acquired == 0


async def test_many_simultaneous_viewers_cause_no_database_access(runtime_only):
    sid = uuid7()
    put("a", sid, field(40))
    bodies = await asyncio.gather(*(call(sid) for _ in range(400)))
    assert all(len(b["vehicles"]) == 40 for b in bodies)
    assert runtime_only.acquired == 0


@pytest.mark.parametrize("age,fresh,live", [(0.5, True, True), (4.9, True, True), (6.0, False, True), (14.0, False, True),
                                            (16.0, False, False)])
async def test_stale_source_is_explicit_and_old_sources_disappear(runtime_only, age, fresh, live):
    sid = uuid7()
    put("a", sid, field(3), age=age)
    body = await call(sid)
    assert body["live"] is live
    if live:
        assert body["vehicles_fresh"] is fresh and len(body["vehicles"]) == 3
    else:
        assert body["vehicles"] == [] and "seq" not in body


async def test_other_session_historical_and_unknown_ids_never_receive_positions(runtime_only):
    mine, other, unknown = uuid7(), uuid7(), uuid7()
    put("a", mine, field(2))
    put("b", other, field(5, start=500.0))
    assert len((await call(mine))["vehicles"]) == 2
    assert len((await call(other))["vehicles"]) == 5
    gone = await call(unknown)
    assert gone["live"] is False and gone["vehicles"] == []
    # even the participant identities of one session are never offered for another
    assert not {v["participant_id"] for v in (await call(mine))["vehicles"]} & {
        v["participant_id"] for v in (await call(other))["vehicles"]}


async def test_identities_cached_for_another_session_are_ignored(runtime_only):
    sid = uuid7()
    put("a", sid, field(3), index_session=uuid7())   # stale cache of a previous session
    body = await call(sid)
    assert [v["participant_id"] for v in body["vehicles"]] == [None] * 3 and [v["name"] for v in body["vehicles"]] == [None] * 3


async def test_missing_and_duplicate_participants_and_invalid_positions(runtime_only):
    sid = uuid7()
    vehicles = field(4)
    vehicles.append(dict(id=2, position=9, pos_x=99.0, pos_z=1.0))                # duplicate local id in one frame
    vehicles.append(dict(id=77, position=10, pos_x=float("nan"), pos_z=1.0))     # invalid position: skipped
    vehicles.append(dict(id=88, position=11, pos_x=None, pos_z=None))            # no position: skipped
    names = put("a", sid, vehicles)
    del main.runtime["a"]["participant_index"]["by_local"][3]                     # participant not yet persisted
    body = await call(sid)
    assert len(body["vehicles"]) == 5
    unknown = [v for v in body["vehicles"] if v["participant_id"] is None]
    assert [v["position"] for v in unknown] == [3]                                # never guessed from names/order
    duplicates = [v for v in body["vehicles"] if v["participant_id"] == str(names[2]["id"])]
    assert len(duplicates) == 2                                                   # reported as is; the page labels one dot


async def test_equal_display_names_do_not_merge_participants(runtime_only):
    sid = uuid7()
    put("a", sid, field(6))
    body = await call(sid)
    assert len({v["participant_id"] for v in body["vehicles"]}) == 6
    assert len({v["name"] for v in body["vehicles"]}) == 3


async def test_newest_fresh_observation_of_the_same_session_wins_across_agents(runtime_only):
    sid = uuid7()
    put("a", sid, field(2), age=1.0, seq=10)
    put("b", sid, field(3), age=0.2, seq=11)
    assert (await call(sid))["seq"] == 11
    main.runtime["a"]["last_scoring_at"] = time.monotonic() - 0.01
    main.runtime["a"]["scoring_seq"] = 12
    assert (await call(sid))["seq"] == 12


async def test_full_live_track_map_carries_the_same_sample_identity(runtime_only):
    sid = uuid7()
    put("a", sid, field(3), seq=5)
    from server.app.trackmap import track_map_payload
    payload = track_map_payload(main.runtime["a"], [], time.monotonic())
    body = await call(sid)
    assert (payload["vehicles_seq"], payload["server_epoch"], payload["vehicle_refresh_ms"]) == (5, SERVER_EPOCH, VEHICLE_REFRESH_MS)
    assert (body["seq"], body["server_epoch"]) == (5, SERVER_EPOCH)


# --- through the real ingest path (PostgreSQL only used by ingest, never by the endpoint) ---------------------------

def scoring(clock=100.0, count=40, player_vehicle_id=1):
    vehicles = [dict(id=i + 1, driver_name="Same Name", position=i + 1, total_laps=1, pos_x=float(i), pos_z=0.0)
                for i in range(count)]
    return dict(session_type="Race", session_time=clock, player_vehicle_id=player_vehicle_id, vehicles=vehicles)


async def test_ingest_maps_vehicles_to_the_stored_participant_ids_without_extra_queries(db):
    agent = await make_agent(db, "one")
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Imola"})
    await main.handle_message(agent, "lmu", "scoring", scoring())
    state = main.runtime[str(agent["id"])]
    sid = state["session_id"]
    real_pool, main.pool = main.pool, NoDatabase()
    try:
        body = await call(sid)
    finally:
        main.pool = real_pool
    live = await main.api_session_live(sid)
    stored = {p["local_vehicle_id"]: p for p in live["participants"]}
    assert len(body["vehicles"]) == 40
    for v, local in zip(body["vehicles"], range(1, 41)):
        assert UUID(v["participant_id"]) == stored[local]["id"] and v["name"] == stored[local]["display_name"]
    assert live["track_map"]["vehicles_seq"] == body["seq"] and live["track_map"]["server_epoch"] == body["server_epoch"]
    assert live["track_map"]["vehicle_refresh_ms"] == VEHICLE_REFRESH_MS


async def test_sequence_increases_with_every_scoring_frame_even_between_persists(db):
    agent = await make_agent(db, "one")
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Imola"})
    await main.handle_message(agent, "lmu", "scoring", scoring(100.0, 3))
    sid = main.runtime[str(agent["id"])]["session_id"]
    seqs = [(await call(sid))["seq"]]
    for tick in range(1, 4):
        await main.handle_message(agent, "lmu", "scoring", scoring(100.0 + tick * 0.2, 3))   # not persisted (1 Hz)
        seqs.append((await call(sid))["seq"])
    assert seqs == sorted(set(seqs)) and len(seqs) == 4


async def test_session_switch_drops_identities_and_never_leaks_across_sessions(db):
    agent = await make_agent(db, "one")
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Imola"})
    await main.handle_message(agent, "lmu", "scoring", scoring(100.0, 3))
    state = main.runtime[str(agent["id"])]
    first = state["session_id"]
    first_ids = {v["participant_id"] for v in (await call(first))["vehicles"]}
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Spa"})
    await main.handle_message(agent, "lmu", "scoring", scoring(10.0, 2))
    second = state["session_id"]
    assert second != first
    assert state["participant_index"]["session_id"] == second
    assert (await call(first)) == {"session_id": str(first), "live": False, "vehicles": []}
    second_ids = {v["participant_id"] for v in (await call(second))["vehicles"]}
    assert len(second_ids) == 2 and not second_ids & first_ids


async def test_short_disconnect_and_reconnect_keep_the_same_participants(db):
    agent = await make_agent(db, "one")
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Imola"})
    await main.handle_message(agent, "lmu", "scoring", scoring(100.0, 3))
    sid = main.runtime[str(agent["id"])]["session_id"]
    before = {v["participant_id"] for v in (await call(sid))["vehicles"]}
    main.runtime.clear()                                 # lost socket / server runtime state
    assert (await call(sid))["live"] is False
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Imola"})
    await main.handle_message(agent, "lmu", "scoring", scoring(101.0, 3))
    assert main.runtime[str(agent["id"])]["session_id"] == sid
    body = await call(sid)
    assert {v["participant_id"] for v in body["vehicles"]} == before and len(before) == 3
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM session_participants") == 3


async def test_two_agents_in_one_session_share_participants_not_duplicates(db):
    a, b = await make_agent(db, "one"), await make_agent(db, "two")

    def frame(player):
        vehicles = [{**v, "position": v["id"] + 1, "pos_x": float(v["id"]), "pos_z": 0.0} for v in roster()]
        return dict(session_type="Practice", session_time=100, player_vehicle_id=player, vehicles=vehicles)
    sid = await deliver(a, frame(0))
    assert await deliver(b, frame(1)) == sid                 # converged: one central session
    first, second = main.runtime[str(a["id"])], main.runtime[str(b["id"])]
    assert first["participant_index"]["by_local"] == second["participant_index"]["by_local"]
    body = await call(sid)
    assert len({v["participant_id"] for v in body["vehicles"]}) == 10 and None not in {v["participant_id"] for v in body["vehicles"]}
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM session_participants") == 10
