import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

import discord_notify  # noqa: E402
import free_note  # noqa: E402
import metrics_db  # noqa: E402


JST = ZoneInfo("Asia/Tokyo")


class FakeResponse:
    def __init__(self, status_code=200, message_id="123"):
        self.status_code = status_code
        self.message_id = message_id

    def json(self):
        return {"id": self.message_id}


class FreeNoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.env_patch = patch.dict(os.environ, {
            "FREE_NOTE_ENABLED": "true",
            "FREE_NOTE_OUTPUT_DIR": str(self.base / "note"),
            "FREE_NOTE_MIN_CHARS": "1800",
            "FREE_NOTE_TARGET_CHARS": "2400",
            "FREE_NOTE_MAX_CHARS": "3200",
            "FREE_NOTE_MIN_PRIMARY_SOURCES": "2",
            "FREE_NOTE_POSTS_PER_WEEK": "2",
            "FREE_NOTE_TOPIC_COOLDOWN_DAYS": "90",
            "STATE_DIR": str(self.base / "data"),
            "DISCORD_NOTE_ENABLED": "false",
            "NOTE_DRAFT_DISCORD_ENABLED": "false",
            "FREE_NOTE_COVER_ENABLED": "false",
        }, clear=False)
        self.env_patch.start()
        self.db = self.base / "data" / "metrics.db"
        metrics_db.init_db(self.db)

    def tearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    def selection(self, article_type="evergreen_institutional_explainer",
                  now=None):
        return free_note.select_topic(
            article_type, path=self.db,
            now=now or datetime(2026, 7, 22, 12, tzinfo=JST))

    def article(self, article_type="evergreen_institutional_explainer"):
        selected = self.selection(article_type)
        primary, secondary = free_note._sources(selected)
        title, article = free_note._local_article(
            selected, primary, secondary)
        return selected, primary, secondary, title, article

    def generate(self, article_type="evergreen_institutional_explainer",
                 now=None):
        return free_note.generate_free_note(
            article_type, dry_run=True, path=self.db,
            now=now or datetime(2026, 7, 22, 12, tzinfo=JST))

    def mark_as_production(self, result):
        folder, metadata = free_note.load_note(result["content_id"])
        metadata["generation_mode"] = "openai"
        free_note._atomic_json(folder / "metadata.json", metadata)

    def test_01_article_type_count(self):
        self.assertEqual(len(free_note.ARTICLE_TYPES), 7)

    def test_02_primary_registry_has_multiple_official_sources(self):
        self.assertGreaterEqual(len(free_note._registry_sources()), 2)

    def test_03_sources_meet_primary_minimum(self):
        _, primary, _, _, _ = self.article()
        self.assertGreaterEqual(len(primary), 2)

    def test_04_local_article_meets_minimum_length(self):
        *_, article = self.article()
        self.assertGreaterEqual(len(article), 1800)

    def test_05_local_article_meets_maximum_length(self):
        *_, article = self.article()
        self.assertLessEqual(len(article), 3200)

    def test_06_local_article_has_every_required_heading(self):
        *_, article = self.article()
        self.assertTrue(all(
            heading in article for heading in free_note.REQUIRED_HEADINGS))

    def test_07_local_article_quality_passes(self):
        _, primary, secondary, title, article = self.article()
        self.assertTrue(free_note.quality_check(
            article, title, primary, secondary)["passed"])

    def test_08_missing_heading_fails_quality(self):
        _, primary, secondary, title, article = self.article()
        article = article.replace(free_note.REQUIRED_HEADINGS[0], "")
        self.assertFalse(free_note.quality_check(
            article, title, primary, secondary)["passed"])

    def test_missing_h1_title_fails_quality(self):
        _, primary, secondary, title, article = self.article()
        article = article.replace(f"# {title}", "## 導入", 1)
        checked = free_note.quality_check(
            article, title, primary, secondary)
        self.assertIn("missing_title_heading", checked["reasons"])

    def test_09_short_article_fails_quality(self):
        _, primary, secondary, title, _ = self.article()
        self.assertFalse(free_note.quality_check(
            "# short", title, primary, secondary)["passed"])

    def test_10_unknown_url_fails_quality(self):
        _, primary, secondary, title, article = self.article()
        article += "\nhttps://example.invalid/unsupported"
        checked = free_note.quality_check(
            article, title, primary, secondary)
        self.assertFalse(checked["passed"])

    def test_11_internal_term_fails_quality(self):
        _, primary, secondary, title, article = self.article()
        article += "\npost_type"
        self.assertFalse(free_note.quality_check(
            article, title, primary, secondary)["passed"])

    def test_12_schedule_has_two_slots(self):
        self.assertEqual(len(free_note.schedule_slots(
            datetime(2026, 7, 20, 10, tzinfo=JST))), 2)

    def test_13_schedule_uses_wednesday_and_sunday(self):
        slots = free_note.schedule_slots(
            datetime(2026, 7, 20, 10, tzinfo=JST))
        self.assertEqual([row["scheduled_at"].weekday() for row in slots],
                         [2, 6])

    def test_14_one_post_week_uses_only_sunday(self):
        with patch.dict(os.environ, {"FREE_NOTE_POSTS_PER_WEEK": "1"}):
            slots = free_note.schedule_slots(
                datetime(2026, 7, 20, 10, tzinfo=JST))
        self.assertEqual([row["schedule_type"] for row in slots], ["sun"])

    def test_15_no_due_slot_before_schedule(self):
        now = datetime(2026, 7, 20, 10, tzinfo=JST)
        self.assertEqual(free_note.due_slots(now), [])

    def test_16_two_due_slots_after_sunday(self):
        now = datetime(2026, 7, 26, 21, tzinfo=JST)
        self.assertEqual(len(free_note.due_slots(now)), 2)

    def test_17_dry_run_creates_draft(self):
        self.assertEqual(self.generate()["status"], "draft")

    def test_18_dry_run_creates_four_files(self):
        result = self.generate()
        names = {path.name for path in Path(result["path"]).iterdir()}
        self.assertEqual(
            names, {"article.md", "metadata.json", "sources.md", "review.md"})

    def test_cover_enabled_creates_exact_note_image(self):
        from PIL import Image
        with patch.dict(os.environ, {"FREE_NOTE_COVER_ENABLED": "true"}):
            result = self.generate()
        cover_path = Path(result["path"], "cover.png")
        self.assertTrue(cover_path.exists())
        with Image.open(cover_path) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (1280, 670))
        self.assertEqual(result["cover_status"], "generated")

    def test_existing_slug_folder_gets_unique_content_suffix(self):
        now = datetime(2026, 7, 26, 12, tzinfo=JST)
        first = self.generate("weekly_top5", now)
        second = self.generate("weekly_top5", now)
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(Path(second["path"]).name.endswith("_002"))

    def test_19_dry_run_never_publishes_or_writes_x(self):
        result = self.generate()
        self.assertFalse(result["published"])
        self.assertEqual(result["x_writes"], 0)

    def test_20_list_notes_includes_generated_draft(self):
        result = self.generate()
        self.assertEqual(free_note.list_notes()[0]["content_id"],
                         result["content_id"])

    def test_21_approve_moves_draft(self):
        result = self.generate()
        metadata = free_note.update_status(
            result["content_id"], "approved", path=self.db)
        self.assertEqual(metadata["status"], "approved")
        self.assertEqual(Path(metadata["draft_path"]).parent.name, "approved")

    def test_22_invalid_publish_url_does_not_move_folder(self):
        result = self.generate()
        original = Path(result["path"])
        with self.assertRaises(ValueError):
            free_note.update_status(
                result["content_id"], "published",
                note_url="https://example.com/x", path=self.db)
        self.assertTrue(original.exists())

    def test_23_mark_published_records_manual_url(self):
        result = self.generate()
        metadata = free_note.mark_published(
            result["content_id"], "https://note.com/example/n/n1",
            path=self.db)
        self.assertEqual(metadata["status"], "published")
        self.assertTrue(metadata["published"])

    def test_24_weekly_top5_cooldown_applies_same_week(self):
        now = datetime(2026, 7, 26, 12, tzinfo=JST)
        result = self.generate("weekly_top5", now)
        self.mark_as_production(result)
        selected = free_note.select_topic(
            "weekly_top5", path=self.db, now=now + timedelta(hours=1))
        self.assertTrue(selected["duplicate"])

    def test_25_weekly_top5_can_run_next_week(self):
        now = datetime(2026, 7, 26, 12, tzinfo=JST)
        self.generate("weekly_top5", now)
        selected = free_note.select_topic(
            "weekly_top5", path=self.db, now=now + timedelta(days=7))
        self.assertFalse(selected["duplicate"])

    def test_dry_run_does_not_block_production_topic(self):
        now = datetime(2026, 7, 26, 12, tzinfo=JST)
        self.generate("weekly_top5", now)
        selected = free_note.select_topic(
            "weekly_top5", path=self.db, now=now + timedelta(minutes=5))
        self.assertFalse(selected["duplicate"])

    def test_revision_required_does_not_block_regeneration(self):
        now = datetime(2026, 7, 22, 12, tzinfo=JST)
        result = self.generate(now=now)
        self.mark_as_production(result)
        free_note.update_status(
            result["content_id"], "revision_required", path=self.db)
        selected = free_note.select_topic(
            "evergreen_institutional_explainer",
            "閣議決定と法律成立の違い",
            path=self.db,
            now=now + timedelta(minutes=5),
        )
        self.assertFalse(selected["duplicate"])

    def test_26_evergreen_topic_obeys_long_cooldown(self):
        now = datetime(2026, 7, 22, 12, tzinfo=JST)
        result = self.generate(now=now)
        self.mark_as_production(result)
        self.assertTrue(free_note.select_topic(
            "evergreen_institutional_explainer",
            "閣議決定と法律成立の違い",
            path=self.db, now=now + timedelta(days=10))["duplicate"])

    def test_27_sqlite_contains_note_tables(self):
        counts = metrics_db.table_counts(self.db)
        self.assertIn("note_drafts", counts)
        self.assertIn("note_generation_runs", counts)

    def test_28_pipeline_status_is_local_only(self):
        status = free_note.pipeline_status(path=self.db)
        self.assertFalse(status["automatic_note_publish"])
        self.assertEqual(status["x_writes"], 0)

    def test_29_disabled_pipeline_is_noop(self):
        with patch.dict(os.environ, {"FREE_NOTE_ENABLED": "false"}):
            result = free_note.generate_free_note(
                dry_run=True, path=self.db)
        self.assertEqual(result["status"], "disabled")

    def test_30_insufficient_primary_sources_skips(self):
        with patch("free_note._registry_sources", return_value=[]):
            result = free_note.generate_free_note(
                dry_run=True, path=self.db)
        self.assertEqual(result["status"], "skipped")

    def test_31_duplicate_returns_update_candidate(self):
        now = datetime(2026, 7, 22, 12, tzinfo=JST)
        initial = self.generate(now=now)
        self.mark_as_production(initial)
        result = free_note.generate_free_note(
            "evergreen_institutional_explainer",
            "閣議決定と法律成立の違い", dry_run=True,
            path=self.db, now=now + timedelta(days=1))
        self.assertEqual(result["status"], "update_candidate")

    def test_32_discord_disabled_does_not_send(self):
        result = self.generate()
        with patch("discord_notify.requests.post") as post:
            self.assertFalse(free_note.send_note_discord(
                result["content_id"], path=self.db))
        post.assert_not_called()

    def test_33_discord_sends_four_attachments_with_cover(self):
        with patch.dict(os.environ, {"FREE_NOTE_COVER_ENABLED": "true"}):
            result = self.generate()
        env = {
            "DISCORD_NOTE_ENABLED": "true",
            "DISCORD_NOTE_WEBHOOK_URL": "https://discord.invalid/hook",
        }
        with patch.dict(os.environ, env), patch(
            "discord_notify.requests.post", return_value=FakeResponse()
        ) as post:
            self.assertTrue(free_note.send_note_discord(
                result["content_id"], path=self.db))
        files = post.call_args.kwargs["files"]
        self.assertEqual(len(files), 4)
        self.assertEqual(files[0][1][0], "cover.png")
        self.assertEqual(files[0][1][2], "image/png")
        self.assertEqual(
            json.loads(post.call_args.kwargs["data"]["payload_json"])
            ["allowed_mentions"], {"parse": []})

    def test_34_attachment_failure_falls_back_to_summary(self):
        paths = [self.base / "missing.md"]
        env = {
            "DISCORD_NOTE_ENABLED": "true",
            "DISCORD_NOTE_WEBHOOK_URL": "https://discord.invalid/hook",
        }
        with patch.dict(os.environ, env), patch(
            "discord_notify.requests.post", return_value=FakeResponse()
        ) as post:
            sent, _ = discord_notify.notify_note_draft_files(
                {"title": "draft"}, paths)
        self.assertTrue(sent)
        self.assertIn("json", post.call_args.kwargs)

    def test_35_note_webhook_secret_is_sanitized(self):
        secret = (
            "https://discord.com/api/" +
            "webhooks/123456789/note-secret-token"
        )
        with patch.dict(os.environ, {"DISCORD_NOTE_WEBHOOK_URL": secret}):
            self.assertNotIn(secret, discord_notify.sanitize(secret))

    def test_57_duplicate_content_id_is_upserted(self):
        result = self.generate()
        _, metadata = free_note.load_note(result["content_id"])
        self.assertTrue(free_note._save_db(metadata, self.db))
        with closing(metrics_db.connect(self.db)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM note_drafts WHERE content_id=?",
                (result["content_id"],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_58_sqlite_failure_writes_json_fallback(self):
        with patch("free_note._save_db", return_value=False):
            self.generate()
        self.assertTrue(
            (Path(os.environ["FREE_NOTE_OUTPUT_DIR"]) / "note_state.json").exists())

    def test_59_correction_candidate_is_excluded(self):
        rows = [{"source_url": "https://example.com/1", "title": "one",
                 "topic_key": "one",
                 "metadata_json": '{"correction_required": true}'}]
        self.assertEqual(free_note._dedupe_candidates(rows), [])

    def test_60_high_anger_candidate_is_excluded(self):
        rows = [{"source_url": "https://example.com/1", "title": "one",
                 "topic_key": "one", "metadata_json": '{"anger_score": 8}'}]
        self.assertEqual(free_note._dedupe_candidates(rows), [])

    def test_61_low_trust_candidate_is_excluded(self):
        rows = [{"source_url": "https://example.com/1", "title": "one",
                 "topic_key": "one", "metadata_json": '{"trust_score": 4}'}]
        self.assertEqual(free_note._dedupe_candidates(rows), [])

    def test_62_too_long_article_fails(self):
        _, primary, secondary, title, article = self.article()
        checked = free_note.quality_check(
            article + ("追記" * 1000), title, primary, secondary)
        self.assertIn("too_long", checked["reasons"])

    def test_63_one_primary_source_fails(self):
        _, primary, secondary, title, article = self.article()
        checked = free_note.quality_check(
            article, title, primary[:1], secondary)
        self.assertIn("insufficient_primary_sources", checked["reasons"])

    def test_64_unsupported_number_fails(self):
        _, primary, secondary, title, article = self.article()
        checked = free_note.quality_check(
            article + "\n987654321円", title, primary, secondary)
        self.assertIn("unsupported_numeric_claim", checked["reasons"])

    def test_65_regeneration_is_at_most_once(self):
        _, primary, _, _, _ = self.article()
        bad = {"article": "# bad", "model": "fake",
               "input_tokens": 1, "output_tokens": 1,
               "estimated_cost_usd": 0}
        with patch("free_note._openai_article", return_value=bad) as generate:
            with patch("free_note._sources", return_value=(primary, [])):
                result = free_note.generate_free_note(
                    "legislative_process", dry_run=False,
                    path=self.db,
                    now=datetime(2026, 7, 22, 13, tzinfo=JST))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(generate.call_count, 2)

    def test_66_revision_required_status_is_saved(self):
        result = self.generate()
        metadata = free_note.update_status(
            result["content_id"], "revision_required", path=self.db)
        self.assertEqual(metadata["status"], "revision_required")

    def test_67_status_history_is_preserved(self):
        result = self.generate()
        metadata = free_note.update_status(
            result["content_id"], "reviewing", path=self.db)
        metadata = free_note.update_status(
            result["content_id"], "approved", path=self.db)
        self.assertEqual(
            [row["status"] for row in metadata["status_history"]],
            ["draft", "reviewing", "approved"])

    def test_68_note_model_estimate_is_below_article_cap(self):
        from api_budget import estimate_openai
        self.assertLessEqual(
            estimate_openai("gpt-5.6-terra", 7000, 6000), .25)

    def test_69_monthly_note_cap_blocks_extra_reservation(self):
        from api_budget import reserve
        metrics_db.write("""INSERT INTO api_usage_events
          (timestamp,provider,operation,model_or_endpoint,resource_count,
           input_tokens,cached_input_tokens,output_tokens,estimated_cost_usd,
           success,fallback_used,error_type,metadata_json)
          VALUES (?,?,?,?,0,0,0,0,1.49,1,0,'','{}')""", (
            datetime.now(JST).isoformat(), "openai",
            "free_note_generation", "gpt-5.6-luna"), self.db)
        reservation, reason = reserve(
            "openai", "free_note_generation", "gpt-5.6-luna", .02,
            path=self.db)
        self.assertIsNone(reservation)
        self.assertEqual(reason, "openai_free_note_generation_budget_guard")

    def test_70_note_budget_guard_does_not_create_x_post_event(self):
        self.test_69_monthly_note_cap_blocks_extra_reservation()
        with closing(metrics_db.connect(self.db)) as conn:
            count = conn.execute("""SELECT COUNT(*) FROM api_usage_events
                WHERE operation='post_generation'""").fetchone()[0]
        self.assertEqual(count, 0)

    def test_71_missing_webhook_still_saves_local_draft(self):
        with patch.dict(os.environ, {
            "DISCORD_NOTE_ENABLED": "true",
            "DISCORD_NOTE_WEBHOOK_URL": "",
            "NOTE_DRAFT_DISCORD_WEBHOOK_URL": "",
        }):
            result = self.generate()
        self.assertTrue(Path(result["path"], "article.md").exists())

    def test_72_duplicate_discord_notification_is_blocked(self):
        result = self.generate()
        env = {
            "DISCORD_NOTE_ENABLED": "true",
            "DISCORD_NOTE_WEBHOOK_URL": "https://discord.invalid/hook",
        }
        with patch.dict(os.environ, env), patch(
            "discord_notify.requests.post", return_value=FakeResponse()
        ) as post:
            self.assertTrue(free_note.send_note_discord(
                result["content_id"], path=self.db))
            self.assertFalse(free_note.send_note_discord(
                result["content_id"], path=self.db))
        self.assertEqual(post.call_count, 1)

    def test_73_discord_failure_is_nonfatal(self):
        env = {
            "DISCORD_NOTE_ENABLED": "true",
            "DISCORD_NOTE_WEBHOOK_URL": "https://discord.invalid/hook",
        }
        with patch.dict(os.environ, env), patch(
            "discord_notify.requests.post",
            return_value=FakeResponse(status_code=500)
        ):
            sent, _ = discord_notify.notify_note_draft_files(
                {"title": "draft"}, [])
        self.assertFalse(sent)

    def test_74_draft_folder_contains_year(self):
        result = self.generate()
        self.assertEqual(Path(result["path"]).parent.name, "2026")

    def test_75_metadata_has_required_fields(self):
        result = self.generate()
        metadata = json.loads(Path(
            result["path"], "metadata.json").read_text(encoding="utf-8"))
        required = {
            "content_id", "title", "slug", "status", "article_type",
            "generated_at", "target_publish_date", "prompt_version", "model",
            "character_count", "estimated_reading_minutes",
            "primary_topic_key", "included_topic_keys",
            "source_news_candidate_ids", "source_x_post_ids",
            "primary_sources", "secondary_sources",
            "discord_notification_status", "discord_message_id", "note_url",
            "published_at", "estimated_cost_usd",
            "cover_path", "cover_status", "cover_width", "cover_height",
        }
        self.assertTrue(required.issubset(metadata))

    def test_76_sources_file_has_audit_fields(self):
        result = self.generate()
        text = Path(result["path"], "sources.md").read_text(encoding="utf-8")
        for label in ("発行主体", "公開日", "URL", "記事中で使った事実", "確認日時"):
            self.assertIn(label, text)

    def test_77_review_file_has_required_sections(self):
        result = self.generate()
        text = Path(result["path"], "review.md").read_text(encoding="utf-8")
        for heading in ("## 事実確認", "## 編集品質", "## 安全性", "## 公開作業"):
            self.assertIn(heading, text)

    def test_78_content_id_increments_same_day(self):
        first = self.generate("legislative_process")
        second = self.generate("social_insurance_burden")
        self.assertNotEqual(first["content_id"], second["content_id"])
        self.assertTrue(second["content_id"].endswith("002"))

    def test_79_no_note_or_browser_automation_code(self):
        text = (ROOT / "src" / "free_note.py").read_text(encoding="utf-8").lower()
        for forbidden in ("selenium", "playwright", "note.com/api", "cookie"):
            self.assertNotIn(forbidden, text)

    def test_80_note_conversion_types_are_supported(self):
        from growth_tracking import CONVERSION_TYPES
        required = {
            "note_view", "note_like", "note_follow", "note_comment",
            "note_purchase", "newsletter_signup",
        }
        self.assertTrue(required.issubset(CONVERSION_TYPES))

    def test_81_weekly_top5_uses_five_distinct_candidates(self):
        topics = ["tax", "diet", "cabinet", "welfare", "diplomacy"]
        for index, topic in enumerate(topics):
            metrics_db.insert_news({
                "source_type": "rss",
                "source_name": "NHK",
                "url": f"https://example.com/news/{index}",
                "title": f"政治ニュース {index}",
                "summary": "確認済みニュース",
                "pub_date": datetime.now(JST).isoformat(),
                "topic_key": topic,
                "final_news_score": 9 - index / 10,
                "source_reliability_score": 9,
                "verified": True,
                "discovered_via": ["rss"],
            }, self.db)
        selected = free_note.select_topic(
            "weekly_top5", path=self.db, now=datetime.now(JST))
        self.assertEqual(len(selected["candidates"]), 5)
        self.assertEqual(
            len({row["topic_key"] for row in selected["candidates"]}), 5)

    def test_82_completed_schedule_is_not_generated_twice(self):
        now = datetime(2026, 7, 26, 21, tzinfo=JST)
        free_note._record_run({
            "run_at": now.isoformat(), "schedule_type": "wed",
            "target_article_type": "evergreen_institutional_explainer",
            "status": "draft",
        }, self.db)
        due = free_note.due_slots(now, self.db)
        self.assertEqual([slot["schedule_type"] for slot in due], ["sun"])

    def test_83_additive_migration_preserves_existing_data(self):
        metrics_db.write("""INSERT INTO conversion_events
          (occurred_at,source,campaign,content_id,event_type,value,
           metadata_json,event_key) VALUES (?,?,?,?,?,?,?,?)""", (
            datetime.now(JST).isoformat(), "manual", "note",
            "note-1", "note_view", 1, "{}", "preserve-me"), self.db)
        metrics_db.apply_additive_migrations(self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM conversion_events WHERE event_key=?",
                ("preserve-me",)).fetchone()[0]
        self.assertEqual(count, 1)


def _add_article_type_test(index, article_type):
    def test(self):
        selection = self.selection(article_type)
        self.assertEqual(selection["article_type"], article_type)
        self.assertTrue(selection["topic_key"])
    setattr(
        FreeNoteTests,
        f"test_{36 + index:02d}_select_{article_type}",
        test,
    )


for _index, _article_type in enumerate(sorted(free_note.ARTICLE_TYPES)):
    _add_article_type_test(_index, _article_type)


def _add_slug_test(index, raw, expected):
    def test(self):
        value = free_note.slugify(raw)
        self.assertEqual(value, expected)
        self.assertNotRegex(value, r'[<>:"/\\|?*]')
    setattr(FreeNoteTests, f"test_{43 + index:02d}_slug_case_{index}", test)


_SLUG_CASES = [
    ("政治 ニュース", "政治-ニュース"),
    ("a/b", "ab"),
    ("a:b", "ab"),
    (" a  b ", "a-b"),
    ("", "untitled"),
    ("...", "untitled"),
    ("a--b", "a-b"),
    ("政策？", "政策"),
]
for _index, (_raw, _expected) in enumerate(_SLUG_CASES):
    _add_slug_test(_index, _raw, _expected)


def _add_official_test(index, url):
    def test(self):
        self.assertTrue(free_note._official(url))
    setattr(FreeNoteTests, f"test_{51 + index:02d}_official_url_{index}", test)


_OFFICIAL_URLS = [
    "https://www.kantei.go.jp/jp/",
    "https://www.shugiin.go.jp/",
    "https://www.sangiin.go.jp/",
    "https://laws.e-gov.go.jp/",
    "https://www.courts.go.jp/",
    "https://www.ndl.go.jp/",
]
for _index, _url in enumerate(_OFFICIAL_URLS):
    _add_official_test(_index, _url)


if __name__ == "__main__":
    unittest.main()
