import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import main
from parser_engine import MatchResult


class ParserRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_classifier_error_is_retryable_and_message_is_not_marked_read(self):
        client = AsyncMock()
        message = SimpleNamespace(
            id=42,
            text="Java developer vacancy",
            caption=None,
            chat=SimpleNamespace(id=-100123, title="Jobs", first_name=None, type="channel"),
            entities=None,
            from_user=None,
        )
        parser = {
            "id": 1,
            "mode": "keyword",
            "source_chat_ids": [],
            "target_chat_id": -100999,
            "config": {},
        }
        fake_db = AsyncMock()
        fake_db.list_parser_configs.return_value = [parser]
        fake_db.try_mark_message_processing.return_value = True

        original_db = main.db
        original_match = main.match_parser_config
        original_excluded = main.EXCLUDED_CHAT_IDS
        original_ignore_private = main.IGNORE_PRIVATE_CHATS
        main.db = fake_db
        main.match_parser_config = AsyncMock(
            return_value=MatchResult(False, reason="classifier unavailable", error="HTTP 429 quota exhausted")
        )
        main.EXCLUDED_CHAT_IDS = set()
        main.IGNORE_PRIVATE_CHATS = True
        try:
            result = await main.process_message(client, message)
        finally:
            main.db = original_db
            main.match_parser_config = original_match
            main.EXCLUDED_CHAT_IDS = original_excluded
            main.IGNORE_PRIVATE_CHATS = original_ignore_private

        self.assertFalse(result)
        fake_db.mark_processed_message.assert_awaited_once()
        client.read_chat_history.assert_not_awaited()
        client.forward_messages.assert_not_awaited()

    async def test_unread_dialog_is_not_marked_read_when_any_message_needs_retry(self):
        client = AsyncMock()
        chat = SimpleNamespace(id=-100123)
        dialog = SimpleNamespace(chat=chat, unread_messages_count=1)
        message = SimpleNamespace(id=42, chat=chat)

        async def dialogs():
            yield dialog

        async def history(_chat_id, limit):
            assert limit == 1
            yield message

        client.get_dialogs = dialogs
        client.get_chat_history = history
        original_process = main.process_message
        original_recruiter = main.is_recruiter_private_message
        original_should_mark = main.should_mark_chat_as_read
        main.process_message = AsyncMock(return_value=False)
        main.is_recruiter_private_message = lambda _message: False
        main.should_mark_chat_as_read = lambda _message: True
        try:
            await main._process_unread_dialogs(client, mark_as_read=True)
        finally:
            main.process_message = original_process
            main.is_recruiter_private_message = original_recruiter
            main.should_mark_chat_as_read = original_should_mark

        client.read_chat_history.assert_not_awaited()

    async def test_pending_backoff_is_not_marked_read_or_sent_to_legacy_path(self):
        client = AsyncMock()
        message = SimpleNamespace(
            id=43,
            text="Java developer vacancy",
            caption=None,
            chat=SimpleNamespace(id=-100123, title="Jobs", first_name=None, type="channel"),
            entities=None,
            from_user=None,
        )
        parser = {"id": 1, "mode": "keyword", "source_chat_ids": [], "target_chat_id": -100999, "config": {}}
        fake_db = AsyncMock()
        fake_db.list_parser_configs.return_value = [parser]
        fake_db.try_mark_message_processing.return_value = False
        fake_db.get_processed_message.return_value = {"error": "HTTP 429", "retry_count": 1}

        original_db = main.db
        original_excluded = main.EXCLUDED_CHAT_IDS
        original_ignore_private = main.IGNORE_PRIVATE_CHATS
        main.db = fake_db
        main.EXCLUDED_CHAT_IDS = set()
        main.IGNORE_PRIVATE_CHATS = True
        try:
            result = await main.process_message(client, message)
        finally:
            main.db = original_db
            main.EXCLUDED_CHAT_IDS = original_excluded
            main.IGNORE_PRIVATE_CHATS = original_ignore_private

        self.assertFalse(result)
        client.read_chat_history.assert_not_awaited()
        client.forward_messages.assert_not_awaited()
        fake_db.add_vacancy.assert_not_awaited()

    def test_database_schema_contains_bounded_retry_backoff(self):
        with open("database.py", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("retry_count INTEGER NOT NULL DEFAULT 0", source)
        self.assertIn("next_retry_at TIMESTAMP NULL", source)
        self.assertIn("processed_messages.retry_count < 10", source)
        self.assertIn("LEAST(3600", source)


if __name__ == "__main__":
    unittest.main()
