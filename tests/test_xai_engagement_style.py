import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
os.environ["POST_ENABLED"] = "false"

import api_budget
import engagement_queue
import metrics_db
import news
import post
import profile_audit
import review_scoring
import xai_radar
from publishing_policy import choose_post_style, phase_daily_limit_reached

JST = ZoneInfo("Asia/Tokyo")


class FakeXAIResponse:
    def __init__(self):
        self.output_text = json.dumps({"generated_at": "2026-07-21T06:00:00+09:00", "topics": [{
            "topic_key": "防衛予算", "summary": "防衛予算を巡る議論", "attention_score": 8,
            "velocity_score": 7, "post_count_estimate": 50, "unique_account_estimate": 12,
            "main_claims": ["増額が必要"], "counter_claims": ["財源確認が必要"],
            "official_or_primary_accounts": ["modjapan_jp"],
            "representative_posts": [{"post_id": "123", "author_handle": "modjapan_jp",
                "author_type": "official", "reason_selected": "一次発表"}],
            "verification_required": True}]}, ensure_ascii=False)
        self.usage = SimpleNamespace(cost_in_usd_ticks=10_000_000, input_tokens=100, output_tokens=50)
        self.output = [SimpleNamespace(type="x_search_call")]


class FakeXAIClient:
    last_kwargs = {}
    def __init__(self, **kwargs):
        self.responses = self
    def create(self, **kwargs):
        FakeXAIClient.last_kwargs = kwargs
        return FakeXAIResponse()


class EmptyThenSuccessXAIClient(FakeXAIClient):
    calls = 0

    def create(self, **kwargs):
        self.__class__.calls += 1
        if self.__class__.calls == 1:
            response = FakeXAIResponse()
            response.output_text = json.dumps({"generated_at": "2026-07-21T06:00:00+09:00",
                                               "topics": []})
            return response
        return FakeXAIResponse()


