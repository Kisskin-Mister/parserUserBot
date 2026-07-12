import asyncpg
import datetime
import os
import json
from dotenv import load_dotenv

load_dotenv()


class Database:
    def __init__(self):
        self.url = os.getenv("DATABASE_URL")
        self.pool = None

    async def init(self):
        if not self.pool:
            self.pool = await asyncpg.create_pool(self.url)

        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS vacancies (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    message_id BIGINT,
                    chat_name TEXT,
                    text TEXT,
                    recruiter_username TEXT,
                    status TEXT DEFAULT 'pending',
                    tech TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, message_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS recruiter_interactions (
                    username TEXT PRIMARY KEY,
                    last_interaction TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS recruiter_private_notifications (
                    chat_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, message_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS vacancy_outbox (
                    id BIGSERIAL PRIMARY KEY,
                    vacancy_id BIGINT NOT NULL,
                    recruiter_username TEXT,
                    command_type TEXT NOT NULL,
                    payload TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_vacancy_outbox_vacancy_command
                ON vacancy_outbox(vacancy_id, command_type)
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS news (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    message_id BIGINT,
                    chat_name TEXT,
                    text TEXT,
                    source_link TEXT,
                    repost_chat_id BIGINT,
                    repost_message_id BIGINT,
                    is_checked BOOLEAN DEFAULT FALSE,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, message_id)
                )
            """)
            await conn.execute("ALTER TABLE news ADD COLUMN IF NOT EXISTS repost_chat_id BIGINT")
            await conn.execute("ALTER TABLE news ADD COLUMN IF NOT EXISTS repost_message_id BIGINT")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS commands (
                    id SERIAL PRIMARY KEY,
                    command_type TEXT,
                    target TEXT,
                    data TEXT,
                    dedupe_key TEXT,
                    status TEXT DEFAULT 'pending'
                )
            """)
            await conn.execute("ALTER TABLE commands ADD COLUMN IF NOT EXISTS dedupe_key TEXT")
            await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_commands_dedupe_key ON commands(dedupe_key) WHERE dedupe_key IS NOT NULL")
            await conn.execute("ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS callback_notified_at TIMESTAMP NULL")
            await conn.execute("ALTER TABLE news ADD COLUMN IF NOT EXISTS callback_notified_at TIMESTAMP NULL")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS callback_batches (
                    id BIGSERIAL PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sent_at TIMESTAMP NULL,
                    UNIQUE(entity_type, fingerprint)
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_callback_batches_status_created_at
                ON callback_batches(status, created_at)
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS callback_outbox (
                    id BIGSERIAL PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    batch_id BIGINT REFERENCES callback_batches(id) ON DELETE SET NULL,
                    session_key TEXT NOT NULL,
                    message TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 10,
                    next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_error TEXT,
                    last_response TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sent_at TIMESTAMP NULL,
                    UNIQUE(idempotency_key)
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_callback_outbox_status_next_attempt
                ON callback_outbox(status, next_attempt_at)
            """)

    async def add_vacancy(self, chat_id, message_id, chat_name, text, recruiter_username, tech):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO vacancies (chat_id, message_id, chat_name, text, recruiter_username, tech)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (chat_id, message_id) DO NOTHING""",
                chat_id, message_id, chat_name, text, recruiter_username, tech
            )

    async def add_news(self, chat_id, message_id, chat_name, text, source_link):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO news (chat_id, message_id, chat_name, text, source_link)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (chat_id, message_id) DO NOTHING""",
                chat_id, message_id, chat_name, text, source_link
            )

    async def get_unread_news(self, limit=50):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM news WHERE is_checked = FALSE ORDER BY timestamp DESC LIMIT $1", limit)
            return [dict(r) for r in rows]

    async def mark_news_as_checked(self, news_ids):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE news SET is_checked = TRUE WHERE id = ANY($1)", news_ids)

    async def get_pending_vacancies(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM vacancies WHERE status = 'pending'")
            return [dict(r) for r in rows]

    async def update_vacancy_status(self, vacancy_id, status):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE vacancies SET status = $1 WHERE id = $2", status, vacancy_id)

    async def add_command(self, cmd_type, target, data):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO commands (command_type, target, data) VALUES ($1, $2, $3)",
                cmd_type, target, data
            )

    async def get_pending_commands(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM commands WHERE status = 'pending'")
            return [dict(r) for r in rows]

    async def mark_command_done(self, cmd_id, status='completed'):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE commands SET status = $1 WHERE id = $2", status, cmd_id)

    async def can_interact_with_recruiter(self, username):
        if not username:
            return True
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT last_interaction FROM recruiter_interactions WHERE username = $1", username)
            if not row:
                return True
            return False

    async def add_interaction(self, username):
        if not username:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO recruiter_interactions (username, last_interaction) VALUES ($1, $2) "
                "ON CONFLICT (username) DO UPDATE SET last_interaction = EXCLUDED.last_interaction",
                username, datetime.datetime.now()
            )

    async def get_vacancy_by_id(self, vacancy_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM vacancies WHERE id = $1", vacancy_id)
            return dict(row) if row else None

    async def has_vacancy_outbox_entry(self, vacancy_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM vacancy_outbox WHERE vacancy_id = $1 LIMIT 1",
                vacancy_id,
            )
            return row is not None

    async def add_vacancy_outbox_entry(self, vacancy_id, recruiter_username, command_type, payload):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO vacancy_outbox (vacancy_id, recruiter_username, command_type, payload)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (vacancy_id, command_type) DO NOTHING
                """,
                vacancy_id,
                recruiter_username,
                command_type,
                payload,
            )

    async def get_vacancy_outbox(self, vacancy_id):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM vacancy_outbox WHERE vacancy_id = $1 ORDER BY created_at ASC, id ASC",
                vacancy_id,
            )
            return [dict(r) for r in rows]

    async def delete_vacancy(self, vacancy_id):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM vacancies WHERE id = $1", vacancy_id)

    async def get_news_by_id(self, news_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM news WHERE id = $1", news_id)
            return dict(row) if row else None

    async def delete_news(self, news_id):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM news WHERE id = $1", news_id)

    async def set_news_repost(self, news_id, repost_chat_id, repost_message_id):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE news SET repost_chat_id = $1, repost_message_id = $2 WHERE id = $3",
                repost_chat_id, repost_message_id, news_id
            )

    async def get_unnotified_vacancies(self, limit=50):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM vacancies
                WHERE status = 'pending' AND callback_notified_at IS NULL
                ORDER BY timestamp ASC, id ASC
                LIMIT $1
                """,
                limit,
            )
            return [dict(r) for r in rows]

    async def get_unnotified_news(self, limit=50):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM news
                WHERE is_checked = FALSE AND callback_notified_at IS NULL
                ORDER BY timestamp ASC, id ASC
                LIMIT $1
                """,
                limit,
            )
            return [dict(r) for r in rows]

    async def create_or_get_callback_batch(self, entity_type, fingerprint, payload):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO callback_batches (entity_type, fingerprint, payload, status)
                VALUES ($1, $2, $3::jsonb, 'pending')
                ON CONFLICT (entity_type, fingerprint)
                DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                entity_type,
                fingerprint,
                json.dumps(payload, ensure_ascii=False, default=str),
            )
            return dict(row) if row else None

    async def touch_callback_batch_attempt(self, batch_id):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE callback_batches
                SET attempts = attempts + 1,
                    updated_at = CURRENT_TIMESTAMP,
                    status = 'sending'
                WHERE id = $1
                """,
                batch_id,
            )

    async def mark_callback_batch_sent(self, batch_id):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE callback_batches
                SET status = 'sent',
                    sent_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    last_error = NULL
                WHERE id = $1
                """,
                batch_id,
            )

    async def mark_callback_batch_failed(self, batch_id, error_text):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE callback_batches
                SET status = 'pending',
                    last_error = $2,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                batch_id,
                error_text[:4000],
            )

    async def mark_entities_as_notified(self, entity_type, entity_ids):
        if not entity_ids:
            return
        table = 'vacancies' if entity_type == 'vacancies' else 'news'
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {table} SET callback_notified_at = CURRENT_TIMESTAMP WHERE id = ANY($1)",
                entity_ids,
            )

    async def enqueue_callback_outbox(self, entity_type, batch_id, session_key, message, idempotency_key, max_attempts=10):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO callback_outbox (entity_type, batch_id, session_key, message, idempotency_key, max_attempts)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (idempotency_key)
                DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                entity_type,
                batch_id,
                session_key,
                message,
                idempotency_key,
                max_attempts,
            )
            return dict(row) if row else None

    async def get_due_callback_outbox(self, limit=20):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM callback_outbox
                WHERE status IN ('pending', 'failed')
                  AND attempts < max_attempts
                  AND next_attempt_at <= CURRENT_TIMESTAMP
                ORDER BY created_at ASC, id ASC
                LIMIT $1
                """,
                limit,
            )
            return [dict(r) for r in rows]

    async def mark_callback_outbox_sending(self, outbox_id):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE callback_outbox
                SET status = 'sending',
                    attempts = attempts + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                outbox_id,
            )

    async def mark_callback_outbox_sent(self, outbox_id, response_text=None):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE callback_outbox
                SET status = 'sent',
                    last_response = $2,
                    last_error = NULL,
                    sent_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                outbox_id,
                response_text,
            )

    async def mark_callback_outbox_failed(self, outbox_id, error_text, response_text=None):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT attempts, max_attempts FROM callback_outbox WHERE id = $1", outbox_id)
            attempts = int(row['attempts']) if row else 0
            max_attempts = int(row['max_attempts']) if row else 10
            status = 'dead' if attempts >= max_attempts else 'failed'
            delay_minutes = min(max(1, attempts), 10)
            await conn.execute(
                """
                UPDATE callback_outbox
                SET status = $2,
                    last_error = $3,
                    last_response = $4,
                    next_attempt_at = CURRENT_TIMESTAMP + ($5::text || ' minutes')::interval,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                outbox_id,
                status,
                (error_text or '')[:4000],
                (response_text or '')[:4000],
                delay_minutes,
            )

    async def get_callback_outbox_stats(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT status, count(*) AS total FROM callback_outbox GROUP BY status"
            )
            return {str(r['status']): int(r['total']) for r in rows}

