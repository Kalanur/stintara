"""Offline lap-counter crossing *evidence* detector.

Detects true lap-counter transitions N -> N+1 of the scoring player's vehicle, separates them from bare lap-distance
wraps, determines the crossing time from ``lap_start_et`` and looks up the telemetry position by interpolating strictly
between two enclosing samples (never extrapolating).

This module produces **diagnostic evidence only**. It computes no start/finish anchor, offers no accepted marker and is
not connected to production transport, ingest or ``TrackModel``. Every provenance dimension that the history cannot
prove (source epoch, telemetry vehicle identity, layout identity) is reported as unknown/unverified.

Input is a stream of the neutral observations in ``model``; memory use is bounded by configuration, not by input size
(except the optional, capped record lists).
"""
import math
from collections import Counter, deque
from dataclasses import dataclass, field

from .model import LinkObs, PhaseObs, ScoringObs, SessionMeta, TelemetryObs
from .stats import dispersion, summary

SCHEMA = "crossing-evidence-diagnostic-v1"
EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Config:
    gap_s: float = 5.0                  # recorder silence that ends a continuity segment
    clock_tol_s: float = 0.5            # tolerated disagreement between scoring and telemetry clocks
    max_bracket_s: float = 0.5          # widest telemetry interval that may be interpolated
    metadata_wait_s: float = 5.0        # how long a missing lap_start_et update is awaited (session clock)
    settle_s: float = 1.0               # wait for the second metadata source before resolving
    ls_conflict_tol_s: float = 0.05     # scoring vs telemetry lap_start_et disagreement
    wrap_drop_frac: float = 0.5         # lap_dist drop (fraction of track length) that counts as a wrap
    near_line_before_m: float = 50.0    # lap_dist window after the crossing: [-before, after] ...
    near_line_after_m: float = 150.0
    near_line_wrap_m: float = 50.0      # ... or the last metres before the wrap
    min_speed_ms: float = 10.0
    pit_cooldown_s: float = 0.0         # 0: only the transition's own scoring ticks count as pit context
    max_step_speed_ms: float = 120.0    # displacement per bracket above this is not a plausible car
    binding_tol_m: float = 1.0          # telemetry vs scoring-player position, same recorder instant
    pair_window_s: float = 0.5          # recorder-time window around the crossing for that comparison
    accepted_phases: tuple = ("green", "full_course_yellow")
    telemetry_buffer: int = 4096
    ls_change_buffer: int = 64
    max_records: int = 20000            # cap for retained per-crossing records and dispersion points
    max_segments: int = 512
    max_pending: int = 16


@dataclass(slots=True)
class Pending:
    segment: int
    lap_from: int
    lap_to: int
    prev_s: ScoringObs
    cur_s: ScoringObs
    prev_pv: object
    cur_pv: object
    phase: str
    wrap: bool
    ls_old: float | None
    t_scoring: float | None = None
    t_scoring_obs: float | None = None
    t_scoring_rec: float | None = None
    t_scoring_late: bool = False
    t_tel: float | None = None
    t_tel_obs: float | None = None
    t_tel_rec: float | None = None
    t_tel_late: bool = False
    implausible: bool = False
    last_pit_time: float | None = None


@dataclass(slots=True)
class Entry:
    et: float
    rec_t: float
    gx: float
    gy: float
    gz: float | None
    speed: float | None
    vx: float | None
    vy: float | None
    ls: float | None
    poisoned: bool = False


@dataclass(slots=True)
class SegmentInfo:
    index: int
    start_rec_t: float | None
    start_reason: str
    end_rec_t: float | None = None
    scoring_obs: int = 0
    telemetry_obs: int = 0
    counter_increases: int = 0
    accepted: int = 0


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def phase_reason(state):
    return "phase_" + state


