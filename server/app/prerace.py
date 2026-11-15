"""Pre-race planning (#66): pure, simulator-neutral race distance and starting-resource estimate.

Nothing here reads or writes the database and nothing is persisted: a plan is a calculation over explicitly
supplied inputs. Every input carries its own origin (manual or observed) and is never replaced by a guess.
Unknown stays unknown: a missing, invalid or contradictory input yields a status and a list of issues, never a
recommendation. Fuel and Virtual Energy are separate resources; a VE-capable car (or one whose capability is
unknown) gets no fuel-only recommendation.

Formulas (documented in docs/ARCHITECTURE.md):
  timed race   laps_min = ceil(T / pace); laps_planning = ceil(T / (pace * (1 - PACE_MARGIN))) + 1
               (+1: the flag falls at the first leader crossing after the clock expires, as in finish_projection)
  lap race     laps_min = laps_planning = configured laps
  fuel         required = (laps_planning + reserve_laps) * fuel_per_lap
Formation, out-lap and in-lap consumption are not modelled; the reserve covers them.
"""
import math
from statistics import median

from .fuel import consumption_estimate, finite_number
from .lmu_resources import best_energy_estimate
from .session_meta import trusted_laps

PACE_MARGIN = 0.02            # planning pace is 2 % faster than the supplied pace: more laps, more fuel
DEFAULT_RESERVE_LAPS = 1.0
MAX_RESERVE_LAPS = 5.0
LAP_TIME_RANGE_S = (20.0, 1200.0)
DURATION_RANGE_MINUTES = (1.0, 1440.0)
LAPS_RANGE = (1, 5000)
FUEL_PER_LAP_RANGE_L = (0.05, 100.0)
VE_PER_LAP_RANGE_PERCENT = (0.05, 100.0)
TANK_RANGE_L = (1.0, 500.0)
PACE_MIN_SAMPLES = 3
PACE_WINDOW = 5
VALUE_FIELDS = ("duration_minutes", "race_laps", "lap_time_s", "fuel_per_lap_l", "ve_per_lap_percent",
                "tank_capacity_l", "reserve_laps")


def number_in(value, low, high):
    return finite_number(value) and low <= value <= high


def ceil_tenth(value):
    return math.ceil(round(value * 10, 9)) / 10


def validate(inputs):
    """Field codes of present-but-unusable values. Absent (None) is not invalid, only missing."""
    bad = []
    checks = {"duration_minutes": DURATION_RANGE_MINUTES, "lap_time_s": LAP_TIME_RANGE_S,
              "fuel_per_lap_l": FUEL_PER_LAP_RANGE_L, "ve_per_lap_percent": VE_PER_LAP_RANGE_PERCENT,
              "tank_capacity_l": TANK_RANGE_L, "reserve_laps": (0.0, MAX_RESERVE_LAPS)}
    for field, (low, high) in checks.items():
        value = inputs.get(field)
        if value is not None and not number_in(value, low, high):
            bad.append(field)
    laps = inputs.get("race_laps")
    if laps is not None and not (type(laps) is int and LAPS_RANGE[0] <= laps <= LAPS_RANGE[1]):
        bad.append("race_laps")
    capable = inputs.get("ve_capable")
    if capable is not None and type(capable) is not bool:
        bad.append("ve_capable")
    return bad


def distance_of(duration_minutes, race_laps, lap_time_s):
    """Race distance in laps. Returns (distance, missing); a timed race without a pace has no lap count."""
    if race_laps is not None:
        return {"kind": "laps", "race_laps": race_laps, "duration_minutes": None, "pace_s": lap_time_s,
                "laps_min": race_laps, "laps_planning": race_laps, "approximate": False}, []
    if duration_minutes is None:
        return {"kind": "unknown", "race_laps": None, "duration_minutes": None, "pace_s": lap_time_s,
                "laps_min": None, "laps_planning": None, "approximate": None}, ["race_length"]
    result = {"kind": "timed", "race_laps": None, "duration_minutes": duration_minutes, "pace_s": lap_time_s,
              "laps_min": None, "laps_planning": None, "approximate": True}
    if lap_time_s is None:
        return result, ["lap_time"]
    seconds = duration_minutes * 60
    result["laps_min"] = math.ceil(seconds / lap_time_s)
    result["laps_planning"] = math.ceil(seconds / (lap_time_s * (1 - PACE_MARGIN))) + 1
    return result, []


