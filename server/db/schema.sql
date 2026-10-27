CREATE TABLE IF NOT EXISTS drivers (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    display_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS driver_identities (
    id uuid PRIMARY KEY,
    driver_id uuid NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    provider text NOT NULL,
    external_id text NOT NULL,
    current_display_name text,
    current_handle text,
    verified boolean NOT NULL DEFAULT false,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(provider, external_id)
);

CREATE TABLE IF NOT EXISTS agents (
    id uuid PRIMARY KEY,
    driver_id uuid NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    name text NOT NULL UNIQUE,
    token_sha256 text NOT NULL UNIQUE,
    enabled boolean NOT NULL DEFAULT true,
    last_seen_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id uuid PRIMARY KEY,
    simulator_provider text NOT NULL,
    official boolean NOT NULL DEFAULT false,
    external_event_id text,
    name text,
    track_name text,
    scheduled_start timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_events_external
    ON events(simulator_provider, external_event_id)
    WHERE external_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS races (
    id uuid PRIMARY KEY,
    event_id uuid REFERENCES events(id) ON DELETE SET NULL,
    simulator_provider text NOT NULL,
    official boolean NOT NULL DEFAULT false,
    external_race_id text,
    split_number integer,
    scheduled_start timestamptz,
    identity_status text NOT NULL DEFAULT 'pending',
    identity_confidence real,
    fingerprint text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_races_external
    ON races(simulator_provider, external_race_id)
    WHERE external_race_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sessions (
    id uuid PRIMARY KEY,
    race_id uuid REFERENCES races(id) ON DELETE SET NULL,
    simulator_provider text NOT NULL,
    external_session_id text,
    session_type text NOT NULL,
    track_name text,
    track_length_m double precision,
    game_version text,
    session_laps integer,
    session_minutes double precision,
    status text NOT NULL DEFAULT 'live',
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_sessions_live ON sessions(status, started_at DESC);

CREATE TABLE IF NOT EXISTS session_observations (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    agent_id uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    UNIQUE(session_id, agent_id)
);

CREATE TABLE IF NOT EXISTS session_participants (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    driver_id uuid REFERENCES drivers(id) ON DELETE SET NULL,
    provider text NOT NULL,
    external_driver_id text,
    local_vehicle_id integer NOT NULL,
    display_name text NOT NULL,
    team_name text,
    car_name text,
    car_class text,
    car_number integer,
    position integer,
    total_laps integer NOT NULL DEFAULT 0,
    last_lap_time double precision,
    best_lap_time double precision,
    gap_to_leader double precision,
    laps_behind_leader integer,
    in_pits boolean NOT NULL DEFAULT false,
    pitstops integer NOT NULL DEFAULT 0,
    finish_status integer NOT NULL DEFAULT 0,
    has_telemetry boolean NOT NULL DEFAULT false,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(session_id, local_vehicle_id)
);
CREATE INDEX IF NOT EXISTS ix_participants_driver ON session_participants(driver_id);

CREATE TABLE IF NOT EXISTS laps (
    id uuid PRIMARY KEY,
    session_participant_id uuid NOT NULL REFERENCES session_participants(id) ON DELETE CASCADE,
    lap_number integer NOT NULL,
    position integer,
    lap_time double precision,
    sector1 double precision,
    sector2 double precision,
    sector3 double precision,
    gap_to_leader double precision,
    gap_ahead double precision,
    fuel_remaining_l double precision,
    fuel_capacity_l double precision,
    compound_front text,
    compound_rear text,
    tyre_remaining_fl double precision,
    tyre_remaining_fr double precision,
    tyre_remaining_rl double precision,
    tyre_remaining_rr double precision,
    in_pits boolean NOT NULL DEFAULT false,
    pitstops integer NOT NULL DEFAULT 0,
    finish_status integer NOT NULL DEFAULT 0,
    has_telemetry boolean NOT NULL DEFAULT false,
    source text NOT NULL DEFAULT 'live',
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(session_participant_id, lap_number)
);

CREATE TABLE IF NOT EXISTS strategy_samples (
    id uuid PRIMARY KEY,
    session_participant_id uuid NOT NULL REFERENCES session_participants(id) ON DELETE CASCADE,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    session_time double precision,
    speed_ms double precision,
    rpm double precision,
    gear integer,
    fuel_l double precision,
    fuel_capacity_l double precision,
    fuel_avg_consumption_lap double precision,
    fuel_laps_remaining double precision,
    fuel_stint_number integer,
    fuel_stint_laps integer,
    fuel_stint_consumption_l double precision,
    water_temp_c double precision,
    oil_temp_c double precision,
    tyre_remaining_fl double precision,
    tyre_remaining_fr double precision,
    tyre_remaining_rl double precision,
    tyre_remaining_rr double precision,
    tyre_pressure_fl_kpa double precision,
    tyre_pressure_fr_kpa double precision,
    tyre_pressure_rl_kpa double precision,
    tyre_pressure_rr_kpa double precision,
    tyre_temp_fl_c double precision,
    tyre_temp_fr_c double precision,
    tyre_temp_rl_c double precision,
    tyre_temp_rr_c double precision
);
CREATE INDEX IF NOT EXISTS ix_strategy_samples_participant_time
    ON strategy_samples(session_participant_id, recorded_at);

-- Additive compatibility changes for existing V0.1 databases.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS weather jsonb NOT NULL DEFAULT '{}';
-- Optional provider-normalized energy context on the existing 1 Hz sample.
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS energy_current_fraction double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS energy_available boolean;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS energy_kind text;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS energy_history jsonb;
ALTER TABLE session_observations ADD COLUMN IF NOT EXISTS last_seen_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE session_observations ADD COLUMN IF NOT EXISTS last_session_time double precision;
ALTER TABLE laps ADD COLUMN IF NOT EXISTS tyre_snapshot jsonb;
ALTER TABLE laps ADD COLUMN IF NOT EXISTS damage jsonb;
CREATE INDEX IF NOT EXISTS ix_observations_agent ON session_observations(agent_id);
CREATE INDEX IF NOT EXISTS ix_observations_active ON session_observations(last_seen_at) WHERE ended_at IS NULL;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_temp_inner_fl_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_temp_mid_fl_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_temp_outer_fl_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_brake_temp_fl_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_temp_inner_fr_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_temp_mid_fr_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_temp_outer_fr_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_brake_temp_fr_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_temp_inner_rl_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_temp_mid_rl_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_temp_outer_rl_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_brake_temp_rl_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_temp_inner_rr_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_temp_mid_rr_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_temp_outer_rr_c double precision;
ALTER TABLE strategy_samples ADD COLUMN IF NOT EXISTS tyre_brake_temp_rr_c double precision;
