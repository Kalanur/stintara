"""Synthetic regression and negative tests for the offline crossing-evidence detector."""
import dataclasses

import pytest

from tests.crossing_synthetic import (
    DT,
    LENGTH,
    OFFSET,
    PLAYER,
    build,
    candidates,
    crossing_time,
    reasons,
    rejected,
    run,
)
from tools.crossing.detector import Config
from tools.crossing.model import LinkObs, ScoringObs, SessionMeta, TelemetryObs, VehicleObs
from tools.crossing.stats import dispersion


def shifted(events, after_rec, delta):
    return [dataclasses.replace(e, rec_t=e.rec_t + delta) if e.rec_t > after_rec else e for e in events]


# ---------------------------------------------------------------- core detection
def test_wrap_without_counter_increase_is_not_a_crossing():
    _, report = run(build((10.0,), counter=False))
    counts = report["counts"]
    assert counts["wrap_candidates"] == 1
    assert counts["wrap_without_counter_increase"] == 1
    assert counts["counter_increases"] == 0
    assert report["crossings"] == []
    assert counts["diagnostic_candidates"] == 0


def test_counter_transition_n_to_n_plus_one_gives_an_interpolated_position():
    _, report = run(build((10.03,)))
    counts = report["counts"]
    assert counts["wrap_with_counter_increase"] == 1 and counts["counter_increases"] == 1
    [crossing] = candidates(report)
    assert (crossing["lap_from"], crossing["lap_to"]) == (3, 4)
    assert crossing["crossing_et"] == pytest.approx(crossing_time(10.03))
    assert crossing["position"][0] == pytest.approx(0.0, abs=1e-6)       # on the line, interpolated
    low, high = crossing["interval_et"]
    assert low < crossing["crossing_et"] < high and high - low == pytest.approx(DT)
    assert crossing["interval_width_s"] == pytest.approx(DT) and crossing["exact_hit"] is False
    assert crossing["vehicle_binding"] == "position_consistent"
    assert report["counts"]["distinct_candidate_lap_crossings"] == 1


def test_exact_hit_on_a_telemetry_timestamp_uses_the_sample_itself():
    _, report = run(build((10.0,)))                                      # 10.0 s is on the 16 Hz grid
    [crossing] = candidates(report)
    assert crossing["exact_hit"] is True and crossing["interval_width_s"] == 0.0
    assert crossing["interval_et"] == [crossing_time(10.0)] * 2
    assert crossing["position"][0] == pytest.approx(0.0, abs=1e-9)


def test_several_laps_are_distinct_and_dispersion_separates_lateral_from_longitudinal():
    lateral = [0.0, 1.0, -1.0, 2.0]
    _, report = run(build((10.0, 20.0, 30.0, 40.0), lateral=lateral))
    assert len(candidates(report)) == 4 and report["counts"]["distinct_candidate_lap_crossings"] == 4
    filtered = report["dispersion"]["filtered"]
    assert filtered["n"] == 4
    assert filtered["longitudinal_m"]["max"] < 1e-6                   # the line is where the clock says it is
    assert filtered["lateral_m"]["max"] == pytest.approx(1.5, abs=1e-6)   # offsets about the median centre 0.5
    assert filtered["radial_m"]["max"] == pytest.approx(1.5, abs=1e-6)
    assert report["dispersion"]["unfiltered"]["n"] == 4


def test_dispersion_projection_on_mean_direction():
    points = [(0.0, 1.0, 1.0, 0.0), (0.2, -1.0, 1.0, 0.0), (-0.2, 0.0, 1.0, 0.0)]
    result = dispersion(points)
    assert result["lateral_m"]["max"] == pytest.approx(1.0)
    assert result["longitudinal_m"]["max"] == pytest.approx(0.2)
    assert dispersion([])["n"] == 0