def plan_prerace(inputs, sources=None):
    """Plan from explicit inputs. `sources` maps a field to its origin ("manual" / "observed"); echoed back only."""
    inputs = {key: inputs.get(key) for key in (*VALUE_FIELDS, "ve_capable")}
    sources = {key: value for key, value in (sources or {}).items() if key in inputs and inputs[key] is not None}
    reserve = DEFAULT_RESERVE_LAPS if inputs["reserve_laps"] is None else inputs["reserve_laps"]
    result = {"status": "incomplete", "recommendation_available": False, "issues": [], "inputs": inputs,
              "sources": sources, "distance": None, "fuel": None, "energy": None, "start": None,
              "reserve_laps": reserve, "pace_margin": PACE_MARGIN}
    issues = result["issues"]

    def issue(code, field=None, severity="missing"):
        issues.append({"code": code, "field": field, "severity": severity})

    invalid = validate(inputs)
    if invalid:
        for field in invalid:
            issue("invalid_value", field, "invalid")
        result["status"] = "invalid_input"
        return result
    if inputs["duration_minutes"] is not None and inputs["race_laps"] is not None:
        issue("conflicting_race_length", "race_laps", "invalid")
        result["status"] = "conflicting_input"
        return result

    distance, missing = distance_of(inputs["duration_minutes"], inputs["race_laps"], inputs["lap_time_s"])
    result["distance"] = distance
    for code in missing:
        issue(f"missing_{code}", {"race_length": "duration_minutes", "lap_time": "lap_time_s"}[code])
    consumption, tank, capable = inputs["fuel_per_lap_l"], inputs["tank_capacity_l"], inputs["ve_capable"]
    ve_rate = inputs["ve_per_lap_percent"]
    if consumption is None:
        issue("missing_fuel_per_lap", "fuel_per_lap_l")
    if capable is None:
        issue("ve_capability_unknown", "ve_capable")      # unknown is never treated as "no VE"
    elif capable and ve_rate is None:
        issue("missing_ve_per_lap", "ve_per_lap_percent")
    elif capable is False and ve_rate is not None:
        issue("ve_rate_without_ve", "ve_per_lap_percent", "invalid")
        result["status"] = "conflicting_input"
        return result
    if tank is None:
        issue("tank_capacity_unknown", "tank_capacity_l", "warning")
    blocking = [i for i in issues if i["severity"] == "missing"]
    if blocking:
        return result                                       # no scheinbar exakte Empfehlung without a data basis

    laps = distance["laps_planning"]
    required_laps = laps + reserve
    fuel_required = required_laps * consumption
    result["fuel"] = {"per_lap_l": consumption, "laps_min_l": distance["laps_min"] * consumption,
                      "reserve_l": reserve * consumption, "required_l": fuel_required, "tank_capacity_l": tank,
                      "capacity_checked": tank is not None,
                      "full_tank_laps": tank / consumption if tank is not None else None,
                      "single_stint_feasible": fuel_required <= tank if tank is not None else None,
                      "shortfall_l": max(0.0, fuel_required - tank) if tank is not None else None}
    energy_ok = None
    if capable:
        ve_required = required_laps * ve_rate
        energy_ok = ve_required <= 100.0
        result["energy"] = {"capable": True, "per_lap_percent": ve_rate, "required_percent": ve_required,
                            "capacity_percent": 100.0, "full_tank_laps": 100.0 / ve_rate,
                            "single_stint_feasible": energy_ok, "shortfall_percent": max(0.0, ve_required - 100.0)}
        if tank is not None:
            result["energy"]["limiting_resource"] = "fuel" if tank / consumption <= 100.0 / ve_rate else "energy"
    else:
        result["energy"] = {"capable": False}
    feasible = result["fuel"]["single_stint_feasible"]
    if feasible is False or energy_ok is False:
        result["status"] = "stop_required"                 # one start load cannot cover the race; no start value
        return result
    result["status"] = "ready" if feasible else "ready_capacity_unchecked"
    result["recommendation_available"] = True
    result["start"] = {"fuel_l": ceil_tenth(fuel_required),
                       "ve_percent": ceil_tenth(result["energy"]["required_percent"]) if capable else None}
    return result


# ---------------------------------------------------------------- evidence (read-only suggestions, never applied)

def pace_evidence(laps):
    """Median of the last clean lap times of one participant: not in the pits, plausible, at least three."""
    clean = [row["lap_time"] for row in sorted(laps, key=lambda r: r["lap_number"])
             if row.get("lap_number", 0) > 0 and row.get("in_pits") is not True
             and number_in(row.get("lap_time"), *LAP_TIME_RANGE_S)][-PACE_WINDOW:]
    if len(clean) < PACE_MIN_SAMPLES:
        return {"value": None, "sample_count": len(clean), "minimum_samples": PACE_MIN_SAMPLES}
    return {"value": median(clean), "sample_count": len(clean), "minimum_samples": PACE_MIN_SAMPLES}


def prerace_evidence(session, laps, sample, provider):
    """Suggested values for ONE own participant in ONE session. Never merged into a plan automatically.

    `session` is the session row (type, track, confirmed lap limit/duration). Race rules are reported only for a
    Race session: during Qualifying the SessionInfo end clock and lap limit describe Qualifying, and the
    upcoming race's configuration is not provided at all (hence unavailable, not estimated).
    """
    sample = sample or {}
    laps = list(laps or [])
    fuel = consumption_estimate(laps)["estimate"]
    is_race = session.get("session_type") == "Race"
    rules = {"available": False, "race_laps": None, "duration_minutes": None, "source": None}
    if is_race:
        confirmed_laps = trusted_laps(session)
        duration = session.get("session_duration_seconds")
        if confirmed_laps is not None:
            rules.update(available=True, race_laps=confirmed_laps, source="confirmed")
        elif finite_number(duration) and duration > 0:
            rules.update(available=True, duration_minutes=duration / 60, source="confirmed")
    capable = sample.get("energy_available") if type(sample.get("energy_available")) is bool else None
    energy, energy_source = best_energy_estimate(laps, sample) if provider == "lmu" and capable else (None, None)
    capacity = sample.get("fuel_capacity_l")
    return {
        "session_type": session.get("session_type"),
        "representative_of_race": True if is_race else None,   # qualifying pace/fuel are not race pace/fuel
        "race_rules": rules,
        "lap_time": pace_evidence(laps),
        "fuel_per_lap": {"value": fuel["consumption_l_per_lap"], "sample_count": fuel["sample_count"],
                         "minimum_samples": fuel["minimum_samples"]},
        "ve_capable": capable,
        "ve_per_lap_percent": {"value": energy["consumption_per_lap"] * 100
                               if energy and energy["consumption_per_lap"] is not None else None,
                               "sample_count": energy["sample_count"] if energy else 0, "source": energy_source},
        "tank_capacity_l": capacity if number_in(capacity, *TANK_RANGE_L) else None,
    }
