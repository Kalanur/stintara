"""Ingest -> history -> API for lapped position intervals (#68). Synthetic frames only; needs the test database."""
import time

import pytest

from server.app import main
from server.app.uuid7 import uuid7
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


# ---- review round (#77) ---------------------------------------------------------------------------------------

def other_agent_state(session_id, *, pit_last=False, behind=0.0, seq_offset=0):
    """Runtime state of a second agent observing the same session, built through the real history ingest."""
    from server.app.interval_history import observe
    state = dict(session_id=session_id, scoring_epoch="eb", track_length_m=L, driver_slug="other-agent",
                 provider="lmu", game_connected=True)
    for k in range(0, 400):
        payload = next(frames(k, k + 1))[1]
        for v in payload["vehicles"]:
            if v["id"] == 9:
                t = payload["session_time"]
                s = 60 * t - behind
                v["total_laps"], v["lap_dist"] = int(s // L), s - int(s // L) * L
                v["in_pits"] = pit_last and k >= 395
        observe(state, payload, epoch="eb")
        state["scoring"] = payload
    state["last_scoring_at"] = time.monotonic()
    state["scoring_seq"] = main.next_sample_seq() + seq_offset
    return state


def reorder(first_key, second_key):
    items = dict(main.runtime)
    main.runtime.clear()
    for key in (first_key, second_key):
        main.runtime[key] = items[key]


@pytest.mark.parametrize("scenario", ["other_in_pits", "other_far_behind"])
@pytest.mark.parametrize("registration", ["agent_first", "agent_last"])
async def test_driver_live_uses_one_observation_whatever_the_registration_order(db, scenario, registration):
    agent = await start(db, "mix")
    await feed(agent, 0, 400)
    key_a = str(agent["id"])
    state_a = main.runtime[key_a]
    sid = state_a["session_id"]
    kwargs = dict(pit_last=True) if scenario == "other_in_pits" else dict(behind=400.0)
    older = other_agent_state(sid, **kwargs)
    older["last_scoring_at"] = state_a["last_scoring_at"] - 1.0           # the real agent is newer
    main.runtime["agent-b"] = older
    if registration == "agent_last":
        reorder(key_a, "agent-b")
    else:
        reorder("agent-b", key_a)
    live = await main.api_driver_live(agent["slug"])
    focus = live["focus"]
    # every field comes from the newer observation (the real agent): the measurement and a consistent TRACK status
    assert focus["interval_source"] == "history" and focus["interval"] == pytest.approx(100 / 60, abs=1e-6)
    assert focus["status"] == "TRACK"
    me = next(p for p in live["participants"] if p["local_vehicle_id"] == 9)
    assert me["in_pits"] is False and me["laps_behind_leader"] == 1
    # the same answer with the other registration order (only the age of the observation moves with time)
    first, second = list(main.runtime)
    reorder(second, first)
    again = await main.api_driver_live(agent["slug"])
    assert {**again["focus"], "live_age_s": None} == {**focus, "live_age_s": None}
    assert again["participants"] == live["participants"]


@pytest.mark.parametrize("registration", ["agent_first", "agent_last"])
async def test_driver_live_follows_the_newer_observation_of_another_agent(db, registration):
    agent = await start(db, "newer")
    await feed(agent, 0, 400)
    key_a = str(agent["id"])
    state_a = main.runtime[key_a]
    sid = state_a["session_id"]
    newer = other_agent_state(sid, pit_last=True)
    state_a["last_scoring_at"] -= 1.5                                       # the other agent is newer
    main.runtime["agent-b"] = newer
    reorder(*((key_a, "agent-b") if registration == "agent_first" else ("agent-b", key_a)))
    live = await main.api_driver_live(agent["slug"])
    me = next(p for p in live["participants"] if p["local_vehicle_id"] == 9)
    # everything, pit state included, now describes the other observation, and no number is shown with it
    assert me["in_pits"] is True and live["focus"]["status"] == "PIT"
    assert live["focus"]["interval"] is None and live["focus"]["interval_source"] is None


async def test_unbindable_observation_shows_no_history_number(db):
    # The newest observation does not contain the car ahead: the pair cannot be tied to it, so the number stays unknown.
    agent = await start(db, "unbound")
    await feed(agent, 0, 400)
    state = main.runtime[str(agent["id"])]
    sid = state["session_id"]
    state["scoring"] = dict(state["scoring"], vehicles=[v for v in state["scoring"]["vehicles"] if v["id"] != 8])
    live = await main.api_driver_live(agent["slug"])
    assert live["focus"]["interval"] is None and live["focus"]["interval_source"] is None
    assert (await main.api_session_live(sid)) is not None


def test_equal_receive_times_are_resolved_by_arrival_sequence_not_registration_order():
    from server.app.timing import newest_state
    sid = uuid7()
    first = dict(session_id=sid, last_scoring_at=99.0, scoring_seq=5, scoring={"v": 1}, name="first")
    second = dict(session_id=sid, last_scoring_at=99.0, scoring_seq=9, scoring={"v": 2}, name="second")
    assert newest_state(sid, True, [first, second], 100.0)["name"] == "second"
    assert newest_state(sid, True, [second, first], 100.0)["name"] == "second"
    assert newest_state(sid, True, [dict(first, last_scoring_at=99.5), second], 100.0)["name"] == "first"


async def test_pit_seen_without_distance_blocks_the_number_through_ingest(db):
    agent = await start(db, "pitnodist")
    await feed(agent, 0, 300)
    sid = main.runtime[str(agent["id"])]["session_id"]
    assert own(await main.api_session_live(sid))["interval_source"] == "history"
    _, payload = next(frames(300, 301))
    payload["vehicles"][2].update(in_pits=True, lap_dist=None)           # car 9: in the pits, no usable distance
    await main.handle_message(agent, "lmu", "scoring", payload, source_epoch="e1")
    await feed(agent, 301, 330)                                          # distance valid again, pit flag cleared
    p = own(await main.api_session_live(sid))
    assert p["interval"] is None and p["interval_reason"] == "lapped" and p["interval_source"] is None
    await feed(agent, 330, 700)                                          # more than 60 s of session clock later
    assert own(await main.api_session_live(sid))["interval_source"] == "history"


async def test_changed_repeat_of_one_clock_removes_the_number_through_ingest(db):
    agent = await start(db, "repeat")
    await feed(agent, 0, 400)
    sid = main.runtime[str(agent["id"])]["session_id"]
    assert own(await main.api_session_live(sid))["interval_source"] == "history"
    _, last = next(frames(399, 400))
    # the very same clock again, same positions and deficits, but car 9 is now reported in the pits
    again = dict(last, vehicles=[dict(v, in_pits=(v["id"] == 9)) for v in last["vehicles"]])
    await main.handle_message(agent, "lmu", "scoring", again, source_epoch="e1")
    p = own(await main.api_session_live(sid))
    assert p["in_pits"] is True and p["interval"] is None and p["interval_source"] is None
    live = await main.api_driver_live(agent["slug"])
    assert live["focus"]["interval"] is None and live["focus"]["status"] == "PIT"
    # an identical repeat of the original packet is simply ignored and does not resurrect anything
    await feed(agent, 400, 410)
    assert own(await main.api_session_live(sid))["interval"] is None       # quarantine after the observed pit flag
    await feed(agent, 410, 760)
    assert own(await main.api_session_live(sid))["interval_source"] == "history"


async def test_identical_repeat_keeps_the_number_through_ingest(db):
    agent = await start(db, "identical")
    await feed(agent, 0, 400)
    sid = main.runtime[str(agent["id"])]["session_id"]
    _, last = next(frames(399, 400))
    await main.handle_message(agent, "lmu", "scoring", dict(last), source_epoch="e1")
    p = own(await main.api_session_live(sid))
    assert p["interval_source"] == "history" and p["interval"] == pytest.approx(100 / 60, abs=1e-6)


# ---- second review round (#77) ----------------------------------------------------------------------------

@pytest.mark.parametrize("length", [6000.0, None])
async def test_track_length_change_at_a_repeated_clock_removes_the_number_through_ingest(db, length):
    agent = await start(db, "geometry")
    await feed(agent, 0, 400)
    sid = main.runtime[str(agent["id"])]["session_id"]
    assert own(await main.api_session_live(sid))["interval_source"] == "history"
    await main.handle_message(agent, "lmu", "session_info", {"track_name": "Imola", "track_length": length})
    _, last = next(frames(399, 400))
    await main.handle_message(agent, "lmu", "scoring", dict(last), source_epoch="e1")     # identical packet, new geometry
    p = own(await main.api_session_live(sid))
    assert p["interval"] is None and p["interval_reason"] == "lapped" and p["interval_source"] is None
    live = await main.api_driver_live(agent["slug"])
    assert live["focus"]["interval"] is None and live["focus"]["interval_source"] is None


async def test_pit_flag_in_a_duplicate_vehicle_row_blocks_the_number_through_ingest(db):
    agent = await start(db, "dupe")
    await feed(agent, 0, 400)
    sid = main.runtime[str(agent["id"])]["session_id"]
    _, last = next(frames(399, 400))
    twin = dict(last["vehicles"][2])
    await main.handle_message(agent, "lmu", "scoring", dict(last, vehicles=last["vehicles"] + [dict(twin)]), source_epoch="e1")
    await main.handle_message(agent, "lmu", "scoring",
                              dict(last, vehicles=last["vehicles"] + [dict(twin, in_pits=True)]), source_epoch="e1")
    await feed(agent, 400, 410)                                       # unique, valid, on-track again
    assert own(await main.api_session_live(sid))["interval"] is None
    await feed(agent, 410, 760)                                       # 60 s of session clock after the observed flag
    assert own(await main.api_session_live(sid))["interval_source"] == "history"