# ---------------------------------------------------------------- late / delayed metadata
def test_late_scoring_lap_start_et_is_used_after_the_counter_not_dropped():
    # Scoring shows the new lap number three ticks (0.6 s) before the new lap_start_et; telemetry never delivers it.
    _, report = run(build((10.0,), ls_late_ticks=3, tel_ls_lag=1e9))
    [crossing] = candidates(report)
    assert crossing["crossing_source"] == "scoring_lap_start_et"
    assert crossing["lap_start_et_late"] is True
    assert crossing["lap_start_et_seen"]["scoring"] == {"after_counter_message": True,
                                                        "recorder_delay_s": pytest.approx(0.6, abs=0.1)}
    assert crossing["crossing_et"] == pytest.approx(crossing_time(10.0))
    assert crossing["position"][0] == pytest.approx(0.0, abs=1e-6)
    assert report["counts"]["late_lap_start_et_updates"] == 1


def test_telemetry_lap_start_et_covers_a_scoring_value_that_never_arrives():
    _, report = run(build((10.0,), ls_late_ticks=1000))
    [crossing] = candidates(report)
    assert crossing["crossing_source"] == "telemetry_lap_start_et"
    assert crossing["position"][0] == pytest.approx(0.0, abs=1e-6)


def test_no_lap_start_et_update_means_no_crossing_time_and_no_position():
    _, report = run(build((10.0,), ls_late_ticks=1000, tel_ls_lag=1e9))
    assert candidates(report) == []
    [bad] = rejected(report)
    assert bad["reasons"] == ["no_crossing_time"] and bad["position"] is None


def test_implausible_lap_start_et_is_rejected():
    events = build((10.0,))
    broken = [dataclasses.replace(e, lap_start_et=e.lap_start_et + 100.0) if isinstance(e, ScoringObs) is False and
              isinstance(e, TelemetryObs) and e.lap_start_et > 0 else e for e in events]
    broken = [dataclasses.replace(e, vehicles=tuple(dataclasses.replace(v, lap_start_et=v.lap_start_et + 100.0)
                                                    if v.lap_start_et > 0 else v for v in e.vehicles))
              if isinstance(e, ScoringObs) else e for e in broken]
    _, report = run(broken)
    assert candidates(report) == [] and reasons(report) == ["lap_start_et_implausible"]


def test_conflicting_lap_start_et_between_sources_is_rejected():
    events = build((10.0,))
    events = [dataclasses.replace(e, lap_start_et=e.lap_start_et + 0.2)
              if isinstance(e, TelemetryObs) and e.lap_start_et > 0 else e for e in events]
    _, report = run(events)
    assert candidates(report) == [] and reasons(report) == ["lap_start_et_conflict"]


# ---------------------------------------------------------------- duplicates, conflicts, regressions
def test_duplicated_messages_do_not_create_a_second_transition():
    events = []
    for e in build((10.0,)):
        events += [e, e]
    detector, report = run(events)
    assert len(report["crossings"]) == 1 and len(candidates(report)) == 1
    assert detector.counters["telemetry_duplicate"] > 0 and detector.counters["scoring_duplicate"] > 0


def test_contradicting_positions_at_the_same_timestamp_reject_an_enclosing_bracket():
    events = build((10.03,))
    out = []
    for e in events:
        out.append(e)
        if isinstance(e, TelemetryObs) and abs(e.et - (crossing_time(10.0) + DT)) < 1e-9:
            out.append(dataclasses.replace(e, gx=e.gx + 30.0))             # same time, different place
    detector, report = run(out)
    assert candidates(report) == [] and reasons(report) == ["conflicting_samples_same_time"]
    assert detector.counters["telemetry_conflict_same_time"] == 1


def test_scoring_tick_contradiction_blocks_a_transition_spanning_it():
    events = build((10.0,))
    out = []
    for e in events:
        out.append(e)
        if isinstance(e, ScoringObs) and abs(e.session_time - crossing_time(10.0)) < 1e-9:
            clash = tuple(dataclasses.replace(v, laps=v.laps + 5) for v in e.vehicles)
            out.append(dataclasses.replace(e, rec_t=e.rec_t + 0.001, vehicles=clash))
            break
    out += [e for e in events[len(out):]]
    detector, _ = run(out)
    assert detector.counters["scoring_conflict_same_time"] >= 1


