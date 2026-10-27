"""LMU Pitwall wire adapter; no identity, session matching or database logic."""
import math

MESSAGE_TYPES = {
    "ConnectionStatus": "connection", "SessionInfo": "session_info",
    "ScoringUpdate": "scoring", "AllDriversUpdate": "lap_snapshots",
    "TelemetryUpdate": "telemetry",
}


def energy_fraction(value):
    return value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value) and 0 <= value <= 1) else None


def clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def wheel(value, snapshot=False):
    result = dict(value)
    # Pitwall forwards native mWear unchanged, including in lap snapshots.
    result["remaining"] = result.pop("wear", None)
    if snapshot:
        temps = result.pop("surface_temp", []) or []
        for key, temp in zip(("temp_inner", "temp_mid", "temp_outer"), temps):
            result[key] = temp
    for key in ("temp_inner", "temp_mid", "temp_outer", "carcass_temp", "brake_temp"):
        if isinstance(result.get(key), (int, float)) and result[key] <= -200:
            result[key] = None
    # Zero pressure with missing surface sensors is a pre-drive sentinel. Keep
    # zero when there is a real temperature or an explicit damaged-wheel flag.
    pressure = result.get("pressure")
    if (isinstance(pressure, (int, float)) and pressure <= 0
            and not result.get("flat") and not result.get("detached")
            and all(result.get(key) is None for key in ("temp_inner", "temp_mid", "temp_outer"))):
        result["pressure"] = None
    return result


def normalize(message):
    if not isinstance(message, dict):
        return None
    kind = MESSAGE_TYPES.get(message.get("type"))
    if kind is None:
        return None
    payload = clean({k: v for k, v in message.items() if k != "type"})
    if kind == "session_info":
        # Pitwall can emit empty metadata during session teardown. Ignore the
        # whole snapshot so the server retains its last valid session metadata.
        track_name = payload.get("track_name")
        if not isinstance(track_name, str) or not track_name.strip():
            return None
    elif kind == "scoring":
        for vehicle in payload.get("vehicles", []):
            # Pitwall uses mID here: keep it as id, never as a race number.
            vehicle["car_number"] = None
            vehicle["energy_current_fraction"] = energy_fraction(vehicle.pop("virtual_energy", None))
    elif kind == "telemetry":
        available = payload.pop("ve_available", None)
        payload["energy_available"] = available if type(available) is bool else None
        payload["energy_kind"] = "VIRTUAL_ENERGY" if available is True else "NONE" if available is False else None
        history = payload.pop("ve_history", None)
        # Pitwall's final entry is the evolving current level, not a completed lap.
        payload["energy_history"] = [energy_fraction(v) for v in history[:-1]][-512:] if isinstance(history, list) else []
        payload["tires"] = [wheel(t) for t in payload.get("tires", [])]
        for key in ("water_temp", "oil_temp"):
            if isinstance(payload.get(key), (int, float)) and payload[key] <= -200:
                payload[key] = None
    elif kind == "lap_snapshots":
        for driver in payload.get("drivers", []):
            has_telemetry = driver.get("has_telemetry", False)
            driver["wheels"] = [wheel(w, True) for w in driver.get("wheels", [])] if has_telemetry else []
            driver["damage"] = {"dent_severity": driver.pop("dent_severity", [])} if has_telemetry else None
            if not has_telemetry:
                for key in ("fuel_remaining", "fuel_capacity", "tire_compound_front_name", "tire_compound_rear_name"):
                    driver[key] = None
            # Pitwall sector2 is cumulative S1+S2; the domain stores individual sectors.
            s1, s2 = driver.get("last_sector1"), driver.get("last_sector2")
            driver["last_sector2"] = s2 - s1 if s1 is not None and s2 is not None and 0 < s1 < s2 else None
            for key in ("last_lap_time", "best_lap_time", "last_sector1", "last_sector2", "last_sector3"):
                if driver.get(key) is not None and driver[key] <= 0:
                    driver[key] = None
    return kind, payload