@dataclass
class Detector:
    cfg: Config = field(default_factory=Config)

    def __post_init__(self):
        self.counters = Counter()
        self.rejections_first = Counter()
        self.rejections_any = Counter()
        self.crossings = []
        self.wraps = []
        self.points_all = []
        self.points_accepted = []
        self.widths_resolved = []
        self.widths_accepted = []
        self.travel_accepted = []
        self.late_delays = []
        self.late_by_source = Counter()
        self.binding = Counter()
        self.binding_deviation = []
        self.clock_offsets = []
        self.segments = []
        self.break_reasons = Counter()
        self.layouts = {}
        self.layout_changes = 0
        self.player_keys = set()
        self.distinct = set()
        self.seen_transitions = set()
        self.pendings = []
        self.segment = -1
        self.track = None
        self.phase = "unknown"
        self.kind = None
        self.last_rec = None
        self.last_scoring = None
        self.prev_player = None
        self.last_session_time = None
        self.tel = deque()
        self.tel_ls_changes = deque()
        self.last_tel_ls = None
        self.latest_et = None
        self.last_pair_scoring = None
        self.last_pair_done = True
        self.last_pit_time = None
        self.pair_log = deque(maxlen=128)
        self._new_segment("stream_start", None)

    # ------------------------------------------------------------------ segments
    def _new_segment(self, reason, rec_t):
        self.segment += 1
        if len(self.segments) < self.cfg.max_segments:
            self.segments.append(SegmentInfo(self.segment, rec_t, reason))
        else:
            self.counters["segments_omitted"] += 1
        self.last_scoring = None
        self.prev_player = None
        self.last_session_time = None
        self.tel.clear()
        self.tel_ls_changes.clear()
        self.last_tel_ls = None
        self.latest_et = None
        self.phase = "unknown"
        self.last_pair_scoring = None
        self.last_pair_done = True
        self.pair_log.clear()
        self.last_pit_time = None

    def _info(self):
        index = self.segment
        return self.segments[index] if index < len(self.segments) else None

    def _break(self, reason, rec_t):
        for pending in self.pendings:
            self._reject_unresolved(pending, "segment_break_before_resolution")
        self.pendings = []
        info = self._info()
        if info is not None:
            info.end_rec_t = self.last_rec if self.last_rec is not None else rec_t
        self.break_reasons[reason] += 1
        self._new_segment(reason, rec_t)

    # ------------------------------------------------------------------ dispatch
    def feed(self, event):
        self.counters["events"] += 1
        rec_t = event.rec_t
        if self.last_rec is not None:
            if rec_t < self.last_rec - EPS:
                self.counters["recorder_time_regression"] += 1
                self._break("recorder_time_regression", rec_t)
            elif rec_t - self.last_rec > self.cfg.gap_s:
                self._break("recorder_gap", rec_t)
        self.last_rec = rec_t
        if isinstance(event, TelemetryObs):
            self._telemetry(event)
        elif isinstance(event, ScoringObs):
            self._scoring(event)
        elif isinstance(event, PhaseObs):
            self.phase = event.state
        elif isinstance(event, SessionMeta):
            self._meta(event)
        elif isinstance(event, LinkObs):
            self.counters["link_events"] += 1
            if not event.connected:
                self._break("game_disconnected", rec_t)

    def finish(self):
        for pending in self.pendings:
            self._reject_unresolved(pending, "stream_ended_unresolved")
        self.pendings = []
        info = self._info()
        if info is not None and info.end_rec_t is None:
            info.end_rec_t = self.last_rec

    # ------------------------------------------------------------------ layout / session
    def _meta(self, meta):
        key = (meta.track_key, round(meta.track_length, 1))
        self.layouts[key] = self.layouts.get(key, 0) + 1
        if self.track is not None and self.track != key:
            self.layout_changes += 1
            self.track = key
            self._break("layout_change", meta.rec_t)
        self.track = key

    # ------------------------------------------------------------------ scoring
    @staticmethod
    def _tick(s):
        """Fields that only change on a scoring tick (5 Hz); live positions are deliberately excluded."""
        return (s.player_key, tuple((v.key, v.laps, v.lap_start_et, v.lap_dist, v.in_pits) for v in s.vehicles))

    def _scoring(self, s):
        self.counters["scoring_obs"] += 1
        if s.session_kind is not None:
            if self.kind is not None and self.kind != s.session_kind:
                self._break("session_change", s.rec_t)
            self.kind = s.session_kind
        last = self.last_scoring
        player = self._player(s)
        if self.last_session_time is not None:
            if s.session_time < self.last_session_time - EPS:
                self.counters["scoring_time_regression"] += 1
                self._break("scoring_time_regression", s.rec_t)
                last = None
            elif abs(s.session_time - self.last_session_time) <= EPS and last is not None:
                # Same scoring tick. The wire delivers several snapshots per tick with live positions; only a
                # contradiction in the tick fields themselves (counter, lap start, distance) is a conflict.
                if self._tick(s) == self._tick(last):
                    self.counters["scoring_same_tick"] += 1
                    if s.vehicles == last.vehicles:
                        self.counters["scoring_duplicate"] += 1
                else:
                    self.counters["scoring_conflict_same_time"] += 1
                    self.prev_player = None  # no transition may span contradicting snapshots
                self._pair(s, player)
                return
        self.last_scoring = s
        self.last_session_time = s.session_time
        info = self._info()
        if info is not None:
            info.scoring_obs += 1
        self.latest_et = max(self.latest_et, s.session_time) if self.latest_et is not None else s.session_time
        self._pair(s, player)
        if player is None:
            if self.prev_player is not None:
                self._break("player_absent", s.rec_t)
            self._resolve()
            return
        self.player_keys.add(player.key)
        if player.in_pits:
            self.last_pit_time = s.session_time
        prev = self.prev_player
        if prev is not None and prev[1].key != player.key:
            self._break("player_change", s.rec_t)
            prev = None
            self.last_scoring = s
            self.last_session_time = s.session_time
            self.latest_et = s.session_time
        if prev is not None:
            self._compare(prev[0], prev[1], s, player)
        self.prev_player = (s, player)
        self._late_scoring_metadata(player)
        self._resolve()

    # Same-instant pairing: scoring and telemetry snapshots stamped with the same recorder time describe the same
    # instant. For the player's own car the scoring position equals the telemetry position; telemetry from another
    # car would not match. This is evidence about the telemetry vehicle, not proof.
    def _pair(self, s, player):
        self.last_pair_scoring = (s, player)
        self.last_pair_done = False
        self._pair_check()

    def _pair_check(self):
        if self.last_pair_scoring is None or self.last_pair_done or not self.tel:
            return
        s, player = self.last_pair_scoring
        entry = self.tel[-1]
        if abs(entry.rec_t - s.rec_t) > 0.01:
            return
        self.last_pair_done = True
        if player is None or player.gx is None or player.gy is None:
            status, deviation = "not_comparable", None
        else:
            deviation = math.hypot(entry.gx - player.gx, entry.gy - player.gy)
            near = any(v.key != player.key and v.gx is not None and v.gy is not None
                       and math.hypot(entry.gx - v.gx, entry.gy - v.gy) <= self.cfg.binding_tol_m
                       for v in s.vehicles)
            if deviation > self.cfg.binding_tol_m:
                status = "position_inconsistent"
            else:
                status = "ambiguous_multiple_vehicles" if near else "position_consistent"
        self.pair_log.append((entry.rec_t, status, deviation))

    def _player(self, s):
        if s.player_key is None:
            return None
        matches = [v for v in s.vehicles if v.key == s.player_key]
        if len(matches) > 1:
            self.counters["ambiguous_player_vehicle"] += 1
            return None
        return matches[0] if matches else None

    def _compare(self, prev_s, prev_pv, cur_s, cur_pv):
        info = self._info()
        length = self.track[1] if self.track else None
        wrap = (prev_pv.lap_dist is not None and cur_pv.lap_dist is not None and length is not None
                and prev_pv.lap_dist - cur_pv.lap_dist > self.cfg.wrap_drop_frac * length)
        if prev_pv.laps is None or cur_pv.laps is None:
            self.counters["lap_counter_missing"] += 1
            return
        step = cur_pv.laps - prev_pv.laps
        if wrap:
            self.counters["wrap_candidates"] += 1
            self.counters["wrap_with_counter_increase" if step == 1 else "wrap_without_counter_increase"] += 1
            if len(self.wraps) < self.cfg.max_records:
                self.wraps.append({"segment": self.segment, "session_time": cur_s.session_time,
                                   "lap_from": prev_pv.laps, "lap_to": cur_pv.laps, "counter_increased": step == 1,
                                   "phase": self.phase,
                                   "in_pits": bool(prev_pv.in_pits or cur_pv.in_pits)})
        if step == 1:
            self.counters["counter_increases"] += 1
            if not wrap:
                self.counters["counter_increase_without_wrap"] += 1
            if info is not None:
                info.counter_increases += 1
            self._open(prev_s, prev_pv, cur_s, cur_pv, wrap)
        elif step != 0:
            self.counters["counter_decrease" if step < 0 else "counter_jump"] += 1

    def _open(self, prev_s, prev_pv, cur_s, cur_pv, wrap):
        if len(self.pendings) >= self.cfg.max_pending:
            self.counters["pending_overflow"] += 1
            return
        pending = Pending(self.segment, prev_pv.laps, cur_pv.laps, prev_s, cur_s, prev_pv, cur_pv, self.phase, wrap,
                          prev_pv.lap_start_et, last_pit_time=self.last_pit_time)
        self._scoring_candidate(pending, cur_pv, cur_s.session_time, cur_s.rec_t, late=False)
        for observed_et, observed_rec, new, _old in self.tel_ls_changes:
            if self._changed(pending, new):
                pending.t_tel, pending.t_tel_obs, pending.t_tel_rec = new, observed_et, observed_rec
        self.pendings.append(pending)

    @staticmethod
    def _changed(pending, new):
        return new is not None and (pending.ls_old is None or new > pending.ls_old + 1e-6)

    def _scoring_candidate(self, pending, pv, observed_et, observed_rec, late):
        if pending.t_scoring is None and pv.laps == pending.lap_to and self._changed(pending, pv.lap_start_et):
            pending.t_scoring, pending.t_scoring_obs = pv.lap_start_et, observed_et
            pending.t_scoring_rec, pending.t_scoring_late = observed_rec, late

    def _late_scoring_metadata(self, player):
        for pending in self.pendings:
            if pending.segment == self.segment and pending.t_scoring is None:
                self._scoring_candidate(pending, player, self.last_session_time, self.last_rec, late=True)

    # ------------------------------------------------------------------ telemetry
    def _telemetry(self, t):
        self.counters["telemetry_obs"] += 1
        if self.tel:
            last = self.tel[-1]
            if t.et < last.et - EPS:
                self.counters["telemetry_time_regression"] += 1
                self._break("telemetry_time_regression", t.rec_t)
            elif abs(t.et - last.et) <= EPS:
                same = (abs(t.gx - last.gx) < 1e-6 and abs(t.gy - last.gy) < 1e-6
                        and t.lap_start_et == last.ls)
                if same:
                    self.counters["telemetry_duplicate"] += 1
                else:
                    self.counters["telemetry_conflict_same_time"] += 1
                    last.poisoned = True
                return
        self.tel.append(Entry(t.et, t.rec_t, t.gx, t.gy, t.gz, t.speed, t.vx, t.vy, t.lap_start_et))
        while len(self.tel) > self.cfg.telemetry_buffer:
            self.tel.popleft()
        self._pair_check()
        info = self._info()
        if info is not None:
            info.telemetry_obs += 1
        self.latest_et = max(self.latest_et, t.et) if self.latest_et is not None else t.et
        ls = t.lap_start_et
        if ls is not None:
            if self.last_tel_ls is not None and ls > self.last_tel_ls + 1e-6:
                self.tel_ls_changes.append((t.et, t.rec_t, ls, self.last_tel_ls))
                while len(self.tel_ls_changes) > self.cfg.ls_change_buffer:
                    self.tel_ls_changes.popleft()
                for pending in self.pendings:
                    if pending.segment == self.segment and pending.t_tel is None and self._changed(pending, ls):
                        pending.t_tel, pending.t_tel_obs, pending.t_tel_rec = ls, t.et, t.rec_t
                        pending.t_tel_late = True
            self.last_tel_ls = ls
        self._resolve()

    # ------------------------------------------------------------------ resolution
    def _resolve(self):
        remaining = []
        for pending in self.pendings:
            if pending.segment != self.segment or not self._try(pending):
                remaining.append(pending)
        self.pendings = [p for p in remaining if p.segment == self.segment]

    def _try(self, p):
        """True when the pending transition has been finalised (accepted or rejected)."""
        cfg = self.cfg
        lo, hi = p.prev_s.session_time - cfg.clock_tol_s, p.cur_s.session_time + cfg.clock_tol_s
        candidates = {}
        for name, value, observed in (("scoring_lap_start_et", p.t_scoring, p.t_scoring_obs),
                                      ("telemetry_lap_start_et", p.t_tel, p.t_tel_obs)):
            if value is not None:
                if lo <= value <= hi:
                    candidates[name] = (value, observed)
                else:
                    p.implausible = True
        latest = self.latest_et if self.latest_et is not None else p.cur_s.session_time
        if not candidates:
            if latest > p.cur_s.session_time + cfg.metadata_wait_s:
                self._finalise(p, None, ["lap_start_et_implausible" if p.implausible else "no_crossing_time"], None)
                return True
            return False
        if len(candidates) == 2:
            a, b = (v[0] for v in candidates.values())
            if abs(a - b) > cfg.ls_conflict_tol_s:
                self._finalise(p, None, ["lap_start_et_conflict"], None)
                return True
        both = len(candidates) == 2
        if not both and latest < p.cur_s.session_time + cfg.settle_s:
            return False
        source = "scoring_lap_start_et" if "scoring_lap_start_et" in candidates else "telemetry_lap_start_et"
        crossing, observed = candidates[source]
        if not self.tel or self.tel[-1].et < crossing - EPS:
            if latest > crossing + cfg.max_bracket_s + cfg.metadata_wait_s:
                self._finalise(p, crossing, ["no_enclosing_sample"], {"source": source, "observed": observed})
                return True
            return False
        self._finalise(p, crossing, [], {"source": source, "observed": observed, "both": both})
        return True

    def _reject_unresolved(self, p, reason):
        self._finalise(p, None, [reason], None)

    def _locate(self, crossing):
        """The enclosing telemetry pair, or (None, None, reason). Never extrapolates."""
        lower = upper = None
        for entry in reversed(self.tel):
            if entry.et >= crossing - EPS:
                upper = entry
            if entry.et <= crossing + EPS:
                lower = entry
                break
        if lower is None or upper is None:
            return None, None, "no_enclosing_sample"
        if lower.poisoned or upper.poisoned:
            return None, None, "conflicting_samples_same_time"
        return lower, upper, None

    def _finalise(self, p, crossing, reasons, meta):
        cfg = self.cfg
        record = {"since_pit_s": None, "segment": p.segment, "lap_from": p.lap_from, "lap_to": p.lap_to,
                  "session_time": p.cur_s.session_time, "wrap": p.wrap, "phase": p.phase,
                  "crossing_et": crossing, "crossing_source": meta.get("source") if meta else None}
        position = lower = upper = None
        hard = list(reasons)
        if not hard and crossing is not None:
            lower, upper, why = self._locate(crossing)
            if why:
                hard.append(why)
            else:
                position = self._interpolate(crossing, lower, upper, hard, record)
        soft = []
        if position is not None and not hard:
            soft = self._quality(p, crossing, meta, lower, upper, position, record)
        if crossing is not None and p.last_pit_time is not None:
            record["since_pit_s"] = crossing - p.last_pit_time
        if crossing is not None:
            # "After the counter" is about arrival order: a reader that looked at the other source's lap_start_et when
            # the counter message arrived would have seen the previous lap's value. Seconds are recorder time.
            seen = {}
            for name, rec, late in (("scoring", p.t_scoring_rec, p.t_scoring_late),
                                    ("telemetry", p.t_tel_rec, p.t_tel_late)):
                if rec is not None:
                    seen[name] = {"after_counter_message": late, "recorder_delay_s": rec - p.cur_s.rec_t}
            record["lap_start_et_seen"] = seen
            late_sources = [name for name, info in seen.items() if info["after_counter_message"]]
            record["lap_start_et_late"] = bool(late_sources)
            if late_sources:
                self.counters["late_metadata"] += 1
                for name in late_sources:
                    self.late_by_source[name] += 1
                self.late_delays.append(max(seen[name]["recorder_delay_s"] for name in late_sources))
        key = (p.segment, p.lap_to)
        if not hard and key in self.seen_transitions:
            soft.append("duplicate_lap_transition")
        self.seen_transitions.add(key)
        all_reasons = self._context(p, crossing) + hard + soft
        if not all_reasons and position is not None:
            self.widths_accepted.append(record["interval_width_s"])
            self.travel_accepted.append(record["interval_travel_m"])
        record["status"] = "diagnostic_rejected" if all_reasons else "diagnostic_candidate"
        record["reasons"] = all_reasons
        record["position"] = list(position[:3]) if position else None
        self.counters["transitions_resolved"] += 1
        if all_reasons:
            self.rejections_first[all_reasons[0]] += 1
            for reason in all_reasons:
                self.rejections_any[reason] += 1
        else:
            self.counters["diagnostic_candidates"] += 1
            self.distinct.add(key)
            info = self._info()
            if info is not None and p.segment == self.segment:
                info.accepted += 1
        if position is not None:
            point = (position[0], position[1], position[3], position[4])
            self._keep(self.points_all, point)
            if not all_reasons:
                self._keep(self.points_accepted, point)
        if len(self.crossings) < cfg.max_records:
            self.crossings.append(record)
        else:
            self.counters["records_omitted"] += 1

    def _keep(self, target, point):
        if len(target) < self.cfg.max_records:
            target.append(point)
        else:
            self.counters["points_omitted"] += 1

    def _interpolate(self, crossing, lower, upper, hard, record):
        cfg = self.cfg
        if abs(lower.et - crossing) <= EPS or lower is upper:
            exact, width, fraction, a, b = True, 0.0, 0.0, lower, lower
        elif abs(upper.et - crossing) <= EPS:
            exact, width, fraction, a, b = True, 0.0, 0.0, upper, upper
        else:
            exact, width = False, upper.et - lower.et
            fraction = (crossing - lower.et) / width
            a, b = lower, upper
        record.update(exact_hit=exact, interval_width_s=width, interval_et=[lower.et, upper.et])
        if not exact:
            if width > cfg.max_bracket_s:
                hard.append("bracket_too_wide")
            if abs((upper.rec_t - lower.rec_t) - width) > cfg.clock_tol_s:
                hard.append("bracket_clock_inconsistent")
            if math.hypot(upper.gx - lower.gx, upper.gy - lower.gy) / width > cfg.max_step_speed_ms:
                hard.append("bracket_position_jump")
            if hard:
                return None
        gx = a.gx + (b.gx - a.gx) * fraction
        gy = a.gy + (b.gy - a.gy) * fraction
        gz = a.gz + (b.gz - a.gz) * fraction if a.gz is not None and b.gz is not None else None
        speed = None
        if a.speed is not None and b.speed is not None:
            speed = a.speed + (b.speed - a.speed) * fraction
        elif lower.vx is not None and lower.vy is not None:
            speed = math.hypot(lower.vx, lower.vy)
        direction = self._direction(a, b)
        record["speed_ms"] = speed
        self.widths_resolved.append(width)
        record["interval_travel_m"] = (speed or 0.0) * width
        dx, dy = direction if direction else (0.0, 0.0)
        return gx, gy, gz, dx, dy, speed, direction

    @staticmethod
    def _direction(a, b):
        dx, dy = b.gx - a.gx, b.gy - a.gy
        norm = math.hypot(dx, dy)
        if norm > 0.05:
            return dx / norm, dy / norm
        if a.vx is not None and a.vy is not None:
            norm = math.hypot(a.vx, a.vy)
            if norm > 1.0:
                return a.vx / norm, a.vy / norm
        return None

    def _quality(self, p, crossing, meta, lower, upper, position, record):
        cfg = self.cfg
        speed, direction = position[5], position[6]
        reasons = []
        if speed is None:
            reasons.append("speed_unknown")
        elif speed < cfg.min_speed_ms:
            reasons.append("low_speed")
        if direction is None:
            reasons.append("direction_unknown")
        scoring_offset = p.cur_s.rec_t - p.cur_s.session_time
        telemetry_offset = (lower.rec_t - lower.et + upper.rec_t - upper.et) / 2
        offset_gap = scoring_offset - telemetry_offset
        record["clock_offset_gap_s"] = offset_gap
        self.clock_offsets.append(offset_gap)
        if abs(offset_gap) > cfg.clock_tol_s:
            reasons.append("clock_offset_mismatch")
        status, deviation = self._bind(crossing, lower, upper)
        record["vehicle_binding"] = status
        record["scoring_position_deviation_m"] = deviation
        self.binding[status] += 1
        if deviation is not None:
            self.binding_deviation.append(deviation)
        if status in ("position_inconsistent", "ambiguous_multiple_vehicles"):
            reasons.append("vehicle_binding_" + status)
        if meta and meta.get("both") is False and p.t_tel is not None and p.t_scoring is None:
            record["metadata_only_from_telemetry"] = True
        return reasons

    def _context(self, p, crossing):
        """Filters that need no position: pit activity, race phase and where on the lap the counter moved."""
        cfg = self.cfg
        reasons = []
        if p.prev_pv.in_pits or p.cur_pv.in_pits:
            reasons.append("pit_context")
        elif cfg.pit_cooldown_s and p.last_pit_time is not None and crossing - p.last_pit_time < cfg.pit_cooldown_s:
            reasons.append("recent_pit_activity")
        if p.phase not in cfg.accepted_phases:
            reasons.append(phase_reason(p.phase))
        length = self.track[1] if self.track else None
        distance = p.cur_pv.lap_dist
        if distance is None or length is None:
            reasons.append("lap_dist_unknown")
        elif not (-cfg.near_line_before_m <= distance <= cfg.near_line_after_m
                  or length - cfg.near_line_wrap_m <= distance <= length):
            reasons.append("lap_dist_not_near_line")
        return reasons

    def _bind(self, crossing, lower, upper):
        """Vehicle binding around the crossing from same-instant scoring/telemetry pairs (see ``_pair``)."""
        ratio = (upper.rec_t - lower.rec_t) / (upper.et - lower.et) if upper.et > lower.et else 1.0
        rec_cross = lower.rec_t + (crossing - lower.et) * ratio
        window = [(status, dev) for rec, status, dev in self.pair_log if abs(rec - rec_cross) <= self.cfg.pair_window_s]
        statuses = {status for status, _ in window}
        deviations = [dev for _, dev in window if dev is not None]
        deviation = max(deviations) if deviations else None
        for status in ("position_inconsistent", "ambiguous_multiple_vehicles", "position_consistent"):
            if status in statuses:
                return status, deviation
        return "not_comparable", deviation

    # ------------------------------------------------------------------ report
    def report(self):
        c = self.counters
        return {
            "schema": SCHEMA,
            "evidence_class": "diagnostic_only",
            "anchor": {"computed": False, "production_accepted": False,
                       "note": "Diagnostic candidates are evidence for review, never an accepted start/finish marker."},
            "counts": {
                "wrap_candidates": c["wrap_candidates"],
                "wrap_with_counter_increase": c["wrap_with_counter_increase"],
                "wrap_without_counter_increase": c["wrap_without_counter_increase"],
                "counter_increases": c["counter_increases"],
                "counter_increase_without_wrap": c["counter_increase_without_wrap"],
                "counter_anomalies": {"decrease": c["counter_decrease"], "jump": c["counter_jump"]},
                "transitions_resolved": c["transitions_resolved"],
                "diagnostic_candidates": c["diagnostic_candidates"],
                "distinct_candidate_lap_crossings": len(self.distinct),
                "late_lap_start_et_updates": c["late_metadata"],
                "late_lap_start_et_by_source": dict(sorted(self.late_by_source.items())),
            },
            "input": {k: v for k, v in sorted(c.items()) if k in (
                "events", "scoring_obs", "telemetry_obs", "link_events", "scoring_same_tick", "scoring_duplicate",
                "scoring_conflict_same_time", "scoring_time_regression", "telemetry_duplicate",
                "telemetry_conflict_same_time", "telemetry_time_regression", "recorder_time_regression",
                "ambiguous_player_vehicle", "lap_counter_missing", "pending_overflow", "records_omitted",
                "points_omitted", "segments_omitted")},
            "rejections": {"first_reason": dict(sorted(self.rejections_first.items())),
                           "any_reason": dict(sorted(self.rejections_any.items()))},
            "interval": {"width_s_resolved": summary(self.widths_resolved),
                         "width_s_candidates": summary(self.widths_accepted),
                         "travel_m_candidates": summary(self.travel_accepted),
                         "late_delay_s": summary(self.late_delays)},
            "dispersion": {"unfiltered": dispersion(self.points_all), "filtered": dispersion(self.points_accepted),
                           "note": "Descriptive spread about the sample median; not a surveyed line."},
            "clock_alignment": {"scoring_minus_telemetry_offset_s": {
                "abs": summary([abs(v) for v in self.clock_offsets])}},
            "provenance": {
                "source": {"authentication": "none_offline_file", "source_epoch": None,
                           "source_epoch_status": "unknown_not_recorded_in_history",
                           "segments_are_connection_epochs": False},
                "vehicle": {"scoring_player_keys_seen": sorted(self.player_keys),
                            "telemetry_vehicle_identity": "absent_from_wire_format",
                            "position_correlation": {k: self.binding[k] for k in (
                                "position_consistent", "position_inconsistent", "ambiguous_multiple_vehicles",
                                "not_comparable")},
                            "scoring_position_deviation_m": summary(self.binding_deviation),
                            "status": "unverified_position_correlation_only"},
                "layout": {"keys_seen": [{"track_key": k[0], "length_m": k[1], "meta_messages": n}
                                         for k, n in sorted(self.layouts.items())],
                           "layout_changes": self.layout_changes, "status": "unverified_track_key_only"},
                "continuity": {"segment_kind": "derived_diagnostic_segment", "segments": self.segment + 1,
                               "break_reasons": dict(sorted(self.break_reasons.items()))},
            },
            "segments": [{"index": s.index, "start_rec_t": s.start_rec_t, "end_rec_t": s.end_rec_t,
                          "start_reason": s.start_reason, "scoring_obs": s.scoring_obs,
                          "telemetry_obs": s.telemetry_obs, "counter_increases": s.counter_increases,
                          "diagnostic_candidates": s.accepted, "production_epoch": False}
                         for s in self.segments],
            "wraps": self.wraps,
            "crossings": self.crossings,
        }
