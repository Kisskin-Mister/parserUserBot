"""Idempotent PostgreSQL migrations for the vacancy application pipeline.

The project deliberately keeps migrations small and dependency-free.  Statements
are safe to run on every process start and are serialized with a PostgreSQL
advisory transaction lock so API and worker containers can start concurrently.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol


class MigrationConnection(Protocol):
    """The subset of :class:`asyncpg.Connection` used by this module."""

    def transaction(self): ...

    def execute(self, query: str, *args: object) -> Awaitable[str]: ...


MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS vacancy_intake (
        id BIGSERIAL PRIMARY KEY,
        source_type TEXT NOT NULL
            CHECK (source_type IN ('telegram', 'hh', 'habr', 'getmatch', 'careerspace', 'direct')),
        source_chat_id BIGINT,
        source_message_id BIGINT,
        source_channel TEXT,
        message_url TEXT,
        raw_text TEXT NOT NULL,
        raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(raw_payload) = 'object'),
        idempotency_key TEXT NOT NULL UNIQUE,
        content_hash TEXT NOT NULL,
        processing_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (processing_status IN ('pending', 'processing', 'completed', 'failed')),
        retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
        last_error TEXT,
        locked_by TEXT,
        locked_until TIMESTAMPTZ,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_vacancy_intake_content_hash
    ON vacancy_intake(content_hash)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_vacancy_intake_claim
    ON vacancy_intake(processing_status, next_attempt_at, locked_until, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS job_vacancies (
        id BIGSERIAL PRIMARY KEY,
        intake_id BIGINT NOT NULL UNIQUE
            REFERENCES vacancy_intake(id) ON DELETE RESTRICT,
        company TEXT,
        title TEXT,
        canonical_url TEXT,
        official_apply_url TEXT,
        description_text TEXT NOT NULL,
        remote_type TEXT NOT NULL DEFAULT 'unknown'
            CHECK (remote_type IN ('remote_only', 'hybrid', 'office', 'unknown')),
        salary_min_rub INTEGER CHECK (salary_min_rub IS NULL OR salary_min_rub >= 0),
        salary_max_rub INTEGER CHECK (salary_max_rub IS NULL OR salary_max_rub >= 0),
        salary_hidden BOOLEAN NOT NULL DEFAULT TRUE,
        stack_tags JSONB NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(stack_tags) = 'array'),
        seniority TEXT,
        ai_verdict TEXT NOT NULL DEFAULT 'needs_review'
            CHECK (ai_verdict IN ('qualified', 'garbage', 'needs_review')),
        ai_confidence NUMERIC(5, 4) NOT NULL DEFAULT 0
            CHECK (ai_confidence >= 0 AND ai_confidence <= 1),
        ai_score NUMERIC(6, 2) NOT NULL DEFAULT 0,
        ai_reason TEXT NOT NULL DEFAULT '',
        rule_reason TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN (
                'pending', 'qualified', 'needs_review', 'garbage',
                'approved_for_browser', 'submitted', 'rejected'
            )),
        is_submitted BOOLEAN NOT NULL DEFAULT FALSE,
        submitted_at TIMESTAMPTZ,
        external_confirmation TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (salary_min_rub IS NULL OR salary_max_rub IS NULL OR salary_min_rub <= salary_max_rub),
        CHECK (NOT is_submitted OR (state = 'submitted' AND submitted_at IS NOT NULL))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_job_vacancies_working_list
    ON job_vacancies(ai_verdict, state, created_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_job_vacancies_official_url
    ON job_vacancies(official_apply_url)
    WHERE official_apply_url IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS applications (
        id BIGSERIAL PRIMARY KEY,
        vacancy_id BIGINT NOT NULL REFERENCES job_vacancies(id) ON DELETE RESTRICT,
        profile_key TEXT NOT NULL DEFAULT 'default',
        application_key TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN (
                'pending', 'approved_for_browser', 'processing_browser',
                'captcha_required', 'ready_for_submit_approval', 'submitting',
                'submitted', 'needs_human_answer',
                'needs_manual_verification', 'rejected'
            )),
        resume_path TEXT NOT NULL,
        resume_sha256 TEXT NOT NULL,
        form_payload JSONB NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(form_payload) = 'object'),
        form_payload_hash TEXT NOT NULL,
        cover_letter TEXT,
        approved_payload_hash TEXT,
        approved_resume_sha256 TEXT,
        approved_at TIMESTAMPTZ,
        is_submitted BOOLEAN NOT NULL DEFAULT FALSE,
        submitted_at TIMESTAMPTZ,
        external_confirmation TEXT,
        last_error TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
        locked_by TEXT,
        locked_until TIMESTAMPTZ,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(vacancy_id, profile_key),
        CHECK (
            (approved_at IS NULL AND approved_payload_hash IS NULL AND approved_resume_sha256 IS NULL)
            OR
            (approved_at IS NOT NULL AND approved_payload_hash IS NOT NULL AND approved_resume_sha256 IS NOT NULL)
        ),
        CHECK (NOT is_submitted OR (state = 'submitted' AND submitted_at IS NOT NULL))
    )
    """,
    # These ALTER statements also upgrade installations created by an earlier
    # development version of the pipeline schema.
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS profile_key TEXT NOT NULL DEFAULT 'default'",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS application_key TEXT",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS approved_resume_sha256 TEXT",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_vacancy_profile
    ON applications(vacancy_id, profile_key)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_application_key
    ON applications(application_key)
    WHERE application_key IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_applications_browser_claim
    ON applications(state, next_attempt_at, locked_until, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS application_events (
        id BIGSERIAL PRIMARY KEY,
        application_id BIGINT NOT NULL REFERENCES applications(id) ON DELETE RESTRICT,
        old_state TEXT,
        new_state TEXT NOT NULL,
        actor TEXT NOT NULL,
        event_type TEXT NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(metadata) = 'object'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_application_events_application_created
    ON application_events(application_id, created_at, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS captcha_checkpoints (
        id BIGSERIAL PRIMARY KEY,
        application_id BIGINT NOT NULL REFERENCES applications(id) ON DELETE RESTRICT,
        state TEXT NOT NULL DEFAULT 'waiting_human'
            CHECK (state IN ('waiting_human', 'resolved', 'aborted')),
        page_url TEXT NOT NULL,
        screenshot_path TEXT,
        persistent_session_key TEXT NOT NULL,
        instructions TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        resolved_at TIMESTAMPTZ,
        CHECK (
            (state = 'waiting_human' AND resolved_at IS NULL)
            OR (state IN ('resolved', 'aborted') AND resolved_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_captcha_one_waiting_per_application
    ON captcha_checkpoints(application_id)
    WHERE state = 'waiting_human'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_captcha_state_created
    ON captcha_checkpoints(state, created_at, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS submission_proofs (
        id BIGSERIAL PRIMARY KEY,
        application_id BIGINT NOT NULL UNIQUE REFERENCES applications(id) ON DELETE RESTRICT,
        before_screenshot_path TEXT,
        after_screenshot_path TEXT,
        confirmation_text TEXT,
        confirmation_url TEXT,
        provider_application_id TEXT,
        uploaded_resume_sha256 TEXT NOT NULL,
        submitted_payload_hash TEXT NOT NULL,
        proof_hash TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (confirmation_text IS NOT NULL OR provider_application_id IS NOT NULL)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_calls (
        id BIGSERIAL PRIMARY KEY,
        purpose TEXT NOT NULL,
        vacancy_id BIGINT REFERENCES job_vacancies(id) ON DELETE SET NULL,
        application_id BIGINT REFERENCES applications(id) ON DELETE SET NULL,
        model TEXT NOT NULL,
        prompt_hash TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'completed'
            CHECK (status IN ('completed', 'failed')),
        cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
        input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
        output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
        response JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_calls_completed_cache
    ON llm_calls(purpose, model, input_hash)
    WHERE status = 'completed' AND cache_hit = FALSE
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_llm_calls_vacancy_created
    ON llm_calls(vacancy_id, created_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_llm_calls_application_created
    ON llm_calls(application_id, created_at DESC, id DESC)
    """,
    """
    CREATE OR REPLACE FUNCTION vacancy_pipeline_touch_updated_at()
    RETURNS TRIGGER AS $$
    BEGIN
        NEW.updated_at = CURRENT_TIMESTAMP;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS vacancy_intake_touch_updated_at ON vacancy_intake",
    """
    CREATE TRIGGER vacancy_intake_touch_updated_at
    BEFORE UPDATE ON vacancy_intake
    FOR EACH ROW EXECUTE FUNCTION vacancy_pipeline_touch_updated_at()
    """,
    "DROP TRIGGER IF EXISTS job_vacancies_touch_updated_at ON job_vacancies",
    """
    CREATE TRIGGER job_vacancies_touch_updated_at
    BEFORE UPDATE ON job_vacancies
    FOR EACH ROW EXECUTE FUNCTION vacancy_pipeline_touch_updated_at()
    """,
    "DROP TRIGGER IF EXISTS applications_touch_updated_at ON applications",
    """
    CREATE TRIGGER applications_touch_updated_at
    BEFORE UPDATE ON applications
    FOR EACH ROW EXECUTE FUNCTION vacancy_pipeline_touch_updated_at()
    """,
    """
    CREATE OR REPLACE FUNCTION vacancy_pipeline_events_append_only()
    RETURNS TRIGGER AS $$
    BEGIN
        RAISE EXCEPTION 'application_events is append-only';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS application_events_append_only ON application_events",
    """
    CREATE TRIGGER application_events_append_only
    BEFORE UPDATE OR DELETE ON application_events
    FOR EACH ROW EXECUTE FUNCTION vacancy_pipeline_events_append_only()
    """,
    """
    CREATE OR REPLACE FUNCTION vacancy_pipeline_validate_proof()
    RETURNS TRIGGER AS $$
    DECLARE
        app applications%ROWTYPE;
    BEGIN
        SELECT * INTO app FROM applications WHERE id = NEW.application_id FOR KEY SHARE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'application % does not exist', NEW.application_id;
        END IF;
        IF app.is_submitted OR app.state <> 'submitting' THEN
            RAISE EXCEPTION 'application % is not in an unsubmitted submitting state', NEW.application_id;
        END IF;
        IF app.approved_at IS NULL
           OR app.approved_payload_hash IS DISTINCT FROM app.form_payload_hash
           OR app.approved_resume_sha256 IS DISTINCT FROM app.resume_sha256 THEN
            RAISE EXCEPTION 'application % does not have exact approval', NEW.application_id;
        END IF;
        IF NEW.submitted_payload_hash IS DISTINCT FROM app.form_payload_hash
           OR NEW.uploaded_resume_sha256 IS DISTINCT FROM app.resume_sha256 THEN
            RAISE EXCEPTION 'submission proof does not match approved payload/resume';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS submission_proofs_validate ON submission_proofs",
    """
    CREATE TRIGGER submission_proofs_validate
    BEFORE INSERT ON submission_proofs
    FOR EACH ROW EXECUTE FUNCTION vacancy_pipeline_validate_proof()
    """,
    """
    CREATE OR REPLACE FUNCTION vacancy_pipeline_enforce_application_submission()
    RETURNS TRIGGER AS $$
    BEGIN
        IF TG_OP = 'UPDATE' AND OLD.is_submitted AND NOT NEW.is_submitted THEN
            RAISE EXCEPTION 'submitted application cannot be reset';
        END IF;
        IF NEW.is_submitted THEN
            IF NEW.state <> 'submitted' OR NEW.submitted_at IS NULL THEN
                RAISE EXCEPTION 'submitted application requires submitted state and timestamp';
            END IF;
            IF NEW.approved_at IS NULL
               OR NEW.approved_payload_hash IS DISTINCT FROM NEW.form_payload_hash
               OR NEW.approved_resume_sha256 IS DISTINCT FROM NEW.resume_sha256 THEN
                RAISE EXCEPTION 'submitted application requires exact payload and resume approval';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM submission_proofs proof
                WHERE proof.application_id = NEW.id
                  AND proof.submitted_payload_hash = NEW.form_payload_hash
                  AND proof.uploaded_resume_sha256 = NEW.resume_sha256
            ) THEN
                RAISE EXCEPTION 'matching submission proof is required before submitted=true';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS applications_enforce_submission ON applications",
    """
    CREATE TRIGGER applications_enforce_submission
    BEFORE INSERT OR UPDATE OF is_submitted, state, submitted_at ON applications
    FOR EACH ROW EXECUTE FUNCTION vacancy_pipeline_enforce_application_submission()
    """,
    """
    CREATE OR REPLACE FUNCTION vacancy_pipeline_enforce_vacancy_submission()
    RETURNS TRIGGER AS $$
    BEGIN
        IF TG_OP = 'UPDATE' AND OLD.is_submitted AND NOT NEW.is_submitted THEN
            RAISE EXCEPTION 'submitted vacancy cannot be reset';
        END IF;
        IF NEW.is_submitted THEN
            IF NEW.state <> 'submitted' OR NEW.submitted_at IS NULL THEN
                RAISE EXCEPTION 'submitted vacancy requires submitted state and timestamp';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM applications app
                JOIN submission_proofs proof ON proof.application_id = app.id
                WHERE app.vacancy_id = NEW.id AND app.is_submitted
            ) THEN
                RAISE EXCEPTION 'submitted vacancy requires a submitted application proof';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS job_vacancies_enforce_submission ON job_vacancies",
    """
    CREATE TRIGGER job_vacancies_enforce_submission
    BEFORE INSERT OR UPDATE OF is_submitted, state, submitted_at ON job_vacancies
    FOR EACH ROW EXECUTE FUNCTION vacancy_pipeline_enforce_vacancy_submission()
    """,
)


async def run_migrations(connection: MigrationConnection) -> None:
    """Apply all pipeline migrations using an existing asyncpg connection."""

    transaction_factory: Callable[[], object] = connection.transaction
    async with transaction_factory():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('parseruserbot_vacancy_pipeline_v1'))"
        )
        for statement in MIGRATION_STATEMENTS:
            await connection.execute(statement)
