import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fabric"))
from synthetic import generate_records, validate_records, processing_start, SURFACES


class SyntheticUsageTests(unittest.TestCase):
    def setUp(self):
        self.start = date(2026, 10, 7)
        self.end = date(2026, 11, 3)
        self.rows = generate_records(self.start, self.end)

    def test_exact_28_day_shape(self):
        self.assertEqual(len(self.rows), 28 * len(SURFACES))
        self.assertEqual(len({r["usage_date"] for r in self.rows}), 28)
        self.assertEqual(len({r["record_id"] for r in self.rows}), len(self.rows))

    def test_backfill_and_daily_reruns_agree(self):
        day = self.start + timedelta(days=9)
        self.assertEqual(
            [r for r in self.rows if r["usage_date"] == day.isoformat()],
            generate_records(day, day),
        )

    def test_m365_unavailable_is_not_zero(self):
        for row in self.rows:
            if row["ecosystem"] == "m365":
                for key in ("input_tokens", "output_tokens", "requests", "estimated_cost_usd"):
                    self.assertIsNone(row[key])
                self.assertEqual(row["credit_unit"], "m365_copilot_credit")

    def test_no_corporate_identity_or_actual_cost(self):
        self.assertEqual({r["subject_id"] for r in self.rows}, {"demo-user"})
        self.assertTrue(all(r["is_synthetic"] for r in self.rows))
        self.assertNotIn("billed", {r["cost_status"] for r in self.rows})

    def test_apim_is_not_an_additional_ecosystem(self):
        self.assertEqual({r["ecosystem"] for r in self.rows}, {"github", "m365", "foundry"})
        self.assertTrue(all(r["ecosystem"] == "foundry" for r in self.rows if r["source_system"] == "apim_demo"))

    def test_rejects_duplicate_and_invalid_values(self):
        with self.assertRaises(ValueError):
            validate_records(self.rows + [self.rows[0]])
        for value in (-1, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_records([{**self.rows[0], "requests": value}])
        with self.assertRaises(ValueError):
            validate_records([{**self.rows[0], "subject_id": "real-user"}])

    def test_date_range_validation(self):
        with self.assertRaises(ValueError):
            generate_records(self.end, self.start)
        with self.assertRaises(ValueError):
            generate_records(self.start, self.start + timedelta(days=366))

    def test_partial_first_run_retries_the_full_window(self):
        self.assertEqual(processing_start(self.end, None), self.end - timedelta(days=89))

    def test_completed_run_reprocesses_seven_days(self):
        self.assertEqual(processing_start(self.end, self.end), self.end - timedelta(days=6))
        self.assertEqual(processing_start(self.end, self.end - timedelta(days=1)), self.end - timedelta(days=6))

    def test_missed_days_are_backfilled_within_90_day_window(self):
        self.assertEqual(processing_start(self.end, self.end - timedelta(days=20)), self.end - timedelta(days=19))
        self.assertEqual(processing_start(self.end, self.end - timedelta(days=200)), self.end - timedelta(days=89))


if __name__ == "__main__":
    unittest.main()
