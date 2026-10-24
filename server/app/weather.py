"""Read-only weather presentation boundary for persisted provider snapshots."""

import json
import math


def number(value, minimum=None, maximum=None):
    if type(value) not in (int, float):
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        return None
    return value


def normalize_weather(raw, provider):
    """Expose only understood LMU/Pitwall fields in a provider-neutral contract.

    Other providers need an explicit mapping; similar key names alone are not evidence
    of matching units. Invalid fields remain unknown, never clamped or interpolated.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            raw = None
    source = raw if provider == "lmu" and isinstance(raw, dict) else {}
    current = {
        "air_temp_c": number(source.get("air_temp")),
        "track_temp_c": number(source.get("track_temp")),
        "rain_intensity": number(source.get("rain_intensity"), 0, 1),
        "cloudiness": number(source.get("cloudiness"), 0, 1),
        "wetness_avg": number(source.get("avg_path_wetness"), 0, 1),
        "wetness_min": number(source.get("min_path_wetness"), 0, 1),
        "wetness_max": number(source.get("max_path_wetness"), 0, 1),
        "wind_speed_mps": number(source.get("wind_speed_live"), 0),
    }
    forecast = source.get("forecast")
    forecast = forecast if isinstance(forecast, list) else []
    nodes = []
    for node in forecast:
        if not isinstance(node, dict):
            continue
        direction = number(node.get("wind_direction"), 0, 7)
        normalized = {
            "position_percent": None,
            "temperature_c": number(node.get("temperature")),
            "rain_chance": number(node.get("rain_chance"), 0, 1),
            "humidity_percent": number(node.get("humidity"), 0, 100),
            "wind_speed_mps": number(node.get("wind_speed"), 0),
            "wind_direction": ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[int(direction)]
            if direction is not None and direction == int(direction) else None,
        }
        # A node needs at least one understood, valid value to be presentable.
        if any(value is not None for value in normalized.values()):
            nodes.append(normalized)
    # Do not recover a timeline by filtering a malformed or oversized source array.
    complete = len(forecast) == len(nodes) == 5
    if complete:
        for index, node in enumerate(nodes):
            node["position_percent"] = index * 25
    return {"current": current, "forecast": {"timeline_complete": complete, "nodes": nodes}}
