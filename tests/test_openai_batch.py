import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import metrics_db  # noqa: E402
import openai_batch  # noqa: E402


PRICING = {
    "gpt-5.6-luna": {
        "input_per_million": 1.0,
        "cached_input_per_million": 0.1,
        "output_per_million": 6.0,
    }
}


class FakeFiles:
    def __init__(self, owner):
        self.owner = owner

    def create(self, *, file, purpose):
        self.owner.input_line = json.loads(file.read().decode("utf-8"))
        self.owner.purpose = purpose
        return SimpleNamespace(id="file-input")

    def content(self, file_id):
        analysis = {
            "summary": "summary", "strengths": ["s"], "weaknesses": ["w"],
            "recommendations": ["r"], "timing_findings": ["t"],
            "operational_findings": [],
            "impression_strategy": {
                "summary": "test", "evidence": [],
                "next_day_policy": {
                    "post_type_priority": ["issue_diagram"],
                    "hook_type_priority": ["conclusion_first"],
                    "preferred_hours_jst": [12],
                    "target_text_min": 120, "target_text_max": 220,
                    "body_structure":
                        "fact_impact_accountability_improvement",
                    "cta_style": "specific_accountability_question",
                    "experiment_name": "test",
                },
            },
        }
        body = {
            "output": [{"content": [{"type": "output_text", "text": json.dumps(analysis)}]}],
            "usage": {"input_tokens": 1000, "output_tokens": 100,
                      "input_tokens_details": {"cached_tokens": 100}},
        }
        item = {"custom_id": self.owner.input_line["custom_id"],
                "response": {"status_code": 200, "body": body}}
        return SimpleNamespace(content=(json.dumps(item) + "\n").encode("utf-8"))


class FakeBatches:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.create_kwargs = kwargs
        return SimpleNamespace(id="batch-1", status="validating")

    def retrieve(self, batch_id):
        return SimpleNamespace(id=batch_id, status=self.owner.retrieve_status,
                               output_file_id="file-output" if self.owner.retrieve_status == "completed" else None,
                               error_file_id=None)


class FakeClient:
    def __init__(self, status="completed"):
        self.retrieve_status = status
        self.input_line = None
        self.create_kwargs = {}
        self.files = FakeFiles(self)
        self.batches = FakeBatches(self)

    def factory(self, **kwargs):
        return self


class OpenAIBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "data"
        self.state.mkdir()
        self.db = self.state / "bot_metrics.db"
        self.env = patch.dict(os.environ, {
            "OPENAI_BATCH_ENABLED": "true",
            "OPENAI_BATCH_TASKS": "daily_review,weekly_report",
            "OPENAI_MONTHLY_BUDGET_USD": "100",
            "OPENAI_BUDGET_RESERVE_USD": "0",
            "TOTAL_MONTHLY_API_BUDGET_USD": "100",
            "TOTAL_BUDGET_RESERVE_USD": "0",
            "OPENAI_DAILY_REVIEW_BUDGET_USD": "10",
            "OPENAI_WEEKLY_REVIEW_BUDGET_USD": "10",
            "MONTHLY_BUDGET_JPY": "999999",
        })
        self.env.start()
        metrics_db.init_db(self.db)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def submit(self, client, target=""):
        return openai_batch.submit_analysis(
            task_type="daily_review", payload={"x": 1}, model="gpt-5.6-luna",
            max_output_tokens=300, schema={"type": "object"}, state_dir=self.state,
            pricing=PRICING, target_json_path=target, dedupe_key="daily:2026-07-21",
            client_factory=client.factory,
        )

    def test_enabled_is_limited_to_configured_nonurgent_tasks(self):
        self.assertFalse(openai_batch.enabled("daily_review"))
        self.assertTrue(openai_batch.enabled("weekly_report"))
        self.assertFalse(openai_batch.enabled("post_generation"))

    def test_disabled_switch(self):
        with patch.dict(os.environ, {"OPENAI_BATCH_ENABLED": "false"}):
            self.assertFalse(openai_batch.enabled("daily_review"))

    def test_submit_uses_responses_endpoint_and_24h_window(self):
        client = FakeClient()
        result = self.submit(client)
        self.assertTrue(result["pending"])
        self.assertEqual(client.purpose, "batch")
        self.assertEqual(client.create_kwargs["endpoint"], "/v1/responses")
        self.assertEqual(client.create_kwargs["completion_window"], "24h")
        self.assertEqual(client.input_line["method"], "POST")
        self.assertEqual(client.input_line["url"], "/v1/responses")
        self.assertFalse(client.input_line["body"]["store"])

    def test_submit_deduplicates_custom_id(self):
        client = FakeClient()
        first = self.submit(client)
        second = self.submit(client)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        with closing(metrics_db.connect(self.db)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM openai_batch_jobs").fetchone()[0]
        self.assertEqual(count, 1)

    def test_collect_completed_applies_daily_result(self):
        client = FakeClient()
        target = self.state / "daily_reviews" / "2026-07-21.json"
        target.parent.mkdir()
        target.write_text(json.dumps({"reviewed_count": 2, "llm_pending": True}), encoding="utf-8")
        self.submit(client, str(target))
        result = openai_batch.collect(state_dir=self.state, pricing=PRICING,
                                      client_factory=client.factory)
        self.assertEqual(result["completed"], 1)
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertFalse(saved["llm_pending"])
        self.assertEqual(saved["llm_analysis"]["summary"], "summary")
        self.assertEqual(saved["llm_usage"]["processing_mode"], "batch")

    def test_batch_cost_is_half_standard_rate(self):
        client = FakeClient()
        self.submit(client)
        openai_batch.collect(state_dir=self.state, pricing=PRICING,
                             client_factory=client.factory)
        # Standard: 900 uncached input + 100 cached + 100 output = $0.00151.
        with closing(metrics_db.connect(self.db)) as conn:
            cost = conn.execute("SELECT estimated_cost_usd FROM api_usage_events").fetchone()[0]
        self.assertAlmostEqual(cost, 0.000755, places=8)

    def test_expired_batch_releases_reservation_as_failed(self):
        client = FakeClient(status="expired")
        self.submit(client)
        result = openai_batch.collect(state_dir=self.state, pricing=PRICING,
                                      client_factory=client.factory)
        self.assertEqual(result["failed"], 1)
        with closing(metrics_db.connect(self.db)) as conn:
            row = conn.execute("SELECT success,error_type,estimated_cost_usd FROM api_usage_events").fetchone()
        self.assertEqual((row[0], row[1], row[2]), (0, "expired", 0.0))

    def test_status_lists_jobs(self):
        self.submit(FakeClient())
        rows = openai_batch.status(self.state)
        self.assertEqual(rows[0]["status"], "validating")

    def test_schema_migration_is_non_destructive(self):
        metrics_db.init_db(self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("openai_batch_jobs", names)


if __name__ == "__main__":
    unittest.main()