class NewRequirementsTests(unittest.TestCase):
    def test_01_xai_disabled_keeps_rss(self):
        with patch.dict(os.environ, {"X_TOPIC_DISCOVERY_PROVIDER": "none"}), patch.object(
                news, "fetch_xai_radar", side_effect=AssertionError):
            self.assertEqual(xai_radar.apply_verified_attention([{"title": "政策"}], []), [{"title": "政策"}])

    def test_02_xai_failure_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "XAI_ENABLED": "true", "X_TOPIC_DISCOVERY_PROVIDER": "xai", "XAI_API_KEY": "dummy",
            "XAI_SEARCH_SCHEDULE": "06:00", "XAI_MONTHLY_BUDGET_USD": "2",
            "XAI_BUDGET_RESERVE_USD": "0"}), patch.object(xai_radar, "_state_dir", return_value=Path(td)):
            result = xai_radar.search(datetime(2026, 7, 21, 6, 0, tzinfo=JST),
                                      client_factory=lambda **_: (_ for _ in ()).throw(RuntimeError()),
                                      path=Path(td) / "db.sqlite")
            self.assertEqual(result, [])

    def test_03_xai_only_runs_at_schedule(self):
        with patch.dict(os.environ, {
            "XAI_SEARCH_SCHEDULE": "06:00,12:00,18:00",
            "XAI_ADAPTIVE_SCHEDULE_ENABLED": "false",
        }), patch.object(xai_radar, "usage_totals", return_value={"xai": 0}), patch.object(
                xai_radar, "forecast", return_value={"projected": {"xai": 0}}):
            self.assertFalse(xai_radar.should_run(datetime(2026, 7, 21, 7, 0, tzinfo=JST)))
            self.assertTrue(xai_radar.should_run(datetime(2026, 7, 21, 12, 0, tzinfo=JST)))

    def test_04_xai_daily_calls_capped_at_three(self):
        self.assertEqual(int(os.environ.get("XAI_SEARCH_MAX_CALLS_PER_DAY", "3")), 3)

    def test_05_xai_ticks_are_saved_as_actual_cost(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "XAI_ENABLED": "true", "X_TOPIC_DISCOVERY_PROVIDER": "xai", "XAI_API_KEY": "dummy",
            "XAI_SEARCH_SCHEDULE": "06:00", "XAI_MONTHLY_BUDGET_USD": "2",
            "XAI_BUDGET_RESERVE_USD": "0"}), patch.object(xai_radar, "_state_dir", return_value=Path(td)):
            path = Path(td) / "db.sqlite"
            xai_radar.search(datetime(2026, 7, 21, 6, 0, tzinfo=JST), FakeXAIClient, path)
            self.assertAlmostEqual(api_budget.usage_totals(path)["xai"], .001, places=6)

    def test_06_xai_budget_two_dollars_stops_search(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "XAI_MONTHLY_BUDGET_USD": "2", "XAI_BUDGET_RESERVE_USD": "0"}):
            path = Path(td) / "db.sqlite"; metrics_db.init_db(path)
            metrics_db.write("INSERT INTO api_usage_events(timestamp,provider,operation,estimated_cost_usd,success) VALUES (?,?,?,?,1)",
                             (datetime.now(JST).isoformat(), "xai", "x_search_radar", 1.9), path)
            self.assertIn("budget_guard", api_budget.reserve("xai", "x_search_radar", "grok-4.5", .2, 1, path=path)[1])

    def test_07_xai_topic_requires_external_confirmation(self):
        topics = [{"topic_key": "防衛予算", "attention_score": 9}]
        self.assertEqual(xai_radar.apply_verified_attention([], topics), [])

    def test_08_xai_schema_has_no_post_text(self):
        self.assertNotIn('"text"', json.dumps(xai_radar._schema(5, 3)))

    def test_09_paid_discovery_is_exclusive(self):
        source = (ROOT / "src" / "news.py").read_text(encoding="utf-8")
        self.assertIn('provider == "xai"', source)
        self.assertIn('provider == "native_x"', source)

    def test_10_normal_automated_posts_stop_at_eight(self):
        now = datetime.now(JST).replace(hour=12, minute=0, second=0, microsecond=0)
        rows = [{"tweet_id": str(i), "post_type": "strong_opinion",
                 "posted_at_jst": (now - timedelta(minutes=61 + i)).isoformat()} for i in range(8)]
        self.assertTrue(phase_daily_limit_reached(rows, now, False, 8, 2, 10))

    def test_11_total_automated_posts_stop_at_ten(self):
        now = datetime.now(JST).replace(hour=12, minute=0, second=0, microsecond=0)
        rows = [{"tweet_id": str(i), "post_type": "strong_opinion" if i < 8 else "breaking_news",
                 "posted_at_jst": (now - timedelta(minutes=61 + i)).isoformat()} for i in range(10)]
        self.assertTrue(phase_daily_limit_reached(rows, now, True, 8, 2, 10))

    def test_12_three_same_styles_are_avoided(self):
        now = datetime.now(JST); history = [{"post_type": "strong_opinion",
            "tweet_id": str(i), "posted_at_jst": (now - timedelta(hours=i + 1)).isoformat()} for i in range(2)]
        style, _ = choose_post_style({"title": "政府が方針", "topic_key": "方針"}, history, now)
        self.assertNotEqual(style, "strong_opinion")

    def test_13_emoji_free_post_is_valid(self):
        text = "制度改正の論点\n\n確認された事実を整理します。\n\n説明責任が必要です。" + "確認" * 30
        self.assertNotIn("missing_required_emojis", post._candidate_quality_violations({"tweet_text": text}, {}))

    def test_14_serious_news_rejects_emoji(self):
        text = "⚖️ 重要判決\n\n確認された事実です。\n\n制度を検証します。" + "確認" * 30
        self.assertIn("emoji_on_serious_news", post._candidate_quality_violations(
            {"tweet_text": text}, {"title": "裁判の重要判決"}))

    def test_15_three_hour_silence_never_lowers_quality(self):
        self.assertFalse(post._score_gate_allows(3, False, False, True))

    def test_16_evergreen_daily_limit_is_one(self):
        now = datetime.now(JST).replace(hour=12, minute=0, second=0, microsecond=0)
        history = [{"tweet_id": "1", "post_type": "evergreen_explainer",
                    "posted_at_jst": (now - timedelta(hours=4)).isoformat()}]
        with patch.dict(os.environ, {"EVERGREEN_FALLBACK_ENABLED": "true", "EVERGREEN_MAX_PER_DAY": "1"}):
            self.assertIsNone(post._evergreen_candidate(history, now))

    def test_17_steelman_prompt_forbids_distortion(self):
        prompt = post.GENERATION_SYSTEM
        self.assertIn("架空の主張", prompt)
        self.assertIn("嘲笑", prompt)

    def test_18_quote_candidate_is_saved(self):
        with tempfile.TemporaryDirectory() as td, patch.object(engagement_queue, "_output_dir", return_value=Path(td)):
            path = Path(td) / "db.sqlite"
            item = {"queue_type": "quote", "post_id": "1", "source_verified": True, "risk_flags": []}
            self.assertTrue(engagement_queue._insert(item, path))

    def test_19_quote_has_three_options(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(len(engagement_queue._comment_options({"topic_key": "予算"})), 3)

    def test_20_quote_queue_has_zero_x_writes(self):
        with patch.object(engagement_queue, "build_quote_queue", return_value=0), patch.object(
                engagement_queue, "build_reply_queue", return_value=0):
            self.assertEqual(engagement_queue.build_all()["x_writes"], 0)

    def test_21_reply_candidate_is_saved(self):
        with tempfile.TemporaryDirectory() as td, patch.object(
                engagement_queue, "_root", return_value=Path(td)), patch.object(
                engagement_queue, "_output_dir", return_value=Path(td)):
            (Path(td) / "data").mkdir()
            (Path(td) / "data" / "mentions_latest.json").write_text(json.dumps([{
                "post_id": "2", "constructive": True, "draft": "確認します"}]), encoding="utf-8")
            self.assertEqual(engagement_queue.build_reply_queue(Path(td) / "db.sqlite"), 1)

    def test_22_reply_queue_has_no_x_write_client(self):
        source = (ROOT / "src" / "engagement_queue.py").read_text(encoding="utf-8")
        self.assertNotIn("create_tweet", source)

    def test_23_private_other_accounts_are_excluded_from_quotes(self):
        self.assertNotIn("other", engagement_queue.SAFE_AUTHOR_TYPES)

    def test_24_duplicate_queue_item_is_not_inserted(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "db.sqlite"
            item = {"queue_type": "quote", "post_id": "1", "source_verified": True, "risk_flags": []}
            engagement_queue._insert(item, path)
            self.assertFalse(engagement_queue._insert(item, path))

    def test_25_queue_status_can_be_updated(self):
        with tempfile.TemporaryDirectory() as td, patch.object(engagement_queue, "_output_dir", return_value=Path(td)):
            path = Path(td) / "db.sqlite"
            item = {"queue_type": "quote", "post_id": "1", "source_verified": True, "risk_flags": []}
            item_id = engagement_queue._insert(item, path)
            self.assertTrue(engagement_queue.update_status("quote", item_id, "approved", path))

    def test_26_four_axes_are_calculated(self):
        scores = review_scoring.calculate_four_axes({"impressions": 100, "impressions_per_hour": 10,
            "reposts": 2, "bookmarks": 2, "quotes": 1, "profile_clicks": 3, "replies": 2})
        self.assertTrue(all(f"{axis}_score" in scores for axis in ("spread", "trust", "conversation", "business")))

    def test_27_anger_only_is_not_winning_example(self):
        self.assertFalse(review_scoring.eligible_winning_example({
            "tweet_text": "無能だ。許せない。", "trust_score": 8}))

    def test_28_exploration_ratio_is_configurable(self):
        with patch.dict(os.environ, {"STYLE_EXPLORATION_RATIO": "1.0"}):
            _, exploration = choose_post_style({"title": "政府方針", "topic_key": "x"}, [], datetime.now(JST))
            self.assertTrue(exploration)

    def test_29_experiment_still_uses_quality_gate(self):
        self.assertFalse(post._score_gate_allows(3, False, False, False))

    def test_30_manual_and_automated_types_are_distinct(self):
        self.assertIn('"automation_type": "automated_original"', (ROOT / "src" / "post.py").read_text(encoding="utf-8"))
        self.assertIn("queue_type", (ROOT / "src" / "engagement_queue.py").read_text(encoding="utf-8"))

    def test_31_secrets_are_not_logged(self):
        source = (ROOT / "src" / "xai_radar.py").read_text(encoding="utf-8")
        self.assertNotIn("print(key", source)
        self.assertNotIn("XAI_API_KEY}", source)

    def test_32_all_engagement_auto_writes_are_false(self):
        keys = ["QUOTE_AUTO_POST_ENABLED", "REPLY_AUTO_POST_ENABLED", "REPOST_AUTO_ENABLED",
                "LIKE_AUTO_ENABLED", "FOLLOW_AUTO_ENABLED", "POLL_AUTO_POST_ENABLED",
                "THREAD_AUTO_POST_ENABLED", "IMAGE_POST_ENABLED"]
        self.assertTrue(all(os.environ.get(key, "false").lower() == "false" for key in keys))

    def test_33_no_browser_automation_added(self):
        text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src").glob("*.py"))
        self.assertNotIn("selenium", text.lower())
        self.assertNotIn("playwright", text.lower())

    def test_34_total_budget_is_thirty_six(self):
        self.assertEqual(float(os.environ.get("TOTAL_MONTHLY_API_BUDGET_USD", "36")), 36)

    def test_35_restriction_stage_pauses_xai(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "XAI_ENABLED": "true", "X_TOPIC_DISCOVERY_PROVIDER": "xai", "XAI_API_KEY": "dummy",
            "XAI_SEARCH_SCHEDULE": "06:00"}), patch.object(xai_radar, "forecast",
            return_value={"pause_x_search": True}), patch.object(xai_radar, "_state_dir", return_value=Path(td)):
            self.assertEqual(xai_radar.search(datetime(2026, 7, 21, 6, 0, tzinfo=JST),
                                             FakeXAIClient, Path(td) / "db.sqlite"), [])

    def test_36_profile_audit_never_changes_profile(self):
        with tempfile.TemporaryDirectory() as td:
            path = profile_audit.run(Path(td))
            self.assertTrue(path.exists())
            self.assertIn("プロフィール変更は行っていません", path.read_text(encoding="utf-8"))

    def test_37_xai_request_caps_tool_calls_at_one(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "XAI_ENABLED": "true", "X_TOPIC_DISCOVERY_PROVIDER": "xai", "XAI_API_KEY": "dummy",
            "XAI_SEARCH_SCHEDULE": "06:00", "XAI_MONTHLY_BUDGET_USD": "2",
            "XAI_BUDGET_RESERVE_USD": "0"}), patch.object(xai_radar, "_state_dir", return_value=Path(td)):
            xai_radar.search(datetime(2026, 7, 21, 6, 0, tzinfo=JST), FakeXAIClient, Path(td) / "db.sqlite")
            self.assertEqual(FakeXAIClient.last_kwargs["max_tool_calls"], 1)

    def test_38_xai_search_is_required(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "XAI_ENABLED": "true", "X_TOPIC_DISCOVERY_PROVIDER": "xai", "XAI_API_KEY": "dummy",
            "XAI_SEARCH_SCHEDULE": "06:00", "XAI_MONTHLY_BUDGET_USD": "2",
            "XAI_BUDGET_RESERVE_USD": "0"}), patch.object(xai_radar, "_state_dir", return_value=Path(td)):
            xai_radar.search(datetime(2026, 7, 21, 6, 0, tzinfo=JST), FakeXAIClient, Path(td) / "db.sqlite")
            self.assertEqual(FakeXAIClient.last_kwargs["tool_choice"], "required")

    def test_39_empty_search_does_not_create_a_second_paid_request(self):
        EmptyThenSuccessXAIClient.calls = 0
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "XAI_ENABLED": "true", "X_TOPIC_DISCOVERY_PROVIDER": "xai", "XAI_API_KEY": "dummy",
            "XAI_SEARCH_SCHEDULE": "06:00", "XAI_MONTHLY_BUDGET_USD": "2",
            "XAI_BUDGET_RESERVE_USD": "0", "XAI_SEARCH_MAX_ATTEMPTS_PER_RUN": "2",
            "XAI_SEARCH_MAX_CALLS_PER_DAY": "6"}), patch.object(
                xai_radar, "_state_dir", return_value=Path(td)):
            rows = xai_radar.search(datetime(2026, 7, 21, 6, 0, tzinfo=JST),
                                    EmptyThenSuccessXAIClient, Path(td) / "db.sqlite")
            self.assertEqual(EmptyThenSuccessXAIClient.calls, 1)
            self.assertEqual(len(rows), 0)

    def test_40_xai_compatible_custom_tool_call_is_counted(self):
        response = SimpleNamespace(
            output=[SimpleNamespace(type="custom_tool_call", name="x_semantic_search")],
            usage=SimpleNamespace(num_server_side_tools_used=1,
                                  server_side_tool_usage_details={"x_search_calls": 1}),
        )
        self.assertEqual(xai_radar._tool_call_count(response), 1)

    def test_41_xai_agentic_turns_are_capped_at_one(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "XAI_ENABLED": "true", "X_TOPIC_DISCOVERY_PROVIDER": "xai", "XAI_API_KEY": "dummy",
            "XAI_SEARCH_SCHEDULE": "06:00", "XAI_MONTHLY_BUDGET_USD": "2",
            "XAI_BUDGET_RESERVE_USD": "0"}), patch.object(xai_radar, "_state_dir", return_value=Path(td)):
            xai_radar.search(datetime(2026, 7, 21, 6, 0, tzinfo=JST), FakeXAIClient, Path(td) / "db.sqlite")
            self.assertEqual(FakeXAIClient.last_kwargs["extra_body"], {"max_turns": 1})


if __name__ == "__main__":
    unittest.main()
