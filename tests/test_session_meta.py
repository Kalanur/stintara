"""#20: SessionInfo limits are bound to a session only when bracketed, repeated and (for an end) green.

Synthetic data only. The Qualifying -> Race sequence mirrors the order and values observed in a real LMU
capture (end clock 630 s for the 10 minute qualifying, pre-race countdown 1.07 / 0 / 2.23 minutes, then
32.23 minutes at green for a 30 minute race), without any participant data.
"""
import pytest

from server.app import session_meta
from server.app.uuid7 import uuid7

TRACK = "Synthetic Circuit"


class Stream:
    """One agent stream as handle_message sees it: SessionInfo candidates, race control and resolved frames."""

    def __init__(self, provider="lmu", epoch=None):
        self.state = {"track_name": TRACK}
        self.provider, self.epoch, self.now = provider, epoch, 100.0
        self.accepted = []

    def phase(self, phase):
        self.state["race_control"], self.state["race_control_at"] = {"phase": phase}, self.now

    def info(self, minutes=None, laps=0, epoch="same", track=TRACK):
        payload = {"track_name": track, "session_minutes": minutes, "session_laps": laps}
        session_meta.observe(self.state, self.provider, payload, self.epoch if epoch == "same" else epoch, self.now)

    def frame(self, session_id, clock, kind="Race", changed=False, epoch="same", dt=0.1):
        """The scoring frame that resolved to `session_id` (None = resolution found no session)."""
        self.now += dt
        resolved = self.epoch if epoch == "same" else epoch
        accepted = None
        if session_id is not None:
            accepted = session_meta.confirm(
                self.state, provider=self.provider, resolved_session_id=session_id,
                scoring=dict(session_time=clock, session_type=kind), changed=changed, source_epoch=resolved, now=self.now)
        else:
            self.state.pop("meta_candidate", None)
        self.state["session_id"] = session_id
        self.accepted.append(accepted)
        return accepted

    def pair(self, session_id, clock, minutes=None, laps=0, **frame):
        self.info(minutes, laps)
        return self.frame(session_id, clock, **frame)


def bound_race(stream, race, minutes=32.2333333, clock=300.0):
    stream.phase("green")
    return [stream.pair(race, clock + i, minutes) for i in range(3)]


def test_single_claim_is_never_accepted_and_two_bracketed_claims_are():
    s, sid = Stream(), uuid7()
    s.phase("green")
    assert s.pair(sid, 300, 10.5, kind="Qualifying") is None  # cold start: no previous resolved session
    assert s.pair(sid, 301, 10.5, kind="Qualifying") is None  # first bracketed claim
    assert s.pair(sid, 302, 10.5, kind="Qualifying") == {"end_et": pytest.approx(630.0)}


def test_qualifying_to_race_never_hands_the_stale_end_to_the_race():
    """The observed fault: Race scoring arrives 0.8 s before the first Race SessionInfo; the old value was 10.5."""
    s, quali, race = Stream(), uuid7(), uuid7()
    s.phase("green")
    for i in range(3):
        s.pair(quali, 300 + i, 10.5, kind="Qualifying")
    assert s.accepted[-1] == {"end_et": pytest.approx(630.0)}
    s.info(10.5)  # stale Qualifying value, still current when the Race frame arrives
    assert s.frame(race, 0.0, changed=True) is None  # resolves to another session than the candidate
    assert s.pair(race, 2.8, 1.0666667) is None  # first claim for the race: nothing bound yet
    assert all(a is None or a.get("end_et") != pytest.approx(630.0) for a in s.accepted[3:])


def test_early_next_session_info_before_the_switch_is_dropped():
    s, a, b = Stream(), uuid7(), uuid7()
    s.phase("green")
    for i in range(3):
        s.pair(a, 300 + i, 82.6, kind="Race")
    s.info(5 / 60)  # SessionInfo of the next session arrives before its first scoring frame
    assert s.frame(b, 0.0, kind="Race", changed=False) is None
    assert s.state.get("meta_seen") is None  # a dropped candidate also forgets earlier repetitions


def test_delayed_session_info_is_bound_only_after_it_is_bracketed_twice():
    s, race = Stream(), uuid7()
    s.phase("green")
    s.frame(race, 135.0)  # scoring first, SessionInfo only afterwards
    assert s.pair(race, 135.2, 32.2333333) is None
    assert s.pair(race, 136.2, 32.2333333) == {"end_et": pytest.approx(1934.0)}


