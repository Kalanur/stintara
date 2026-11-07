"""Durable track geometry: persistence rules, restart reuse, fail-closed loading and historical sessions (synthetic)."""
import asyncio
import copy
import json
import math

import pytest

from server.app import main, track_repo
from server.app.protocol import RaceControl
from server.app.trackmap import (
    GEOMETRY_SCHEMA_VERSION,
    CenterlineLearner,
    TrackModel,
    geometry_record,
    static_track_map_payload,
    track_key,
    validate_record,
)
from tests.conftest import make_agent
from tests.test_race_control import drive, scoring, start
from tests.track_fixtures import LENGTH, lap_samples

NAME = "Synthetic Circuit"
KEY = ("lmu", NAME, round(LENGTH))


def closed_model(step=12.0, length=LENGTH):
    model = TrackModel(length)
    learner = CenterlineLearner(model)
    for sample in lap_samples(start=2.0, end=length + 40, step=step, length=length):
        learner.add(*sample)
    assert model.complete
    return model


async def rows(db):
    async with db.acquire() as conn:
        return await conn.fetch("SELECT * FROM track_geometries ORDER BY track_name, track_length_key")


def restart():
    """Server restart: every in-memory structure is gone, PostgreSQL stays."""
    main.runtime.clear()
    main.active_agents.clear()
    main.track_store.clear()


async def connect(db, name="after-restart", track=NAME, length=LENGTH):
    agent = await make_agent(db, name)
    await main.handle_message(agent, "lmu", "session_info", {"track_name": track, "track_length": length})
    return agent


async def session_of(agent):
    return main.runtime[str(agent["id"])]["session_id"]


def model_of(agent):
    return main.runtime[str(agent["id"])]["track_model"]


# --- pure validation -------------------------------------------------------------------------------------

def good_raw():
    return json.loads(json.dumps(geometry_record(closed_model())))


def test_a_learned_record_round_trips_validation_unchanged():
    model = closed_model()
    record = validate_record(json.loads(json.dumps(geometry_record(model))), KEY)
    assert record["points"] == model.points and record["distances"] == model.distances
    assert record["length_m"] == model.length


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(schema_version=GEOMETRY_SCHEMA_VERSION + 1),
    lambda r: r.update(schema_version=None),
    lambda r: r.update(points="not a list"),
    lambda r: r.update(points=r["points"][:-1]),                      # points / distances differ in count
    lambda r: r.update(points=r["points"][:5], distances=r["distances"][:5]),   # too few points
    lambda r: r["points"].__setitem__(3, [1.0]),
    lambda r: r["points"].__setitem__(3, [1.0, "x"]),
    lambda r: r["points"].__setitem__(3, [float("nan"), 1.0]),
    lambda r: r["points"].__setitem__(3, [1e9, 1e9]),                 # teleport inside the stored line
    lambda r: r["distances"].__setitem__(3, float("inf")),
    lambda r: r["distances"].__setitem__(3, r["distances"][2]),       # not strictly increasing
    lambda r: r.update(distances=[d + LENGTH for d in r["distances"]]),
    lambda r: r.update(length_m=50.0),
    lambda r: r.update(length_m=float("nan")),
    lambda r: r.update(length_m=LENGTH + 500),                        # contradicts the track key
    lambda r: r.update(points=r["points"] * 40, distances=r["distances"] * 40),  # absurd point count
    lambda r: r.update(distances=r["distances"][:10] + [d + 150 for d in r["distances"][10:]]),  # oversized gap
    lambda r: r.update(points=None),
    lambda r: r.update(distances=None),
])
def test_corrupt_or_implausible_geometry_is_rejected_as_a_whole(mutate):
    raw = good_raw()
    mutate(raw)
    assert validate_record(raw, KEY) is None


def test_non_dict_and_missing_records_are_rejected():
    for raw in (None, [], "x", 5, {}):
        assert validate_record(raw, KEY) is None


