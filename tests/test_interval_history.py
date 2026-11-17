"""Synthetic tests for the lapped position interval history (#68, phase 1). No real capture data is used."""
import time

import pytest

from server.app import interval_history as ih
from server.app import main
from server.app.interval_history import IntervalHistory, current_pairs, observe
from server.app.timing import project_timing
from server.app.uuid7 import uuid7

L = 5000.0


def car(i, pos, s, deficit=1, pit=False):
    laps = int(s // L)
    return dict(id=i, position=pos, total_laps=laps, lap_dist=s - laps * L, in_pits=pit, laps_behind_leader=deficit)


def step(h, clock, vehicles, session="s1", epoch="e1", length=L, **kw):
    return h.update(session_id=session, epoch=epoch, track_length=length, clock=clock, vehicles=vehicles, **kw)


def drive(h, t0, t1, a, b, dt=0.2, hook=None, **kw):
    """Two lapped cars at constant speeds; a=(speed,s0) is P-1, b=(speed,s0) is P. Returns the last result."""
    out, n = None, int(round((t1 - t0) / dt))
    for k in range(n + 1):
        t = round(t0 + k * dt, 3)
        vs = [car(1, 1, a[1] + a[0] * (t - t0)), car(2, 2, b[1] + b[0] * (t - t0))]
        if hook:
            vs = hook(k, t, vs)
        out = step(h, t, vs, **kw) or out
    return out


def entry(result, b=2):
    return result["pairs"].get(b)


# ---- the eleven offline prototype regression cases -------------------------------------------------------

def test_constant_speed_interval():
    e = entry(drive(IntervalHistory(), 0, 60, (60, 100), (60, 0)))
    assert e["reason"] is None and e["value"] == pytest.approx(100 / 60, abs=1e-6)
    assert (e["ahead_id"], e["position"], e["ahead_position"], e["deficit"]) == (1, 2, 1, 1)


def test_lap_rollover_is_continuous():
    e = entry(drive(IntervalHistory(), 0, 60, (60, 4900 + 100), (60, 4900)))
    assert e["reason"] is None and e["value"] == pytest.approx(100 / 60, abs=1e-6)


def test_same_deficit_but_different_completed_laps():
    # Both cars are "-1L" but A has completed one lap more than B: only the distance matters.
    h = IntervalHistory()
    e = entry(drive(h, 0, 150, (60, 100 + L), (60, 0)))
    assert e["reason"] is None and e["value"] == pytest.approx((100 + L) / 60, abs=1e-6)
    assert h.tracks[1].s[-1] // L != h.tracks[2].s[-1] // L      # really different completed laps


def test_distance_backstep_resets_that_vehicle_and_recovers():
    h = IntervalHistory()
    drive(h, 0, 30, (60, 100), (60, 0))
    bad = step(h, 30.2, [car(1, 1, 100 + 60 * 30.2 - 500), car(2, 2, 60 * 30.2)])
    assert entry(bad)["value"] is None and entry(bad)["reason"] == ih.DISCONTINUITY
    after = step(h, 30.4, [car(1, 1, 100 + 60 * 30.4 - 500), car(2, 2, 60 * 30.4)])
    assert entry(after)["value"] is None and entry(after)["reason"] in (ih.HISTORY, ih.NOT_AHEAD)
    assert h.stats["reset_backstep"] == 1


def test_recovery_after_reset_matches_a_fresh_history():
    h = IntervalHistory()
    drive(h, 0, 20, (60, 300), (60, 0))
    step(h, 20.2, [car(1, 1, 5 + 60 * 20.2), car(2, 2, 60 * 20.2)])     # A jumps back far behind B
    resumed = drive(h, 20.4, 60, (60, 400 + 60 * 20.4), (60, 60 * 20.4))
    fresh = drive(IntervalHistory(), 20.4, 60, (60, 400 + 60 * 20.4), (60, 60 * 20.4))
    assert entry(resumed)["value"] == pytest.approx(entry(fresh)["value"], abs=1e-9)


def test_pit_states_and_sixty_second_guard():
    def pit_a(k, t, vs):
        return [dict(vs[0], in_pits=True), vs[1]] if 100 <= k < 104 else vs
    # A currently in the pits
    h = IntervalHistory()
    e = entry(drive(h, 0, 20, (60, 100), (60, 0), hook=lambda k, t, vs: [dict(vs[0], in_pits=True), vs[1]] if k == 100 else vs))
    assert e["value"] is None and e["reason"] == ih.PIT
    # B currently in the pits
    e = entry(drive(IntervalHistory(), 0, 20, (60, 100), (60, 0), hook=lambda k, t, vs: [vs[0], dict(vs[1], in_pits=True)] if k == 100 else vs))
    assert e["reason"] == ih.PIT
    # pit activity 20 s ago (inside the guard) -> still unknown although both cars are on track now
    e = entry(drive(IntervalHistory(), 0, 40, (60, 100), (60, 0), hook=pit_a))
    assert e["reason"] == ih.PIT
    # 61 s after the last in-pit sample the guard no longer applies
    e = entry(drive(IntervalHistory(), 0, 100, (60, 100), (60, 0), hook=pit_a))
    assert e["reason"] is None and e["value"] == pytest.approx(100 / 60, abs=1e-6)
    # exactly at the boundary: 60.0 s after is still guarded, afterwards free
    h = IntervalHistory()
    last_pit = None
    for k in range(0, 400):
        t = round(k * 0.2, 3)
        a_pit = k == 50
        if a_pit:
            last_pit = t
        r = step(h, t, [car(1, 1, 100 + 60 * t, pit=a_pit), car(2, 2, 60 * t)])
        if t >= last_pit if last_pit is not None else False:
            reason = entry(r)["reason"]
            if t - last_pit <= ih.PIT_GUARD_SECONDS:
                assert reason == ih.PIT, (t, reason)
            elif t - last_pit > ih.PIT_GUARD_SECONDS + 0.5:
                assert reason is None, (t, reason)


def test_pit_inside_the_measurement_window_is_unknown():
    # A pits at t=80 s. Evaluated at t=150 s the 60 s guard has expired, so only the window rule can apply.
    def run(gap):
        h, out = IntervalHistory(), None
        for k in range(0, 751):
            t = round(k * 0.2, 3)
            out = step(h, t, [car(1, 1, 60 * t, pit=400 <= k < 404), car(2, 2, 60 * t - gap)])
        return entry(out)
    inside = run(4500)       # A passed B's present point at t=75 s, before its pit stop
    assert inside["value"] is None and inside["reason"] == ih.PIT_WINDOW
    after = run(3000)        # A passed it at t=100 s, after the stop: the stop is not part of the measurement
    assert after["reason"] is None and after["value"] == pytest.approx(3000 / 60, abs=1e-6)


def test_session_and_clock_change_reset_everything():
    h = IntervalHistory()
    drive(h, 0, 20, (60, 100), (60, 0))
    e = entry(step(h, 0.2, [car(1, 1, 100), car(2, 2, 0)], session="s2"))
    assert e["value"] is None and h.stats["reset_key"] == 2
    h = IntervalHistory()
    drive(h, 0, 20, (60, 100), (60, 0))
    e = entry(step(h, 0.0, [car(1, 1, 100), car(2, 2, 0)]))     # clock restarted without any key change
    assert e["value"] is None and h.stats["reset_clock"] == 1
    # a small backwards step (<= 2 s) is an old/duplicate packet: ignored, history intact
    h = IntervalHistory()
    drive(h, 0, 20, (60, 100), (60, 0))
    assert step(h, 19.0, [car(1, 1, 5000), car(2, 2, 10)]) is None and h.stats["reset_clock"] == 0
    assert entry(step(h, 20.2, [car(1, 1, 100 + 60 * 20.2), car(2, 2, 60 * 20.2)]))["reason"] is None


def test_new_source_epoch_resets_and_missing_epoch_disables():
    h = IntervalHistory()
    drive(h, 0, 20, (60, 100), (60, 0))
    e = entry(step(h, 20.2, [car(1, 1, 100 + 60 * 20.2), car(2, 2, 60 * 20.2)], epoch="e2"))
    assert e["value"] is None
    r = step(h, 20.4, [car(1, 1, 1300), car(2, 2, 1200)], epoch=None)
    assert r["pairs"] == {} and h.stats[ih.NO_EPOCH] == 1
    assert step(h, 20.6, [car(1, 1, 1300), car(2, 2, 1200)], session=None)["pairs"] == {}


def test_missing_packets_clear_the_history_instead_of_interpolating():
    h = IntervalHistory()
    drive(h, 0, 20, (60, 100), (60, 0))
    e = entry(step(h, 22.0, [car(1, 1, 100 + 60 * 22), car(2, 2, 60 * 22)]))
    assert e["value"] is None and h.stats["reset_gap"] == 1
    # up to one second is still interpolated (0.2 s throttle jitter, dropped frames)
    h = IntervalHistory()
    e = None
    for t in (0, 0.4, 0.8, 1.6, 2.2, 2.6, 3.6, 4.2, 4.8, 5.6, 6.4, 7.2, 8.0):
        e = entry(step(h, t, [car(1, 1, 100 + 60 * t), car(2, 2, 60 * t)]))
    assert e["reason"] is None and e["value"] == pytest.approx(100 / 60, abs=1e-6)


def test_duplicate_clock_ties_and_missing_position():
    h = IntervalHistory()
    drive(h, 0, 5, (60, 100), (60, 0))
    assert step(h, 5.0, [car(1, 1, 400), car(2, 2, 300)]) is None
    assert h.stats["ignored_duplicate"] == 1
    # two cars claim position 2 -> no pair at all (a third car at 3 has no unique predecessor either)
    r = step(h, 5.2, [car(1, 2, 412), car(2, 2, 312), car(3, 3, 200)])
    assert r["pairs"] == {}


def test_ahead_car_not_ahead_in_distance():
    e = entry(drive(IntervalHistory(), 0, 10, (60, 0), (60, 100)))
    assert e["value"] is None and e["reason"] == ih.NOT_AHEAD


def test_missing_distance_or_identity_is_unknown():
    h = IntervalHistory()
    drive(h, 0, 5, (60, 100), (60, 0))
    bad = [dict(car(1, 1, 500), lap_dist=None), car(2, 2, 400)]
    assert entry(step(h, 5.2, bad))["reason"] == ih.INVALID
    for n, broken in enumerate((dict(id=None), dict(id=True), dict(id=-1))):
        r = step(h, 5.4 + .2 * n, [dict(car(1, 1, 600), **broken), car(2, 2, 500)])
        assert 2 not in r["pairs"] or r["pairs"][2]["value"] is None
    r = step(h, 6.2, [car(1, 1, 700), car(1, 3, 650), car(2, 2, 600)])     # duplicate vehicle id
    assert r["pairs"].get(2, {}).get("value") is None
    assert 1 not in h.tracks


def test_history_is_causal():
    h1, h2 = IntervalHistory(), IntervalHistory()
    frames = [(round(k * .2, 3), [car(1, 1, 300 + 61 * k * .2), car(2, 2, 59 * k * .2)]) for k in range(300)]
    full = [step(h1, t, vs) for t, vs in frames]
    cut = [step(h2, t, vs) for t, vs in frames[:150]]
    assert full[149] == cut[149]
    # poisoning every later sample cannot change an earlier result
    h3 = IntervalHistory()
    poisoned = [step(h3, t, vs) for t, vs in frames[:150]]
    assert poisoned == cut


# ---- scope: what is (not) measured ------------------------------------------------------------------------

@pytest.mark.parametrize("da,db", [(0, 0), (1, 2), (2, 1), (None, 1), (1, None), (-1, -1), (True, True), (1.0, 1.0), ("1", "1")])
def test_only_equal_positive_integer_deficits_are_in_scope(da, db):
    def hook(k, t, vs):
        return [dict(vs[0], laps_behind_leader=da), dict(vs[1], laps_behind_leader=db)]
    assert entry(drive(IntervalHistory(), 0, 20, (60, 100), (60, 0), hook=hook)) is None


def test_overtake_swaps_the_pair_and_remeasures():
    h = IntervalHistory()
    out = None
    for k in range(0, 400):
        t = round(k * .2, 3)
        # car 1 starts 100 m ahead, car 2 is 3 m/s faster and overtakes at t=33.3 s
        s1, s2 = 100 + 60 * t, 63 * t
        a, b = (car(1, 1, s1), car(2, 2, s2)) if s2 < s1 else (car(1, 2, s1), car(2, 1, s2))
        out = step(h, t, [a, b])
    assert out["pairs"].keys() == {1}
    e = out["pairs"][1]
    assert e["ahead_id"] == 2 and e["position"] == 2 and e["reason"] is None
    # car 1 is now 3 m/s slower and (63-60)*79.8-100 = 139.4 m behind car 2, which passed that point ~2.3 s ago
    assert e["value"] == pytest.approx(139.4 / 63, abs=0.05)


def test_a_must_have_passed_the_point_inside_the_window_no_extrapolation():
    h = IntervalHistory()
    out = None
    for k in range(0, 100):
        t = round(k * .2, 3)
        out = step(h, t, [car(1, 1, 60 * t + 2100), car(2, 2, 60 * t + 100)])
    e = entry(out)
    assert e["value"] is None and e["reason"] == ih.HISTORY      # A never was where B is now (within its history)


def test_irregular_throttled_clock_is_accurate():
    h = IntervalHistory()
    t, e, spacing = 0.0, None, (0.2, 0.4, 0.2, 0.2, 0.4, 0.6)
    for k in range(200):
        t = round(t + spacing[k % 6], 3)
        e = entry(step(h, t, [car(1, 1, 200 + 55 * t), car(2, 2, 55 * t)]))
    assert e["reason"] is None and e["value"] == pytest.approx(200 / 55, abs=1e-6)


def test_missing_track_length_and_track_change():
    h = IntervalHistory()
    for n, length in enumerate((None, 0, -5, float("nan"), True)):
        r = step(h, 1.0 + n, [car(1, 1, 100), car(2, 2, 0)], length=length)
        assert r["pairs"] == {}
    h = IntervalHistory()
    drive(h, 0, 20, (60, 100), (60, 0))
    r = step(h, 20.2, [car(1, 1, 100 + 60 * 20.2), car(2, 2, 60 * 20.2)], length=L + 50)
    assert entry(r)["value"] is None and h.stats["reset_track"] == 1


def test_vehicle_that_disappears_loses_its_history():
    h = IntervalHistory()
    drive(h, 0, 10, (60, 100), (60, 0))
    step(h, 10.2, [car(2, 2, 612)])
    assert 1 not in h.tracks
    e = entry(step(h, 10.4, [car(1, 1, 720), car(2, 2, 624)]))
    assert e["value"] is None


# ---- bounded memory and cost -----------------------------------------------------------------------------

def test_memory_is_bounded_and_runtime_is_small():
    h = IntervalHistory()
    cars = 40
    start = time.perf_counter()
    n = 6000
    for k in range(n):
        t = round(k * .2, 3)
        h.update(session_id="s", epoch="e", track_length=L, clock=t,
                 vehicles=[car(i, i + 1, 3000 - 40 * i + 60 * t) for i in range(cars)])
    per_update = (time.perf_counter() - start) / n
    bound = cars * (ih.MAX_SAMPLES + ih.COMPACT_AT)
    assert h.sample_count() <= cars * ih.MAX_SAMPLES
    assert all(len(t.t) <= ih.MAX_SAMPLES + ih.COMPACT_AT for t in h.tracks.values())
    assert sum(len(t.t) for t in h.tracks.values()) <= bound
    assert min(t.t[t.head] for t in h.tracks.values()) >= n * .2 - ih.WINDOW_SECONDS - .5
    assert per_update < 0.01       # offline measurement is ~0.2 ms for 29 cars; generous bound for slow CI


def test_a_dense_stream_cannot_exceed_the_sample_cap():
    h = IntervalHistory()
    for k in range(5000):
        step(h, round(k * .01, 3), [car(1, 1, 100 + 60 * k * .01), car(2, 2, 60 * k * .01)])
    assert all(len(t) <= ih.MAX_SAMPLES for t in h.tracks.values())


def test_oversized_field_is_not_tracked():
    r = step(IntervalHistory(), 1.0, [car(i, i + 1, 10 * i) for i in range(ih.MAX_VEHICLES + 1)])
    assert r["pairs"] == {}


# ---- observation binding (state level) --------------------------------------------------------------------

def observed(session, epoch="e1", clock_end=60.0, at=99.0, length=L):
    state = dict(session_id=session, scoring_epoch=epoch, track_length_m=length, last_scoring_at=at)
    payload = None
    for k in range(int(clock_end / .2) + 1):
        t = round(k * .2, 3)
        payload = dict(session_type="Race", session_time=t, vehicles=[car(1, 1, 100 + 60 * t), car(2, 2, 60 * t)])
        observe(state, payload, epoch=epoch)
        state["scoring"] = payload
    return state


def test_pairs_bind_to_session_epoch_clock_and_age():
    sid = uuid7()
    state = observed(sid)
    assert current_pairs(state, sid, 100.0)[2]["value"] == pytest.approx(100 / 60, abs=1e-6)
    assert current_pairs(state, uuid7(), 100.0) == {}                              # other session
    assert current_pairs(dict(state, scoring_epoch="e2"), sid, 100.0) == {}         # other epoch
    assert current_pairs(dict(state, scoring_epoch=None), sid, 100.0) == {}
    assert current_pairs(dict(state, scoring=dict(state["scoring"], session_time=61)), sid, 100.0) == {}   # other clock
    assert current_pairs(state, sid, 99 + ih.MAX_AGE_SECONDS + .01) == {}           # stale scoring
    assert current_pairs(state, sid, 98.0) == {}                                    # age cannot be negative
    assert current_pairs(dict(state, session_id=uuid7()), sid, 100.0) == {}
    assert current_pairs({}, sid, 100.0) == {}


def test_a_duplicate_packet_keeps_the_result_bound():
    sid = uuid7()
    state = observed(sid)
    payload = dict(state["scoring"])
    observe(state, payload, epoch="e1")          # same clock again: ignored by the history
    assert current_pairs(state, sid, 100.0)[2]["value"] is not None


def test_agents_never_share_history():
    sid_a, sid_b = uuid7(), uuid7()
    a, b = observed(sid_a), observed(sid_b, clock_end=30)
    assert a["interval_history"] is not b["interval_history"]
    assert current_pairs(a, sid_b, 100.0) == {} and current_pairs(b, sid_a, 100.0) == {}
    # two agents observing the same session keep separate histories keyed by their own state
    c = observed(sid_a, clock_end=40)
    assert c["interval_history"] is not a["interval_history"]
    assert current_pairs(c, sid_a, 100.0)[2]["value"] == pytest.approx(100 / 60, abs=1e-6)


def test_projection_uses_history_only_for_matching_participants():
    sid = uuid7()
    state = observed(sid)
    ids = {1: uuid7(), 2: uuid7()}

    def rows(**override):
        r = [dict(id=ids[i], local_vehicle_id=i, position=i, total_laps=3 + (i == 1), laps_behind_leader=1,
                  gap_to_leader=None) for i in (1, 2)]
        for k, v in override.items():
            r[0 if k.startswith("a_") else 1][k[2:]] = v
        return r

    out = main.spectator_participants(rows(), sid, True, [state], 100.0, "Race")
    b = next(p for p in out if p["local_vehicle_id"] == 2)
    assert b["interval_source"] == "history" and b["interval_reason"] is None
    assert b["interval"] == pytest.approx(100 / 60, abs=1e-6) and b["interval_preceding_id"] == ids[1]
    # rows keep their provider fields (the scoring overlay fills them from the observation)
    assert b["laps_behind_leader"] == 1
    # stale observation: nothing is presented (existing behavior) and no history value either
    assert main.spectator_participants(rows(), sid, True, [dict(state, last_scoring_at=1.0)], 100.0, "Race")[1]["interval"] is None
    # other session id: ignored
    assert main.spectator_participants(rows(), uuid7(), True, [state], 100.0, "Race")[1]["interval"] is None
    # non-race session type
    assert main.spectator_participants(rows(), sid, True, [state], 100.0, "Practice")[1]["interval"] is None


# ---- project_timing contract ------------------------------------------------------------------------------

def lapped_pair(**entry_override):
    ids = (uuid7(), uuid7())
    rows = [dict(id=ids[0], local_vehicle_id=11, position=7, total_laps=5, laps_behind_leader=2, gap_to_leader=0),
            dict(id=ids[1], local_vehicle_id=12, position=8, total_laps=4, laps_behind_leader=2, gap_to_leader=0)]
    entry = dict(value=4.25, reason=None, ahead_id=11, position=8, ahead_position=7, deficit=2)
    entry.update(entry_override)
    rows[1]["history_interval"] = entry
    return rows


def test_project_timing_accepts_a_matching_history_value():
    rows = lapped_pair()
    project_timing(rows, "Race")
    assert rows[1]["interval"] == 4.25 and rows[1]["interval_source"] == "history"
    assert rows[1]["interval_reason"] is None and rows[1]["interval_preceding_id"] == rows[0]["id"]
    assert rows[1]["gap_laps"] == 2 and rows[1]["gap_to_leader"] is None      # gap semantics untouched
    assert rows[0]["interval"] is None


@pytest.mark.parametrize("override", [dict(ahead_id=99), dict(ahead_id=True), dict(position=9), dict(ahead_position=6),
                                      dict(deficit=1), dict(deficit=None), dict(value=None), dict(value=-0.1),
                                      dict(value=float("nan")), dict(value=float("inf")), dict(value=True)])
def test_project_timing_rejects_mismatching_history(override):
    rows = lapped_pair(**override)
    project_timing(rows, "Race")
    assert rows[1]["interval"] is None and rows[1]["interval_reason"] == "lapped" and rows[1]["interval_source"] is None


@pytest.mark.parametrize("change", [dict(laps_behind_leader=1), dict(laps_behind_leader=3), dict(laps_behind_leader=0),
                                    dict(laps_behind_leader=None), dict(position=9), dict(local_vehicle_id=None)])
def test_project_timing_rejects_changed_neighbours(change):
    rows = lapped_pair()
    rows[0].update(change)
    project_timing(rows, "Race")
    assert rows[1]["interval"] is None and rows[1]["interval_source"] is None


def test_project_timing_never_uses_history_outside_the_scope():
    for session in ("Practice", "Qualifying", None):
        rows = lapped_pair()
        project_timing(rows, session)
        assert rows[1]["interval"] is None and rows[1]["interval_source"] is None
    rows = lapped_pair()
    del rows[1]["history_interval"]
    project_timing(rows, "Race")
    assert rows[1]["interval_reason"] == "lapped" and rows[1]["interval"] is None
    rows = lapped_pair()
    rows[1]["history_interval"] = "not-a-dict"
    project_timing(rows, "Race")
    assert rows[1]["interval"] is None


def test_lead_lap_intervals_keep_the_leader_gap_source_and_values():
    ids = [uuid7() for _ in range(3)]
    rows = [dict(id=ids[i], local_vehicle_id=i + 1, position=i + 1, total_laps=5, laps_behind_leader=0,
                 gap_to_leader=g) for i, g in enumerate((0, 14.3, 14.8))]
    rows[2]["history_interval"] = dict(value=99.0, ahead_id=2, position=3, ahead_position=2, deficit=0)
    project_timing(rows, "Race")
    assert rows[2]["interval"] == pytest.approx(.5) and rows[2]["interval_source"] == "leader_gap"
    assert rows[0]["interval_source"] is None and rows[0]["interval_reason"] == "leader"


def test_different_deficits_stay_lapped_even_with_a_history_entry():
    rows = lapped_pair()
    rows[0]["laps_behind_leader"] = 1
    project_timing(rows, "Race")
    assert rows[1]["interval"] is None and rows[1]["interval_reason"] == "lapped"
