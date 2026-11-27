"""Small numeric helpers: percentiles and ground-plane dispersion (radial, longitudinal, lateral)."""
import math
import statistics


def percentile(sorted_values, fraction):
    """Linear-interpolated percentile of an ascending list; None when empty."""
    if not sorted_values:
        return None
    position = fraction * (len(sorted_values) - 1)
    low = math.floor(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def summary(values):
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "median": None, "p90": None, "max": None}
    return {"n": len(ordered), "median": percentile(ordered, 0.5), "p90": percentile(ordered, 0.9), "max": ordered[-1]}


def spread(values):
    ordered = sorted(abs(v) for v in values)
    result = summary(ordered)
    result["std"] = statistics.pstdev(values) if len(values) > 1 else (0.0 if values else None)
    return result


def dispersion(points):
    """points: [(gx, gy, dir_x, dir_y)]. Spread about the per-axis median centre.

    Radial is plain distance to the centre. Longitudinal/lateral project the offsets on the mean direction of travel
    (circular mean of the unit vectors); lateral is that direction rotated by +90 degrees. Both are reported as
    absolute-value summaries plus a standard deviation. The centre and the direction are *descriptive statistics of
    this sample*, not a surveyed start/finish line.
    """
    if not points:
        return {"n": 0, "centre": None, "radial_m": summary([]), "longitudinal_m": spread([]),
                "lateral_m": spread([]), "direction_spread_deg": None}
    centre = (statistics.median(p[0] for p in points), statistics.median(p[1] for p in points))
    sx, sy = sum(p[2] for p in points), sum(p[3] for p in points)
    norm = math.hypot(sx, sy)
    if norm < 1e-9:
        mean = None
        longitudinal = lateral = None
    else:
        mean = (sx / norm, sy / norm)
        longitudinal = [(p[0] - centre[0]) * mean[0] + (p[1] - centre[1]) * mean[1] for p in points]
        lateral = [-(p[0] - centre[0]) * mean[1] + (p[1] - centre[1]) * mean[0] for p in points]
    radial = [math.hypot(p[0] - centre[0], p[1] - centre[1]) for p in points]
    angle = None
    if mean is not None:
        angle = max(math.degrees(math.acos(max(-1.0, min(1.0, p[2] * mean[0] + p[3] * mean[1])))) for p in points)
    return {"n": len(points), "centre": [centre[0], centre[1]], "radial_m": summary(radial),
            "longitudinal_m": spread(longitudinal) if longitudinal is not None else None,
            "lateral_m": spread(lateral) if lateral is not None else None,
            "direction_spread_deg": angle}
