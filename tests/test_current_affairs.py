import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import current_affairs as ca
import metrics_db


class CurrentAffairsPhaseATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        metrics_db.init_db(self.db)
        ca.apply_migrations(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self, content_id):
        return next(row for row in ca.DEFAULT_FIXTURES if row["content_id"] == content_id)

    def test_01_politics_classification(self):
        self.assertEqual(ca.classify(self.fixture("fixture-politics"))["primary_category"], "politics_policy")

    def test_02_corporate_governance_classification(self):
        result = ca.classify(self.fixture("fixture-corporate"))
        self.assertEqual(result["primary_category"], "economy_business")
        self.assertIn("governance_accountability", result["secondary_categories"])

    def test_03_ai_classification(self):
        self.assertEqual(ca.classify(self.fixture("fixture-ai"))["primary_category"], "technology_ai")

    def test_04_ransomware_classification(self):
        self.assertEqual(ca.classify(self.fixture("fixture-cyber"))["primary_category"], "cybersecurity")

    def test_05_healthcare_is_society_and_politics(self):
        result = ca.classify(self.fixture("fixture-society"))
        self.assertEqual(result["primary_category"], "society_living")
        self.assertIn("politics_policy", result["secondary_categories"])

    def test_06_outage_classification(self):
        self.assertEqual(ca.classify(self.fixture("fixture-disaster"))["primary_category"], "disaster_infrastructure")

    def test_07_defense_classification(self):
        self.assertEqual(ca.classify(self.fixture("fixture-defense"))["primary_category"], "security_defense")

    def test_08_incident_uses_existing_pipeline(self):
        result = ca.classify(self.fixture("fixture-incident"))
        self.assertEqual(result["primary_category"], "major_incidents")
        self.assertEqual(result["route"], "existing_major_incident_pipeline")

    def test_09_multiple_categories_are_saved(self):
        ca.classify_items([self.fixture("fixture-corporate")], path=self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM content_category_assignments WHERE content_id='fixture-corporate'"
            ).fetchone()[0]
        self.assertGreaterEqual(count, 2)

    def test_10_one_primary_category(self):
        self.assertIsInstance(ca.classify(self.fixture("fixture-ai"))["primary_category"], str)

    def test_11_entertainment_romance_excluded(self):
        result = ca.classify_items([self.fixture("fixture-gossip")], path=self.db)
        self.assertEqual(result["excluded"][0]["reason"], "entertainment_gossip")

    def test_12_minor_flame_excluded(self):
        item = {**self.fixture("fixture-gossip"), "title": "軽微な芸能炎上", "summary": ""}
        self.assertTrue(ca.exclusion_reason(item))

    def test_13_sports_result_excluded(self):
        item = {**self.fixture("fixture-gossip"), "title": "試合結果だけを紹介", "summary": ""}
        self.assertEqual(ca.exclusion_reason(item), "sports_results_only")

    def test_14_product_release_only_excluded(self):
        item = {**self.fixture("fixture-gossip"), "title": "新商品発売を発表", "summary": ""}
        self.assertEqual(ca.exclusion_reason(item), "product_release_only")

    def test_15_corporate_public_interest_not_excluded(self):
        self.assertEqual(ca.exclusion_reason(self.fixture("fixture-corporate")), "")

    def test_16_entertainment_structural_issue_allowed(self):
        item = {**self.fixture("fixture-gossip"), "title": "芸能業界の内部統制と労働問題",
                "summary": "プラットフォームの責任と規制"}
        self.assertEqual(ca.exclusion_reason(item), "")

    def test_17_to_23_all_scores_exist(self):
        scores = ca.score_item(self.fixture("fixture-corporate"))
        for key in ("social_impact", "household_impact", "economic_impact",
                    "systemic_issue", "accountability_value", "public_safety_value",
                    "brand_fit"):
            self.assertIn(key, scores)
            self.assertGreaterEqual(scores[key], 0)

    def test_24_low_brand_fit_excluded(self):
        result = ca.classify_items([self.fixture("fixture-gossip")], path=self.db)
        self.assertLess(result["excluded"][0]["brand_fit_score"], 6.5)

    def test_25_to_29_source_categories_exist(self):
        sources = ca.source_health(self.db)
        categories = {row["category"] for row in sources["sources"]}
        for category in ("politics_policy", "economy_business", "technology_ai",
                         "cybersecurity", "disaster_infrastructure"):
            self.assertIn(category, categories)

    def test_30_no_html_scraping(self):
        result = ca.source_health(self.db)
        self.assertFalse(result["html_scraping"])
        self.assertFalse(result["browser_automation"])
        self.assertFalse(result["unofficial_api"])

    def test_31_category_candidates_generated(self):
        result = ca.category_candidates(dry_run=True, path=self.db)
        self.assertGreaterEqual(len(result["counts"]), 6)

    def test_32_politics_floor(self):
        rows = [{"primary_category": "politics_policy", "current_affairs_score": 7}] * 3
        rows += [{"primary_category": "technology_ai", "current_affairs_score": 9}] * 7
        selected = ca.select_balanced(rows, 8)
        ratio = sum(r["primary_category"] == "politics_policy" for r in selected) / len(selected)
        self.assertGreaterEqual(ratio, .25)

    def test_33_single_category_cap(self):
        rows = [{"primary_category": "politics_policy", "current_affairs_score": 8}] * 3
        rows += [{"primary_category": "technology_ai", "current_affairs_score": 9}] * 10
        rows += [{"primary_category": "economy_business", "current_affairs_score": 7}] * 4
        selected = ca.select_balanced(rows, 10)
        counts = {}
        for row in selected:
            counts[row["primary_category"]] = counts.get(row["primary_category"], 0) + 1
        self.assertLessEqual(max(counts.values()) / len(selected), .5)

    def test_34_major_incident_safety_route_in_candidate(self):
        result = ca.classify_items([self.fixture("fixture-incident")], path=self.db)
        self.assertEqual(result["accepted"][0]["packet"]["major_incident_route"],
                         "existing_major_incident_pipeline")

    def test_35_production_limits_unchanged(self):
        self.assertFalse(ca.status(self.db)["production_posting_limits_changed"])

    def test_36_profile_unchanged(self):
        self.assertFalse(ca.status(self.db)["profile_changed"])

    def test_37_ai_short_candidate(self):
        result = ca.short_candidates(path=self.db)
        self.assertIn("technology_ai", {r["primary_category"] for r in result["candidates"]})

    def test_38_corporate_governance_short(self):
        result = ca.short_candidates(path=self.db)
        corporate = next(r for r in result["candidates"] if r["primary_category"] == "economy_business")
        self.assertIn("ガバナンス", corporate["angles"])

    def test_39_social_system_article(self):
        result = ca.article_candidates(path=self.db)
        self.assertIn("society_living", {r["primary_category"] for r in result["candidates"]})

    def test_40_gossip_not_video(self):
        result = ca.short_candidates(path=self.db)
        self.assertNotIn("fixture-gossip", {r["content_id"] for r in result["candidates"]})

    def test_41_short_uses_category_relative_evaluation(self):
        result = ca.short_candidates(path=self.db)
        self.assertTrue(all(r["category_relative_evaluation"] for r in result["candidates"]))

    def test_42_social_is_not_fact_source(self):
        packet = ca.extend_packet(self.fixture("fixture-ai"))
        self.assertFalse(packet["sns_demand_is_fact"])

    def test_43_private_person_excluded(self):
        item = {**self.fixture("fixture-gossip"), "title": "一般人を晒す私人同士の問題"}
        self.assertTrue(ca.exclusion_reason(item))

    def test_44_unverified_crime_is_not_accepted(self):
        item = {**self.fixture("fixture-incident"), "verified": False,
                "source_name": "SNS", "source_type": "social"}
        result = ca.classify_items([item], path=self.db)
        self.assertFalse(result["accepted"])

    def test_45_env_is_not_modified(self):
        env = ROOT / ".env"
        before = hashlib.sha256(env.read_bytes()).hexdigest()
        ca.full_cycle(path=self.db)
        after = hashlib.sha256(env.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_46_no_task_registration_code(self):
        source = (SRC / "current_affairs.py").read_text(encoding="utf-8")
        self.assertNotIn("Register-ScheduledTask", source)

    def test_47_to_51_no_existing_publish_clients(self):
        source = (SRC / "current_affairs.py").read_text(encoding="utf-8")
        for forbidden in ("tweepy", "ThreadsClient", "post_to_x", "publish_note",
                          "media_publish"):
            self.assertNotIn(forbidden, source)

    def test_52_migration_is_idempotent(self):
        self.assertTrue(ca.apply_migrations(self.db))
        self.assertTrue(ca.apply_migrations(self.db))
        with closing(metrics_db.connect(self.db)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM content_categories WHERE parent_category_key IS NULL"
            ).fetchone()[0]
        self.assertEqual(count, 9)

    def test_53_full_cycle_safety(self):
        result = ca.full_cycle(path=self.db)
        self.assertTrue(all(value == 0 or value is False for value in result["safety"].values()))

    def test_54_packet_has_required_extensions(self):
        packet = ca.extend_packet(self.fixture("fixture-corporate"))
        required = {
            "primary_category", "secondary_categories", "current_affairs_score",
            "social_impact_score", "household_impact_score", "economic_impact_score",
            "systemic_issue_score", "accountability_score", "public_safety_score",
            "brand_fit_score", "shelf_life", "audience_segments",
            "recommended_platforms", "recommended_formats",
        }
        self.assertTrue(required <= set(packet))


if __name__ == "__main__":
    unittest.main()
