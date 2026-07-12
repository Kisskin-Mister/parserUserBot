import unittest
from unittest.mock import AsyncMock, patch

import callback_dispatcher


class CallbackDispatcherTest(unittest.IsolatedAsyncioTestCase):
    async def test_build_command_contains_chat_send_params(self):
        dispatcher = callback_dispatcher.OutboxDispatcher(AsyncMock())
        command = dispatcher.build_command(
            {
                "session_key": "agent:main:main",
                "message": "[SCRIPT_CALLBACK] hello",
                "idempotency_key": "abc",
            }
        )
        self.assertIn("chat.send", command)
        self.assertIn("agent:main:main", " ".join(command))

    async def test_dispatch_row_marks_sent_on_success(self):
        fake_db = AsyncMock()
        dispatcher = callback_dispatcher.OutboxDispatcher(fake_db)
        row = {"id": 1, "batch_id": 5, "session_key": "agent:main:main", "message": "[SCRIPT_CALLBACK] hi", "idempotency_key": "abc"}
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = "ok"
            ok = await dispatcher.dispatch_row(row, dry_run=False)
        self.assertTrue(ok)
        fake_db.mark_callback_outbox_sending.assert_awaited_with(1)
        fake_db.mark_callback_outbox_sent.assert_awaited()
        fake_db.mark_callback_batch_sent.assert_awaited_with(5)

    async def test_dispatch_row_marks_failed_on_error(self):
        fake_db = AsyncMock()
        dispatcher = callback_dispatcher.OutboxDispatcher(fake_db)
        row = {"id": 2, "batch_id": 6, "session_key": "agent:main:main", "message": "[SCRIPT_CALLBACK] hi", "idempotency_key": "abc"}
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 1
            run_mock.return_value.stderr = "boom"
            ok = await dispatcher.dispatch_row(row, dry_run=False)
        self.assertFalse(ok)
        fake_db.mark_callback_outbox_failed.assert_awaited()
        fake_db.mark_callback_batch_failed.assert_awaited_with(6, "boom")


if __name__ == "__main__":
    unittest.main()
