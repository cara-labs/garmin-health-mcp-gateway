CREATE TABLE activity_feedback_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    activity_id bigint NOT NULL REFERENCES activities(garmin_activity_id),
    revision integer NOT NULL CHECK (revision>0),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    source text NOT NULL DEFAULT 'user_reported',
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    payload_sha256 text NOT NULL,
    idempotency_key text NOT NULL,
    UNIQUE(activity_id,revision),
    UNIQUE(activity_id,idempotency_key)
);
CREATE INDEX feedback_latest ON activity_feedback_revisions(activity_id,revision DESC);

CREATE FUNCTION append_activity_feedback(
    p_activity_id bigint, p_payload jsonb, p_idempotency_key text,
    p_expected_revision integer DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE
    prior public.activity_feedback_revisions;
    latest integer;
    digest text;
    key text;
    val jsonb;
    item jsonb;
    child text;
    childval jsonb;
BEGIN
    IF p_activity_id IS NULL OR p_activity_id<=0 OR p_payload IS NULL
       OR jsonb_typeof(p_payload)<>'object' OR octet_length(p_payload::text)>16384
       OR p_idempotency_key IS NULL OR length(p_idempotency_key) NOT BETWEEN 1 AND 128
       OR p_idempotency_key ~ '[[:cntrl:]]'
       OR p_expected_revision<0 THEN
        RAISE EXCEPTION 'invalid_feedback_input' USING ERRCODE='22023';
    END IF;
    FOR key,val IN SELECT * FROM jsonb_each(p_payload) LOOP
        IF NOT key=ANY(ARRAY['breathing_effort','leg_effort','overall_rpe',
           'heaviness_onset_seconds','heaviness_location','pain','pain_severity',
           'walking_reasons','fueling','fueling_carbohydrate_g','hydration',
           'hydration_ml','strap_problems','recovery_hr_protocol']) THEN
            RAISE EXCEPTION 'unknown_feedback_field' USING ERRCODE='22023';
        END IF;
        IF val='null'::jsonb THEN CONTINUE; END IF;
        IF key=ANY(ARRAY['breathing_effort','leg_effort','overall_rpe','pain_severity',
                        'heaviness_onset_seconds','fueling_carbohydrate_g','hydration_ml']) THEN
            IF jsonb_typeof(val)<>'number' THEN
                RAISE EXCEPTION 'invalid_feedback_number' USING ERRCODE='22023';
            END IF;
            IF val::numeric<0 OR (key=ANY(ARRAY['breathing_effort','leg_effort',
                'overall_rpe','pain_severity']) AND val::numeric>10) THEN
                RAISE EXCEPTION 'feedback_number_out_of_range' USING ERRCODE='22023';
            END IF;
        ELSIF key='walking_reasons' THEN
            IF jsonb_typeof(val)<>'array' THEN
                RAISE EXCEPTION 'invalid_walking_reasons' USING ERRCODE='22023';
            END IF;
            IF jsonb_array_length(val)>20 THEN
                RAISE EXCEPTION 'too_many_walking_reasons' USING ERRCODE='22023';
            END IF;
            FOR item IN SELECT * FROM jsonb_array_elements(val) LOOP
                IF jsonb_typeof(item)<>'string' OR length(item#>>'{}')>200 THEN
                    RAISE EXCEPTION 'invalid_walking_reason' USING ERRCODE='22023';
                END IF;
            END LOOP;
        ELSIF key='recovery_hr_protocol' THEN
            IF jsonb_typeof(val)<>'object' THEN
                RAISE EXCEPTION 'invalid_recovery_protocol' USING ERRCODE='22023';
            END IF;
            FOR child,childval IN SELECT * FROM jsonb_each(val) LOOP
                IF NOT child=ANY(ARRAY['baseline_hr_bpm','recovery_hr_bpm','interval_seconds',
                                      'posture','method','notes']) THEN
                    RAISE EXCEPTION 'unknown_recovery_protocol_field' USING ERRCODE='22023';
                END IF;
                IF childval='null'::jsonb THEN CONTINUE; END IF;
                IF child=ANY(ARRAY['baseline_hr_bpm','recovery_hr_bpm','interval_seconds']) THEN
                    IF jsonb_typeof(childval)<>'number' THEN
                        RAISE EXCEPTION 'invalid_recovery_number' USING ERRCODE='22023';
                    END IF;
                    IF childval::numeric<0 OR (child<>'interval_seconds' AND childval::numeric>300)
                    THEN
                        RAISE EXCEPTION 'recovery_number_out_of_range' USING ERRCODE='22023';
                    END IF;
                ELSIF jsonb_typeof(childval)<>'string' OR length(childval#>>'{}')>2000 THEN
                    RAISE EXCEPTION 'invalid_recovery_text' USING ERRCODE='22023';
                END IF;
            END LOOP;
        ELSIF jsonb_typeof(val)<>'string' OR length(val#>>'{}')>2000
              OR (key='heaviness_location' AND length(val#>>'{}')>200) THEN
            RAISE EXCEPTION 'invalid_feedback_text' USING ERRCODE='22023';
        END IF;
    END LOOP;
    PERFORM pg_advisory_xact_lock(719042883,(p_activity_id%2147483647)::integer);
    IF NOT EXISTS(SELECT 1 FROM public.activities WHERE garmin_activity_id=p_activity_id) THEN
        RAISE EXCEPTION 'activity_not_found' USING ERRCODE='P0002';
    END IF;
    digest:=encode(sha256(convert_to(p_payload::text,'UTF8')),'hex');
    SELECT * INTO prior FROM public.activity_feedback_revisions
        WHERE activity_id=p_activity_id AND idempotency_key=p_idempotency_key;
    IF FOUND THEN
        IF prior.payload_sha256<>digest THEN
            RAISE EXCEPTION 'idempotency_payload_conflict' USING ERRCODE='40001';
        END IF;
        RETURN to_jsonb(prior);
    END IF;
    SELECT coalesce(max(revision),0) INTO latest FROM public.activity_feedback_revisions
        WHERE activity_id=p_activity_id;
    IF p_expected_revision IS NOT NULL AND p_expected_revision<>latest THEN
        RAISE EXCEPTION 'feedback_revision_conflict' USING ERRCODE='40001';
    END IF;
    INSERT INTO public.activity_feedback_revisions(activity_id,revision,payload,payload_sha256,
                                                  idempotency_key)
        VALUES(p_activity_id,latest+1,p_payload,digest,p_idempotency_key) RETURNING * INTO prior;
    RETURN to_jsonb(prior);
END;
$$;
REVOKE ALL ON FUNCTION append_activity_feedback(bigint,jsonb,text,integer) FROM PUBLIC;