def test_pre_race_countdown_is_never_an_end_and_derives_the_30_minute_duration():
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    s.phase("pre_session")
    for clock, minutes in ((2.8, 1.0666667), (67.0, 0.0), (134.4, 2.2333333)):
        for _ in range(2):
            assert s.pair(race, clock, minutes) is None  # countdown values are not accepted, however often seen
    s.phase("green")
    assert s.pair(race, 135.6, 32.2333333) is None
    accepted = s.pair(race, 136.6, 32.2333333)
    assert accepted == {"end_et": pytest.approx(1934.0), "duration": pytest.approx(1800.0)}


def test_pre_race_countdown_derives_the_80_minute_duration_of_the_second_reference_race():
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    s.phase("formation")
    s.pair(race, 156.0, 2.6)
    s.phase("green")
    s.pair(race, 157.0, 82.6)
    accepted = s.pair(race, 158.0, 82.6)
    assert accepted == {"end_et": pytest.approx(4956.0), "duration": pytest.approx(4800.0)}


def test_duration_stays_unknown_without_a_consistent_pre_race_reference():
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    s.phase("pre_session")
    s.pair(race, 67.0, 0.0)  # last observed countdown does not coincide with the clock
    s.pair(race, 67.1, 2.5)  # 150 s end at clock 67: more than 5 s away
    s.phase("green")
    s.pair(race, 135.0, 32.2333333)
    accepted = s.pair(race, 136.0, 32.2333333)
    assert accepted == {"end_et": pytest.approx(1934.0)}  # the end is known, the duration is not guessed


def test_joining_a_running_race_knows_the_end_but_not_the_duration():
    s, race = Stream(), uuid7()
    assert all(a is None or "duration" not in a for a in bound_race(s, race))
    assert s.accepted[-1] == {"end_et": pytest.approx(1934.0)}


def test_practice_and_qualifying_never_derive_a_duration():
    s, quali = Stream(), uuid7()
    s.frame(quali, 0.0, kind="Qualifying")
    s.phase("pre_session")
    s.pair(quali, 30.0, 0.5, kind="Qualifying")
    s.phase("green")
    s.pair(quali, 31.0, 10.5, kind="Qualifying")
    accepted = s.pair(quali, 32.0, 10.5, kind="Qualifying")
    assert accepted == {"end_et": pytest.approx(630.0)}


@pytest.mark.parametrize("phase", [None, "pre_session", "formation", "session_over", "session_stopped", "unknown"])
def test_end_is_unknown_unless_the_phase_is_fresh_and_green(phase):
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    if phase:
        s.phase(phase)
    assert [s.pair(race, 300 + i, 32.2333333) for i in range(4)] == [None] * 4


def test_stale_race_control_is_not_a_green_phase():
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    s.phase("green")
    s.now += 60  # no race control for a minute
    assert [s.pair(race, 300 + i, 32.2333333) for i in range(4)] == [None] * 4


def test_full_course_yellow_still_counts_as_a_running_session():
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    s.phase("full_course_yellow")
    s.pair(race, 300, 32.2333333)
    assert s.pair(race, 301, 32.2333333) == {"end_et": pytest.approx(1934.0)}


def test_an_end_that_the_clock_has_already_passed_is_rejected():
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    s.phase("green")
    assert [s.pair(race, 700, 10.5) for _ in range(4)] == [None] * 4  # 630 s end at clock 700: stale Qualifying


def test_claim_changes_reset_the_repetition_count():
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    s.phase("green")
    assert s.pair(race, 300, 32.0) is None
    assert s.pair(race, 301, 40.0) is None  # a different claim: count restarts
    assert s.pair(race, 302, 40.0) == {"end_et": pytest.approx(2400.0)}


def test_candidate_expires_when_no_frame_follows_in_time():
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    s.phase("green")
    s.pair(race, 300, 32.0)
    s.info(32.0)
    assert s.frame(race, 301, dt=session_meta.BIND_MAX_AGE_SECONDS + 1) is None