def test_bad_sector_data_only_drops_the_sectors_not_the_geometry():
    raw = good_raw()
    raw["sector_observations"] = {"1": [1000.0, float("nan")], "2": [2000.0]}
    record = validate_record(raw, KEY)
    assert record is not None and record["sector_observations"] is None
    raw["sector_observations"] = {"1": [LENGTH * 2], "2": [2000.0]}
    assert validate_record(raw, KEY)["sector_observations"] is None


def test_track_identity_is_exact_never_fuzzy():
    assert track_key("lmu", NAME, 3000.4) == track_key("lmu", NAME, 2999.6)
    assert track_key("lmu", NAME, 3000) != track_key("lmu", NAME, 3001)
    assert track_key("lmu", "Synthetic Circuit B", 3000) != track_key("lmu", NAME, 3000)
    assert track_key("other", NAME, 3000) != track_key("lmu", NAME, 3000)
    assert track_key("lmu", NAME, None) is None and track_key("lmu", NAME, float("nan")) is None


def test_static_payload_is_geometry_only_and_unknown_stays_unknown():
    payload = static_track_map_payload(closed_model())
    assert payload["static_only"] is True and payload["vehicles"] == [] and payload["race_control"] is None
    assert payload["vehicles_fresh"] is False and payload["geometry"]["closed"] is True
    for empty in (None, TrackModel(LENGTH)):
        unknown = static_track_map_payload(empty)
        assert unknown["static_only"] is False and unknown["geometry"]["state"] == "unknown"
        assert unknown["geometry"]["points"] == [] and unknown["vehicles"] == []


def test_geometry_record_is_a_copy_of_raw_values():
    model = closed_model()
    record = geometry_record(model)
    snapshot = copy.deepcopy(model.points)
    record["points"][0][0] += 1
    assert model.points == snapshot
    assert all(math.isfinite(v) for p in record["points"] for v in p)


# --- persistence -----------------------------------------------------------------------------------------

async def test_incomplete_learning_is_never_persisted(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=LENGTH - 400))      # lap not finished
    assert await rows(db) == []
    await drive(agent, lap_samples(start=1500.0, end=1700.0))          # mid-lap restart of the attempt
    assert await rows(db) == []


async def test_rejected_attempts_are_not_persisted(db):
    attempts = {
        "teleport": [s if s[0] < 1400 or s[0] > 1412 else (s[0], s[1] + 900, s[2], s[3])
                     for s in lap_samples(start=2.0, end=LENGTH + 40)],
        "pit": lap_samples(start=2.0, end=LENGTH + 40, pits=lambda d: 1500 < d < 1600),
        "gap": lap_samples(start=2.0, end=LENGTH + 40, skip=lambda d: 1500 < d < 1700),
    }
    for name, samples in attempts.items():
        restart()
        await drive(await connect(db, name), samples)
        assert await rows(db) == [], name


async def test_accepted_closed_geometry_is_persisted_unchanged(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40))
    model = model_of(agent)
    (row,) = await rows(db)
    assert (row["provider"], row["track_name"], row["track_length_key"]) == KEY
    assert row["schema_version"] == GEOMETRY_SCHEMA_VERSION and row["learned_at"] and row["updated_at"]
    assert json.loads(row["points"]) == model.points and json.loads(row["distances"]) == model.distances
    assert row["length_m"] == model.length and len(json.loads(row["points"])) <= 700
    assert model.stored is True and model.source == "learned"
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40))        # further laps change nothing
    assert len(await rows(db)) == 1


async def test_server_restart_reuses_the_stored_track_without_a_learning_lap(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40))
    original = json.loads((await rows(db))[0]["points"])
    restart()
    again = await connect(db)
    assert model_of(again).complete and model_of(again).source == "stored"
    assert model_of(again).points == original
    await main.handle_message(again, "lmu", "scoring", scoring((1500.0, 1.0, -1.0, False)))
    geometry = (await main.api_session_live(await session_of(again)))["track_map"]["geometry"]
    assert geometry["closed"] and geometry["source"] == "stored" and geometry["points"] == original
    assert len(await rows(db)) == 1


