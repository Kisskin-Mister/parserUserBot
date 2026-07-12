import unittest
from types import SimpleNamespace

from pyrogram import enums

from classifier import classify_post
from message_policy import is_recruiter_private_message, should_mark_chat_as_read


class MainHelpersTest(unittest.IsolatedAsyncioTestCase):
    async def test_private_incoming_message_is_not_marked_read(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(type=enums.ChatType.PRIVATE),
            from_user=SimpleNamespace(is_self=False),
        )

        self.assertTrue(is_recruiter_private_message(message))
        self.assertFalse(should_mark_chat_as_read(message))

    async def test_self_message_can_be_marked_read(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(type=enums.ChatType.PRIVATE),
            from_user=SimpleNamespace(is_self=True),
        )

        self.assertFalse(is_recruiter_private_message(message))
        self.assertTrue(should_mark_chat_as_read(message))

    async def test_group_message_can_be_marked_read(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(type=enums.ChatType.GROUP),
            from_user=SimpleNamespace(is_self=False),
        )

        self.assertFalse(is_recruiter_private_message(message))
        self.assertTrue(should_mark_chat_as_read(message))

    async def test_classify_go_vacancy(self):
        post_type, tech = await classify_post('Ищем Go developer, удаленка, высокая зп')
        self.assertEqual(post_type, 'vacancy')
        self.assertEqual(tech, 'go')

    async def test_classify_java_vacancy(self):
        post_type, tech = await classify_post('Hiring Java backend engineer, remote, salary обсуждаема')
        self.assertEqual(post_type, 'vacancy')
        self.assertEqual(tech, 'java')

    async def test_classify_news(self):
        post_type, tech = await classify_post('OpenAI выпустили новый релиз модели и API обновление')
        self.assertEqual(post_type, 'news')
        self.assertIsNone(tech)

    async def test_do_not_match_go_inside_other_word(self):
        post_type, tech = await classify_post('Ищем специалиста по django и postgresql, удаленка')
        self.assertNotEqual((post_type, tech), ('vacancy', 'go'))

    async def test_do_not_match_without_job_context(self):
        post_type, tech = await classify_post('Мы обсуждали java memory model на созвоне')
        self.assertEqual((post_type, tech), (None, None))


if __name__ == '__main__':
    unittest.main()
