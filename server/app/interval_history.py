"""Backward-looking position intervals from a bounded scoring history (#68, phase 1).

Pure module: no database, no I/O, no clock of its own. One ``IntervalHistory`` belongs to exactly one agent
runtime state; it is keyed by (session id, Pitwall source epoch) and resets itself whenever either changes.

Measurement: the interval of car B to the car A directly ahead in the standings is
``now - t_pass`` where ``t_pass`` is the session-clock time at which A passed the distance B has reached *now*
(distance = total_laps * track_length + lap_dist). ``t_pass`` is interpolated linearly between two stored past
samples of A. Nothing is extrapolated, no speed is estimated and no later sample is ever used. When the data
cannot establish the value, ``value`` is None and ``reason`` says why; nothing is guessed.

Phase 1 scope is deliberately narrow: only adjacent standings positions whose cars both report the *same*
positive ``laps_behind_leader`` (same deficit does not imply the same ``total_laps``). Lead-lap intervals keep
using the leader gaps of ``timing.py``; different deficits stay "lapped".

Offline evidence (private Silverstone captures, not part of the repository): against LMU Results line-crossing
times, every produced value was within 0.21 s (1403 values). Mid-lap values were only compared with
``gap_ahead``, which is a diagnostic here and never a fallback.
"""
import math
from bisect import bisect_left
from collections import Counter

WINDOW_SECONDS = 180.0          # history kept per vehicle, on the provider session clock
MAX_SAMPLES = 1200              # hard cap per vehicle (180 s at 5 Hz is 900)
COMPACT_AT = 256                # dropped-prefix length that triggers a compaction (bounds list length)
MAX_VEHICLES = 128              # a larger field is not tracked at all
MAX_PACKET_GAP_SECONDS = 1.0    # a longer scoring gap clears the history instead of interpolating over it
CLOCK_RESET_SECONDS = 2.0       # clock moved back by more than this: new session state
BACKSTEP_METERS = 3.0           # distance moved back by more than this: that vehicle's history is discontinuous
PIT_GUARD_SECONDS = 60.0        # no value after pit activity of either car
MAX_AGE_SECONDS = 3.0           # a projected history value must come from scoring at most this old (wall time)
LENGTH_TOLERANCE_METERS = 1.0

# unknown reasons
NO_SESSION = "no_session"
NO_EPOCH = "no_epoch"
NO_TRACK = "no_track_length"
INVALID = "invalid_data"
PIT = "pit"
PIT_WINDOW = "pit_in_window"
NOT_AHEAD = "ahead_not_ahead"
HISTORY = "no_history"
DISCONTINUITY = "discontinuity"


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def count(value, minimum=0):
    return type(value) is int and value >= minimum


class _Track:
    """Samples of one vehicle: parallel lists with a start index; length is bounded by construction."""
    __slots__ = ("t", "s", "head")

    def __init__(self):
        self.t, self.s, self.head = [], [], 0

    def clear(self):
        self.t, self.s, self.head = [], [], 0

    def __len__(self):
        return len(self.t) - self.head

    def push(self, clock, distance):
        self.t.append(clock)
        self.s.append(distance)
        lower = clock - WINDOW_SECONDS
        head = self.head
        while head < len(self.t) - 1 and (self.t[head] < lower or len(self.t) - head > MAX_SAMPLES):
            head += 1
        self.head = head
        if head >= COMPACT_AT:
            self.t, self.s, self.head = self.t[head:], self.s[head:], 0


