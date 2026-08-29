CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS daily_health (
    date date PRIMARY KEY,
    resting_hr smallint,
    hrv_average numeric(8,2),
    hrv_status text,
    sleep_minutes integer,
    deep_sleep_minutes integer,
    rem_sleep_minutes integer,
    light_sleep_minutes integer,
    awake_minutes integer,
    sleep_score smallint,
    body_battery_high smallint,
    body_battery_low smallint,
    average_stress smallint,
    training_readiness smallint,
    training_readiness_level text,
    training_status text,
    acute_training_load numeric(10,2),
    load_focus jsonb,
    recovery_hours numeric(8,2),
    vo2max numeric(6,2),
    running_vo2max numeric(6,2),
    fitness_age numeric(6,2),
    lactate_threshold_hr smallint,
    lactate_threshold_pace_seconds_per_km numeric(8,2),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS daily_activity (
    date date PRIMARY KEY,
    steps integer,
    distance_m numeric(12,2),
    active_calories integer,
    floors integer,
    moderate_intensity_minutes integer,
    vigorous_intensity_minutes integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS activities (
    garmin_activity_id bigint PRIMARY KEY,
    activity_name text,
    activity_type text NOT NULL,
    start_time timestamptz NOT NULL,
    duration_seconds numeric(12,2),
    moving_duration_seconds numeric(12,2),
    distance_m numeric(14,2),
    calories integer,
    average_hr smallint,
    max_hr smallint,
    average_pace_seconds_per_km numeric(10,2),
    best_pace_seconds_per_km numeric(10,2),
    average_cadence numeric(8,2),
    elevation_gain_m numeric(10,2),
    elevation_loss_m numeric(10,2),
    aerobic_training_effect numeric(5,2),
    anaerobic_training_effect numeric(5,2),
    training_load numeric(10,2),
    recovery_hours numeric(8,2),
    fit_file_path text,
    fit_download_status text NOT NULL DEFAULT 'pending'
        CHECK (fit_download_status IN ('pending', 'archived', 'error')),
    fit_download_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_daily_health_date_desc
    ON daily_health (date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_activity_date_desc
    ON daily_activity (date DESC);
CREATE INDEX IF NOT EXISTS idx_activities_start_time_desc
    ON activities (start_time DESC);
CREATE INDEX IF NOT EXISTS idx_activities_type_start_time_desc
    ON activities (activity_type, start_time DESC);

CREATE TABLE IF NOT EXISTS sync_state (
    resource text PRIMARY KEY,
    last_successful_sync timestamptz,
    last_attempt timestamptz,
    status text NOT NULL CHECK (status IN ('never', 'running', 'success', 'error')),
    error_message text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO sync_state(resource, status)
VALUES ('daily_health', 'never'), ('activities', 'never')
ON CONFLICT (resource) DO NOTHING;
