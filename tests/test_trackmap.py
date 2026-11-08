"""Centerline learning, closure plausibility and sector attribution on wholly synthetic geometry."""
import math

import pytest

from server.app.trackmap import (
    MAX_POINTS,
    CenterlineLearner,
    SectorTracker,
    TrackModel,
    TrackStore,
    attach_track,
    normalize_lap_distance,
    public_geometry,
    track_key,
)
from tests.track_fixtures import LENGTH, circuit, lap_samples


def learn(samples, model=None):
    model = model or TrackModel(LENGTH)
    learner = CenterlineLearner(model)
    closed = [learner.add(*sample) for sample in samples]
    return model, learner, closed


def test_continuous_lap_from_the_line_produces_a_closed_bounded_centerline():
    model, learner, closed = learn(lap_samples(start=2.0, end=LENGTH + 40))
    assert model.complete and sum(closed) == 1
    assert 20 <= len(model.points) <= MAX_POINTS
    gaps = [b - a for a, b in zip(model.distances, model.distances[1:])]
    assert min(gaps) >= 8 - 1e-6 and max(gaps) <= 60
    geometry = public_geometry(model, learner)
    assert geometry["closed"] and geometry["state"] == "complete" and geometry["progress"] is None


def test_starting_mid_lap_waits_for_the_line_and_never_learns_a_partial_start():
    model, learner, _ = learn(lap_samples(start=1200.0, end=LENGTH - 10))
    assert not model.complete and not learner.active and learner.progress() is None
    geometry = public_geometry(model, learner)
    assert geometry["points"] == [] and geometry["progress"] is None and not geometry["closed"]


def test_partial_lap_reports_real_progress_and_never_carries_a_closing_edge():
    model, learner, _ = learn(lap_samples(start=2.0, end=1500.0))
    geometry = public_geometry(model, learner)
    assert geometry["state"] == "learning" and not geometry["closed"]
    assert geometry["progress"] == pytest.approx(0.5, abs=0.03)
    assert geometry["points"][0] != geometry["points"][-1]


def test_lap_distance_wrap_alone_does_not_close_the_map():
    # The car is unobserved for the last 700 m, then lap distance wraps at the line.
    samples = lap_samples(start=2.0, end=LENGTH + 40, skip=lambda d: LENGTH - 700 < d < LENGTH)
    model, learner, closed = learn(samples)
    assert not model.complete and not any(closed)
    assert not public_geometry(model, learner)["closed"]


@pytest.mark.parametrize("missing", [100.0, 110.0])
def test_unobserved_stretch_below_the_step_limit_is_still_not_closed_by_the_wrap(missing):
    """Only the closure plausibility check can stop this: every single step is individually acceptable."""
    samples = lap_samples(start=2.0, end=LENGTH + 40, skip=lambda d: LENGTH - missing < d < LENGTH - 2)
    model, learner, closed = learn(samples)
    assert not model.complete and not any(closed) and learner.rejected == "closure_not_plausible"


def test_regression_no_artificial_sector3_to_start_finish_chord():
    """Last part of S3 unobserved: positions near the end are far from the start, yet lap_dist wraps."""
    samples = lap_samples(start=2.0, end=LENGTH - 900)
    x, z = circuit(LENGTH - 900)
    samples += [(1.0, x, z, False), (13.0, x + 12, z, False)]
    model, learner, closed = learn(samples)
    assert not model.complete and not any(closed)
    assert learner.rejected in {"backwards", "gap", "teleport", "closure_not_plausible"}
    assert public_geometry(model, learner)["closed"] is False


def test_teleport_discards_the_attempt_and_draws_no_line_across_the_track():
    samples = lap_samples(start=2.0, end=900.0)
    ld, x, z, _ = samples[-1]
    samples.append((ld + 12, x + 900, z + 400, False))  # plausible lap-distance step, impossible position
    model, learner, _ = learn(samples)
    assert not model.complete and learner.rejected == "teleport" and not learner.active


def test_pit_samples_discard_the_attempt_and_the_next_clean_lap_learns_the_track():
    samples = lap_samples(start=2.0, end=LENGTH + 40, pits=lambda d: 1000 < d < 1100)
    model, learner, closed = learn(samples)
    assert not model.complete and not any(closed)
    model, learner, closed = learn(samples + lap_samples(start=2.0, end=LENGTH + 40), model)
    assert model.complete and sum(closed) == 1


@pytest.mark.parametrize("bad", [None, math.nan, math.inf, True, "1"])
def test_invalid_positions_are_never_learned(bad):
    _, learner, _ = learn(lap_samples(start=2.0, end=600.0))
    assert learner.active
    learner.add(612.0, bad, 0.0, False)
    assert not learner.active and learner.rejected == "invalid_sample"


@pytest.mark.parametrize("value,expected", [(-40.0, LENGTH - 40), (LENGTH + 5, 5), (10.0, 10.0),
                                            (-5000.0, None), (LENGTH + 5000, None), (math.nan, None),
                                            (None, None), (True, None)])
