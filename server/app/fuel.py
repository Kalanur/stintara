"""Experimental lap fuel checkpoints, not proven start/finish-line measurements."""
import math
from collections import deque
from statistics import median

FUEL_SAMPLE_WINDOW = 5
FUEL_MIN_SAMPLES = 3
FUEL_DIAGNOSTIC_TRANSITIONS = 12
FUEL_FRESH_SECONDS = 15


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def nonnegative(value):
    return value if finite_number(value) and value >= 0 else None


def checkpoint_transition(previous, current):
    before, after = nonnegative(previous.get("fuel_remaining_l")), nonnegative(current.get("fuel_remaining_l"))
    delta = before - after if before is not None and after is not None else None
    if delta is not None and not math.isfinite(delta):
        delta = None
    # Priority is part of the public diagnostic contract.
    if current["lap_number"] != previous["lap_number"] + 1:
        reason = "non_consecutive_laps"
    elif delta is None:
        reason = "missing_or_invalid_fuel"
    elif previous.get("pitstops") != current.get("pitstops"):
        reason = "pitstop_transition"
    elif previous.get("in_pits") or current.get("in_pits"):
        reason = "pit_context"
    elif delta <= 0:
        reason = "fuel_not_decreasing"
    else:
        reason = None
    return {"from_lap": previous["lap_number"], "to_lap": current["lap_number"],
            "consumption_l": delta, "included": reason is None,
            "used_for_estimate": False, "excluded_reason": reason}


def consumption_estimate(checkpoints):
    ordered = sorted(checkpoints, key=lambda row: row["lap_number"])
    window = deque(maxlen=FUEL_SAMPLE_WINDOW)
    diagnostics = deque(maxlen=FUEL_DIAGNOSTIC_TRANSITIONS)
    total = 0
    for previous, current in zip(ordered, ordered[1:]):
        transition = checkpoint_transition(previous, current)
        diagnostics.append(transition)
        if transition["included"]:
            window.append(transition)
            total += 1
    consumption = None
    if len(window) >= FUEL_MIN_SAMPLES:
        values = sorted(sample["consumption_l"] for sample in window)
        consumption = median(values)
        if not math.isfinite(consumption):
            # Avoid overflow in the even-sized median's addition; no outlier filtering.
            low, high = values[len(values) // 2 - 1:len(values) // 2 + 1]
            consumption = low + (high - low) / 2
        for sample in window:
            sample["used_for_estimate"] = True
    return {"estimate": {"status": "ready" if consumption is not None else "collecting",
                         "consumption_l_per_lap": consumption,
                         "sample_count": len(window) if consumption is not None else 0,
                         "available_sample_count": len(window),
                         "minimum_samples": FUEL_MIN_SAMPLES, "window_size": FUEL_SAMPLE_WINDOW,
                         "valid_sample_count_total": total, "estimated_range_laps": None},
            "samples": list(diagnostics)}


def fuel_report(checkpoints, strategy, now):
    result = consumption_estimate(checkpoints)
    strategy = strategy or {}
    recorded_at = strategy.get("recorded_at")
    fresh = recorded_at is not None and 0 <= (now - recorded_at).total_seconds() <= FUEL_FRESH_SECONDS
    fuel = nonnegative(strategy.get("fuel_l"))
    result["current"] = {"fuel_l": fuel, "capacity_l": nonnegative(strategy.get("fuel_capacity_l")),
                         "recorded_at": recorded_at, "fresh": fresh}
    consumption = result["estimate"]["consumption_l_per_lap"]
    if fresh and fuel is not None and consumption is not None and consumption > 0:
        distance = fuel / consumption
        result["estimate"]["estimated_range_laps"] = distance if math.isfinite(distance) else None
    # Comparison values cannot affect the independent estimate above.
    fields = {"avg_consumption_l_per_lap": "fuel_avg_consumption_lap", "laps_remaining": "fuel_laps_remaining",
              "stint_number": "fuel_stint_number", "stint_laps": "fuel_stint_laps",
              "stint_consumption_l": "fuel_stint_consumption_l"}
    result["provider"] = {key: strategy.get(source) if finite_number(strategy.get(source)) else None
                          for key, source in fields.items()}
    return result
