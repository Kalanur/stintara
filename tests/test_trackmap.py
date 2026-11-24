"""Centerline learning, closure plausibility and sector attribution on wholly synthetic geometry."""
import copy
import math

import pytest

from server.app.trackmap import (
    MAX_POINTS,
    CenterlineLearner,
    SectorTracker,
    TrackModel,
    TrackStore,
    attach_track,
    geometry_record,
    normalize_lap_distance,
    public_geometry,
    track_key,
    validate_record,
)
from tests.track_fixtures import LENGTH, circuit, closed_geometry, lap_samples, model_from


def end_points_displaced(arc, amount):
    """Displacement for closed_geometry: the last point lags and the first one leads by `amount` metres along the
    track, so the closing chord shrinks by twice that while the rest of the outline stays consistent."""
    def along(d):
        if abs(d - arc / 2) < 1e-6:
            return -amount
        return amount if abs(d - (LENGTH - arc / 2)) < 1e-6 else 0.0
    return along


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


# --- Start/finish marker (#71): derived from closed geometry + lap distance only -----------------------------

def test_start_finish_marker_lies_on_the_line_and_points_in_the_direction_of_travel():
    model, _, _ = learn(lap_samples(start=2.0, end=LENGTH + 40))
    marker = public_geometry(model)["start_finish_marker"]
    assert marker["basis"] == "lap_distance"
    assert math.dist(marker["point"], circuit(0.0)) < 12                  # lap distance 0, within the closing gap
    assert marker["direction"] == pytest.approx([1.0, 0.0], abs=0.15)     # the bottom straight is driven towards +x
    assert math.hypot(*marker["direction"]) == pytest.approx(1.0)
    assert 0 < marker["arc_m"] <= 80                                      # accuracy bound: the gap it was interpolated across


def test_start_finish_marker_sits_at_lap_distance_zero_of_a_consistent_closing_arc():
    for arc in (8.0, 20.0, 40.0):
        marker = model_from(*closed_geometry(arc=arc)).start_finish_marker()
        assert marker["arc_m"] == pytest.approx(arc)
        assert math.dist(marker["point"], circuit(0.0)) < 3.0             # the line, not just somewhere in the gap
        assert marker["direction"] == pytest.approx([1.0, 0.0], abs=0.15)


def test_start_finish_marker_survives_noisy_closing_points_when_the_neighbouring_segments_agree():
    # Sebring-like (chord about 0.55 of the arc) and Algarve-like (about 0.65): the last point lags and the first one
    # leads by a few metres. The closing chord alone looks short, but the fitted neighbouring segments still agree.
    for arc, lag in ((16.4, 3.7), (14.8, 2.8)):
        points, distances = closed_geometry(arc=arc, along=end_points_displaced(arc, lag))
        chord = math.dist(points[-1], points[0])
        assert chord < 0.7 * arc                                          # a failing case for the former chord rule
        marker = model_from(points, distances).start_finish_marker()
        assert marker is not None and math.dist(marker["point"], circuit(0.0)) < 5.0


def test_start_finish_marker_recovers_from_a_closing_chord_far_below_the_arc():
    # chord below a quarter of a 12 m arc, yet head and tail windows agree on the line: the position is reliable
    points, distances = closed_geometry(arc=12.0, along=end_points_displaced(12.0, 5.0))
    assert math.dist(points[-1], points[0]) < 0.25 * 12.0
    marker = model_from(points, distances).start_finish_marker()
    assert marker is not None and math.dist(marker["point"], circuit(0.0)) < 6.0


def test_start_finish_marker_refuses_a_position_the_two_sides_contradict():
    # Daytona-like: the whole stretch before the line sits about one closing arc further along the track than its lap
    # distance says, so the tail and the head extrapolate to different places. No marker, no invented position.
    def lag(d):
        return 12.0 if d > LENGTH - 120 else 0.0
    points, distances = closed_geometry(arc=11.3, along=lag)
    model = model_from(points, distances)
    assert model.start_finish_marker() is None
    assert public_geometry(model)["start_finish_marker"] is None
    assert public_geometry(model)["complete"] is True                      # the outline itself stays usable
    # one side moved by less than the closing arc is within the published accuracy and still accepted
    mild = model_from(*closed_geometry(arc=11.3, along=lambda d: 5.0 if d > LENGTH - 120 else 0.0))
    assert mild.start_finish_marker() is not None


