"""LMU presentation/capability mapping, separate from resource planning."""
import math
from decimal import ROUND_CEILING, Decimal, localcontext

from .resource_strategy import (
    EnergyKind,
    PitServiceMode,
    energy_checkpoint_estimate,
    energy_estimate,
    fraction,
    resource,
)


def round_up(value, places=0):
    # Remove only binary arithmetic noise at a setting increment boundary.
    scaled = value * 10 ** places
    if math.isfinite(scaled) and math.isclose(scaled, round(scaled), rel_tol=0, abs_tol=1e-10):
        return round(scaled) / 10 ** places
    number = Decimal(str(value))
    with localcontext() as context:
        context.prec = max(28, number.adjusted() + places + 4)
        return float(number.quantize(Decimal(10) ** -places, rounding=ROUND_CEILING))


def best_energy_estimate(laps, sample):
    """Lap-checkpoint VE is primary. Pitwall ve_history (null in real LMU captures) is only a fallback."""
    estimate = energy_checkpoint_estimate(laps or [])
    if estimate["consumption_per_lap"] is not None:
        return estimate, "lap_checkpoints"
    history = energy_estimate(sample.get("energy_history") or [])
    if history["consumption_per_lap"] is not None:
        return history, "provider_history"
    return estimate, None


def energy_diagnostics(laps, sample):
    estimate, source = best_energy_estimate(laps, sample)
    return dict(source=source, sample_count=estimate["sample_count"], minimum_samples=estimate["minimum_samples"],
                checkpoint_laps=sum(1 for lap in laps or [] if fraction(lap.get("energy_remaining_fraction")) is not None))


def lmu_resources(fuel, sample, laps=()):
    current, estimate = fuel["current"], fuel["estimate"]
    resources = [resource("fuel", current["fuel_l"], current["capacity_l"],
                          estimate["consumption_l_per_lap"], current["fresh"], estimate["sample_count"], "L",
                          pit_service_mode=PitServiceMode.TARGET_LEVEL)]
    available = sample.get("energy_available")
    known = type(available) is bool and (not available or sample.get("energy_kind") == EnergyKind.VIRTUAL_ENERGY)
    if available is not False:
        energy, _ = best_energy_estimate(laps, sample)
        resources.append(resource("energy", fraction(sample.get("energy_current_fraction")), 1.0,
                                  energy["consumption_per_lap"], current["fresh"] and available is True,
                                  energy["sample_count"], "fraction", energy_kind=EnergyKind.VIRTUAL_ENERGY,
                                  pit_service_mode=PitServiceMode.TARGET_LEVEL))
    return resources, known


def pit_settings(plan):
    """Read-only recommendations. Expected added fuel is not an LMU setting."""
    if plan["status"] not in ("stop_required", "unsafe_plan", "box_this_lap"):
        return None
    required = {r["resource_kind"]: r["amount"] for r in plan["next_stint_requirements"]}
    if "fuel" not in required:
        return None
    resources = {r["resource_kind"]: r for r in plan["resources"]}
    result = dict(provider="lmu", fuel_service_mode=PitServiceMode.TARGET_LEVEL,
                  additional_stop_required=plan["additional_stop_required"], capacity_feasible=None)
    fuel_target = required["fuel"]
    if "energy" in resources:
        # Fraction consumption becomes percentage points before division.
        measured = resources["fuel"]["consumption_per_lap"] / (resources["energy"]["consumption_per_lap"] * 100)
        if not math.isfinite(measured):
            return None
        ratio = round_up(measured, 2)
        target = min(100, round_up(required["energy"] * 100))
        fuel_target = target * ratio
        if not math.isfinite(fuel_target):
            return None
        result.update(energy_kind=EnergyKind.VIRTUAL_ENERGY, ve_target_percent=target,
                      measured_fuel_ratio_l_per_percent_ve=measured, fuel_ratio_l_per_percent_ve=ratio,
                      resulting_fuel_target_l=fuel_target)
    else:
        result.update(energy_kind=EnergyKind.NONE, fuel_target_l=fuel_target)
    projected = next(r["amount"] for r in plan["at_pit"] if r["resource_kind"] == "fuel")
    fuel_out = max(projected, fuel_target)  # TARGET_LEVEL does not imply draining excess fuel.
    capacity = resources["fuel"]["capacity"]
    if capacity is not None:
        result["capacity_feasible"] = fuel_out <= capacity
        result["additional_stop_required"] |= fuel_out > capacity
    result["expected_fuel_added_l"] = max(0, fuel_target - projected)
    if not math.isfinite(result["expected_fuel_added_l"]):
        return None
    # Do not present an achievable output with unknown/exceeded capacity or
    # a negative arrival quantity (the car cannot reach that stop as projected).
    result["projected_fuel_out_l"] = fuel_out if result["capacity_feasible"] is True and projected >= 0 else None
    return result