async def test_later_session_of_the_same_track_needs_no_new_lap(db):
    agent = await start(db, session_type="Practice")
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40), session_type="Practice")
    restart()
    qualifier = await connect(db, "qualifier")
    await drive(qualifier, lap_samples(start=1500.0, end=1520.0), session_type="Qualifying")
    racer = await connect(db, "racer")
    await drive(racer, lap_samples(start=1500.0, end=1520.0), session_type="Race")
    assert model_of(qualifier).complete and model_of(racer) is model_of(qualifier)
    assert len(await rows(db)) == 1


async def test_other_tracks_never_receive_the_stored_geometry(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40))
    restart()
    for name, track, length in (("other-name", "Synthetic Circuit B", LENGTH),
                                ("other-length", NAME, LENGTH + 250.0),
                                ("no-length", NAME, None)):
        other = await connect(db, name, track, length)
        model = main.runtime[str(other["id"])].get("track_model")
        assert model is None or not model.complete, name
    stranger = await make_agent(db, "other-provider")
    await main.handle_message(stranger, "other", "session_info", {"track_name": NAME, "track_length": LENGTH})
    assert not model_of(stranger).complete
    assert len(await rows(db)) == 1


@pytest.mark.parametrize("damage", [
    "UPDATE track_geometries SET points='\"garbage\"'::jsonb",
    "UPDATE track_geometries SET points='[[1,2]]'::jsonb, distances='[1]'::jsonb",
    "UPDATE track_geometries SET distances='[]'::jsonb",
    "UPDATE track_geometries SET length_m=5",
    "UPDATE track_geometries SET schema_version=99",
])
async def test_corrupt_stored_geometry_is_ignored_and_the_track_is_learned_again(db, damage):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40))
    async with db.acquire() as conn:
        await conn.execute(damage)
    restart()
    again = await connect(db)
    assert not model_of(again).complete
    await main.handle_message(again, "lmu", "scoring", scoring((10.0, 1.0, -1.0, False)))
    geometry = (await main.api_session_live(await session_of(again)))["track_map"]["geometry"]
    assert geometry["state"] == "learning" and geometry["closed"] is False


async def test_corrupt_current_version_row_is_replaced_but_a_newer_schema_row_is_kept(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40))
    good = (await rows(db))[0]["points"]
    async with db.acquire() as conn:
        await conn.execute("UPDATE track_geometries SET points='\"garbage\"'::jsonb")
    restart()
    await drive(await connect(db), lap_samples(start=2.0, end=LENGTH + 40))
    (row,) = await rows(db)
    assert json.loads(row["points"]) == json.loads(good)               # healed by the new closed lap
    async with db.acquire() as conn:
        await conn.execute("UPDATE track_geometries SET schema_version=99, points='[]'::jsonb")
    restart()
    newer = await connect(db, "newer")
    await drive(newer, lap_samples(start=2.0, end=LENGTH + 40))
    (row,) = await rows(db)
    assert row["schema_version"] == 99 and json.loads(row["points"]) == []   # never downgraded
    assert model_of(newer).complete                                           # still usable in memory


async def test_concurrent_writers_converge_on_one_canonical_geometry(db):
    first, second = closed_model(step=12.0), closed_model(step=10.0)
    assert first.points != second.points

    async def write(model):
        async with db.acquire() as conn:
            return await track_repo.save_geometry(conn, KEY, model)

    results = await asyncio.gather(write(first), write(second), write(first), write(second))
    assert len(await rows(db)) == 1
    assert all(r["points"] == results[0]["points"] for r in results)
    assert results[0]["points"] in (first.points, second.points)


async def test_two_agents_learning_the_same_track_share_one_row_and_model(db):
    one, two = await start(db), await connect(db, "second")
    samples = lap_samples(start=2.0, end=LENGTH + 40)
    await asyncio.gather(drive(one, samples), drive(two, samples))
    assert len(await rows(db)) == 1
    assert model_of(one) is model_of(two)


