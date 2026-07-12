import unittest
from unittest.mock import AsyncMock

import main


class RecruiterDedupeTest(unittest.IsolatedAsyncioTestCase):
    async def test_recruiter_private_message_is_forwarded_once(self):
        fake_client = AsyncMock()
        fake_message = AsyncMock()
        fake_message.id = 777
        fake_message.chat.id = 12345
        fake_message.chat.type = main.enums.ChatType.PRIVATE
        fake_message.chat.title = None
        fake_message.chat.first_name = "Yana"
        fake_message.text = "hello"
        fake_message.caption = None
        fake_message.entities = None
        fake_message.from_user = AsyncMock()
        fake_message.from_user.is_self = False
        fake_message.from_user.username = "yanaapon"

        original_db = main.db
        fake_db = AsyncMock()
        fake_db.recruiter_private_notification_exists.side_effect = [False, True]
        main.db = fake_db
        try:
            await main.process_message(fake_client, fake_message)
            await main.process_message(fake_client, fake_message)
        finally:
            main.db = original_db

        fake_client.send_message.assert_awaited_once()
        fake_message.forward.assert_awaited_once()
        fake_db.add_recruiter_private_notification.assert_awaited_once_with(12345, 777)


if __name__ == "__main__":
    unittest.main()
