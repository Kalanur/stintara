"""Persisted session history / recent results. Synthetic data only."""
import base64
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from server.app import history, main
from server.app.uuid7 import uuid7

T0 = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


async def add_session(db, *, minutes=0, status="complete", stype="Race", track="Synthetic Ring",
                      provider="lmu", ended=True, ended_minutes=None, last_seen_minutes=None):
    sid = uuid7()
    started = T0 + timedelta(minutes=minutes)
    ended_at = started + timedelta(minutes=(ended_minutes if ended_minutes is not None else 30)) if ended else None
    last_seen = T0 + timedelta(minutes=last_seen_minutes if last_seen_minutes is not None else minutes)
    async with db.acquire() as conn:
        await conn.execute("""INSERT INTO sessions(id,simulator_provider,session_type,track_name,status,
            started_at,ended_at,last_seen_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8)""",
                           sid, provider, stype, track, status, started, ended_at, last_seen)
    return sid


async def add_participant(db, sid, local_id, position, finish=0, name=None, driver_id=None):
    pid = uuid7()
    async with db.acquire() as conn:
        await conn.execute("""INSERT INTO session_participants(id,session_id,driver_id,provider,local_vehicle_id,
            display_name,position,finish_status) VALUES($1,$2,$3,'lmu',$4,$5,$6,$7)""",
                           pid, sid, driver_id, local_id, name or f"Synthetic {local_id}", position, finish)
    return pid


async def add_driver(db, slug):
    did = uuid7()
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO drivers(id,slug,display_name) VALUES($1,$2,$3)", did, slug, slug.title())
    return did