class IntervalHistory:
    def __init__(self):
        self.stats = Counter()
        self._reset(None)

    def _reset(self, key):
        self.key, self.clock, self.length = key, None, None
        self.tracks = {}
        self.pit_clock = {}                    # vehicle id -> clock of its newest in-pit sample (survives clears)

    def sample_count(self):
        return sum(len(t) for t in self.tracks.values())

    def update(self, *, session_id, epoch, track_length, clock, vehicles, reset=False):
        """Feed one scoring snapshot of one observation.

        Returns the result for this snapshot, or None when it was ignored as a duplicate / slightly older clock
        (the previous result then still describes the same clock).
        """
        def result(pairs=None):
            return dict(session_id=session_id, epoch=epoch, clock=clock if finite(clock) else None, pairs=pairs or {})

        if session_id is None:
            self._reset(None)
            self.stats[NO_SESSION] += 1
            return result()
        if epoch is None:
            # Without a connection generation continuity of the frame sequence cannot be established.
            self._reset(None)
            self.stats[NO_EPOCH] += 1
            return result()
        key = (session_id, epoch)
        if key != self.key or reset:
            self.stats["reset_key" if key != self.key else "reset_requested"] += 1
            self._reset(key)
        if not finite(clock) or clock < 0 or not isinstance(vehicles, (list, tuple)):
            self._reset(key)
            self.stats[INVALID] += 1
            return result()
        if self.clock is not None:
            if clock < self.clock - CLOCK_RESET_SECONDS:
                self.stats["reset_clock"] += 1
                self._reset(key)
            elif clock <= self.clock:
                self.stats["ignored_duplicate"] += 1
                return None
            elif clock - self.clock > MAX_PACKET_GAP_SECONDS:
                self.stats["reset_gap"] += 1
                self.tracks.clear()
        length = track_length if finite(track_length) and track_length > 0 else None
        if length is None:
            self.tracks.clear()
            self.clock, self.length = clock, None
            self.stats[NO_TRACK] += 1
            return result()
        if self.length is not None and abs(length - self.length) > LENGTH_TOLERANCE_METERS:
            self.stats["reset_track"] += 1
            self._reset(key)
        self.clock, self.length = clock, length
        if len(vehicles) > MAX_VEHICLES:
            self.tracks.clear()
            self.stats["field_too_large"] += 1
            return result()

        seen, duplicated, valid = set(), set(), {}
        for v in vehicles:
            vid = v.get("id") if isinstance(v, dict) else None
            if not count(vid):
                continue
            if vid in seen:
                duplicated.add(vid)
            seen.add(vid)
            valid[vid] = v
        for vid in [k for k, c in self.pit_clock.items() if clock - c > WINDOW_SECONDS]:
            del self.pit_clock[vid]
        for vid in [k for k in self.tracks if k not in seen or k in duplicated]:
            del self.tracks[vid]               # identity continuity: absent or ambiguous vehicles lose their past
        usable = {}
        for vid, v in valid.items():
            if vid in duplicated:
                continue
            laps, dist, pit = v.get("total_laps"), v.get("lap_dist"), v.get("in_pits")
            track = self.tracks.get(vid)
            if not count(laps) or not finite(dist) or abs(dist) > 2 * length or type(pit) is not bool:
                self.tracks.pop(vid, None)
                continue
            distance = laps * length + dist
            if track is None:
                track = self.tracks[vid] = _Track()
            elif len(track) and distance < track.s[-1] - BACKSTEP_METERS:
                track.clear()                  # reset / teleport / pit-lane distance switch: past is not comparable
                self.stats["reset_backstep"] += 1
                usable[vid] = DISCONTINUITY
            elif len(track):
                distance = max(distance, track.s[-1])   # only sub-threshold jitter is flattened
            track.push(clock, distance)
            if pit:
                self.pit_clock[vid] = clock
            usable.setdefault(vid, None)
        return result(self._pairs(valid, duplicated, usable, clock))

    # ------------------------------------------------------------------
    def _pairs(self, vehicles, duplicated, usable, clock):
        by_position = {}
        for vid, v in vehicles.items():
            p = v.get("position")
            if count(p, 1):
                by_position.setdefault(p, []).append(vid)
        pairs = {}
        for position, ids in by_position.items():
            ahead_ids = by_position.get(position - 1)
            if position < 2 or len(ids) != 1 or not ahead_ids or len(ahead_ids) != 1:
                continue
            b, a = ids[0], ahead_ids[0]
            vb, va = vehicles[b], vehicles[a]
            deficit = vb.get("laps_behind_leader")
            if not count(deficit, 1) or va.get("laps_behind_leader") != deficit:
                continue                         # phase 1 scope: both lapped, same deficit
            entry = dict(value=None, reason=None, ahead_id=a, position=position, ahead_position=position - 1,
                         deficit=deficit)
            pairs[b] = entry
            entry["reason"] = self._measure(entry, a, b, usable, duplicated, vb, va, clock)
        return pairs

    def _measure(self, entry, a, b, usable, duplicated, vb, va, clock):
        """Fills entry['value'] and returns the reason (None when measured)."""
        ta, tb = self.tracks.get(a), self.tracks.get(b)
        if a in duplicated or b in duplicated or ta is None or tb is None or not len(ta) or not len(tb):
            return INVALID
        if vb["in_pits"] or va["in_pits"] or any(
                clock - self.pit_clock.get(x, -math.inf) <= PIT_GUARD_SECONDS for x in (a, b)):
            return PIT
        if usable.get(a) == DISCONTINUITY or usable.get(b) == DISCONTINUITY:
            return DISCONTINUITY
        sb = tb.s[-1]
        if ta.s[-1] <= sb:
            return NOT_AHEAD
        i = bisect_left(ta.s, sb, ta.head)
        if i <= ta.head or i >= len(ta.s):
            return HISTORY                       # A's pass of this distance is not inside its contiguous history
        s0, s1 = ta.s[i - 1], ta.s[i]
        t0, t1 = ta.t[i - 1], ta.t[i]
        if self.pit_clock.get(a, -math.inf) >= t0:
            return PIT_WINDOW
        if not s1 > s0:
            return HISTORY
        value = clock - (t0 + (t1 - t0) * (sb - s0) / (s1 - s0))
        if not (finite(value) and 0 <= value <= WINDOW_SECONDS):
            return HISTORY
        entry["value"] = value
        return None


def observe(state, payload, *, epoch, reset=False):
    """Feed the scoring snapshot of one agent runtime state; keeps the newest result in ``state``."""
    history = state.get("interval_history")
    if history is None:
        history = state["interval_history"] = IntervalHistory()
    result = history.update(session_id=state.get("session_id"), epoch=epoch,
                            track_length=state.get("track_length_m"), clock=payload.get("session_time"),
                            vehicles=payload.get("vehicles") or [], reset=reset)
    if result is not None:
        state["interval_pairs"] = result


def current_pairs(state, session_id, now):
    """Pairs that describe exactly the scoring observation ``state`` currently holds, else {}.

    The result must belong to the same session, the same Pitwall source epoch and the same session clock as the
    stored scoring, and that scoring must be recent. Anything else (older result, other epoch, stale feed) yields
    nothing, so a history value can never be shown with positions of a different observation.
    """
    result, scoring = state.get("interval_pairs"), state.get("scoring") or {}
    epoch = state.get("scoring_epoch")
    if (not result or session_id is None or epoch is None or state.get("session_id") != session_id
            or result.get("session_id") != session_id or result.get("epoch") != epoch
            or not finite(result.get("clock")) or result["clock"] != scoring.get("session_time")
            or not 0 <= now - state.get("last_scoring_at", -1e9) <= MAX_AGE_SECONDS):
        return {}
    return result["pairs"]
