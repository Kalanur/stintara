"""Wholly synthetic stream builder for the crossing-evidence tests. No real capture data is used or derived.

A car drives along +x at constant speed and crosses a line at x = 0 at the given crossing times (seconds on the
session clock, offset by ``OFFSET``). Telemetry is live at 16 Hz. Scoring snapshots come with every telemetry step but,
like the real wire, only their tick fields (counter, lap start, lap distance, session time) change every 0.2 s while
the player position stays live. The lap counter increases on the first tick at or after the crossing.
"""
import math

from tools.crossing.detector import Config, Detector
from tools.crossing.model import PhaseObs, ScoringObs, SessionMeta, TelemetryObs, VehicleObs

DT = 0.0625
OFFSET = 1000.0
LENGTH = 5000.0
PLAYER = 7


def build(crossings=(10.0,), *, end=None, laps0=3, speed=70.0, ls_late_ticks=0, tel_ls_lag=0.0, counter=True,
          in_pits=False, phase="green", lateral=None, tel_keep=None, track=("Test Circuit", LENGTH),
          others=(), kind="Race", tel_shift=(0.0, 0.0), meta=True):
    times = [OFFSET + c for c in crossings]
    lateral = list(lateral) if lateral is not None else [0.0] * len(times)
    horizon = end if end is not None else crossings[-1] + 6.0
    events = [SessionMeta(0.0, track[0], track[1])] if meta else []

    def last(limit):
        past = [t for t in times if t <= limit + 1e-9]
        return max(past) if past else None

    def nearest(moment):
        index = min(range(len(times)), key=lambda k: abs(moment - times[k]))
        return times[index], lateral[index]

    for i in range(int(horizon / DT) + 1):
        rec = i * DT
        et = OFFSET + rec
        tick = int(et * 5 + 1e-9) / 5
        events.append(PhaseObs(rec, phase))
        t_star, _ = nearest(tick)
        distance = (speed * (tick - t_star)) % LENGTH
        t_near, lat = nearest(et)
        x, y = speed * (et - t_near), lat
        laps = laps0 + (sum(1 for t in times if t <= tick + 1e-9) if counter else 0)
        scoring_ls = last(tick - ls_late_ticks * 0.2)
        pit = in_pits(tick - OFFSET) if callable(in_pits) else in_pits
        vehicles = (VehicleObs(PLAYER, laps, scoring_ls if scoring_ls is not None else -1.0, distance, x, y,
                               pit), *others)
        events.append(ScoringObs(rec, tick, kind, PLAYER, vehicles))
        if tel_keep is None or tel_keep(et):
            tel_ls = last(et - tel_ls_lag)
            events.append(TelemetryObs(rec, et, tel_ls if tel_ls is not None else -1.0, x + tel_shift[0],
                                       y + tel_shift[1], 0.0, speed, speed, 0.0))
    return events


def run(events, cfg=None):
    detector = Detector(cfg or Config())
    for event in events:
        detector.feed(event)
    detector.finish()
    return detector, detector.report()


def candidates(report):
    return [c for c in report["crossings"] if c["status"] == "diagnostic_candidate"]


def rejected(report):
    return [c for c in report["crossings"] if c["status"] == "diagnostic_rejected"]


def reasons(report):
    return sorted({r for c in report["crossings"] for r in c["reasons"]})


def crossing_time(seconds):
    return OFFSET + seconds


__all__ = ["build", "run", "candidates", "rejected", "reasons", "crossing_time", "math", "OFFSET", "DT", "LENGTH",
           "PLAYER"]
