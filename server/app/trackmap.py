"""Simulator-neutral track centerline learning and sector attribution.

A centerline is learned from one vehicle's continuous (lap distance, x, z) samples. Missing track is
never bridged: any invalid sample, pit context, backwards or oversized lap-distance step, or position
step inconsistent with the travelled distance discards the running attempt. A closed track requires a
plausible start/finish wrap (end near the lap end, restart near the lap start, positions that fit the
small arc between them). An incomplete correct map is preferred over a complete-looking wrong one.
"""
import math
from collections import OrderedDict
from statistics import median

MAX_POINTS = 700
MIN_POINTS = 20
MAX_LD_STEP_M = 120.0          # larger lap-distance steps mean unobserved track
BACKWARD_TOLERANCE_M = 5.0
MAX_GAP_M = 60.0               # largest allowed arc between two stored points
STEP_FACTOR, STEP_SLACK_M = 1.5, 10.0   # euclidean step <= travelled * factor + slack
SECTOR_MIN_OBSERVATIONS = 2
SECTOR_MAX_SPREAD_M = 60.0
MAX_TRACKS = 16


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize_lap_distance(lap_dist, length):
    """Providers report slightly negative / over-length values around the line; anything further is invalid."""
    if not finite(lap_dist) or not finite(length) or length <= 0:
        return None
    tolerance = min(150.0, length * 0.03)
    if lap_dist < -tolerance or lap_dist >= length + tolerance:
        return None
    return lap_dist % length


def track_key(provider, name, length):
    if not isinstance(name, str) or not name.strip() or not finite(length) or length <= 0:
        return None
    return (provider, name.strip(), round(length))


class TrackModel:
    """Learned, shareable result for one physical track. Holds no per-vehicle attempt state."""

    def __init__(self, length):
        self.length = float(length)
        self.points = None            # [[x, z], ...] once a lap closed
        self.distances = None         # lap distance per point
        self.sector_observations = {1: [], 2: []}   # boundary S1|S2 and S2|S3, metres
        self.generation = 0

    @property
    def complete(self):
        return self.points is not None

    def sector_boundaries(self):
        """Median of the observations that agree; a lone outlier is ignored, a contradictory majority is unknown."""
        result = []
        for key in (1, 2):
            observed = self.sector_observations[key]
            if not observed:
                return None
            centre = median(observed)
            agreeing = [o for o in observed if abs(o - centre) <= SECTOR_MAX_SPREAD_M / 2]
            if len(agreeing) < SECTOR_MIN_OBSERVATIONS or len(agreeing) * 2 <= len(observed):
                return None
            result.append(median(agreeing))
        return result if 0 < result[0] < result[1] < self.length else None

    def record_sector_boundary(self, boundary, lap_dist):
        distance = normalize_lap_distance(lap_dist, self.length)
        if boundary in self.sector_observations and distance is not None:
            observed = self.sector_observations[boundary]
            observed.append(distance)
            del observed[:-7]
            self.generation += 1

    def point_sectors(self, distances=None, boundaries=None):
        boundaries = boundaries if boundaries is not None else self.sector_boundaries()
        distances = distances if distances is not None else self.distances
        if boundaries is None or distances is None:
            return None
        return [1 if d < boundaries[0] else 2 if d < boundaries[1] else 3 for d in distances]


class TrackStore:
    """Bounded in-memory models keyed by (provider, track name, rounded length). Never shared across tracks."""

    def __init__(self, limit=MAX_TRACKS):
        self.limit = limit
        self.models = OrderedDict()

    def get(self, key, length):
        if key is None:
            return None
        model = self.models.get(key)
        if model is None:
            model = self.models[key] = TrackModel(length)
            while len(self.models) > self.limit:
                self.models.popitem(last=False)
        self.models.move_to_end(key)
        return model

    def clear(self):
        self.models.clear()