def test_lap_distance_normalization_accepts_only_small_line_overshoot(value, expected):
    result = normalize_lap_distance(value, LENGTH)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_same_track_geometry_is_reused_across_sessions_and_other_tracks_never_share_it():
    store = TrackStore()
    first, second = {}, {}
    attach_track(first, store, "lmu", "Synthetic Circuit", LENGTH)
    for sample in lap_samples(start=2.0, end=LENGTH + 40):
        first["track_learner"].add(*sample)
    assert first["track_model"].complete
    attach_track(second, store, "lmu", "Synthetic Circuit", LENGTH + 0.4)  # Practice -> Qualifying -> Race
    assert second["track_model"] is first["track_model"] and second["track_model"].complete
    attach_track(second, store, "lmu", "Other Circuit", LENGTH)
    assert second["track_model"] is not first["track_model"] and not second["track_model"].complete
    attach_track(second, store, "lmu", "Synthetic Circuit", 4100.0)  # same name, different length
    assert not second["track_model"].complete
    assert track_key("lmu", " ", LENGTH) is None and track_key("lmu", "x", None) is None


def test_store_is_bounded():
    store = TrackStore(limit=3)
    for index in range(10):
        store.get(("lmu", f"track {index}", 1000 + index), 1000)
    assert len(store.models) == 3


def observe(tracker, laps, b1=1000.0, b2=2000.0, drift=0.0):
    for lap in range(laps):
        for sector, ld in ((1, 5.0), (2, b1 + drift * lap), (3, b2 + drift * lap)):
            tracker.observe(sector, ld, False)


def test_sector_boundaries_come_only_from_observed_forward_transitions():
    model = TrackModel(LENGTH)
    tracker = SectorTracker(model)
    observe(tracker, 1)
    assert model.sector_boundaries() is None  # a single observation: unknown, nothing invented
    observe(tracker, 1)
    assert model.sector_boundaries() == [1000.0, 2000.0]
    model.points, model.distances = [[0, 0], [1, 1], [2, 2]], [500.0, 1500.0, 2500.0]
    assert model.point_sectors() == [1, 2, 3]
    assert public_geometry(model)["sector_boundaries_m"] == [1000.0, 2000.0]


def test_conflicting_or_misordered_sector_boundaries_degrade_to_unknown():
    model = TrackModel(LENGTH)
    observe(SectorTracker(model), 4, drift=60.0)  # drifting by 60 m per lap: contradictory
    assert model.sector_boundaries() is None
    swapped = TrackModel(LENGTH)
    observe(SectorTracker(swapped), 4, b1=2000.0, b2=1000.0)
    assert swapped.sector_boundaries() is None


def test_a_single_outlier_does_not_destroy_an_otherwise_consistent_sector_layer():
    model = TrackModel(LENGTH)
    tracker = SectorTracker(model)
    observe(tracker, 3)
    tracker.observe(1, 5.0, False)
    tracker.observe(2, 40.0, False)  # bogus transition observed at the wrong place
    assert model.sector_boundaries() == [1000.0, 2000.0]


def test_pit_unknown_and_backwards_sector_transitions_are_ignored():
    model = TrackModel(LENGTH)
    tracker = SectorTracker(model)
    for _ in range(5):
        tracker.observe(1, 5.0, False)
        tracker.observe(2, 1000.0, True)    # in pits
        tracker.observe(3, 2000.0, None)    # unknown pit state
        tracker.observe(2, 1000.0, False)   # backwards 3 -> 2
        tracker.observe(None, 900.0, False)
    assert model.sector_observations == {1: [], 2: []}


def test_sector_markers_lie_on_the_learned_centerline_at_observed_boundaries():
    model, _, _ = learn(lap_samples(start=2.0, end=LENGTH + 40))
    assert public_geometry(model)["sector_markers"] is None     # no sector observations: nothing invented
    observe(SectorTracker(model), 3)
    markers = public_geometry(model)["sector_markers"]
    assert [m["boundary"] for m in markers] == [1, 2]
    for marker, distance in zip(markers, (1000.0, 2000.0)):
        expected = circuit(distance)
        assert math.dist(marker["point"], expected[:2]) < 15     # on the centerline, not a third of the outline
        assert math.hypot(*marker["direction"]) == pytest.approx(1.0)


def test_sector_markers_are_omitted_for_unknown_unplaceable_or_incomplete_geometry():
    model = TrackModel(LENGTH)
    assert model.boundary_markers([1000.0, 2000.0]) is None      # no closed geometry yet
    model.points, model.distances = [[0, 0], [10, 0], [20, 0]], [500.0, 600.0, 700.0]
    assert model.boundary_markers([1000.0, 2000.0]) is None      # outside the sampled arc: never guessed
    assert model.boundary_markers(None) is None
    model.points, model.distances = [[0, 0], [0, 0], [20, 0]], [900.0, 1100.0, 2500.0]
    assert model.boundary_markers([1000.0, 2000.0]) is None      # degenerate segment
    contradictory = TrackModel(LENGTH)
    contradictory.points, contradictory.distances = [[0, 0], [1, 1], [2, 2]], [500.0, 1500.0, 2500.0]
    observe(SectorTracker(contradictory), 2, drift=0.0)
    contradictory.sector_observations[1] = [100.0, 900.0]
    assert public_geometry(contradictory)["sector_markers"] is None
