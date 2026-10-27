"""Stintara transport contracts, independent of provider wire classes."""
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field, FiniteFloat, StrictBool


def numeric_only(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected numeric fraction")
    return value


Fraction = Annotated[FiniteFloat, Field(ge=0, le=1), BeforeValidator(numeric_only)]


class Wheel(BaseModel):
    remaining: FiniteFloat | None = Field(default=None, ge=0, le=1)
    pressure: FiniteFloat | None = None
    temp_inner: FiniteFloat | None = None
    temp_mid: FiniteFloat | None = None
    temp_outer: FiniteFloat | None = None
    brake_temp: FiniteFloat | None = None
    carcass_temp: FiniteFloat | None = None
    flat: bool | None = None
    detached: bool | None = None


class Connection(BaseModel):
    game_connected: bool
    telemetry_valid: bool = True
    plugin_version: str | None = None


class SessionInfo(BaseModel):
    track_name: str = Field(min_length=1, max_length=512)
    track_length: FiniteFloat | None = None
    session_laps: int | None = None
    session_minutes: FiniteFloat | None = None
    weather: dict = Field(default_factory=dict)


class Vehicle(BaseModel):
    energy_current_fraction: Fraction | None = None
    id: int = Field(ge=0)
    driver_name: str = Field(default="Unknown", max_length=512)
    team_name: str | None = None
    car_name: str | None = None
    vehicle_class: str | None = None
    car_number: int | None = None
    position: int | None = None
    total_laps: int = Field(default=0, ge=0)
    last_lap_time: FiniteFloat | None = None
    best_lap_time: FiniteFloat | None = None
    time_behind_leader: FiniteFloat | None = None
    laps_behind_leader: int | None = None
    in_pits: bool = False


class Scoring(BaseModel):
    session_type: str = Field(min_length=1, max_length=128)
    session_time: FiniteFloat | None = None
    player_vehicle_id: int | None = None
    vehicles: list[Vehicle] = Field(default_factory=list, max_length=256)


class Lap(Vehicle):
    last_sector1: FiniteFloat | None = None
    last_sector2: FiniteFloat | None = None
    last_sector3: FiniteFloat | None = None
    gap_to_leader: FiniteFloat | None = None
    gap_ahead: FiniteFloat | None = None
    num_pitstops: int = 0
    finish_status: int = 0
    fuel_remaining: FiniteFloat | None = None
    fuel_capacity: FiniteFloat | None = None
    tire_compound_front_name: str | None = None
    tire_compound_rear_name: str | None = None
    wheels: list[Wheel] = Field(default_factory=list, max_length=4)
    damage: dict | None = None
    has_telemetry: bool = False


class LapSnapshots(BaseModel):
    session_type: str
    session_time: FiniteFloat | None = None
    drivers: list[Lap] = Field(default_factory=list, max_length=256)


class Telemetry(BaseModel):
    energy_available: StrictBool | None = None
    energy_kind: Literal["NONE", "VIRTUAL_ENERGY", "HYBRID", "ERS"] | None = None
    energy_history: list[Fraction | None] = Field(default_factory=list, max_length=512)
    current_et: FiniteFloat | None = None
    speed_ms: FiniteFloat | None = None
    rpm: FiniteFloat | None = None
    gear: int | None = None
    fuel: FiniteFloat | None = None
    fuel_capacity: FiniteFloat | None = None
    fuel_avg_consumption: FiniteFloat | None = None
    fuel_laps_remaining: FiniteFloat | None = None
    fuel_stint_number: int | None = None
    fuel_stint_laps: int | None = None
    fuel_stint_consumption: FiniteFloat | None = None
    water_temp: FiniteFloat | None = None
    oil_temp: FiniteFloat | None = None
    tires: list[Wheel] = Field(default_factory=list, max_length=4)


class Hello(BaseModel):
    type: Literal["agent_hello"]
    protocol: Literal["stintara-agent-v1"]
    token: str = Field(min_length=1, max_length=1024)
    simulator: Literal["lmu"]  # only implemented adapter; domain remains generic


PAYLOADS = {"connection": Connection, "session_info": SessionInfo, "scoring": Scoring,
            "lap_snapshots": LapSnapshots, "telemetry": Telemetry}
