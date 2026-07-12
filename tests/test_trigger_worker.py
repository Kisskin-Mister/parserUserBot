import unittest
from unittest.mock import AsyncMock

import trigger_worker


class TriggerWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_build_payload_truncates_long_text(self):
        payload = trigger_worker.build_payload(
            "vacancies",
            [{"id": 1, "text": "x" * 1305, "timestamp": None}],
            "fp",
        )
        self.assertEqual(payload["fingerprint"], "fp")
        self.assertEqual(payload["count"], 1)
        self.assertTrue(payload["items"][0]["text"].endswith("…"))

    async def test_run_once_enqueues_outbox_and_marks_entities(self):
        fake_db = AsyncMock()
        fake_db.get_unnotified_vacancies.return_value = [{"id": 10, "text": "ищем go dev", "timestamp": None}]
        fake_db.create_or_get_callback_batch.return_value = {"id": 5, "status": "pending"}
        fake_db.enqueue_callback_outbox.return_value = {"id": 99}

        original_database = trigger_worker.Database
        try:
            trigger_worker.Database = lambda: fake_db
            result = await trigger_worker.run_once("vacancies", dry_run=False)
        finally:
            trigger_worker.Database = original_database

        self.assertEqual(result, 1)
        fake_db.enqueue_callback_outbox.assert_awaited()
        fake_db.mark_entities_as_notified.assert_awaited_with("vacancies", [10])


if __name__ == "__main__":
    unittest.main()
