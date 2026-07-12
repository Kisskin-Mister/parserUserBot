import unittest
from unittest.mock import AsyncMock

import main


class RecruiterSyncSkipTest(unittest.IsolatedAsyncioTestCase):
    async def test_sync_skips_recruiter_private_unread(self):
        recruiter_msg = AsyncMock()
        recruiter_msg.chat.id = 1
        recruiter_msg.chat.type = main.enums.ChatType.PRIVATE
        recruiter_msg.from_user = AsyncMock()
        recruiter_msg.from_user.is_self = False

        dialog = AsyncMock()
        dialog.unread_messages_count = 1
        dialog.chat.id = 1

        client = AsyncMock()

        async def fake_dialogs():
            yield dialog

        async def fake_history(chat_id, limit):
            yield recruiter_msg

        client.get_dialogs = fake_dialogs
        client.get_chat_history = fake_history

        original_process = main.process_message
        mock_process = AsyncMock()
        main.process_message = mock_process
        try:
            await main.catch_up_unread(client)
        finally:
            main.process_message = original_process

        self.assertEqual(mock_process.await_count, 0)
        client.read_chat_history.assert_not_called()


if __name__ == '__main__':
    unittest.main()
