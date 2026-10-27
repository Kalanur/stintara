"""Pure simulator-neutral next-stop resource math. Quantities retain resource units."""
import math
from enum import StrEnum
from statistics import median

from .fuel import finite_number, nonnegative


class PitServiceMode(StrEnum):
    TARGET_LEVEL = "TARGET_LEVEL"
    ADD_AMOUNT = "ADD_AMOUNT"
    FIXED = "FIXED"
    UNSUPPORTED = "UNSUPPORTED"


class EnergyKind(StrEnum):
    NONE = "NONE"
    VIRTUAL_ENERGY = "VIRTUAL_ENERGY"
    HYBRID = "HYBRID"
    ERS = "ERS"


def fraction(value):
    return value if finite_number(value) and 0 <= value <= 1 else None


def energy_estimate(history):
    # Provider-normalized completed checkpoint levels, oldest first. Do not
    # bridge malformed checkpoints or treat refills as negative consumption.
    samples = []
    for before, after in zip(history, history[1:]):
        a, b = fraction(before), fraction(after)
        if a is not None and b is not None and a > b:
            samples.append(a - b)
    window = samples[-5:]
    return dict(consumption_per_lap=median(window) if len(window) >= 3 else None,
                sample_count=len(window), minimum_samples=3, window_size=5)


def resource(kind, current, capacity, consumption, fresh, samples, units, **capabilities):
    current, capacity, consumption = map(nonnegative, (current, capacity, consumption))
    distance = current / consumption if fresh and current is not None and consumption else None
    distance = nonnegative(distance)
    return dict(resource_kind=kind, current=current, capacity=capacity, consumption_per_lap=consumption,
                range_laps=distance, fresh=fresh, sample_count=samples, units=units,
                available=distance is not None, **capabilities)


def finish_projection(completed, timing, lap_times, race=True):
    result = dict(kind="unavailable", finish_lap=None, pace_seconds=None)
    if not race or type(completed) is not int or completed < 0:
        return result
    duration = nonnegative(timing.get("configured_minutes"))
    if duration:
        pace = [v for v in lap_times if finite_number(v) and v > 0][-5:]
        elapsed = nonnegative(timing.get("elapsed_seconds"))
        if len(pace) < 3 or elapsed is None:
            return result
        seconds = duration * 60
        if not math.isfinite(seconds):
            return result
        typical = median(pace)
        if not math.isfinite(typical):
            ordered = sorted(pace)
            low, high = ordered[len(ordered)//2-1:len(ordered)//2+1]
            typical = low + (high - low) / 2
        remaining = max(0, seconds - elapsed) / typical
        if not math.isfinite(remaining):
            return result
        return dict(kind="timed_estimate", finish_lap=completed + math.ceil(remaining) + 1,
                    pace_seconds=typical)
    laps = timing.get("configured_laps")
    if type(laps) is int and laps > 0:
        result.update(kind="exact", finish_lap=laps)
    return result


def plan_resources(resources, completed, finish, *, manual_stop=None, active=True, capability_known=True):
    """No provider names/settings participate in the core formulas."""
    result = dict(status="unavailable", completed_laps=completed, finish_projection=finish,
                  reserve_laps=1.0, resources=resources, limiting_resource=None, effective_range_laps=None,
                  latest_safe_stop_lap=None, planned_stop_lap=None, planned_stop_safe=None,
                  shortfall=[], at_pit=[], next_stint_requirements=[], additional_stop_required=False,
                  plan_source="manual" if manual_stop is not None else "auto")
    if not active:
        result["status"] = "inactive"
        return result
    if not capability_known:
        result["status"] = "capability_unknown"
        return result
    if not resources or any(not r["available"] for r in resources):
        result["status"] = "collecting_or_stale"
        return result
    if type(completed) is not int or completed < 0:
        return result
    ranges = [r["range_laps"] for r in resources]
    distance = min(ranges)
    result.update(effective_range_laps=distance,
                  limiting_resource=resources[ranges.index(distance)]["resource_kind"])
    # Balanced is a presentation hint only; never replaces the true minimum.
    result["balanced"] = len(ranges) > 1 and max(ranges) - min(ranges) <= .1
    end = finish["finish_lap"]
    if end is None:
        result["status"] = "finish_unknown"
        return result
    safe = completed + max(0, math.floor(distance - result["reserve_laps"]))
    result["latest_safe_stop_lap"] = min(end, safe)
    if manual_stop is not None and (type(manual_stop) is not int or not completed < manual_stop < end):
        result["status"] = "invalid_plan"
        return result
    if manual_stop is None and distance >= max(0, end - completed) + 1:
        result.update(status="no_stop_required", planned_stop_safe=True)
        return result
    # An immediate boundary means pit at the end of the current running lap.
    planned = manual_stop if manual_stop is not None else max(completed + 1, min(safe, end))
    if planned >= end:
        result["status"] = "finish_shortfall"
        result["shortfall"] = [dict(resource_kind=r["resource_kind"], laps=max(0, end-completed+1-r["range_laps"]))
                               for r in resources if r["range_laps"] < end-completed+1]
        return result
    result["planned_stop_lap"] = planned
    for r in resources:
        until = planned - completed
        at_pit = r["current"] - until * r["consumption_per_lap"]
        required = (end - planned + 1) * r["consumption_per_lap"]
        if not math.isfinite(at_pit) or not math.isfinite(required):
            return {**result, "status": "unavailable", "at_pit": [], "next_stint_requirements": []}
        result["at_pit"].append(dict(resource_kind=r["resource_kind"], amount=at_pit))
        result["next_stint_requirements"].append(dict(resource_kind=r["resource_kind"], amount=required))
        short = until + 1 - r["range_laps"]
        if short > 0:
            result["shortfall"].append(dict(resource_kind=r["resource_kind"], laps=short))
        if r["capacity"] is not None and required > r["capacity"]:
            result["additional_stop_required"] = True
    result["planned_stop_safe"] = not result["shortfall"]
    result["status"] = "unsafe_plan" if result["shortfall"] else "stop_required"
    if manual_stop is None and safe <= completed:
        result["status"] = "box_this_lap"
    return result