def test_backward_telemetry_time_is_a_continuity_break_and_fails_open_transitions():
    events = build((10.0,), tel_ls_lag=1e9)                 # keeps the transition pending for the settle window
    cut = next(i for i, e in enumerate(events) if isinstance(e, TelemetryObs) and e.et >= crossing_time(10.1))
    rewind = dataclasses.replace(events[cut], et=crossing_time(5.0), rec_t=events[cut].rec_t)
    detector, report = run(events[:cut] + [rewind] + events[cut + 1:])
    assert report["provenance"]["continuity"]["break_reasons"]["telemetry_time_regression"] == 1
    assert candidates(report) == []
    assert reasons(report) == ["segment_break_before_resolution"]


def test_backward_scoring_session_time_and_recorder_time_break_continuity():
    events = build((10.0,), tel_ls_lag=1e9)
    cut = next(i for i, e in enumerate(events) if isinstance(e, ScoringObs) and e.session_time >= crossing_time(10.1))
    back = dataclasses.replace(events[cut], session_time=crossing_time(2.0))
    _, report = run(events[:cut] + [back] + events[cut + 1:])
    assert report["provenance"]["continuity"]["break_reasons"].get("scoring_time_regression") == 1
    earlier = dataclasses.replace(events[cut], rec_t=events[cut].rec_t - 3.0)
    _, report = run(events[:cut] + [earlier] + events[cut + 1:])
    assert report["provenance"]["continuity"]["break_reasons"].get("recorder_time_regression") == 1


# ---------------------------------------------------------------- gaps, reconnects, sparse data
def test_data_gap_between_counter_and_resolution_is_a_break_not_a_guess():
    events = build((10.0,), tel_ls_lag=1e9)
    _, report = run(shifted(events, 10.15, 10.0))
    assert candidates(report) == []
    assert report["provenance"]["continuity"]["break_reasons"] == {"recorder_gap": 1}


def test_game_disconnect_starts_a_new_derived_segment_and_never_claims_an_epoch():
    events = build((10.0,), tel_ls_lag=1e9)
    cut = next(i for i, e in enumerate(events) if isinstance(e, TelemetryObs) and e.et >= crossing_time(10.1))
    events.insert(cut, LinkObs(events[cut].rec_t, False))
    _, report = run(events)
    continuity = report["provenance"]["continuity"]
    assert continuity["break_reasons"] == {"game_disconnected": 1} and continuity["segments"] == 2
    assert continuity["segment_kind"] == "derived_diagnostic_segment"
    assert all(s["production_epoch"] is False for s in report["segments"])
    assert report["provenance"]["source"]["source_epoch"] is None
    assert report["provenance"]["source"]["segments_are_connection_epochs"] is False
    assert "unknown" in report["provenance"]["source"]["source_epoch_status"]


def test_missing_enclosing_sample_never_extrapolates():
    # Telemetry stops right at the crossing while scoring continues: no upper sample may be invented.
    detector, report = run(build((10.0,), tel_keep=lambda et: et <= crossing_time(10.0) - DT / 2))
    assert candidates(report) == []
    [bad] = rejected(report)
    assert bad["reasons"] == ["no_enclosing_sample"] and bad["position"] is None


def test_telemetry_gap_wider_than_the_limit_is_not_interpolated():
    gap = lambda et: not crossing_time(9.6) < et < crossing_time(10.6)      # noqa: E731
    _, report = run(build((10.0,), tel_keep=gap))
    assert candidates(report) == []
    [bad] = rejected(report)
    assert "bracket_too_wide" in bad["reasons"] and bad["position"] is None


def test_position_jump_between_enclosing_samples_is_rejected():
    events = build((10.03,))
    out = []
    for e in events:
        if isinstance(e, TelemetryObs) and e.et > crossing_time(10.03):
            e = dataclasses.replace(e, gx=e.gx + 500.0)
        out.append(e)
    _, report = run(out)
    assert candidates(report) == [] and "bracket_position_jump" in reasons(report)


def test_first_telemetry_after_the_crossing_is_missing_lower_sample_at_stream_start():
    _, report = run(build((0.02,), tel_keep=lambda et: et > crossing_time(0.5)))
    assert candidates(report) == []


