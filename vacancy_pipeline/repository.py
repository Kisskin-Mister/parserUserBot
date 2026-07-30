"""Async PostgreSQL repository for the vacancy pipeline.

All queue claims and submission-safety changes happen in PostgreSQL transactions.
The repository returns plain dictionaries so callers do not depend on asyncpg's
``Record`` type and API responses remain straightforward to serialize.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any


class RepositoryError(RuntimeError):
    """Base class for repository failures safe to translate at API boundaries."""


class NotFoundError(RepositoryError):
    """The requested pipeline entity does not exist."""


class RepositoryConflict(RepositoryError, ValueError):
    """The entity exists but its current state conflicts with the operation."""


class SafetyViolation(RepositoryConflict):
    """An exact-approval, idempotency, or submission-proof invariant failed."""


def canonical_json(value: Mapping[str, Any] | Sequence[Any]) -> str:
    """Return the stable JSON representation used by approval hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_hash(value: Mapping[str, Any] | Sequence[Any]) -> str:
    """SHA-256 of :func:`canonical_json`, encoded as lowercase hex."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_application_key(
    vacancy_id: int,
    profile_key: str,
    resume_sha256: str,
    form_payload_hash: str,
) -> str:
    """Build the immutable dedupe fingerprint for application contents."""

    material = "\0".join(
        (str(vacancy_id), profile_key, resume_sha256, form_payload_hash)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_submission_proof_hash(
    *,
    application_id: int,
    uploaded_resume_sha256: str,
    submitted_payload_hash: str,
    confirmation_text: str | None,
    confirmation_url: str | None,
    provider_application_id: str | None,
    before_screenshot_path: str | None,
    after_screenshot_path: str | None,
) -> str:
    """Create a stable evidence fingerprint without reading screenshot contents."""

    evidence = {
        "after_screenshot_path": after_screenshot_path,
        "application_id": application_id,
        "before_screenshot_path": before_screenshot_path,
        "confirmation_text": confirmation_text,
        "confirmation_url": confirmation_url,
        "provider_application_id": provider_application_id,
        "submitted_payload_hash": submitted_payload_hash,
        "uploaded_resume_sha256": uploaded_resume_sha256,
    }
    return canonical_json_hash(evidence)


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


_JSON_FIELDS = {"raw_payload", "stack_tags", "form_payload", "metadata", "response"}


def _row_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for field in _JSON_FIELDS:
        value = data.get(field)
        if isinstance(value, str):
            data[field] = json.loads(value)
    for field in ("ai_confidence", "ai_score"):
        if data.get(field) is not None:
            data[field] = float(data[field])
    return data


class PipelineRepository:
    """Repository backed by an initialized ``asyncpg.Pool``."""

    def __init__(self, pool: Any):
        if pool is None:
            raise ValueError("an initialized asyncpg pool is required")
        self.pool = pool

    @asynccontextmanager
    async def _connection(self, *, transaction: bool = False) -> AsyncIterator[Any]:
        async with self.pool.acquire() as connection:
            if transaction:
                async with connection.transaction():
                    yield connection
            else:
                yield connection

    @staticmethod
    async def _add_event(
        connection: Any,
        application_id: int,
        old_state: str | None,
        new_state: str,
        actor: str,
        event_type: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = await connection.fetchrow(
            """
            INSERT INTO application_events
                (application_id, old_state, new_state, actor, event_type, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            RETURNING *
            """,
            application_id,
            old_state,
            new_state,
            actor,
            event_type,
            canonical_json(metadata or {}),
        )
        return _row_dict(row) or {}

    async def create_intake(
        self,
        *,
        source_type: str,
        raw_text: str,
        raw_payload: Mapping[str, Any],
        idempotency_key: str,
        content_hash: str,
        source_chat_id: int | str | None = None,
        source_message_id: int | str | None = None,
        source_channel: str | None = None,
        message_url: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Insert raw intake once and return ``(row, created)``."""

        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        if not content_hash:
            raise ValueError("content_hash must not be empty")
        async with self._connection(transaction=True) as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO vacancy_intake (
                    source_type, source_chat_id, source_message_id, source_channel,
                    message_url, raw_text, raw_payload, idempotency_key, content_hash
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *
                """,
                _enum_value(source_type),
                int(source_chat_id) if source_chat_id is not None else None,
                int(source_message_id) if source_message_id is not None else None,
                source_channel,
                message_url,
                raw_text,
                canonical_json(raw_payload),
                idempotency_key,
                content_hash,
            )
            if row is not None:
                return _row_dict(row) or {}, True
            row = await connection.fetchrow(
                "SELECT * FROM vacancy_intake WHERE idempotency_key = $1",
                idempotency_key,
            )
            if row is None:  # defensive: conflict row must be visible in this statement
                raise RepositoryConflict("idempotent intake row could not be read")
            return _row_dict(row) or {}, False

    async def get_intake(self, intake_id: int) -> dict[str, Any] | None:
        async with self._connection() as connection:
            return _row_dict(
                await connection.fetchrow(
                    "SELECT * FROM vacancy_intake WHERE id = $1", intake_id
                )
            )

    async def claim_intakes(
        self,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 300,
    ) -> list[dict[str, Any]]:
        """Atomically lease due intake rows to one worker."""

        if not worker_id:
            raise ValueError("worker_id must not be empty")
        if limit < 1 or lease_seconds < 1:
            raise ValueError("limit and lease_seconds must be positive")
        async with self._connection(transaction=True) as connection:
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT id
                    FROM vacancy_intake
                    WHERE processing_status IN ('pending', 'failed', 'processing')
                      AND next_attempt_at <= CURRENT_TIMESTAMP
                      AND (locked_until IS NULL OR locked_until < CURRENT_TIMESTAMP)
                    ORDER BY next_attempt_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT $2
                )
                UPDATE vacancy_intake AS intake
                SET processing_status = 'processing',
                    locked_by = $1,
                    locked_until = CURRENT_TIMESTAMP + ($3 * INTERVAL '1 second'),
                    last_error = NULL
                FROM candidates
                WHERE intake.id = candidates.id
                RETURNING intake.*
                """,
                worker_id,
                limit,
                lease_seconds,
            )
            return [_row_dict(row) or {} for row in rows]

    async def mark_intake_completed(
        self, intake_id: int, *, worker_id: str | None = None
    ) -> dict[str, Any]:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                """
                UPDATE vacancy_intake
                SET processing_status = 'completed', locked_by = NULL,
                    locked_until = NULL, last_error = NULL
                WHERE id = $1 AND ($2::text IS NULL OR locked_by = $2)
                RETURNING *
                """,
                intake_id,
                worker_id,
            )
        if row is None:
            raise RepositoryConflict("intake is missing or lease is owned by another worker")
        return _row_dict(row) or {}

    async def mark_intake_failed(
        self,
        intake_id: int,
        error: str,
        *,
        worker_id: str | None = None,
        retry_delay_seconds: int = 60,
    ) -> dict[str, Any]:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                """
                UPDATE vacancy_intake
                SET processing_status = 'failed', retry_count = retry_count + 1,
                    last_error = $3, locked_by = NULL, locked_until = NULL,
                    next_attempt_at = CURRENT_TIMESTAMP + ($4 * INTERVAL '1 second')
                WHERE id = $1 AND ($2::text IS NULL OR locked_by = $2)
                RETURNING *
                """,
                intake_id,
                worker_id,
                error[:4000],
                max(0, retry_delay_seconds),
            )
        if row is None:
            raise RepositoryConflict("intake is missing or lease is owned by another worker")
        return _row_dict(row) or {}

    async def create_vacancy(
        self,
        intake_id: int,
        *,
        description_text: str,
        company: str | None = None,
        title: str | None = None,
        canonical_url: str | None = None,
        official_apply_url: str | None = None,
        remote_type: str = "unknown",
        salary_min_rub: int | None = None,
        salary_max_rub: int | None = None,
        salary_hidden: bool = True,
        stack_tags: Sequence[str] | None = None,
        seniority: str | None = None,
        ai_verdict: str = "needs_review",
        ai_confidence: float = 0.0,
        ai_score: float = 0.0,
        ai_reason: str = "",
        rule_reason: str = "",
        state: str | None = None,
    ) -> dict[str, Any]:
        """Create the one vacancy extracted from an intake, idempotently."""

        verdict = str(_enum_value(ai_verdict))
        vacancy_state = str(_enum_value(state or verdict))
        async with self._connection(transaction=True) as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO job_vacancies (
                    intake_id, company, title, canonical_url, official_apply_url,
                    description_text, remote_type, salary_min_rub, salary_max_rub,
                    salary_hidden, stack_tags, seniority, ai_verdict,
                    ai_confidence, ai_score, ai_reason, rule_reason, state
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11::jsonb, $12, $13, $14, $15, $16, $17, $18
                )
                ON CONFLICT (intake_id) DO NOTHING
                RETURNING *
                """,
                intake_id,
                company,
                title,
                canonical_url,
                official_apply_url,
                description_text,
                _enum_value(remote_type),
                salary_min_rub,
                salary_max_rub,
                salary_hidden,
                canonical_json(list(stack_tags or [])),
                seniority,
                verdict,
                ai_confidence,
                ai_score,
                ai_reason,
                rule_reason,
                vacancy_state,
            )
            if row is None:
                row = await connection.fetchrow(
                    "SELECT * FROM job_vacancies WHERE intake_id = $1", intake_id
                )
            if row is None:
                raise RepositoryConflict("vacancy insert conflict could not be resolved")
            return _row_dict(row) or {}

    async def get_vacancy(self, vacancy_id: int) -> dict[str, Any] | None:
        async with self._connection() as connection:
            return _row_dict(
                await connection.fetchrow(
                    "SELECT * FROM job_vacancies WHERE id = $1", vacancy_id
                )
            )

    async def list_vacancies(
        self,
        *,
        verdict: str | None = None,
        ai_verdict: str | None = None,
        state: str | None = None,
        include_garbage: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        values: list[Any] = []

        def add_condition(sql: str, value: Any) -> None:
            values.append(_enum_value(value))
            conditions.append(sql.format(index=len(values)))

        if verdict is not None and ai_verdict is not None and verdict != ai_verdict:
            raise ValueError("verdict and ai_verdict filters disagree")
        verdict_filter = ai_verdict if ai_verdict is not None else verdict
        if verdict_filter is not None:
            add_condition("ai_verdict = ${index}", verdict_filter)
        elif not include_garbage:
            conditions.append("ai_verdict <> 'garbage'")
        if state is not None:
            add_condition("state = ${index}", state)
        elif not include_garbage:
            conditions.append("state <> 'garbage'")
        values.extend((max(1, min(limit, 500)), max(0, offset)))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            f"SELECT * FROM job_vacancies {where} "
            f"ORDER BY created_at DESC, id DESC LIMIT ${len(values) - 1} OFFSET ${len(values)}"
        )
        async with self._connection() as connection:
            rows = await connection.fetch(sql, *values)
        return [_row_dict(row) or {} for row in rows]

    async def update_vacancy(self, vacancy_id: int, **changes: Any) -> dict[str, Any]:
        """Update allowlisted review/extraction fields (never submission flags)."""

        # ``human_actor`` is accepted by the service contract.  Vacancy decisions
        # do not belong to application_events until an application exists.
        changes.pop("human_actor", None)

        allowed = {
            "company",
            "title",
            "canonical_url",
            "official_apply_url",
            "description_text",
            "remote_type",
            "salary_min_rub",
            "salary_max_rub",
            "salary_hidden",
            "stack_tags",
            "seniority",
            "ai_verdict",
            "ai_confidence",
            "ai_score",
            "ai_reason",
            "rule_reason",
            "state",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported vacancy fields: {sorted(unknown)}")
        if not changes:
            row = await self.get_vacancy(vacancy_id)
            if row is None:
                raise NotFoundError(f"vacancy {vacancy_id} not found")
            return row
        assignments: list[str] = []
        values: list[Any] = [vacancy_id]
        for field, value in changes.items():
            values.append(
                canonical_json(list(value or []))
                if field == "stack_tags"
                else _enum_value(value)
            )
            cast = "::jsonb" if field == "stack_tags" else ""
            assignments.append(f"{field} = ${len(values)}{cast}")
        async with self._connection() as connection:
            row = await connection.fetchrow(
                f"UPDATE job_vacancies SET {', '.join(assignments)} "
                "WHERE id = $1 AND is_submitted = FALSE RETURNING *",
                *values,
            )
        if row is None:
            raise RepositoryConflict("vacancy is missing or already submitted")
        return _row_dict(row) or {}

    async def prepare_application(
        self,
        vacancy_id: int,
        *,
        profile_key: str = "default",
        resume_path: str,
        resume_sha256: str,
        form_payload: Mapping[str, Any] | None = None,
        form_payload_hash: str | None = None,
        cover_letter: str | None = None,
        actor: str = "service",
    ) -> dict[str, Any]:
        """Create/update the unique vacancy/profile application safely."""

        payload = dict(form_payload or {})
        calculated_hash = canonical_json_hash(payload)
        if form_payload_hash is not None and form_payload_hash != calculated_hash:
            raise SafetyViolation("form_payload_hash does not match canonical form_payload")
        payload_hash = form_payload_hash or calculated_hash
        if not profile_key or not resume_path or not resume_sha256:
            raise ValueError("profile_key, resume_path and resume_sha256 are required")
        application_key = build_application_key(
            vacancy_id, profile_key, resume_sha256, payload_hash
        )
        async with self._connection(transaction=True) as connection:
            existing = await connection.fetchrow(
                """
                SELECT * FROM applications
                WHERE vacancy_id = $1 AND profile_key = $2
                FOR UPDATE
                """,
                vacancy_id,
                profile_key,
            )
            if existing is None:
                row = await connection.fetchrow(
                    """
                    INSERT INTO applications (
                        vacancy_id, profile_key, application_key, state,
                        resume_path, resume_sha256, form_payload,
                        form_payload_hash, cover_letter
                    )
                    VALUES ($1, $2, $3, 'pending', $4, $5, $6::jsonb, $7, $8)
                    RETURNING *
                    """,
                    vacancy_id,
                    profile_key,
                    application_key,
                    resume_path,
                    resume_sha256,
                    canonical_json(payload),
                    payload_hash,
                    cover_letter,
                )
                if row is None:
                    raise RepositoryConflict("application could not be created")
                await self._add_event(
                    connection,
                    row["id"],
                    None,
                    "pending",
                    actor,
                    "application_prepared",
                    {"profile_key": profile_key},
                )
                return _row_dict(row) or {}

            current = _row_dict(existing) or {}
            changed = (
                current.get("resume_path") != resume_path
                or current.get("resume_sha256") != resume_sha256
                or current.get("form_payload_hash") != payload_hash
                or current.get("cover_letter") != cover_letter
            )
            if current.get("is_submitted"):
                if changed:
                    raise SafetyViolation("submitted application contents cannot be changed")
                return current
            if current.get("state") == "submitting":
                raise SafetyViolation("application contents cannot change after submit started")

            new_state = "pending" if changed else str(current["state"])
            row = await connection.fetchrow(
                """
                UPDATE applications
                SET application_key = $3, state = $4, resume_path = $5,
                    resume_sha256 = $6, form_payload = $7::jsonb,
                    form_payload_hash = $8, cover_letter = $9,
                    approved_payload_hash = CASE WHEN $10 THEN NULL ELSE approved_payload_hash END,
                    approved_resume_sha256 = CASE WHEN $10 THEN NULL ELSE approved_resume_sha256 END,
                    approved_at = CASE WHEN $10 THEN NULL ELSE approved_at END,
                    locked_by = NULL, locked_until = NULL
                WHERE vacancy_id = $1 AND profile_key = $2
                RETURNING *
                """,
                vacancy_id,
                profile_key,
                application_key,
                new_state,
                resume_path,
                resume_sha256,
                canonical_json(payload),
                payload_hash,
                cover_letter,
                changed,
            )
            if row is None:
                raise RepositoryConflict("application could not be updated")
            if changed:
                await self._add_event(
                    connection,
                    row["id"],
                    str(current["state"]),
                    new_state,
                    actor,
                    "application_contents_changed",
                    {"approval_invalidated": True},
                )
            return _row_dict(row) or {}

    create_application = prepare_application

    async def get_application(self, application_id: int) -> dict[str, Any] | None:
        async with self._connection() as connection:
            return _row_dict(
                await connection.fetchrow(
                    "SELECT * FROM applications WHERE id = $1", application_id
                )
            )

    async def get_application_for_vacancy(
        self, vacancy_id: int, profile_key: str = "default"
    ) -> dict[str, Any] | None:
        async with self._connection() as connection:
            return _row_dict(
                await connection.fetchrow(
                    """
                    SELECT * FROM applications
                    WHERE vacancy_id = $1 AND profile_key = $2
                    """,
                    vacancy_id,
                    profile_key,
                )
            )

    async def list_applications(
        self,
        *,
        state: str | None = None,
        vacancy_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with self._connection() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM applications
                WHERE ($1::text IS NULL OR state = $1)
                  AND ($2::bigint IS NULL OR vacancy_id = $2)
                ORDER BY created_at DESC, id DESC
                LIMIT $3
                """,
                _enum_value(state),
                vacancy_id,
                max(1, min(limit, 500)),
            )
        return [_row_dict(row) or {} for row in rows]

    async def approve_application_for_browser(
        self, application_id: int, *, actor: str = "human"
    ) -> dict[str, Any]:
        """Give initial browser-preparation approval, without submit approval."""

        async with self._connection(transaction=True) as connection:
            current = await connection.fetchrow(
                "SELECT * FROM applications WHERE id = $1 FOR UPDATE", application_id
            )
            if current is None:
                raise NotFoundError(f"application {application_id} not found")
            if current["is_submitted"] or current["state"] == "submitting":
                raise SafetyViolation("submitted/submitting application cannot be requeued")
            old_state = str(current["state"])
            row = await connection.fetchrow(
                """
                UPDATE applications
                SET state = 'approved_for_browser', next_attempt_at = CURRENT_TIMESTAMP,
                    locked_by = NULL, locked_until = NULL, last_error = NULL
                WHERE id = $1
                RETURNING *
                """,
                application_id,
            )
            await connection.execute(
                """
                UPDATE job_vacancies
                SET state = 'approved_for_browser'
                WHERE id = $1 AND is_submitted = FALSE
                """,
                current["vacancy_id"],
            )
            await self._add_event(
                connection,
                application_id,
                old_state,
                "approved_for_browser",
                actor,
                "browser_preparation_approved",
            )
            return _row_dict(row) or {}

    async def update_prepared_application(
        self,
        application_id: int,
        *,
        form_payload: Mapping[str, Any],
        form_payload_hash: str,
        resume_sha256: str | None = None,
        actor: str = "browser_worker",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist browser read-back and invalidate every prior exact approval."""

        if canonical_json_hash(form_payload) != form_payload_hash:
            raise SafetyViolation("form_payload_hash does not match canonical form_payload")
        async with self._connection(transaction=True) as connection:
            current = await connection.fetchrow(
                "SELECT * FROM applications WHERE id = $1 FOR UPDATE", application_id
            )
            if current is None:
                raise NotFoundError(f"application {application_id} not found")
            if current["is_submitted"] or current["state"] == "submitting":
                raise SafetyViolation("prepared payload cannot change after submit started")
            effective_resume_hash = resume_sha256 or str(current["resume_sha256"])
            application_key = build_application_key(
                int(current["vacancy_id"]),
                str(current["profile_key"]),
                effective_resume_hash,
                form_payload_hash,
            )
            row = await connection.fetchrow(
                """
                UPDATE applications
                SET state = 'ready_for_submit_approval', form_payload = $2::jsonb,
                    form_payload_hash = $3, resume_sha256 = $4,
                    application_key = $5, approved_payload_hash = NULL,
                    approved_resume_sha256 = NULL, approved_at = NULL,
                    locked_by = NULL, locked_until = NULL, last_error = NULL
                WHERE id = $1
                RETURNING *
                """,
                application_id,
                canonical_json(form_payload),
                form_payload_hash,
                effective_resume_hash,
                application_key,
            )
            await self._add_event(
                connection,
                application_id,
                str(current["state"]),
                "ready_for_submit_approval",
                actor,
                "form_prepared",
                {**dict(metadata or {}), "approval_invalidated": True},
            )
            return _row_dict(row) or {}

    update_application_payload = update_prepared_application

    async def approve_application_exact(
        self,
        application_id: int,
        payload_hash: str,
        resume_sha256: str,
        *,
        actor: str = "human",
    ) -> dict[str, Any]:
        """Approve exactly the current application id, payload hash and resume."""

        async with self._connection(transaction=True) as connection:
            current = await connection.fetchrow(
                "SELECT * FROM applications WHERE id = $1 FOR UPDATE", application_id
            )
            if current is None:
                raise NotFoundError(f"application {application_id} not found")
            if current["is_submitted"]:
                raise SafetyViolation("application was already submitted")
            if current["state"] != "ready_for_submit_approval":
                raise RepositoryConflict("application is not waiting for exact approval")
            if current["form_payload_hash"] != payload_hash:
                raise SafetyViolation("payload hash is stale or does not match")
            if current["resume_sha256"] != resume_sha256:
                raise SafetyViolation("resume hash is stale or does not match")
            row = await connection.fetchrow(
                """
                UPDATE applications
                SET approved_payload_hash = $2, approved_resume_sha256 = $3,
                    approved_at = CURRENT_TIMESTAMP, state = 'approved_for_browser',
                    next_attempt_at = CURRENT_TIMESTAMP,
                    locked_by = NULL, locked_until = NULL, last_error = NULL
                WHERE id = $1
                RETURNING *
                """,
                application_id,
                payload_hash,
                resume_sha256,
            )
            await self._add_event(
                connection,
                application_id,
                "ready_for_submit_approval",
                "approved_for_browser",
                actor,
                "exact_payload_approved",
                {"payload_hash": payload_hash, "resume_sha256": resume_sha256},
            )
            return _row_dict(row) or {}

    async def claim_browser_applications(
        self,
        worker_id: str,
        limit: int = 1,
        lease_seconds: int = 300,
    ) -> list[dict[str, Any]]:
        """Atomically claim only applications explicitly approved for browser work."""

        if not worker_id:
            raise ValueError("worker_id must not be empty")
        if limit < 1 or lease_seconds < 1:
            raise ValueError("limit and lease_seconds must be positive")
        async with self._connection(transaction=True) as connection:
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT id
                    FROM applications
                    WHERE state = 'approved_for_browser'
                      AND is_submitted = FALSE
                      AND next_attempt_at <= CURRENT_TIMESTAMP
                      AND (locked_until IS NULL OR locked_until < CURRENT_TIMESTAMP)
                    ORDER BY next_attempt_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT $2
                ), claimed AS (
                    UPDATE applications AS app
                    SET state = 'processing_browser', locked_by = $1,
                        locked_until = CURRENT_TIMESTAMP + ($3 * INTERVAL '1 second'),
                        last_error = NULL
                    FROM candidates
                    WHERE app.id = candidates.id
                    RETURNING app.*
                ), audit AS (
                    INSERT INTO application_events (
                        application_id, old_state, new_state, actor, event_type, metadata
                    )
                    SELECT id, 'approved_for_browser', 'processing_browser', $1,
                           'browser_claimed',
                           jsonb_build_object('lease_seconds', $3)
                    FROM claimed
                    RETURNING id
                )
                SELECT claimed.* FROM claimed
                """,
                worker_id,
                limit,
                lease_seconds,
            )
        return [_row_dict(row) or {} for row in rows]

    async def claim_browser_application(
        self, worker_id: str, lease_seconds: int = 300
    ) -> dict[str, Any] | None:
        rows = await self.claim_browser_applications(
            worker_id, limit=1, lease_seconds=lease_seconds
        )
        return rows[0] if rows else None

    async def transition_application(
        self,
        application_id: int,
        new_state: str,
        *,
        actor: str,
        event_type: str,
        metadata: Mapping[str, Any] | None = None,
        last_error: str | None = None,
        expected_states: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Transition state with submit-boundary safety and an audit event."""

        target = str(_enum_value(new_state))
        expected = {str(_enum_value(item)) for item in (expected_states or [])}
        async with self._connection(transaction=True) as connection:
            current = await connection.fetchrow(
                "SELECT * FROM applications WHERE id = $1 FOR UPDATE", application_id
            )
            if current is None:
                raise NotFoundError(f"application {application_id} not found")
            old_state = str(current["state"])
            if expected and old_state not in expected:
                raise RepositoryConflict(
                    f"expected application state {sorted(expected)}, got {old_state}"
                )
            if current["is_submitted"] and target != "submitted":
                raise SafetyViolation("submitted application state is irreversible")
            if target == "submitted":
                raise SafetyViolation(
                    "direct submitted transition is forbidden; record matching proof atomically"
                )
            if old_state == "submitting" and target not in {
                "submitting",
                "needs_manual_verification",
            }:
                raise SafetyViolation(
                    "after submit starts, uncertain outcomes require manual verification"
                )
            if target == "submitting":
                if old_state != "processing_browser":
                    raise RepositoryConflict("submit may start only from processing_browser")
                if (
                    current["approved_at"] is None
                    or current["approved_payload_hash"]
                    != current["form_payload_hash"]
                    or current["approved_resume_sha256"] != current["resume_sha256"]
                ):
                    raise SafetyViolation("exact payload and resume approval is required")
                proof = await connection.fetchrow(
                    "SELECT id FROM submission_proofs WHERE application_id = $1",
                    application_id,
                )
                if proof is not None:
                    raise SafetyViolation("a submission proof already exists")
            row = await connection.fetchrow(
                """
                UPDATE applications
                SET state = $2, last_error = $3, locked_by = NULL, locked_until = NULL
                WHERE id = $1
                RETURNING *
                """,
                application_id,
                target,
                last_error[:4000] if last_error else None,
            )
            await self._add_event(
                connection,
                application_id,
                old_state,
                target,
                actor,
                event_type,
                metadata,
            )
            return _row_dict(row) or {}

    async def release_application(
        self,
        application_id: int,
        worker_id: str,
        *,
        next_attempt_at: dt.datetime | None = None,
        error: str | None = None,
        state: str = "approved_for_browser",
    ) -> dict[str, Any]:
        """Release a pre-submit browser lease owned by ``worker_id``."""

        target = str(_enum_value(state))
        if target == "submitting":
            raise SafetyViolation("submitting work cannot be released for automatic retry")
        async with self._connection(transaction=True) as connection:
            current = await connection.fetchrow(
                """
                SELECT * FROM applications
                WHERE id = $1 AND locked_by = $2
                FOR UPDATE
                """,
                application_id,
                worker_id,
            )
            if current is None:
                raise RepositoryConflict("application lease is not owned by this worker")
            if current["state"] == "submitting":
                target = "needs_manual_verification"
            row = await connection.fetchrow(
                """
                UPDATE applications
                SET state = $3, locked_by = NULL, locked_until = NULL,
                    next_attempt_at = COALESCE($4, CURRENT_TIMESTAMP),
                    last_error = $5,
                    retry_count = retry_count + CASE WHEN $5::text IS NULL THEN 0 ELSE 1 END
                WHERE id = $1 AND locked_by = $2
                RETURNING *
                """,
                application_id,
                worker_id,
                target,
                next_attempt_at,
                error[:4000] if error else None,
            )
            await self._add_event(
                connection,
                application_id,
                str(current["state"]),
                target,
                worker_id,
                "browser_lease_released",
                {"error": error} if error else {},
            )
            return _row_dict(row) or {}

    async def mark_manual_verification(
        self,
        application_id: int,
        *,
        error: str,
        actor: str = "browser_worker",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.transition_application(
            application_id,
            "needs_manual_verification",
            actor=actor,
            event_type="submission_outcome_uncertain",
            metadata=metadata,
            last_error=error,
        )

    async def reject_vacancy_for_browser(
        self,
        vacancy_id: int,
        application_id: int,
        *,
        reason: str,
        actor: str = "browser_worker",
    ) -> dict[str, Any]:
        async with self._connection(transaction=True) as connection:
            current = await connection.fetchrow(
                """
                SELECT * FROM applications
                WHERE id = $1 AND vacancy_id = $2
                FOR UPDATE
                """,
                application_id,
                vacancy_id,
            )
            if current is None:
                raise NotFoundError("vacancy/application pair not found")
            if current["state"] == "submitting" or current["is_submitted"]:
                raise SafetyViolation("cannot reject after submission starts")
            await connection.execute(
                """
                UPDATE job_vacancies
                SET state = 'rejected', ai_reason = $2
                WHERE id = $1 AND is_submitted = FALSE
                """,
                vacancy_id,
                reason,
            )
            row = await connection.fetchrow(
                """
                UPDATE applications
                SET state = 'rejected', last_error = $2,
                    locked_by = NULL, locked_until = NULL
                WHERE id = $1
                RETURNING *
                """,
                application_id,
                reason[:4000],
            )
            await self._add_event(
                connection,
                application_id,
                str(current["state"]),
                "rejected",
                actor,
                "browser_vacancy_rejected",
                {"reason": reason},
            )
            return _row_dict(row) or {}

    async def create_captcha_checkpoint(
        self,
        application_id: int,
        *,
        page_url: str,
        screenshot_path: str | None,
        persistent_session_key: str,
        instructions: str,
        actor: str = "browser_worker",
    ) -> dict[str, Any]:
        """Pause an application for a human; no CAPTCHA solving is attempted."""

        async with self._connection(transaction=True) as connection:
            application = await connection.fetchrow(
                "SELECT * FROM applications WHERE id = $1 FOR UPDATE", application_id
            )
            if application is None:
                raise NotFoundError(f"application {application_id} not found")
            if application["is_submitted"] or application["state"] == "submitting":
                raise SafetyViolation("CAPTCHA checkpoint cannot be added after submit starts")
            existing = await connection.fetchrow(
                """
                SELECT * FROM captcha_checkpoints
                WHERE application_id = $1 AND state = 'waiting_human'
                FOR UPDATE
                """,
                application_id,
            )
            if existing is None:
                checkpoint = await connection.fetchrow(
                    """
                    INSERT INTO captcha_checkpoints (
                        application_id, state, page_url, screenshot_path,
                        persistent_session_key, instructions
                    )
                    VALUES ($1, 'waiting_human', $2, $3, $4, $5)
                    RETURNING *
                    """,
                    application_id,
                    page_url,
                    screenshot_path,
                    persistent_session_key,
                    instructions,
                )
            else:
                checkpoint = existing
            old_state = str(application["state"])
            await connection.execute(
                """
                UPDATE applications
                SET state = 'captcha_required', locked_by = NULL, locked_until = NULL
                WHERE id = $1
                """,
                application_id,
            )
            if old_state != "captcha_required":
                await self._add_event(
                    connection,
                    application_id,
                    old_state,
                    "captcha_required",
                    actor,
                    "captcha_detected",
                    {
                        "checkpoint_id": checkpoint["id"],
                        "page_url": page_url,
                        "screenshot_path": screenshot_path,
                        "instructions": instructions,
                    },
                )
            return _row_dict(checkpoint) or {}

    async def get_captcha_checkpoint(
        self, checkpoint_id: int
    ) -> dict[str, Any] | None:
        async with self._connection() as connection:
            return _row_dict(
                await connection.fetchrow(
                    "SELECT * FROM captcha_checkpoints WHERE id = $1", checkpoint_id
                )
            )

    async def list_captcha_checkpoints(
        self,
        *,
        state: str | None = None,
        application_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with self._connection() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM captcha_checkpoints
                WHERE ($1::text IS NULL OR state = $1)
                  AND ($2::bigint IS NULL OR application_id = $2)
                ORDER BY created_at DESC, id DESC
                LIMIT $3
                """,
                _enum_value(state),
                application_id,
                max(1, min(limit, 500)),
            )
        return [_row_dict(row) or {} for row in rows]

    async def resolve_captcha_checkpoint(
        self,
        checkpoint_id: int,
        *,
        state: str,
        actor: str = "human",
        note: str | None = None,
    ) -> dict[str, Any]:
        """Mark resolved/aborted and requeue only for a fresh CAPTCHA check."""

        target = str(_enum_value(state))
        if target not in {"resolved", "aborted"}:
            raise ValueError("checkpoint target must be resolved or aborted")
        async with self._connection(transaction=True) as connection:
            checkpoint = await connection.fetchrow(
                "SELECT * FROM captcha_checkpoints WHERE id = $1 FOR UPDATE",
                checkpoint_id,
            )
            if checkpoint is None:
                raise NotFoundError(f"CAPTCHA checkpoint {checkpoint_id} not found")
            if checkpoint["state"] != "waiting_human":
                if checkpoint["state"] == target:
                    return _row_dict(checkpoint) or {}
                raise RepositoryConflict("CAPTCHA checkpoint is already finalized")
            application = await connection.fetchrow(
                "SELECT * FROM applications WHERE id = $1 FOR UPDATE",
                checkpoint["application_id"],
            )
            new_application_state = (
                "approved_for_browser" if target == "resolved" else "rejected"
            )
            row = await connection.fetchrow(
                """
                UPDATE captcha_checkpoints
                SET state = $2, resolved_at = CURRENT_TIMESTAMP
                WHERE id = $1
                RETURNING *
                """,
                checkpoint_id,
                target,
            )
            await connection.execute(
                """
                UPDATE applications
                SET state = $2, next_attempt_at = CURRENT_TIMESTAMP,
                    locked_by = NULL, locked_until = NULL
                WHERE id = $1 AND is_submitted = FALSE
                """,
                checkpoint["application_id"],
                new_application_state,
            )
            await self._add_event(
                connection,
                checkpoint["application_id"],
                str(application["state"]),
                new_application_state,
                actor,
                f"captcha_{target}",
                {
                    "checkpoint_id": checkpoint_id,
                    "must_recheck": target == "resolved",
                    "note": note,
                },
            )
            return _row_dict(row) or {}

    async def record_submission_proof_and_mark_submitted(
        self,
        application_id: int,
        *,
        uploaded_resume_sha256: str,
        submitted_payload_hash: str,
        before_screenshot_path: str | None = None,
        after_screenshot_path: str | None = None,
        confirmation_text: str | None = None,
        confirmation_url: str | None = None,
        provider_application_id: str | None = None,
        proof_hash: str | None = None,
        actor: str = "browser_worker",
    ) -> dict[str, Any]:
        """Atomically store proof, then set application and vacancy submitted."""

        if not confirmation_text and not provider_application_id:
            raise SafetyViolation(
                "business confirmation text or provider application id is required"
            )
        evidence_hash = proof_hash or build_submission_proof_hash(
            application_id=application_id,
            uploaded_resume_sha256=uploaded_resume_sha256,
            submitted_payload_hash=submitted_payload_hash,
            confirmation_text=confirmation_text,
            confirmation_url=confirmation_url,
            provider_application_id=provider_application_id,
            before_screenshot_path=before_screenshot_path,
            after_screenshot_path=after_screenshot_path,
        )
        async with self._connection(transaction=True) as connection:
            current = await connection.fetchrow(
                "SELECT * FROM applications WHERE id = $1 FOR UPDATE", application_id
            )
            if current is None:
                raise NotFoundError(f"application {application_id} not found")
            existing_proof = await connection.fetchrow(
                "SELECT * FROM submission_proofs WHERE application_id = $1",
                application_id,
            )
            if current["is_submitted"]:
                if (
                    existing_proof is not None
                    and existing_proof["submitted_payload_hash"]
                    == submitted_payload_hash
                    and existing_proof["uploaded_resume_sha256"]
                    == uploaded_resume_sha256
                ):
                    return _row_dict(current) or {}
                raise SafetyViolation("application was already submitted with other evidence")
            if current["state"] != "submitting":
                raise RepositoryConflict("application must enter submitting before proof")
            if (
                current["approved_at"] is None
                or current["approved_payload_hash"] != current["form_payload_hash"]
                or current["approved_resume_sha256"] != current["resume_sha256"]
                or submitted_payload_hash != current["form_payload_hash"]
                or uploaded_resume_sha256 != current["resume_sha256"]
            ):
                raise SafetyViolation("proof does not match the exact approved payload/resume")
            if existing_proof is None:
                await connection.fetchrow(
                    """
                    INSERT INTO submission_proofs (
                        application_id, before_screenshot_path, after_screenshot_path,
                        confirmation_text, confirmation_url, provider_application_id,
                        uploaded_resume_sha256, submitted_payload_hash, proof_hash
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    RETURNING *
                    """,
                    application_id,
                    before_screenshot_path,
                    after_screenshot_path,
                    confirmation_text,
                    confirmation_url,
                    provider_application_id,
                    uploaded_resume_sha256,
                    submitted_payload_hash,
                    evidence_hash,
                )
            elif (
                existing_proof["submitted_payload_hash"] != submitted_payload_hash
                or existing_proof["uploaded_resume_sha256"] != uploaded_resume_sha256
            ):
                raise SafetyViolation("existing proof does not match current application")
            confirmation = confirmation_text or provider_application_id
            row = await connection.fetchrow(
                """
                UPDATE applications
                SET state = 'submitted', is_submitted = TRUE,
                    submitted_at = CURRENT_TIMESTAMP, external_confirmation = $2,
                    locked_by = NULL, locked_until = NULL, last_error = NULL
                WHERE id = $1
                RETURNING *
                """,
                application_id,
                confirmation,
            )
            await connection.execute(
                """
                UPDATE job_vacancies
                SET state = 'submitted', is_submitted = TRUE,
                    submitted_at = CURRENT_TIMESTAMP, external_confirmation = $2
                WHERE id = $1
                """,
                current["vacancy_id"],
                confirmation,
            )
            await self._add_event(
                connection,
                application_id,
                "submitting",
                "submitted",
                actor,
                "submission_confirmed",
                {
                    "proof_hash": evidence_hash,
                    "provider_application_id": provider_application_id,
                },
            )
            return _row_dict(row) or {}

    async def get_submission_proof(
        self, application_id: int
    ) -> dict[str, Any] | None:
        async with self._connection() as connection:
            return _row_dict(
                await connection.fetchrow(
                    "SELECT * FROM submission_proofs WHERE application_id = $1",
                    application_id,
                )
            )

    async def list_submission_proofs(
        self, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self._connection() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM submission_proofs
                ORDER BY created_at DESC, id DESC
                LIMIT $1
                """,
                max(1, min(limit, 500)),
            )
        return [_row_dict(row) or {} for row in rows]

    async def list_application_events(
        self, application_id: int, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        async with self._connection() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM application_events
                WHERE application_id = $1
                ORDER BY created_at ASC, id ASC
                LIMIT $2
                """,
                application_id,
                max(1, min(limit, 1000)),
            )
        return [_row_dict(row) or {} for row in rows]

    async def get_cached_llm_call(
        self, purpose: str, model: str, input_hash: str
    ) -> dict[str, Any] | None:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM llm_calls
                WHERE purpose = $1 AND model = $2 AND input_hash = $3
                  AND status = 'completed' AND cache_hit = FALSE
                ORDER BY id ASC
                LIMIT 1
                """,
                purpose,
                model,
                input_hash,
            )
        return _row_dict(row)

    async def record_llm_call(
        self,
        *,
        purpose: str,
        model: str,
        prompt_hash: str,
        input_hash: str,
        response: Mapping[str, Any],
        vacancy_id: int | None = None,
        application_id: int | None = None,
        cache_hit: bool = False,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        status: str = "completed",
    ) -> dict[str, Any]:
        async with self._connection(transaction=True) as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO llm_calls (
                    purpose, vacancy_id, application_id, model, prompt_hash,
                    input_hash, status, cache_hit, input_tokens, output_tokens, response
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
                ON CONFLICT (purpose, model, input_hash)
                    WHERE status = 'completed' AND cache_hit = FALSE
                DO NOTHING
                RETURNING *
                """,
                purpose,
                vacancy_id,
                application_id,
                model,
                prompt_hash,
                input_hash,
                status,
                cache_hit,
                input_tokens,
                output_tokens,
                canonical_json(response),
            )
            if row is None and status == "completed" and not cache_hit:
                row = await connection.fetchrow(
                    """
                    SELECT * FROM llm_calls
                    WHERE purpose = $1 AND model = $2 AND input_hash = $3
                      AND status = 'completed' AND cache_hit = FALSE
                    LIMIT 1
                    """,
                    purpose,
                    model,
                    input_hash,
                )
            if row is None:
                raise RepositoryConflict("LLM call could not be recorded")
            return _row_dict(row) or {}

    async def list_llm_calls(
        self,
        *,
        vacancy_id: int | None = None,
        application_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with self._connection() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM llm_calls
                WHERE ($1::bigint IS NULL OR vacancy_id = $1)
                  AND ($2::bigint IS NULL OR application_id = $2)
                ORDER BY created_at DESC, id DESC
                LIMIT $3
                """,
                vacancy_id,
                application_id,
                max(1, min(limit, 500)),
            )
        return [_row_dict(row) or {} for row in rows]


# Descriptive alias retained for worker wiring and third-party adapters.
VacancyPipelineRepository = PipelineRepository