async def get(path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost") as client:
        return await client.get(path)


async def test_recent_results_only_completed_races_newest_ended_first_max_five(db):
    for i in range(7):  # ended_at ascending with i, but started in reverse order
        await add_session(db, minutes=100 - i, ended_minutes=None)
    expected = []
    for i in range(7):
        sid = await add_session(db, minutes=i * 10, ended_minutes=5 + i)
        expected.append(sid)
    live = await add_session(db, minutes=500, status="live", ended=False)
    quali = await add_session(db, minutes=501, stype="Qualifying")
    practice = await add_session(db, minutes=502, stype="Practice")
    no_end = await add_session(db, minutes=503, ended=False)  # complete but no completion time
    data = (await get("/api/sessions/recent-results")).json()
    ids = [x["id"] for x in data["items"]]
    assert len(ids) == 5 and data["limit"] == 5
    async with db.acquire() as conn:
        want = [str(r["id"]) for r in await conn.fetch(
            "SELECT id FROM sessions WHERE status='complete' AND session_type='Race' AND ended_at IS NOT NULL "
            "ORDER BY ended_at DESC, id DESC LIMIT 5")]
    assert ids == want
    assert not {str(live), str(quali), str(practice), str(no_end)} & set(ids)


async def test_recent_results_use_ended_at_not_last_seen(db):
    older_end = await add_session(db, minutes=0, ended_minutes=10, last_seen_minutes=9000)
    newer_end = await add_session(db, minutes=1, ended_minutes=60, last_seen_minutes=2)
    items = (await get("/api/sessions/recent-results")).json()["items"]
    assert [x["id"] for x in items] == [str(newer_end), str(older_end)]
    assert items[0]["ended_at"] == (T0 + timedelta(minutes=61)).isoformat().replace("+00:00", "Z")
    assert "last_seen_at" not in items[0]


async def test_recent_results_tie_break_is_deterministic(db):
    a = await add_session(db, minutes=0, ended_minutes=30)
    b = await add_session(db, minutes=0, ended_minutes=30)
    items = (await get("/api/sessions/recent-results")).json()["items"]
    assert [x["id"] for x in items] == [str(max(a, b)), str(min(a, b))]


async def test_results_are_persisted_only_and_survive_restart(db):
    sid = await add_session(db, minutes=1)
    main.runtime["ghost"] = {"session_id": sid, "scoring": {"vehicles": []}, "last_scoring_at": 1e12}
    with_runtime = (await get("/api/sessions/recent-results")).json()
    main.runtime.clear()
    main.active_agents.clear()  # simulated restart: only PostgreSQL remains
    after = (await get("/api/sessions/recent-results")).json()
    assert with_runtime == after and after["items"][0]["id"] == str(sid)
    assert (await get("/api/sessions/history")).json()["items"][0]["id"] == str(sid)
    assert (await get(f"/api/sessions/{sid}/live")).status_code == 200  # detail reachable without agent


async def test_history_newest_start_first_and_filters(db):
    a = await add_session(db, minutes=10, stype="Practice", track="Alpha", provider="lmu")
    b = await add_session(db, minutes=20, stype="Race", track="Beta", provider="acc")
    c = await add_session(db, minutes=30, stype="Race", track="Alpha", provider="lmu", status="live", ended=False)

    async def ids(query=""):
        return [x["id"] for x in (await get("/api/sessions/history" + query)).json()["items"]]
    assert await ids() == [str(c), str(b), str(a)]
    assert await ids("?session_type=Race") == [str(c), str(b)]
    assert await ids("?provider=acc") == [str(b)]
    assert await ids("?track=Alpha") == [str(c), str(a)]
    assert await ids("?track=Alph") == []  # exact, not fuzzy
    assert await ids("?status=complete&session_type=Race") == [str(b)]
    assert await ids("?status=live") == [str(c)]
    live = (await get("/api/sessions/history?status=live")).json()["items"][0]
    assert live["status"] == "live" and live["ended_at"] is None
    options = (await get("/api/sessions/history/filters")).json()
    assert options == {"providers": ["acc", "lmu"], "tracks": ["Alpha", "Beta"],
                       "session_types": ["Practice", "Race"]}


async def test_history_filters_are_parameterised(db):
    await add_session(db, minutes=1, track="Alpha")
    r = await get("/api/sessions/history?track=" + "x' OR '1'='1")
    assert r.status_code == 200 and r.json()["items"] == []
    r = await get("/api/sessions/history?session_type=Race';DROP TABLE sessions;--")
    assert r.status_code == 200 and r.json()["items"] == []
    assert len((await get("/api/sessions/history")).json()["items"]) == 1


async def walk(path, limit=2):
    """Follow next_cursor until exhausted; returns the ids in order."""
    ids, cursor = [], None
    while True:
        url = f"{path}{'&' if '?' in path else '?'}limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        data = (await get(url)).json()
        ids += [x["id"] for x in data["items"]]
        cursor = data["next_cursor"]
        assert data["has_more"] == (cursor is not None)
        if not cursor:
            return ids


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode()


async def test_history_limit_cursor_bounds_and_pagination(db):
    sids = [await add_session(db, minutes=i) for i in range(5)]
    newest = [str(s) for s in reversed(sids)]
    page1 = (await get("/api/sessions/history?limit=2")).json()
    assert page1["has_more"] and "offset" not in page1
    page2 = (await get(f"/api/sessions/history?limit=2&cursor={page1['next_cursor']}")).json()
    assert [x["id"] for x in page1["items"] + page2["items"]] == newest[:4]
    assert await walk("/api/sessions/history") == newest
    assert (await get("/api/sessions/history?limit=100")).status_code == 200
    forged = [b64(b'["2026-01-01T00:00:00","' + str(uuid7()).encode() + b'"]'),  # naive timestamp
              b64(b"[1,2]"),
              b64(b'["2026-01-01T00:00:00+00:00","1; DROP TABLE sessions"]')]
    for bad in ["limit=0", "limit=101", "status=bogus", "limit=x", "session_type=", "cursor=",
                "cursor=garbage", "cursor=" + "A" * 600] + ["cursor=" + f for f in forged]:
        assert (await get("/api/sessions/history?" + bad)).status_code == 422, bad
    with pytest.raises(ValueError):
        async with db.acquire() as conn:
            await history.query_sessions(conn, limit=1000)
    assert (await get("/api/sessions/history")).json()["limit"] == 50
    assert (await get("/api/sessions/recent-results")).json()["next_cursor"] is None


async def test_cursor_ties_on_started_at_are_not_skipped_or_duplicated(db):
    sids = [await add_session(db, minutes=0) for _ in range(5)]  # identical started_at
    assert await walk("/api/sessions/history") == [str(s) for s in sorted(sids, reverse=True)]


async def test_cursor_is_stable_when_session_inserted_between_pages(db):
    old = [await add_session(db, minutes=i) for i in range(4)]
    first = (await get("/api/sessions/history?limit=2")).json()
    seen = [x["id"] for x in first["items"]]
    inserted = await add_session(db, minutes=1000)  # newer than everything already seen
    rest = (await get(f"/api/sessions/history?limit=2&cursor={first['next_cursor']}")).json()
    seen += [x["id"] for x in rest["items"]]
    assert seen == [str(s) for s in reversed(old)]  # no duplicate, nothing skipped
    assert str(inserted) not in seen
    assert (await get("/api/sessions/history?limit=1")).json()["items"][0]["id"] == str(inserted)


async def test_cursor_is_stable_when_seen_live_session_completes_between_pages(db):
    sids = [await add_session(db, minutes=i, status="live", ended=False) for i in range(5)]
    first = (await get("/api/sessions/history?status=live&limit=2")).json()
    seen = [x["id"] for x in first["items"]]
    async with db.acquire() as conn:  # an already-seen live session completes
        await conn.execute("UPDATE sessions SET status='complete',ended_at=now() WHERE id=$1", sids[4])
    cursor = first["next_cursor"]
    while cursor:
        page = (await get(f"/api/sessions/history?status=live&limit=2&cursor={cursor}")).json()
        seen += [x["id"] for x in page["items"]]
        cursor = page["next_cursor"]
    assert seen == [str(s) for s in reversed(sids)]
    assert len(seen) == len(set(seen))


async def test_history_empty_state_and_pages(db):
    assert (await get("/api/sessions/history")).json() == {"items": [], "limit": 50, "has_more": False, "next_cursor": None}
    assert (await get("/api/sessions/recent-results")).json()["items"] == []
    page = await get("/history")
    assert page.status_code == 200 and "SESSION HISTORY" in page.text and "__BUILD_LABEL__" not in page.text


async def test_unfinished_session_without_geometry_or_participants_is_listed(db):
    sid = await add_session(db, minutes=1)
    item = (await get("/api/sessions/history")).json()["items"][0]
    assert item["id"] == str(sid) and item["participant_count"] == 0
    assert item["winner"] is None and item["stintara_driver"] is None


@pytest.mark.parametrize("positions, expect_winner", [
    ([(1, 1), (2, 1)], True),          # unique P1 with FIN
    ([(1, 0), (2, 1)], False),         # P1 without finish semantics
    ([(1, 2), (2, 1)], False),         # DNF
    ([(1, 3), (2, 1)], False),         # DSQ
    ([(1, 1), (1, 1)], False),         # duplicate P1
    ([(2, 1), (3, 1)], False),         # no P1
])
async def test_winner_only_when_unambiguous(db, positions, expect_winner):
    sid = await add_session(db)
    for i, (position, finish) in enumerate(positions, 1):
        await add_participant(db, sid, i, position, finish, name=f"Synthetic {i}")
    item = (await get("/api/sessions/recent-results")).json()["items"][0]
    if expect_winner:
        assert item["winner"]["display_name"] == "Synthetic 1"
        assert item["winner"]["position"] == 1 and item["winner"]["finish_status"] == 1
    else:
        assert item["winner"] is None


async def test_no_winner_for_live_or_non_race_sessions(db):
    live = await add_session(db, minutes=1, status="live", ended=False)
    quali = await add_session(db, minutes=2, stype="Qualifying")
    for sid in (live, quali):
        await add_participant(db, sid, 1, 1, 1)
    items = (await get("/api/sessions/history")).json()["items"]
    assert len(items) == 2 and all(x["winner"] is None for x in items)


async def test_stintara_driver_only_when_exactly_one_known(db):
    one, two, none = [await add_session(db, minutes=m) for m in (1, 2, 3)]
    d1, d2 = await add_driver(db, "one"), await add_driver(db, "two")
    p = await add_participant(db, one, 1, 7, 1, name="Same Name", driver_id=d1)
    await add_participant(db, one, 2, 8, 1, name="Same Name")  # equal display name, no identity: ignored
    await add_participant(db, two, 1, 3, 1, driver_id=d1)
    await add_participant(db, two, 2, 4, 1, driver_id=d2)
    await add_participant(db, none, 1, 1, 1)
    by_id = {x["id"]: x for x in (await get("/api/sessions/history")).json()["items"]}
    assert by_id[str(one)]["stintara_driver"] == {
        "participant_id": str(p), "driver_slug": "one", "display_name": "One", "position": 7, "finish_status": 1}
    assert by_id[str(two)]["stintara_driver"] is None
    assert by_id[str(none)]["stintara_driver"] is None
    assert by_id[str(one)]["participant_count"] == 2


def test_serialize_uses_no_runtime_and_exposes_stable_keys():
    row = dict(id=uuid7(), simulator_provider="lmu", session_type="Race", track_name="T", status="complete",
               started_at=T0, ended_at=T0, participant_count=1, w_id=None, w_name=None, w_position=None,
               w_finish=None, w_n=None, k_id=None, k_slug=None, k_name=None, k_position=None,
               k_finish=None, k_n=None, race_id=None, prev_id=None, prev_type=None)
    assert set(history.serialize(row)) == {"id", "simulator_provider", "session_type", "track_name", "status",
                                           "started_at", "ended_at", "participant_count", "winner",
                                           "stintara_driver", "race_id", "previous_session"}


async def test_schema_indexes_are_idempotent(db):
    async with db.acquire() as conn:
        await conn.execute(main.SCHEMA_FILE.read_text())
        names = {r["indexname"] for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()")}
    assert {"ix_sessions_history", "ix_sessions_recent_races"} <= names
