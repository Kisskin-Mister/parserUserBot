import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import mcp_server


class McpServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db = mcp_server.db
        self.mock_db = AsyncMock()
        self.mock_db.has_vacancy_outbox_entry.return_value = False
        mcp_server.db = self.mock_db

    async def asyncTearDown(self):
        mcp_server.db = self.original_db

    async def test_apply_to_vacancy_returns_error_when_not_found(self):
        self.mock_db.get_vacancy_by_id.return_value = None

        result = await mcp_server.apply_to_vacancy(1, "hello")

        self.assertEqual(result, {"error": "Vacancy not found"})
        self.mock_db.add_command.assert_not_called()

    async def test_apply_to_vacancy_rejects_already_applied_status(self):
        self.mock_db.get_vacancy_by_id.return_value = {"recruiter_username": "@recruiter", "status": "applied"}

        result = await mcp_server.apply_to_vacancy(7, "Hi there")

        self.assertEqual(result, {"error": "Vacancy already applied", "vacancy_id": 7})
        self.mock_db.add_command.assert_not_called()

    async def test_apply_to_vacancy_rejects_existing_outbox_history(self):
        self.mock_db.get_vacancy_by_id.return_value = {"recruiter_username": "@recruiter", "status": "pending"}
        self.mock_db.has_vacancy_outbox_entry.return_value = True

        result = await mcp_server.apply_to_vacancy(8, "Hi there")

        self.assertEqual(result, {"error": "Vacancy already applied", "vacancy_id": 8})
        self.mock_db.add_command.assert_not_called()

    async def test_apply_to_vacancy_enqueues_message_and_resume(self):
        self.mock_db.get_vacancy_by_id.return_value = {"recruiter_username": "@recruiter", "status": "pending"}

        with tempfile.NamedTemporaryFile() as tmp:
            with patch.dict(os.environ, {"RESUME_PATH": tmp.name}, clear=False):
                result = await mcp_server.apply_to_vacancy(7, "Hi there")

        self.assertEqual(result, {"status": "success", "recruiter": "@recruiter"})
        self.mock_db.add_command.assert_any_await("send_message", "@recruiter", "Hi there")
        self.mock_db.add_command.assert_any_await("send_document", "@recruiter", tmp.name)
        self.mock_db.add_vacancy_outbox_entry.assert_any_await(7, "@recruiter", "send_message", "Hi there")
        self.mock_db.add_vacancy_outbox_entry.assert_any_await(7, "@recruiter", "send_document", tmp.name)
        self.mock_db.update_vacancy_status.assert_awaited_with(7, "applied")
        self.mock_db.add_interaction.assert_awaited_with("@recruiter")

    async def test_send_status_report_uses_owner_username_from_env(self):
        with patch.dict(os.environ, {"OWNER_USERNAME": "dev-agent"}, clear=False):
            result = await mcp_server.send_status_report("status text")

        self.assertEqual(result, {"status": "report_sent"})
        self.mock_db.add_command.assert_awaited()

    async def test_post_to_channel_requires_log_chat_id(self):
        with patch.dict(os.environ, {}, clear=True):
            result = await mcp_server.post_to_channel("hello")

        self.assertEqual(result, {"error": "LOG_CHAT_ID not set"})

    async def test_repost_news_builds_command_payload(self):
        self.mock_db.get_news_by_id.return_value = {
            "chat_id": -1001234567890,
            "message_id": 55,
            "text": "Важная новость\nподробности ниже",
        }

        with patch.dict(os.environ, {"NEWS_CHAT_ID": "999"}, clear=False):
            result = await mcp_server.repost_news(12)

        self.assertEqual(result["status"], "repost_queued")
        self.assertEqual(result["target_chat_id"], "999")
        self.assertIn("https://t.me/c/1234567890/55", result["source_link"])
        self.mock_db.add_command.assert_awaited()


if __name__ == "__main__":
    unittest.main()
