from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from career_store import CareerStore
from job_delivery import (
    claim_deliveries,
    filter_undelivered,
    finalize_deliveries,
    record_deliveries,
    release_deliveries,
    was_delivered,
)
from job_identity import canonical_key, stable_job_id


class JobDeliveryTests(unittest.TestCase):
    def test_delivery_suppresses_same_job_from_another_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CareerStore(Path(temporary) / "career.db")
            original = {
                "company": "Example Robotics",
                "title": "Graduate Robotics Engineer",
                "location": "Singapore",
                "link": "https://jobs.example.com/roles/123?utm_source=board",
            }
            original["job_id"] = stable_job_id(original)
            store.upsert_job(original, canonical_key(original))

            self.assertEqual(record_deliveries([original], channel="TEST", store=store), 1)
            self.assertTrue(was_delivered(original, store))

            duplicate = {
                **original,
                "job_id": "JOB-ALTERNATE1",
                "link": "https://another-board.example/jobs/robotics",
            }
            self.assertTrue(was_delivered(duplicate, store))
            self.assertEqual(filter_undelivered([duplicate], store), [])

    def test_delivery_record_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CareerStore(Path(temporary) / "career.db")
            row = {
                "company": "Example AI",
                "title": "Machine Learning Intern",
                "location": "Singapore",
                "link": "https://jobs.example.ai/roles/42",
            }
            row["job_id"] = stable_job_id(row)
            store.upsert_job(row, canonical_key(row))

            self.assertEqual(record_deliveries([row], channel="TEST", store=store), 1)
            self.assertEqual(record_deliveries([row], channel="TEST", store=store), 0)
            self.assertEqual(len(store.list_job_deliveries()), 1)

    def test_failed_delivery_claim_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CareerStore(Path(temporary) / "career.db")
            row = {
                "job_id": "JOB-CLAIMTEST1",
                "company": "Example Autonomy",
                "title": "Autonomous Systems Graduate Engineer",
                "location": "Singapore",
                "link": "https://jobs.example.com/autonomy/1",
            }
            store.upsert_job(row, canonical_key(row))

            claimed = claim_deliveries([row], channel="TEST", store=store)
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claim_deliveries([row], channel="TEST", store=store), [])
            release_deliveries(claimed, store)

            retried = claim_deliveries([row], channel="TEST", store=store)
            self.assertEqual(len(retried), 1)
            finalize_deliveries(retried, channel="TEST", message_ids=[42], store=store)
            self.assertTrue(was_delivered(row, store))


if __name__ == "__main__":
    unittest.main()