# ---------------------------------------------------------------- provenance
def test_ambiguous_player_vehicle_yields_no_transition():
    events = build((10.0,))
    out = []
    for e in events:
        if isinstance(e, ScoringObs):
            e = dataclasses.replace(e, vehicles=e.vehicles + (dataclasses.replace(e.vehicles[0]),))
        out.append(e)
    detector, report = run(out)
    assert report["counts"]["counter_increases"] == 0 and report["crossings"] == []
    assert detector.counters["ambiguous_player_vehicle"] > 0


def test_telemetry_from_another_vehicle_is_inconsistent_and_never_a_candidate():
    _, report = run(build((10.0,), tel_shift=(40.0, 3.0)))
    assert candidates(report) == []
    assert "vehicle_binding_position_inconsistent" in reasons(report)
    correlation = report["provenance"]["vehicle"]["position_correlation"]
    assert correlation["position_inconsistent"] == 1 and correlation["position_consistent"] == 0


def test_second_vehicle_on_the_same_spot_is_ambiguous_not_verified():
    # A second car exactly where the telemetry car is: telemetry identity cannot be told apart by position.
    twin = VehicleObs(9, 3, -1.0, 100.0, 0.0, 0.0, False)
    events = build((10.0,))
    out = []
    for e in events:
        if isinstance(e, ScoringObs):
            moved = dataclasses.replace(twin, gx=e.vehicles[0].gx, gy=e.vehicles[0].gy)
            e = dataclasses.replace(e, vehicles=e.vehicles + (moved,))
        out.append(e)
    _, report = run(out)
    assert candidates(report) == []
    assert "vehicle_binding_ambiguous_multiple_vehicles" in reasons(report)


def test_vehicle_and_layout_provenance_are_always_unverified():
    _, report = run(build((10.0,)))
    vehicle, layout = report["provenance"]["vehicle"], report["provenance"]["layout"]
    assert vehicle["telemetry_vehicle_identity"] == "absent_from_wire_format"
    assert vehicle["status"] == "unverified_position_correlation_only"
    assert layout["status"] == "unverified_track_key_only" and layout["layout_changes"] == 0
    assert report["provenance"]["source"]["authentication"] == "none_offline_file"


def test_layout_change_is_a_continuity_break_and_blocks_a_pending_transition():
    events = build((10.0,), tel_ls_lag=1e9)
    cut = next(i for i, e in enumerate(events) if isinstance(e, TelemetryObs) and e.et >= crossing_time(10.1))
    events.insert(cut, SessionMeta(events[cut].rec_t, "Test Circuit", LENGTH + 40.0))   # same name, other length
    _, report = run(events)
    assert report["provenance"]["layout"]["layout_changes"] == 1
    assert len(report["provenance"]["layout"]["keys_seen"]) == 2
    assert report["provenance"]["continuity"]["break_reasons"] == {"layout_change": 1}
    assert candidates(report) == []


def test_session_kind_change_is_a_continuity_break():
    events = build((10.0,))
    cut = next(i for i, e in enumerate(events) if isinstance(e, ScoringObs) and e.session_time >= crossing_time(15))
    events = events[:cut] + [dataclasses.replace(e, session_kind="Qualify") if isinstance(e, ScoringObs) else e
                             for e in events[cut:]]
    _, report = run(events)
    assert report["provenance"]["continuity"]["break_reasons"] == {"session_change": 1}


# ---------------------------------------------------------------- pit lane and formation
def test_pit_activity_counter_increase_is_rejected_with_a_reason():
    _, report = run(build((10.0,), in_pits=True))
    assert report["counts"]["counter_increases"] == 1
    assert candidates(report) == [] and "pit_context" in reasons(report)
    assert report["rejections"]["first_reason"] == {"pit_context": 1}


def test_formation_phase_counter_increase_is_rejected_with_a_reason():
    _, report = run(build((10.0,), phase="formation"))
    assert report["counts"]["wrap_with_counter_increase"] == 1
    assert candidates(report) == [] and reasons(report) == ["phase_formation"]


