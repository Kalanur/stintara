"""Wholly invented scenarios, not evidence of actual LMU semantics or frequency."""
from .common import Parser, run_cli
from .format import header, write

NAMES = ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot")


def record(t, kind, **payload):
    return dict(record_type="message", t=t, message=dict(type=kind, **payload))


def scenario(name="demo-race"):
    if name == "session-transition":
        return header(), [record(0, "ConnectionStatus", game_connected=True, telemetry_valid=True),
            record(0, "SessionInfo", track_name="Synthetic Circuit A"),
            record(1, "SessionInfo", track_name=""),
            record(2, "SessionInfo", track_name="Synthetic Circuit B")]
    if name != "demo-race":
        raise ValueError("Unknown scenario")
    rows = [record(0, "ConnectionStatus", game_connected=True, telemetry_valid=True),
            record(0, "SessionInfo", track_name="Synthetic Circuit A", track_length=5000, session_laps=12)]
    for lap, fuel in enumerate((50, 47, 44, 41, 38, 70, 67, 64, 61), 1):
        t = (lap-1)*2.0
        pit = lap in (5, 6)
        vehicles = [dict(id=i+1, driver_name="Driver "+name, team_name=f"Synthetic Team {i+1}",
                         car_name=f"Synthetic Entry {i+1}", vehicle_class="Synthetic Class",
                         position=([2, 1, 3, 4, 6, 5] if lap >= 4 else [1, 2, 3, 4, 5, 6])[i],
                         total_laps=lap, last_lap_time=90+i, best_lap_time=89+i, in_pits=pit and i == 0)
                    for i, name in enumerate(NAMES)]
        rows.append(record(t, "ScoringUpdate", session_type="Race", session_time=lap*90,
                           player_vehicle_id=1, vehicles=vehicles))
        rows.append(record(t, "AllDriversUpdate", session_type="Race", session_time=lap*90,
                           drivers=[dict(vehicles[0], has_telemetry=True, fuel_remaining=fuel, fuel_capacity=100,
                                         num_pitstops=int(lap >= 6), finish_status=0,
                                         last_sector1=30, last_sector2=60, last_sector3=30,
                                         wheels=[dict(wear=.95, surface_temp=[80, 85, 90])]*4)]))
        rows.append(record(t, "TelemetryUpdate", current_et=lap*90, fuel=fuel, fuel_capacity=100,
                           fuel_avg_consumption=3.2, fuel_laps_remaining=fuel/3.2,
                           tires=[dict(wear=.95, pressure=170, temp_inner=80, temp_mid=85, temp_outer=90)]*4))
    return header(), rows


def cli():
    parser = Parser(description="Generate deterministic, wholly synthetic Pitwall scenarios.")
    parser.add_argument("name", choices=("demo-race", "session-transition"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run_cli(lambda: write(args.output, *scenario(args.name)))


if __name__ == "__main__":
    cli()
