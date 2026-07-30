import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import post_experiments as pe


class PostExperimentTests(unittest.TestCase):
    def test_rates_and_missing_values(self):
        self.assertIsNone(pe.safe_rate(1, None))
        self.assertIsNone(pe.safe_rate(1, 0))
        row = pe.outcome_record({
            "impressions": 100, "likes": 5, "reposts": 3, "quotes": 2,
            "replies": 4, "profile_clicks": 6, "follows": 1})
        self.assertEqual(row["repost_rate"], .03)
        self.assertEqual(row["quote_rate"], .02)
        self.assertEqual(row["reply_rate"], .04)
        self.assertEqual(row["profile_click_rate"], .06)
        self.assertEqual(row["follow_conversion_rate"], .01)
        self.assertIsNone(pe.outcome_record({"impressions": 0})["like_rate"])

    def test_specific_replies_do_not_reward_abuse(self):
        self.assertEqual(pe.specific_reply_rate([
            "specific_question", "party_attack", "attribute_attack", "spam"]), 1 / 3)
        self.assertIsNone(pe.specific_reply_rate(["spam"]))
        self.assertEqual(pe.classify_reply("自民はゴミ工作員"), "party_attack")
        self.assertEqual(pe.classify_reply("自民は無能だ"), "party_attack")
        self.assertEqual(pe.classify_reply("民族を排除しろ"), "attribute_attack")
        self.assertEqual(pe.classify_reply("お前は無能だ"), "personal_attack")
        classes = pe.deduplicated_reply_classes([
            {"reply_id": "r1", "author_hash": "same",
             "reply_text": "誰がいくら負担するのですか？",
             "replied_at": "2026-07-29T01:00:00Z"},
            {"reply_id": "r2", "author_hash": "same",
             "reply_text": "追加の連投ですか？",
             "replied_at": "2026-07-29T02:00:00Z"},
            {"reply_id": "r3", "author_hash": "other",
             "reply_text": "自民はゴミ工作員",
             "replied_at": "2026-07-29T03:00:00Z"},
        ])
        self.assertEqual(
            classes, ["specific_question", "party_attack"])

    def test_missing_reply_authors_are_not_falsely_deduplicated(self):
        classes = pe.deduplicated_reply_classes([
            {"reply_id": "r1", "reply_text": "誰がいくら負担するのですか？"},
            {"reply_id": "r2", "reply_text": "根拠の資料ではどうですか？"},
        ])
        self.assertEqual(classes, ["specific_question", "specific_question"])

    def test_demand_uses_velocity_authors_and_not_truth(self):
        low = pe.topic_demand_score({})
        base = {"x_search_velocity": 10, "unique_author_count": 20,
                "engagement_velocity": 100, "official_news_count": 5,
                "audience_relevance": 10, "news_freshness_hours": 0}
        high = pe.topic_demand_score(base)
        self.assertGreater(high, low)
        self.assertEqual(high, pe.topic_demand_score(
            {**base, "verified": False, "ban_risk": 10}))

    def test_feature_extraction(self):
        recent = [{"text": "政府の負担を比較する。", "hook_type": "対比",
                   "structure_type": "一撃型", "ending_type": "assertion"}]
        f = pe.extract_features({
            "post_id": "p", "text": "問題は政府が決めた10%の家計負担です。一方、誰がいくら払う？",
            "posted_at": "2026-07-29T12:00:00+09:00"}, recent)
        for key in ("specific_number_present", "specific_actor_present",
                    "cost_bearer_present", "decision_maker_present",
                    "human_stake_present", "contrast_present",
                    "concrete_question_present", "problem_wa_pattern"):
            self.assertTrue(f[key], key)
        self.assertEqual(f["publish_hour"], 12)
        self.assertIn(f["hook_type"], {"数字先出し", "対比"})
        self.assertTrue(f["structure_type"])
        self.assertGreaterEqual(f["same_structure_recent_count"], 0)
        self.assertIsInstance(f["abstract_term_ratio"], float)
        self.assertIsNotNone(f["noun_density"])
        self.assertIn(
            f["noun_density_method"], {"sudachi", "regex_proxy_v1"})
        self.assertFalse(f["url_present"])

    def test_required_structured_features_are_migrated_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "metrics.db"
            self.assertTrue(pe.apply_migrations(db))
            self.assertTrue(pe.apply_migrations(db))
            feature = pe.extract_features({
                "post_id": "structured", "platform": "x",
                "model": "test-model", "digest_type": "daily",
                "source_count": 2, "reader_effect": "理解",
                "target_audience": "有権者", "ban_risk": 0.1,
                "text": "政府資料はこちら https://example.test/source #政治",
                "posted_at": "2026-07-29T12:00:00+09:00",
            })
            pe.persist_features(db, [feature])
            with closing(sqlite3.connect(db)) as conn:
                columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(post_performance_features)")
                }
                row = conn.execute(
                    """SELECT model,published_at,weekday,publish_hour,
                              digest_flag,source_count,url_present,
                              reader_effect,target_audience,hashtag_count,
                              ban_risk
                       FROM post_performance_features
                       WHERE post_id='structured'""").fetchone()
            required = {
                "model", "published_at", "weekday", "publish_hour",
                "digest_flag", "source_count", "url_present",
                "x_search_post_count", "x_search_velocity",
                "x_search_unique_authors", "x_search_engagement",
                "official_news_count", "audience_relevance_score",
                "longest_sentence_length", "reader_effect", "target_audience",
                "question_count", "emoji_count", "hashtag_count",
                "sensational_term_count", "problem_wa_pattern",
                "should_end_pattern", "ban_risk", "defamation_risk",
                "unsupported_number_flag", "unsupported_claim_flag",
                "generalization_risk", "mob_targeting_risk",
            }
            self.assertTrue(required <= columns)
            self.assertEqual(
                row,
                ("test-model", "2026-07-29T12:00:00+09:00", "Wed", 12,
                 1, 2, 1, "理解", "有権者", 1, .1))

    def test_time_saturation_and_previous_outcome_features(self):
        current = {
            "post_id": "p2", "topic_key": "tax",
            "text": "政府が税負担を決めた。",
            "posted_at": "2026-07-29T12:00:00+09:00",
        }
        previous = {
            "post_id": "p1", "topic_key": "tax", "text": "過去投稿",
            "posted_at": "2026-07-29T10:00:00+09:00",
            "normalized_24h_impressions": 1.25,
        }
        feature = pe.extract_features(current, [previous])
        self.assertEqual(feature["previous_post_interval_hours"], 2)
        self.assertEqual(
            feature["previous_24h_normalized_impressions"], 1.25)
        self.assertEqual(feature["topic_saturation_score"], 1)

    def test_statistics_and_outlier_resistance(self):
        s = pe.descriptive_stats(list(range(1, 100)) + [10000], 20)
        for key in ("mean", "median", "p25", "p75", "top_25_mean",
                    "bottom_25_mean", "trimmed_mean", "stddev",
                    "confidence_low", "confidence_high"):
            self.assertIn(key, s)
        self.assertLess(s["trimmed_mean"], s["mean"])
        self.assertFalse(s["insufficient_sample"])
        self.assertTrue(pe.descriptive_stats([1, 2], 20)["insufficient_sample"])

    def test_correlations(self):
        c = pe.correlation([1, 2, 3, 4, 5], [2, 4, 6, 8, 10], 3)
        self.assertEqual(c["pearson_correlation"], 1)
        self.assertEqual(c["spearman_correlation"], 1)
        self.assertEqual(c["sample_size"], 5)
        self.assertTrue(pe.correlation([1, 2], [2, 1], 20)["insufficient_sample"])
        negative = pe.correlation(list(range(20)), list(reversed(range(20))), 5)
        self.assertEqual(negative["classification"], "negative_associations")
        self.assertNotEqual(negative["classification"], "positive_associations")
        weak = pe.correlation(list(range(20)), [0, 1] * 10, 5)
        self.assertEqual(weak["classification"], "weak_or_inconclusive")
        self.assertAlmostEqual(
            pe.robust_slope([1, 2, 3], [2, 4, 6]), 2)
        importance = pe.permutation_importance(
            list(range(30)), [value * 2 for value in range(30)])
        self.assertGreater(importance["importance"], 0)

    def test_window_selection_is_one_row_per_post_and_never_mixed(self):
        rows = [
            {"platform": "x", "post_id": "a", "measurement_window": "24h",
             "captured_at": "2026-01-01T01:00:00", "impressions": 1},
            {"platform": "x", "post_id": "a", "measurement_window": "24h",
             "captured_at": "2026-01-01T02:00:00", "impressions": 2},
            {"platform": "x", "post_id": "a", "measurement_window": "72h",
             "captured_at": "2026-01-03T00:00:00", "impressions": 3},
            {"platform": "x", "post_id": "b", "measurement_window": "72h",
             "captured_at": "2026-01-03T00:00:00", "impressions": 4},
        ]
        selected_24, coverage_24 = pe.select_measurement_window(rows, "24h")
        selected_72, _ = pe.select_measurement_window(rows, "72h")
        self.assertEqual([(r["post_id"], r["impressions"]) for r in selected_24],
                         [("a", 2)])
        self.assertEqual({r["measurement_window"] for r in selected_24}, {"24h"})
        self.assertEqual({r["measurement_window"] for r in selected_72}, {"72h"})
        self.assertEqual(coverage_24["excluded_missing_window"], 1)

    def test_binary_effect_size_and_mann_whitney(self):
        self.assertEqual(pe.cliffs_delta([3, 4], [1, 2]), 1)
        u, p = pe.mann_whitney_u([3, 4, 5], [0, 1, 2])
        self.assertIsNotNone(u)
        self.assertLess(p, .2)

    def test_similarity_excludes_self_and_future_posts(self):
        current = {"post_id": "p2", "text": "同じ文章です。",
                   "posted_at": "2026-07-29T12:00:00+09:00"}
        recent = [
            {"post_id": "p2", "text": "同じ文章です。",
             "posted_at": "2026-07-29T12:00:00+09:00"},
            {"post_id": "p3", "text": "同じ文章です。",
             "posted_at": "2026-07-29T13:00:00+09:00"},
            {"post_id": "p1", "text": "別の過去投稿。",
             "posted_at": "2026-07-29T11:00:00+09:00"},
        ]
        feature = pe.extract_features(current, recent)
        self.assertLess(feature["semantic_similarity_recent"], 1)

    def test_candidate_counts_hash_similarity_and_safety(self):
        with patch.dict(os.environ, {
            "POST_EXPERIMENT_MIN_VARIANT_CANDIDATES_NORMAL": "3",
            "POST_EXPERIMENT_MIN_VARIANT_CANDIDATES_IMPORTANT": "5"}):
            normal = pe.generate_candidates({"text": "通常ニュース"})
            important = pe.generate_candidates(pe.SOCIAL_SECURITY_FIXTURE, True)
        self.assertEqual(normal["control"]["candidate_type"], "control")
        self.assertGreaterEqual(len(normal["variants"]), 3)
        self.assertGreaterEqual(len(important["variants"]), 5)
        self.assertEqual(len(important["fact_packet_hash"]), 64)
        self.assertFalse(important["auto_publish"])
        self.assertTrue(all(len(v["prediction"]) == 6 for v in important["variants"]))
        self.assertFalse(any("一人あたり" in v["text"] for v in important["variants"]))
        self.assertEqual(len({v["angle_type"] for v in important["variants"]}),
                         len(important["variants"]))

    def test_social_security_has_all_seven_angles(self):
        with patch.dict(os.environ, {
            "POST_EXPERIMENT_MIN_VARIANT_CANDIDATES_IMPORTANT": "7"}):
            result = pe.generate_candidates(pe.SOCIAL_SECURITY_FIXTURE, True)
        expected = {"数字の意味", "現役世代", "負担上限", "給付と負担非対称",
                    "10年後", "反論先出し", "具体質問"}
        self.assertEqual({v["angle_type"] for v in result["variants"]}, expected)
        self.assertTrue(all("boring_signals" in v for v in result["variants"]))
        self.assertTrue(all("fact_consistent" in v for v in result["variants"]))

    def test_normalization_retains_null_metrics(self):
        rows = pe.normalize_records([{
            "post_id": "1", "platform": "x", "category": "politics",
            "publish_hour": 12, "weekday": "Wed", "topic_demand_score": 5,
            "followers_at_publish": 1000, "impressions": 100,
            "profile_click_rate": None, "follow_conversion_rate": None,
            "quote_rate": .02}])
        self.assertEqual(rows[0]["normalized_impression_score"], 1)
        self.assertIsNone(rows[0]["normalized_profile_click_score"])

    def test_normalization_stratifies_recent_timing_outcome_and_competition(self):
        base = {
            "platform": "x", "category": "politics", "publish_hour": 12,
            "weekday": "Wed", "topic_demand_score": 5,
            "followers_at_publish": 1000, "profile_click_rate": None,
            "follow_conversion_rate": None, "quote_rate": None,
        }
        rows = pe.normalize_records([
            {**base, "post_id": "a", "impressions": 100,
             "previous_post_interval_hours": .5,
             "previous_24h_normalized_impressions": .5,
             "topic_competition_score": 1},
            {**base, "post_id": "b", "impressions": 300,
             "previous_post_interval_hours": 8,
             "previous_24h_normalized_impressions": 1.5,
             "topic_competition_score": 9},
        ])
        self.assertEqual(
            {row["post_id"]: row["normalized_impression_score"] for row in rows},
            {"a": 1, "b": 1})

    def test_migration_and_all_persistence_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "metrics.db"
            self.assertTrue(pe.apply_migrations(db))
            self.assertTrue(pe.apply_migrations(db))
            with closing(sqlite3.connect(db)) as conn:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({
                "post_performance_features", "post_performance_outcomes",
                "post_experiments", "post_candidate_predictions",
                "post_experiment_results", "post_feature_correlations",
                "post_experiment_recommendations",
                "post_experiment_snapshots", "post_reply_events"} <= tables)

    def test_fact_packet_snapshot_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "metrics.db"
            pe.apply_migrations(db)
            experiment = pe.generate_candidates(
                pe.SOCIAL_SECURITY_FIXTURE, important=True)
            pe.persist_experiment(db, experiment)
            with closing(sqlite3.connect(db)) as connection:
                row = connection.execute(
                    """SELECT snapshot_hash,payload_json
                       FROM post_experiment_snapshots
                       WHERE snapshot_type='fact_packet'""").fetchone()
            self.assertEqual(row[0], experiment["fact_packet_hash"])
            self.assertEqual(
                json.loads(row[1]), experiment["fact_packet"])

    def test_measurement_windows_and_followers_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "metrics.db"
            pe.apply_migrations(db)
            base = {"post_id": "p", "captured_at": "2026-07-29T00:00:00+09:00",
                    "followers_at_publish": 1234, "impressions": 10}
            pe.persist_outcomes(db, [
                pe.outcome_record({**base, "measurement_window": "1h"}),
                pe.outcome_record({**base, "measurement_window": "24h",
                                   "impressions": 20})])
            with closing(sqlite3.connect(db)) as conn:
                rows = conn.execute(
                    "SELECT measurement_window,followers_at_publish FROM "
                    "post_performance_outcomes ORDER BY measurement_window").fetchall()
            self.assertEqual(rows, [("1h", 1234), ("24h", 1234)])

    def test_exact_publish_follower_count_wins_over_nearest_snapshot(self):
        snapshots = [{
            "captured_at": "2026-07-29T12:01:00+09:00",
            "followers_count": 9999,
        }]
        post = {
            "followers_before_window": 1234,
            "posted_at": "2026-07-29T12:00:00+09:00",
        }
        self.assertEqual(
            pe._followers_at_publish(post, snapshots, post["posted_at"]),
            1234)
        self.assertEqual(
            pe._followers_at_publish({}, snapshots, post["posted_at"]),
            9999)

    def test_video_metrics_are_migrated_and_persisted_without_zero_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "metrics.db"
            pe.apply_migrations(db)
            pe.persist_outcomes(db, [pe.outcome_record({
                "post_id": "video-post",
                "captured_at": "2026-07-29T00:00:00+09:00",
                "measurement_window": "24h",
                "impressions": 100,
                "video_views": 40,
                "video_completion": .375,
            })])
            with closing(sqlite3.connect(db)) as conn:
                columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(post_performance_outcomes)")
                }
                row = conn.execute(
                    """SELECT video_views,video_completion,profile_clicks
                       FROM post_performance_outcomes
                       WHERE post_id='video-post'""").fetchone()
            self.assertTrue({"video_views", "video_completion"} <= columns)
            self.assertEqual(row[:2], (40, .375))
            self.assertIsNone(row[2])

    def test_all_reports_and_docs_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = {"generated": [], "published": [], "x_metrics": [],
                      "followers": [], "threads": []}
            analysis = {"records": [],
                        "correlations": {"normalized_impressions": {
                            "semantic_similarity_recent": pe.correlation([], [])}},
                        "binary_comparisons": [], "continuous_analysis": [],
                        "measurement_window": "24h",
                        "coverage": {"measurement_window": "24h"},
                        "window_days": 90,
                        "groups": {k: {} for k in (
                            "hook_type", "angle_type", "structure_type",
                            "publish_hour", "topic_demand_score")}}
            q = pe.write_reports(root, source, analysis, [])
            expected = {
                "overview.md", "data_coverage.md", "feature_summary.csv",
                "feature_performance.csv", "hook_performance.csv",
                "angle_performance.csv", "structure_performance.csv",
                "publish_time_performance.csv", "topic_demand_performance.csv",
                "control_vs_variant.md", "prediction_correlation.md",
                "prediction_correlation_24h.md",
                "binary_feature_comparison_24h.csv",
                "continuous_feature_analysis_24h.csv",
                "outlier_sensitivity.md", "semantic_similarity_investigation.md",
                "missing_feature_diagnosis.md", "measurement_window_coverage.md",
                "inconclusive_patterns.md",
                "backtest_results.md", "winning_patterns.md",
                "losing_patterns.md", "recommended_features.md",
                "features_to_remove.md", "phase_b_plan.md",
                "quality_report.json"}
            output = root / "outputs/post_experiments/latest"
            self.assertTrue(expected <= {p.name for p in output.iterdir()})
            self.assertFalse(q["external_publish_attempted"])
            saved = json.loads(
                (output / "quality_report.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["prediction_feature_limit"], 5)
            self.assertFalse(saved["phase_b_recommended"])
            self.assertTrue((root / "docs/POST_AB_TEST_DESIGN.md").exists())
            for path in output.glob("*.md"):
                text = path.read_text(encoding="utf-8")
                self.assertFalse(any(marker in text for marker in ("縺", "蜈", "繧", "螟")))
            for path in output.glob("*.csv"):
                self.assertEqual(path.read_bytes()[:3], b"\xef\xbb\xbf")
                path.read_text(encoding="utf-8-sig")

    def test_settings_hard_disable_auto_publish(self):
        with patch.dict(os.environ, {"POST_EXPERIMENTS_AUTO_PUBLISH": "true"}):
            self.assertFalse(pe.settings()["auto_publish"])

    def test_phase_b_requires_assignment_and_explicit_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "metrics.db"
            experiment = pe.generate_candidates(pe.SOCIAL_SECURITY_FIXTURE)
            pe.persist_experiment(db, experiment)
            assigned = pe.assign_phase_b(
                db, experiment["experiment_id"], "x", "politics|evening")
            repeated = pe.assign_phase_b(
                db, experiment["experiment_id"], "x", "politics|evening")
            self.assertEqual(assigned["candidate_id"], repeated["candidate_id"])
            self.assertTrue(assigned["human_approval_required"])
            blocked = pe.link_phase_b_publication(
                db, experiment["experiment_id"], assigned["candidate_id"],
                "x", "123")
            self.assertEqual(blocked["reason"], "human_approval_required")
            approval = pe.approve_phase_b(
                db, experiment["experiment_id"], assigned["candidate_id"],
                "operator", reason="reviewed")
            self.assertEqual(approval["external_posts"], 0)
            linked = pe.link_phase_b_publication(
                db, experiment["experiment_id"], assigned["candidate_id"],
                "x", "123")
            self.assertEqual(linked["status"], "linked")
            self.assertEqual(linked["result_status"], "collecting")

    def test_phase_b_blocks_second_experiment_for_same_fact(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "metrics.db"
            first = pe.generate_candidates(pe.SOCIAL_SECURITY_FIXTURE)
            second = pe.generate_candidates(pe.SOCIAL_SECURITY_FIXTURE)
            pe.persist_experiment(db, first)
            pe.persist_experiment(db, second)
            self.assertEqual(
                pe.assign_phase_b(db, first["experiment_id"], "threads")["status"],
                "assigned")
            blocked = pe.assign_phase_b(
                db, second["experiment_id"], "threads")
            self.assertEqual(blocked["reason"], "fact_already_assigned")

    def test_rollout_evaluation_never_auto_adopts(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "metrics.db"
            pe.apply_migrations(db)
            result = pe.evaluate_phase_b_rollout(db, minimum=1)
            self.assertFalse(result["rollout_eligible"])
            self.assertFalse(result["auto_adopted"])
            self.assertTrue(result["human_approval_required"])


if __name__ == "__main__":
    unittest.main()
