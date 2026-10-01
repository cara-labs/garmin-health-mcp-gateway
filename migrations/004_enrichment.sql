-- Append-only source observations. A failed refresh never replaces last good data.
CREATE TABLE analysis_observations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    resource text NOT NULL CHECK (resource IN ('weather_recorded','training_profile')),
    activity_id bigint REFERENCES activities(garmin_activity_id),
    collected_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    observed_at timestamptz,
    effective_at timestamptz,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    CHECK ((resource='training_profile') = (activity_id IS NULL))
);
CREATE INDEX analysis_observations_latest
    ON analysis_observations(resource,activity_id,collected_at DESC);
CREATE TABLE enrichment_state (
    resource text NOT NULL,
    activity_key bigint NOT NULL DEFAULT 0 CHECK (activity_key>=0),
    status text NOT NULL CHECK (status IN
        ('success','partial','pending','unsupported','not_recorded','error')),
    last_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_success_at timestamptz,
    retry_after timestamptz,
    error_code text,
    details jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY(resource,activity_key)
);
CREATE INDEX enrichment_retry ON enrichment_state(resource,retry_after,activity_key);
CREATE TABLE weather_cache (
    cache_key text PRIMARY KEY,
    collected_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    observed_at timestamptz NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object')
);
