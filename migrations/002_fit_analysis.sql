-- Additive: original archives, activity summaries and 1.1.0 readers remain intact.
CREATE TABLE fit_generations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    activity_id bigint NOT NULL REFERENCES activities(garmin_activity_id),
    archive_sha256 text NOT NULL CHECK (archive_sha256 ~ '^[0-9a-f]{64}$'),
    decoder_version text NOT NULL,
    schema_version text NOT NULL,
    status text NOT NULL CHECK (status IN ('staging', 'ready', 'error')),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    error_code text,
    message_count integer NOT NULL DEFAULT 0 CHECK (message_count >= 0),
    archive_bytes bigint NOT NULL CHECK (archive_bytes >= 0),
    UNIQUE (id, activity_id)
);
CREATE INDEX fit_generations_activity_attempt ON fit_generations(activity_id, started_at DESC);

CREATE TABLE fit_processing_state (
    activity_id bigint PRIMARY KEY REFERENCES activities(garmin_activity_id),
    status text NOT NULL CHECK (status IN ('running', 'success', 'error')),
    last_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_success_at timestamptz,
    error_code text
);

CREATE TABLE fit_active_generations (
    activity_id bigint PRIMARY KEY REFERENCES activities(garmin_activity_id),
    generation_id uuid NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (generation_id, activity_id) REFERENCES fit_generations(id, activity_id)
);

CREATE TABLE fit_messages (
    generation_id uuid NOT NULL REFERENCES fit_generations(id) ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    message_number integer NOT NULL,
    message_name text,
    observed_at timestamptz,
    fields jsonb NOT NULL CHECK (jsonb_typeof(fields) = 'array'),
    PRIMARY KEY (generation_id, ordinal)
);

CREATE TABLE fit_samples (
    generation_id uuid NOT NULL,
    ordinal integer NOT NULL,
    observed_at timestamptz,
    elapsed_seconds double precision,
    timer_seconds double precision,
    data jsonb NOT NULL CHECK (jsonb_typeof(data) = 'object'),
    PRIMARY KEY (generation_id, ordinal),
    FOREIGN KEY (generation_id, ordinal) REFERENCES fit_messages(generation_id, ordinal)
        ON DELETE CASCADE
);
CREATE INDEX fit_samples_window ON fit_samples(generation_id, elapsed_seconds, ordinal);

CREATE TABLE fit_timer_events (
    generation_id uuid NOT NULL,
    ordinal integer NOT NULL,
    observed_at timestamptz,
    elapsed_seconds double precision,
    event_type text,
    data jsonb NOT NULL,
    PRIMARY KEY (generation_id, ordinal),
    FOREIGN KEY (generation_id, ordinal) REFERENCES fit_messages(generation_id, ordinal)
        ON DELETE CASCADE
);
CREATE INDEX fit_events_window ON fit_timer_events(generation_id, elapsed_seconds, ordinal);

CREATE TABLE fit_laps (
    generation_id uuid NOT NULL,
    ordinal integer NOT NULL,
    data jsonb NOT NULL,
    PRIMARY KEY (generation_id, ordinal),
    FOREIGN KEY (generation_id, ordinal) REFERENCES fit_messages(generation_id, ordinal)
        ON DELETE CASCADE
);
CREATE TABLE fit_workout_steps (
    generation_id uuid NOT NULL,
    ordinal integer NOT NULL,
    kind text NOT NULL CHECK (kind IN ('planned', 'executed')),
    data jsonb NOT NULL,
    PRIMARY KEY (generation_id, ordinal),
    FOREIGN KEY (generation_id, ordinal) REFERENCES fit_messages(generation_id, ordinal)
        ON DELETE CASCADE
);
CREATE TABLE fit_extra_metrics (
    generation_id uuid NOT NULL,
    ordinal integer NOT NULL,
    kind text NOT NULL,
    data jsonb NOT NULL,
    PRIMARY KEY (generation_id, ordinal),
    FOREIGN KEY (generation_id, ordinal) REFERENCES fit_messages(generation_id, ordinal)
        ON DELETE CASCADE
);