def test_start_finish_marker_needs_more_than_a_good_closing_chord():
    # the last point and the first point are consistent with each other, but the points around them are not
    def jump(d):
        return 15.0 if LENGTH - 90 < d < LENGTH - 20 else 0.0
    points, distances = closed_geometry(arc=11.3, along=jump)
    assert math.dist(points[-1], points[0]) == pytest.approx(11.3, abs=3.0)   # the chord alone looks fine
    assert model_from(points, distances).start_finish_marker() is None


def test_start_finish_marker_is_never_guessed():
    assert TrackModel(LENGTH).start_finish_marker() is None                # nothing learned
    assert public_geometry(None)["start_finish_marker"] is None
    learning = TrackModel(LENGTH)
    learner = CenterlineLearner(learning)
    for sample in lap_samples(start=2.0, end=800):
        learner.add(*sample)
    assert public_geometry(learning, learner)["start_finish_marker"] is None   # partial outline
    points, distances = closed_geometry(arc=12.0)

    def marker(points=points, distances=distances, length=LENGTH):
        return model_from(points, distances, length).start_finish_marker()
    assert marker() is not None
    assert marker(points[:1], distances[:1]) is None                       # single point
    assert marker(points[:8], distances[:8]) is None                       # too few points for two windows
    assert marker(distances=[distances[0]] + [d + 150 for d in distances[1:]]) is None   # arc is not a closing arc
    assert marker(distances=[0.0] + distances[1:-1] + [LENGTH]) is None    # last point at the length: no closing arc
    assert marker(distances=distances[:-1] + [LENGTH + 5]) is None         # last point beyond the length
    assert marker(distances=[1.0] + distances[1:-1] + [LENGTH - 90.0]) is None            # arc above the limit


def test_start_finish_marker_ignores_degenerate_or_non_finite_neighbours():
    points, distances = closed_geometry(arc=12.0)
    same = [[points[-1][0], points[-1][1]] for _ in points[-6:]]
    assert model_from(points[:-6] + same, distances).start_finish_marker() is None         # no movement at all
    twice = distances[:-1] + [distances[-2]]
    assert model_from(points, twice).start_finish_marker() is None                         # repeated lap distance
    backwards = distances[:-3] + [distances[-2], distances[-3], distances[-1]]
    assert model_from(points, backwards).start_finish_marker() is None                     # distances not increasing
    for index in (0, 1, 2, -1, -3, -6):
        bad = [list(p) for p in points]
        bad[index] = [math.nan, 0.0]
        assert model_from(bad, distances).start_finish_marker() is None                    # NaN inside a window
    bad = [list(p) for p in points]
    bad[len(bad) // 2] = [math.nan, math.nan]
    assert model_from(bad, distances).start_finish_marker() is not None                    # interior NaN is not used
    corner = [list(p) for p in points]
    for i in range(1, 7):                                                                  # a sharp bend at the line
        corner[-i] = [corner[-7][0] + 12.0 * i * 0.2, corner[-7][1] + 90.0 * i / 6]
    assert model_from(corner, distances).start_finish_marker() is None                     # not a straight stretch


def test_start_finish_marker_of_stored_geometry_needs_no_persistence_and_survives_a_reload():
    model, _, _ = learn(lap_samples(start=2.0, end=LENGTH + 40))
    record = geometry_record(model)
    assert "start_finish_marker" not in record                              # derived, never stored
    restored = TrackModel(record["length_m"])
    restored.adopt(validate_record(record))
    assert restored.start_finish_marker() == model.start_finish_marker()


def test_reloaded_stored_geometry_is_judged_on_its_own_evidence_and_never_rewritten():
    for along, expected in ((lambda d: 0.0, True), (lambda d: 12.0 if d > LENGTH - 120 else 0.0, False)):
        original = model_from(*closed_geometry(arc=11.3, along=along))
        record = geometry_record(original)
        snapshot = copy.deepcopy(record)
        restored = TrackModel(record["length_m"])
        restored.adopt(validate_record(record))
        assert (restored.start_finish_marker() is not None) is expected
        assert (original.start_finish_marker() is not None) is expected
        assert restored.complete and restored.source == "stored"          # the geometry is kept either way
        assert (restored.points, restored.distances) == (original.points, original.distances)
        assert record == snapshot                                         # reading the marker writes nothing back
        assert restored.start_finish_marker() == restored.start_finish_marker()   # deterministic