def test_unknown_phase_is_not_treated_as_green():
    _, report = run([e for e in build((10.0,)) if type(e).__name__ != "PhaseObs"])
    assert candidates(report) == [] and reasons(report) == ["phase_unknown"]


def test_counter_increase_far_from_the_line_and_anomalies_are_not_confused_with_crossings():
    events = build((10.0,))
    out = []
    for e in events:
        if isinstance(e, ScoringObs):
            e = dataclasses.replace(e, vehicles=tuple(dataclasses.replace(v, lap_dist=-300.0) if v.key == PLAYER and
                                                       v.laps == 4 else v for v in e.vehicles))
        out.append(e)
    _, report = run(out)
    assert candidates(report) == [] and "lap_dist_not_near_line" in reasons(report)
    jump = [dataclasses.replace(e, vehicles=tuple(dataclasses.replace(v, laps=v.laps + 1) if v.laps > 3 else v
                                                  for v in e.vehicles)) if isinstance(e, ScoringObs) else e
            for e in build((10.0,))]
    _, report = run(jump)
    assert report["counts"]["counter_anomalies"]["jump"] == 1 and report["counts"]["counter_increases"] == 0


def test_standing_car_is_rejected_for_low_speed():
    _, report = run(build((10.0,), speed=2.0))
    assert candidates(report) == [] and "low_speed" in reasons(report)


# ---------------------------------------------------------------- guarantees
def test_report_never_claims_an_anchor_or_accepted_marker():
    _, report = run(build((10.0, 20.0)))
    assert report["evidence_class"] == "diagnostic_only"
    assert report["anchor"] == {"computed": False, "production_accepted": False, "note": report["anchor"]["note"]}
    assert all(c["status"] in ("diagnostic_candidate", "diagnostic_rejected") for c in report["crossings"])
    assert not any("accepted" in key for key in report if key != "anchor")


def test_memory_stays_bounded_for_long_streams():
    config = Config(telemetry_buffer=64, max_records=3)
    crossings = tuple(float(10 * k) for k in range(1, 9))
    detector, report = run(build(crossings, end=90.0), config)
    assert len(detector.tel) <= 64
    assert len(report["crossings"]) == 3 and report["input"]["records_omitted"] == 5
    assert report["dispersion"]["unfiltered"]["n"] == 3 and report["input"]["points_omitted"] >= 5
    assert report["counts"]["diagnostic_candidates"] == 8 and OFFSET > 0


def test_telemetry_lap_start_et_arriving_after_the_counter_message_is_flagged_late_and_still_used():
    # The naive reading ("telemetry lap_start_et when the counter rises") would see the previous lap here.
    _, report = run(build((10.03,), tel_ls_lag=0.5))
    [crossing] = candidates(report)
    assert crossing["lap_start_et_late"] is True
    assert crossing["lap_start_et_seen"]["telemetry"]["after_counter_message"] is True
    assert crossing["lap_start_et_seen"]["scoring"]["after_counter_message"] is False
    assert crossing["crossing_et"] == pytest.approx(crossing_time(10.03))        # scoring value, available at once
    assert report["counts"]["late_lap_start_et_by_source"] == {"telemetry": 1}


def test_lap_start_et_seen_before_the_counter_is_not_late():
    _, report = run(build((10.03,)))
    [crossing] = candidates(report)
    assert crossing["lap_start_et_late"] is False
    assert report["counts"]["late_lap_start_et_updates"] == 0


def test_recent_pit_activity_is_recorded_and_can_be_excluded_by_configuration():
    events = build((10.0,), in_pits=lambda t: t < 8.0)
    _, report = run(events)
    [crossing] = candidates(report)
    assert crossing["since_pit_s"] == pytest.approx(2.2, abs=0.3)                # recorded, not silently dropped
    _, strict = run(events, Config(pit_cooldown_s=5.0))
    assert candidates(strict) == [] and reasons(strict) == ["recent_pit_activity"]
    _, never = run(build((10.0,)))
    assert candidates(never)[0]["since_pit_s"] is None