class CenterlineLearner:
    """Per-agent attempt state; writes its result into a TrackModel only after a plausible closed lap."""

    def __init__(self, model):
        self.model = model
        self.reset()

    def reset(self):
        self.points = []
        self.distances = []
        self.progress_m = 0.0
        self.last = None            # (ld, x, z) of the last accepted sample
        self.start = None
        self.tail = None            # last accepted sample not yet stored
        self.rejected = None

    @property
    def active(self):
        return self.start is not None

    def progress(self):
        if self.model.complete:
            return 1.0
        return min(0.99, self.progress_m / self.model.length) if self.active else None

    def _discard(self, reason):
        self.reset()
        self.rejected = reason

    def _spacing(self):
        return max(8.0, self.model.length / 500)

    def add(self, lap_dist, x, z, in_pits):
        """Feed one sample of the learning vehicle. Returns True when a closed lap was just accepted."""
        model = self.model
        if model.complete:
            return False
        distance = normalize_lap_distance(lap_dist, model.length)
        if in_pits is not False or distance is None or not finite(x) or not finite(z):
            if self.active:
                self._discard("invalid_sample")
            return False
        spacing = self._spacing()
        if not self.active:
            if distance <= max(2 * spacing, 40.0):
                self.start = (distance, x, z)
                self.last = self.start
                self.points, self.distances = [[x, z]], [distance]
                self.progress_m = distance
            return False
        step = distance - self.last[0]
        if step < -model.length / 2:
            step += model.length          # forward across the line
        elif step > model.length / 2:
            step -= model.length
        wrapped = self.last[0] + step >= model.length
        if step < -BACKWARD_TOLERANCE_M:
            self._discard("backwards")
            return False
        if step > MAX_LD_STEP_M:
            self._discard("gap")
            return False
        travelled = max(step, 0.0)
        if math.dist((x, z), self.last[1:]) > travelled * STEP_FACTOR + STEP_SLACK_M:
            self._discard("teleport")
            return False
        if wrapped:
            return self._close(distance, x, z)
        self.progress_m += max(step, 0.0)
        self.last = (distance, x, z)
        last_stored = self.distances[-1]
        if distance - last_stored >= spacing:
            if len(self.points) >= MAX_POINTS:
                self._discard("too_many_points")
                return False
            self.points.append([x, z])
            self.distances.append(distance)
            self.tail = None
        else:
            self.tail = self.last
        return False

    def _close(self, distance, x, z):
        model = self.model
        spacing = self._spacing()
        end = self.tail or self.last
        if self.tail is not None and self.tail[0] > self.distances[-1]:
            self.points.append([end[1], end[2]])
            self.distances.append(end[0])
        first_ld, first_x, first_z = self.start
        arc = (model.length - self.distances[-1]) + first_ld
        gaps = [b - a for a, b in zip(self.distances, self.distances[1:])]
        plausible = (len(self.points) >= MIN_POINTS and arc <= max(2 * spacing, 40.0) + 40.0
                     and max(gaps, default=MAX_GAP_M + 1) <= MAX_GAP_M
                     and math.dist(self.points[-1], (first_x, first_z)) <= arc * STEP_FACTOR + STEP_SLACK_M)
        if not plausible:
            self._discard("closure_not_plausible")
            return False
        model.points = [[round(px, 1), round(pz, 1)] for px, pz in self.points]
        model.distances = [round(d, 1) for d in self.distances]
        model.generation += 1
        self.reset()
        return True


class SectorTracker:
    """Derives sector boundaries only from observed sector transitions of the learning vehicle."""

    def __init__(self, model):
        self.model = model
        self.previous = None

    def observe(self, sector, lap_dist, in_pits):
        previous, self.previous = self.previous, sector if sector in (1, 2, 3) else None
        if in_pits is not False or previous is None or self.previous is None:
            return
        # Only the forward order S1 -> S2 -> S3 contributes; S3 -> S1 is the start/finish line itself.
        if (previous, self.previous) in ((1, 2), (2, 3)):
            self.model.record_sector_boundary(previous, lap_dist)


