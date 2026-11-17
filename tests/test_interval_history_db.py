"""Ingest -> history -> API for lapped position intervals (#68). Synthetic frames only; needs the test database."""
import pytest

from server.app import main
from tests.conftest import make_agent

L = 5000.0


def frames(start, end, epoch_of=lambda k: "e1", pit=lambda k, i: False):
    """Car 7 leads on the lead lap; cars 8 (P2) and 9 (P3) are lapped by one with 8 being 100 m ahead of 9."""
    for k in range(start, end):
        t = round(k * .2, 3)
        vs = []
        for vid, pos, s, deficit in ((7, 1, 90000 + 70 * t, 0), (8, 2, 100 + 60 * t, 1), (9, 3, 60 * t, 1)):
            laps = int(s // L)
            vs.append(dict(id=vid, driver_name=f"Driver {vid}", position=pos, total_laps=laps, lap_dist=s - laps * L,
                           in_pits=pit(k, vid), time_behind_leader=0.0, laps_behind_leader=deficit))
        yield epoch_of(k), dict(session_type="Race", session_time=t, player_vehicle_id=9, vehicles=vs)


async def feed(agent, start, end, **kw):
    for epoch, payload in frames(start, end, **kw):
        await main.handle_message(agent, "lmu", "scoring", payload, source_epoch=epoch)


async def start(db, name="interval"):
    agent = await make_agent(db, name)
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Imola", "track_length": L})
    return agent


def own(data):
    return next(p for p in data["participants"] if p["local_vehicle_id"] == 9)


async def test_lapped_same_deficit_interval_through_ingest_and_both_apis(db):
    agent = await start(db)
    await feed(agent, 0, 400)
    sid = main.runtime[str(agent["id"])]["session_id"]
    data = await main.api_session_live(sid)
    p3 = next(p for p in data["participants"] if p["local_vehicle_id"] == 9)
    assert p3["interval_source"] == "history" and p3["interval_reason"] is None
    assert p3["interval"] == pytest.approx(100 / 60, abs=1e-6) and p3["gap_laps"] == 1
    assert "history_interval" not in p3
    # P2 is lapped but P1 is on the lead lap: different deficits stay "lapped" (phase 1 scope)
    p2 = next(p for p in data["participants"] if p["local_vehicle_id"] == 8)
    assert p2["interval"] is None and p2["interval_reason"] == "lapped" and p2["interval_source"] is None
    live = await main.api_driver_live(agent["slug"])
    assert live["focus"]["interval_source"] == "history"
    assert live["focus"]["interval"] == pytest.approx(100 / 60, abs=1e-6) and live["focus"]["interval_reason"] is None
    assert all("history_interval" not in p for p in live["participants"])


async def test_new_source_epoch_falls_back_to_lapped_then_recovers(db):
    agent = await start(db, "epoch")
    await feed(agent, 0, 300)
    sid = main.runtime[str(agent["id"])]["session_id"]
    await feed(agent, 300, 303, epoch_of=lambda k: "e2")
    p = own(await main.api_session_live(sid))
    assert p["interval"] is None and p["interval_reason"] == "lapped" and p["interval_source"] is None
    await feed(agent, 303, 420, epoch_of=lambda k: "e2")
    assert own(await main.api_session_live(sid))["interval_source"] == "history"


async def test_agent_without_source_epoch_never_gets_history_values(db):
    agent = await start(db, "noepoch")
    await feed(agent, 0, 400, epoch_of=lambda k: None)
    sid = main.runtime[str(agent["id"])]["session_id"]
    p = own(await main.api_session_live(sid))
    assert p["interval"] is None and p["interval_reason"] == "lapped"


async def test_stale_scoring_does_not_present_a_history_value(db):
    agent = await start(db, "stale")
    await feed(agent, 0, 400)
    state = main.runtime[str(agent["id"])]
    sid = state["session_id"]
    state["last_scoring_at"] -= 5          # still "fresh" for the 15 s spectator overlay, too old for history
    p = own(await main.api_session_live(sid))
    assert p["position"] == 3 and p["interval"] is None and p["interval_reason"] == "lapped"


async def test_pit_activity_blocks_the_value_for_sixty_seconds(db):
    agent = await start(db, "pit")
    await feed(agent, 0, 200, pit=lambda k, i: i == 8 and 100 <= k < 104)
    sid = main.runtime[str(agent["id"])]["session_id"]
    assert own(await main.api_session_live(sid))["interval"] is None        # pit sample 20 s ago
    await feed(agent, 200, 520, pit=lambda k, i: False)
    assert own(await main.api_session_live(sid))["interval_source"] == "history"


async def test_two_agents_keep_separate_histories(db):
    a, b = await start(db, "agent-a"), await start(db, "agent-b")
    await feed(a, 0, 400)
    sa, sb = main.runtime[str(a["id"])], main.runtime[str(b["id"])]
    assert "interval_history" not in sb
    await feed(b, 0, 10)
    assert sa["interval_history"] is not sb["interval_history"]
    assert sa["interval_history"].sample_count() > sb["interval_history"].sample_count()


async def test_session_change_drops_the_history(db):
    agent = await start(db, "change")
    await feed(agent, 0, 300)
    state = main.runtime[str(agent["id"])]
    first = state["interval_history"]
    payload = dict(next(frames(0, 1))[1], session_type="Qualifying")
    await main.handle_message(agent, "lmu", "scoring", payload, source_epoch="e1")
    assert state["interval_history"] is not first          # a new session starts a new history
    assert state["interval_history"].sample_count() == 3   # one sample for each of the three cars, nothing carried over
