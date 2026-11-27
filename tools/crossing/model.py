"""Simulator-independent observation types for the offline crossing-evidence detector.

Nothing here knows LMU, Pitwall or Stintara's domain model. An adapter (see ``lmu_raw``) translates a transport format
into these events; the detector never sees raw payloads, driver names or provider field names.

Clocks are kept apart on purpose:

* ``rec_t``           recorder time: seconds since the recording started (transport clock).
* ``session_time``    scoring clock: elapsed session time carried by scoring snapshots.
* ``et``              telemetry clock: elapsed time carried by telemetry samples.
* ``lap_start_et``    the elapsed time at which the current lap began, in the same unit as ``et``.

Ground coordinates ``gx``/``gy`` span the horizontal plane; ``gz`` is height.
"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VehicleObs:
    key: int
    laps: int | None
    lap_start_et: float | None
    lap_dist: float | None
    gx: float | None
    gy: float | None
    in_pits: bool | None


@dataclass(frozen=True, slots=True)
class ScoringObs:
    rec_t: float
    session_time: float
    session_kind: str | None
    player_key: int | None  # None: the transport named no player vehicle
    vehicles: tuple[VehicleObs, ...]


@dataclass(frozen=True, slots=True)
class TelemetryObs:
    """One telemetry sample. The wire format carries no vehicle identity for it."""
    rec_t: float
    et: float
    lap_start_et: float | None
    gx: float
    gy: float
    gz: float | None
    speed: float | None
    vx: float | None
    vy: float | None


@dataclass(frozen=True, slots=True)
class SessionMeta:
    rec_t: float
    track_key: str
    track_length: float


@dataclass(frozen=True, slots=True)
class PhaseObs:
    rec_t: float
    state: str  # green, formation, pre_session, full_course_yellow, session_stopped, session_over, unknown


@dataclass(frozen=True, slots=True)
class LinkObs:
    rec_t: float
    connected: bool