def test_epoch_change_between_info_and_frame_drops_the_candidate():
    s, race = Stream(epoch=7), uuid7()
    s.frame(race, 0.0)
    s.phase("green")
    s.pair(race, 300, 32.0)
    s.info(32.0)
    assert s.frame(race, 301, epoch=8) is None  # Pitwall reconnected between the two messages
    assert s.pair(race, 302, 32.0) is None  # and the repetition count starts over


def test_agent_without_source_epoch_gets_the_same_guards_and_no_extra_trust():
    s, race = Stream(epoch=None), uuid7()
    s.frame(race, 0.0)
    s.phase("green")
    assert s.pair(race, 300, 32.2333333) is None
    assert s.pair(race, 301, 32.2333333) == {"end_et": pytest.approx(1934.0)}
    s.info(40.0)  # without an epoch a reconnect cannot be seen; the age bound still limits the evidence
    assert s.frame(race, 302, dt=session_meta.BIND_MAX_AGE_SECONDS + 1) is None


def test_reconnect_starts_cold_and_binds_again_only_after_two_pairs():
    s, race = Stream(), uuid7()
    bound_race(s, race)
    fresh = Stream()  # game_connected false/true resets the runtime state
    fresh.phase("green")
    assert [fresh.pair(race, 400 + i, 32.2333333) for i in range(3)] == [None, None, {"end_et": pytest.approx(1934.0)}]


def test_changed_frames_never_confirm():
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    s.phase("green")
    s.pair(race, 300, 32.0)
    assert s.pair(race, 5.0, 32.0, changed=True) is None  # clock rollback / session type change in this frame


def test_other_track_never_confirms():
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    s.phase("green")
    s.pair(race, 300, 32.0)
    s.info(32.0, track="Other Circuit")
    assert s.frame(race, 301) is None


def test_unresolved_frame_consumes_the_candidate():
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    s.phase("green")
    s.info(0.0833)
    assert s.frame(None, 0.0) is None and "meta_candidate" not in s.state


def test_other_providers_define_no_end_clock():
    s, race = Stream(provider="acc"), uuid7()
    s.frame(race, 0.0)
    s.phase("green")
    assert [s.pair(race, 300 + i, 32.0) for i in range(4)] == [None] * 4


def test_lap_limit_needs_confirmation_but_no_green_phase():
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    assert s.pair(race, 300, None, laps=20) is None
    assert s.pair(race, 301, None, laps=20) == {"laps": 20}


def test_info_with_time_end_and_lap_value_is_ambiguous_for_laps():
    s, race = Stream(), uuid7()
    s.frame(race, 0.0)
    s.phase("green")
    s.pair(race, 300, 32.0, laps=1)
    assert s.pair(race, 301, 32.0, laps=1) == {"end_et": pytest.approx(1920.0)}  # laps=1 is never taken


@pytest.mark.parametrize("laps", [0, -1, None, 2**31 - 1, True, 12.0])
def test_unusable_lap_values_are_never_a_limit(laps):
    assert session_meta.lap_limit({"session_laps": laps}) is None


@pytest.mark.parametrize("minutes", [0, -5, None, float("nan"), float("inf"), True, "30"])
def test_unusable_end_values_are_never_an_end(minutes):
    assert session_meta.provider_end_clock_seconds("lmu", {"session_minutes": minutes}) is None


def row(end=None, duration=None, laps=None):
    return dict(session_end_et_seconds=end, session_duration_seconds=duration, session_laps=laps)


def test_merge_is_write_once_per_dimension():
    assert session_meta.merge(row(), {"end_et": 1934.0, "duration": 1800.0}) == (1934.0, 1800.0, None)
    assert session_meta.merge(row(1934.0, 1800.0), {"end_et": 2400.0}) == (1934.0, 1800.0, None)  # never replaced
    assert session_meta.merge(row(1934.0), {"end_et": 1934.2, "duration": 1800.0}) == (1934.0, 1800.0, None)
    assert session_meta.merge(row(1934.0), {"end_et": 3000.0, "duration": 2866.0}) == (1934.0, None, None)


def test_merge_keeps_one_limit_dimension_per_session():
    assert session_meta.merge(row(laps=20), {"end_et": 5.0}) == (None, None, 20)
    assert session_meta.merge(row(end=1934.0), {"laps": 1}) == (1934.0, None, None)
    assert session_meta.merge(row(laps=0), {"laps": 20}) == (None, None, 20)
    assert session_meta.merge(row(laps=20), {"laps": 30}) == (None, None, 20)