async def test_sector_boundaries_refine_the_stored_row_without_touching_the_geometry(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40))
    row = (await rows(db))[0]
    assert json.loads(row["sector_boundaries_m"]) is None
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40) * 2, with_sectors=True)
    after = (await rows(db))[0]
    assert json.loads(after["sector_boundaries_m"]) == pytest.approx([1000.0, 2000.0], abs=15)
    assert after["points"] == row["points"] and after["distances"] == row["distances"]
    assert after["learned_at"] == row["learned_at"] and after["updated_at"] > row["updated_at"]
    restart()
    again = await connect(db)
    assert model_of(again).sector_boundaries() == pytest.approx([1000.0, 2000.0], abs=15)


async def test_database_failures_never_break_ingest(db):
    agent = await start(db)
    async with db.acquire() as conn:
        await conn.execute("ALTER TABLE track_geometries RENAME TO track_geometries_gone")
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40))
    model = model_of(agent)
    assert model.complete and not model.stored and model.store_retry_at > 0   # kept in memory, retried later


async def test_no_vehicle_positions_or_race_control_are_persisted(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40), with_sectors=True)
    await main.handle_message(agent, "lmu", "race_control", RaceControl(
        phase="full_course_yellow", sector_flags=["local_yellow", "clear", "clear"]).model_dump())
    async with db.acquire() as conn:
        columns = {r["column_name"] for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name='track_geometries' "
            "AND table_schema=current_schema()")}
    assert columns == {"id", "provider", "track_name", "track_length_key", "schema_version", "length_m", "points",
                       "distances", "sector_observations", "sector_boundaries_m", "learned_at", "updated_at"}
    (row,) = await rows(db)
    text = json.dumps([row["points"], row["distances"], row["sector_observations"]])
    assert "yellow" not in text and "vehicle" not in text


# --- historical sessions ---------------------------------------------------------------------------------

async def test_historical_session_without_runtime_state_gets_only_the_static_geometry(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40), with_sectors=True)
    sid = await session_of(agent)
    stored = json.loads((await rows(db))[0]["points"])
    restart()                                                            # no runtime state at all
    live = await main.api_session_live(sid)
    track = live["track_map"]
    assert track["static_only"] is True and track["geometry"]["closed"] is True
    assert track["geometry"]["points"] == stored and track["geometry"]["source"] == "stored"
    assert track["vehicles"] == [] and track["race_control"] is None
    assert track["vehicles_fresh"] is False and track["vehicles_age_s"] is None
    assert "track_length_m" not in live["session"]                       # internal lookup field only


async def test_historical_session_of_an_unknown_track_stays_unknown(db):
    agent = await start(db, track="Never Learned", length=2500.0)
    await drive(agent, lap_samples(start=2.0, end=300.0, length=2500.0))
    sid = await session_of(agent)
    restart()
    track = (await main.api_session_live(sid))["track_map"]
    assert track["static_only"] is False and track["geometry"]["state"] == "unknown"
    assert track["geometry"]["points"] == [] and track["vehicles"] == [] and track["race_control"] is None


async def test_historical_session_ignores_corrupt_stored_geometry(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40))
    sid = await session_of(agent)
    async with db.acquire() as conn:
        await conn.execute("UPDATE track_geometries SET points='{\"a\":1}'::jsonb")
    restart()
    track = (await main.api_session_live(sid))["track_map"]
    assert track["geometry"]["state"] == "unknown" and track["static_only"] is False


async def test_live_runtime_state_still_wins_over_the_static_fallback(db):
    agent = await start(db)
    await drive(agent, lap_samples(start=2.0, end=LENGTH + 40))
    track = (await main.api_session_live(await session_of(agent)))["track_map"]
    assert track["static_only"] is False and track["vehicles"] and track["vehicles_fresh"] is True