def public_geometry(model, learner=None):
    """Compact read-only representation. Closed polylines are explicit; partials never carry a closing edge."""
    if model is None:
        return dict(state="unknown", complete=False, closed=False, progress=None, points=[], point_sectors=None,
                    sector_boundaries_m=None, length_m=None)
    complete = model.complete
    points = model.points if complete else (learner.points if learner and learner.active else [])
    distances = model.distances if complete else (learner.distances if learner and learner.active else [])
    boundaries = model.sector_boundaries()
    return dict(state="complete" if complete else "learning", complete=complete, closed=complete,
                progress=None if complete else (learner.progress() if learner else None),
                points=[list(p) for p in points], length_m=model.length,
                point_sectors=model.point_sectors(distances, boundaries) if points else None,
                sector_boundaries_m=boundaries)


# --- runtime glue (per agent state dict; bounded, nothing here is persisted) --------------------------

VEHICLE_FRESH_SECONDS = 5.0
RACE_CONTROL_FRESH_SECONDS = 5.0
SECTOR_SAMPLE_MAX_AGE_SECONDS = 1.0


def attach_track(state, store, provider, name, length):
    """Select the shared model for this track; a different track drops the vehicle's running attempt."""
    key = track_key(provider, name, length)
    if key == state.get("track_key"):
        return
    state["track_key"] = key
    model = store.get(key, length)
    state["track_model"] = model
    state["track_learner"] = CenterlineLearner(model) if model else None
    state["track_sectors"] = SectorTracker(model) if model else None


def player_vehicle(scoring):
    pid = scoring.get("player_vehicle_id")
    return next((v for v in scoring.get("vehicles", []) if v.get("id") == pid), None) if pid is not None else None


def feed_scoring(state, scoring, reset=False):
    learner, model = state.get("track_learner"), state.get("track_model")
    if learner is None or model is None:
        return
    if reset:
        learner.reset()
        if state.get("track_sectors"):
            state["track_sectors"].previous = None
    vehicle = player_vehicle(scoring)
    if vehicle is None:
        learner.reset()
        return
    learner.add(vehicle.get("lap_dist"), vehicle.get("pos_x"), vehicle.get("pos_z"), vehicle.get("in_pits"))


def feed_race_control(state, payload, now):
    state["race_control"], state["race_control_at"] = payload, now
    tracker, scoring = state.get("track_sectors"), state.get("scoring", {})
    vehicle = player_vehicle(scoring)
    if tracker is None:
        return
    fresh = vehicle is not None and now - state.get("last_scoring_at", 0) <= SECTOR_SAMPLE_MAX_AGE_SECONDS
    tracker.observe(payload.get("player_sector"), vehicle.get("lap_dist") if fresh else None,
                    vehicle.get("in_pits") if fresh else None)


def public_race_control(state, now):
    payload = state.get("race_control")
    if not payload:
        return None
    age = now - state.get("race_control_at", 0)
    return dict(payload, age_s=round(age, 2), fresh=0 <= age <= RACE_CONTROL_FRESH_SECONDS)


def track_map_payload(state, participants, now):
    """Read-only current spatial state for the Engineer. Stale vehicles are flagged, never presented as live."""
    if not state:
        return dict(geometry=public_geometry(None), vehicles=[], vehicles_age_s=None, vehicles_fresh=False,
                    race_control=None)
    age = now - state.get("last_scoring_at", 0)
    by_local = {p.get("local_vehicle_id"): p for p in participants}
    vehicles = []
    for v in state.get("scoring", {}).get("vehicles", []):
        x, z = v.get("pos_x"), v.get("pos_z")
        if not (finite(x) and finite(z)):
            continue
        p = by_local.get(v.get("id"), {})
        vehicles.append(dict(participant_id=p.get("id"), x=round(x, 1), z=round(z, 1), in_pits=bool(v.get("in_pits")),
                             position=v.get("position"), name=p.get("display_name")))
    return dict(geometry=public_geometry(state.get("track_model"), state.get("track_learner")),
                vehicles=vehicles, vehicles_age_s=round(age, 2), vehicles_fresh=0 <= age <= VEHICLE_FRESH_SECONDS,
                race_control=public_race_control(state, now))
