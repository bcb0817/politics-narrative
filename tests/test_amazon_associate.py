import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

import amazon_associate as amazon  # noqa: E402
import free_note  # noqa: E402
import local_bot  # noqa: E402
import metrics_db  # noqa: E402


JST = ZoneInfo("Asia/Tokyo")


class AmazonAssociateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "FREE_NOTE_ENABLED": "true",
            "FREE_NOTE_OUTPUT_DIR": str(self.base / "note"),
            "FREE_NOTE_MIN_CHARS": "1800",
            "FREE_NOTE_TARGET_CHARS": "2400",
            "FREE_NOTE_MAX_CHARS": "3200",
            "STATE_DIR": str(self.base / "data"),
            "DISCORD_NOTE_ENABLED": "false",
            "NOTE_DRAFT_DISCORD_ENABLED": "false",
            "FREE_NOTE_COVER_ENABLED": "false",
            "AMAZON_ASSOCIATE_ENABLED": "true",
            "AMAZON_ASSOCIATE_MODE": "manual",
            "AMAZON_ASSOCIATE_TRACKING_ID": "example-22",
            "AMAZON_ASSOCIATE_DISCLOSURE_ENABLED": "true",
            "AMAZON_RELATED_ITEMS_MIN": "1",
            "AMAZON_RELATED_ITEMS_MAX": "3",
            "AMAZON_RELATED_ITEMS_REQUIRE_RELEVANCE": "true",
            "AMAZON_MIN_RELEVANCE_SCORE": "7.0",
            "AMAZON_MANUAL_LINK_PLACEHOLDER": "true",
            "AMAZON_REQUIRE_LINKS_BEFORE_APPROVAL": "true",
            "AMAZON_REQUIRE_DISCLOSURE_BEFORE_APPROVAL": "true",
            "AMAZON_PAAPI_ENABLED": "false",
            "AMAZON_PAAPI_MARKETPLACE": "www.amazon.co.jp",
            "AMAZON_RECOMMENDATION_MONTHLY_BUDGET_USD": "0.20",
            "AMAZON_RECOMMENDATION_MAX_EXTRA_CALLS_PER_ARTICLE": "1",
            "AMAZON_PRODUCT_SCRAPING_ENABLED": "true",
            "AMAZON_AUTO_PURCHASE_ENABLED": "true",
        }, clear=False)
        self.env.start()
        self.db = self.base / "data" / "metrics.db"
        metrics_db.init_db(self.db)
        self.selection = free_note.select_topic(
            "evergreen_institutional_explainer",
            path=self.db,
            now=datetime(2026, 7, 25, 12, tzinfo=JST),
        )
        self._draft = None

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def draft(self):
        if self._draft is None:
            self._draft = free_note.generate_free_note(
                "evergreen_institutional_explainer",
                dry_run=True,
                path=self.db,
                now=datetime(2026, 7, 25, 12, tzinfo=JST),
            )
        return self._draft

    def folder_metadata(self):
        result = self.draft()
        folder = Path(result["path"])
        metadata = json.loads(
            (folder / "metadata.json").read_text(encoding="utf-8"))
        return folder, metadata

    def test_01_default_mode_is_manual(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(amazon.associate_settings()["mode"], "manual")

    def test_02_invalid_mode_becomes_manual(self):
        with patch.dict(os.environ, {"AMAZON_ASSOCIATE_MODE": "scrape"}):
            self.assertEqual(amazon.associate_settings()["mode"], "manual")

    def test_03_scraping_is_forced_off(self):
        self.assertFalse(amazon.associate_settings()["scraping_enabled"])

    def test_04_auto_purchase_is_forced_off(self):
        self.assertFalse(amazon.associate_settings()["auto_purchase_enabled"])

    def test_05_disabled_has_no_candidates(self):
        with patch.dict(os.environ, {"AMAZON_ASSOCIATE_ENABLED": "false"}):
            self.assertEqual(amazon.manual_candidates(self.selection), [])

    def test_06_candidate_count_is_one_to_three(self):
        rows = amazon.manual_candidates(self.selection)
        self.assertGreaterEqual(len(rows), 1)
        self.assertLessEqual(len(rows), 3)

    def test_07_candidates_meet_relevance_threshold(self):
        self.assertTrue(all(
            row["relevance_score"] >= 7.0
            for row in amazon.manual_candidates(self.selection)))

    def test_08_candidate_isbn_is_verified_shape(self):
        self.assertTrue(all(
            len(row["isbn"]) == 13 and row["isbn"].startswith(("978", "979"))
            for row in amazon.manual_candidates(self.selection)))

    def test_09_candidate_ids_are_stable(self):
        rows = amazon.manual_candidates(self.selection)
        self.assertEqual(
            [row["item_id"] for row in rows],
            [f"amazon-{index:03d}" for index in range(1, len(rows) + 1)],
        )

    def test_10_candidate_has_no_link_in_manual_mode(self):
        rows = amazon.manual_candidates(self.selection)
        self.assertTrue(all(not row["affiliate_url"] for row in rows))

    def test_11_manual_items_require_manual_link(self):
        rows = amazon.manual_candidates(self.selection)
        self.assertTrue(all(
            row["link_status"] == "manual_required" for row in rows))

    def test_12_relevance_weights_are_complete(self):
        row = amazon.manual_candidates(self.selection)[0]
        self.assertEqual(set(row["relevance_components"]), {
            "topic_match", "reader_utility", "authority",
            "accessibility", "diversity_bonus",
        })

    def test_13_section_contains_disclosure(self):
        text = amazon.render_section(amazon.manual_candidates(self.selection))
        self.assertIn(amazon.DISCLOSURE, text)

    def test_14_section_contains_placeholder_per_item(self):
        rows = amazon.manual_candidates(self.selection)
        text = amazon.render_section(rows)
        self.assertEqual(text.count(amazon.PENDING_PREFIX), len(rows))

    def test_15_section_contains_no_live_amazon_url_in_manual_mode(self):
        text = amazon.render_section(amazon.manual_candidates(self.selection))
        self.assertNotIn("https://www.amazon.co.jp/", text)

    def test_16_empty_items_have_no_section(self):
        self.assertEqual(amazon.render_section([]), "")

    def test_17_replace_section_does_not_duplicate_heading(self):
        rows = amazon.manual_candidates(self.selection)
        once = amazon.append_or_replace_section("# A\n\n本文", rows)
        twice = amazon.append_or_replace_section(once, rows)
        self.assertEqual(twice.count("## 関連書籍"), 1)

    def test_18_valid_japan_url(self):
        self.assertTrue(amazon.validate_amazon_url(
            "https://www.amazon.co.jp/dp/4123456789?tag=example-22"))

    def test_19_reject_http_url(self):
        self.assertFalse(amazon.validate_amazon_url(
            "http://www.amazon.co.jp/dp/4123456789"))

    def test_20_reject_wrong_marketplace(self):
        self.assertFalse(amazon.validate_amazon_url(
            "https://www.amazon.com/dp/4123456789"))

    def test_21_reject_embedded_credentials(self):
        self.assertFalse(amazon.validate_amazon_url(
            "https://user:pass@www.amazon.co.jp/dp/4123456789"))

    def test_22_draft_metadata_has_items(self):
        _, metadata = self.folder_metadata()
        self.assertGreaterEqual(len(metadata["amazon_items"]), 1)

    def test_23_draft_article_has_disclosure(self):
        folder, _ = self.folder_metadata()
        self.assertIn(
            amazon.DISCLOSURE,
            (folder / "article.md").read_text(encoding="utf-8"),
        )

    def test_24_draft_sources_have_link_status(self):
        folder, _ = self.folder_metadata()
        text = (folder / "sources.md").read_text(encoding="utf-8")
        self.assertIn("リンク状態：manual_required", text)

    def test_25_draft_review_has_amazon_checklist(self):
        folder, _ = self.folder_metadata()
        text = (folder / "review.md").read_text(encoding="utf-8")
        self.assertIn("## Amazonアソシエイト確認", text)

    def test_26_approval_is_blocked_until_links_ready(self):
        folder, metadata = self.folder_metadata()
        self.assertIn(
            "amazon_links_not_ready",
            amazon.approval_blockers(folder, metadata),
        )

    def test_27_manual_link_replaces_one_placeholder(self):
        folder, metadata = self.folder_metadata()
        item = metadata["amazon_items"][0]
        before = (folder / "article.md").read_text(encoding="utf-8")
        amazon.set_manual_link(
            metadata["content_id"],
            "https://www.amazon.co.jp/dp/4123456789?tag=example-22",
            item_id=item["item_id"],
            path=self.db,
        )
        after = (folder / "article.md").read_text(encoding="utf-8")
        self.assertEqual(
            before.count(amazon.PENDING_PREFIX) - 1,
            after.count(amazon.PENDING_PREFIX),
        )

    def test_28_manual_link_creates_article_backup(self):
        _, metadata = self.folder_metadata()
        result = amazon.set_manual_link(
            metadata["content_id"],
            "https://www.amazon.co.jp/dp/4123456789?tag=example-22",
            item_id=metadata["amazon_items"][0]["item_id"],
            path=self.db,
        )
        self.assertTrue(Path(result["backup_path"]).exists())

    def test_29_manual_link_does_not_return_url(self):
        _, metadata = self.folder_metadata()
        result = amazon.set_manual_link(
            metadata["content_id"],
            "https://www.amazon.co.jp/dp/4123456789?tag=example-22",
            item_id=metadata["amazon_items"][0]["item_id"],
            path=self.db,
        )
        self.assertNotIn("affiliate_url", result)

    def test_30_duplicate_link_is_idempotent(self):
        _, metadata = self.folder_metadata()
        url = "https://www.amazon.co.jp/dp/4123456789?tag=example-22"
        item_id = metadata["amazon_items"][0]["item_id"]
        amazon.set_manual_link(
            metadata["content_id"], url, item_id=item_id, path=self.db)
        result = amazon.set_manual_link(
            metadata["content_id"], url, item_id=item_id, path=self.db)
        self.assertEqual(result["status"], "duplicate")

    def test_31_manual_link_event_stores_hash_not_url(self):
        _, metadata = self.folder_metadata()
        url = "https://www.amazon.co.jp/dp/4123456789?tag=example-22"
        amazon.set_manual_link(
            metadata["content_id"], url,
            item_id=metadata["amazon_items"][0]["item_id"], path=self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            rows = conn.execute(
                "SELECT url_hash FROM amazon_link_events").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(url not in str(row["url_hash"]) for row in rows))

    def test_32_invalid_manual_link_changes_nothing(self):
        folder, metadata = self.folder_metadata()
        before = (folder / "article.md").read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            amazon.set_manual_link(
                metadata["content_id"], "https://example.com/book",
                item_id=metadata["amazon_items"][0]["item_id"], path=self.db)
        self.assertEqual(
            before, (folder / "article.md").read_text(encoding="utf-8"))

    def test_33_unknown_item_is_rejected(self):
        _, metadata = self.folder_metadata()
        with self.assertRaises(ValueError):
            amazon.set_manual_link(
                metadata["content_id"],
                "https://www.amazon.co.jp/dp/4123456789",
                item_id="amazon-999", path=self.db)

    def test_34_status_has_counts_but_no_urls(self):
        _, metadata = self.folder_metadata()
        rows = amazon.links_status(metadata["content_id"])
        serialized = json.dumps(rows, ensure_ascii=False)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("https://", serialized)
        self.assertNotIn("example-22", serialized)

    def test_35_csv_import_success(self):
        _, metadata = self.folder_metadata()
        source = self.base / "links.csv"
        source.write_text(
            "content_id,item_id,isbn,affiliate_url\n"
            f"{metadata['content_id']},{metadata['amazon_items'][0]['item_id']},,"
            "https://www.amazon.co.jp/dp/4123456789?tag=example-22\n",
            encoding="utf-8",
        )
        result = amazon.import_links(source, self.db)
        self.assertEqual(result["success"], 1)
        self.assertTrue(result["source_unchanged"])

    def test_36_csv_invalid_row_is_quarantined_without_url(self):
        _, metadata = self.folder_metadata()
        source = self.base / "links.csv"
        bad_url = "https://evil.example/secret-link"
        source.write_text(
            "content_id,item_id,isbn,affiliate_url\n"
            f"{metadata['content_id']},amazon-001,,{bad_url}\n",
            encoding="utf-8",
        )
        result = amazon.import_links(source, self.db)
        self.assertEqual(result["failed"], 1)
        with closing(metrics_db.connect(self.db)) as conn:
            row = conn.execute(
                "SELECT row_json FROM amazon_link_import_quarantine").fetchone()
        self.assertNotIn(bad_url, row["row_json"])

    def test_37_disable_removes_section_and_preserves_backup(self):
        folder, metadata = self.folder_metadata()
        result = amazon.disable_for_note(metadata["content_id"], self.db)
        article = (folder / "article.md").read_text(encoding="utf-8")
        self.assertNotIn("## 関連書籍", article)
        self.assertTrue(Path(result["backup_path"]).exists())

    def test_38_disable_updates_note_database(self):
        _, metadata = self.folder_metadata()
        amazon.disable_for_note(metadata["content_id"], self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            row = conn.execute(
                "SELECT related_books_json FROM note_drafts "
                "WHERE content_id=?", (metadata["content_id"],)).fetchone()
        self.assertEqual(row["related_books_json"], "[]")

    def test_39_paapi_disabled_falls_back_to_manual(self):
        with patch.dict(os.environ, {
            "AMAZON_ASSOCIATE_MODE": "paapi",
            "AMAZON_PAAPI_ENABLED": "false",
        }):
            rows, mode, reason = amazon.build_items(self.selection)
        self.assertTrue(rows)
        self.assertEqual((mode, reason), ("manual", "paapi_disabled"))

    def test_40_missing_current_api_credentials_falls_back(self):
        with patch.dict(os.environ, {
            "AMAZON_ASSOCIATE_MODE": "paapi",
            "AMAZON_PAAPI_ENABLED": "true",
            "AMAZON_CREATORS_API_CREDENTIAL_ID": "",
            "AMAZON_CREATORS_API_CREDENTIAL_SECRET": "",
            "AMAZON_CREATORS_API_CREDENTIAL_VERSION": "",
        }):
            rows, mode, reason = amazon.build_items(self.selection)
        self.assertTrue(rows)
        self.assertEqual(mode, "manual")
        self.assertEqual(reason, "creators_api_credentials_missing")

    def test_41_api_failure_falls_back_without_stopping_article(self):
        client = Mock()
        client.configured.return_value = True
        client.search_items.side_effect = RuntimeError("temporary")
        with patch.dict(os.environ, {
            "AMAZON_ASSOCIATE_MODE": "paapi",
            "AMAZON_PAAPI_ENABLED": "true",
        }):
            rows, mode, reason = amazon.build_items(self.selection, client)
        self.assertTrue(rows)
        self.assertEqual((mode, reason), (
            "manual", "paapi_failed_manual_fallback"))

    def test_42_zero_extra_call_budget_prevents_api_call(self):
        client = Mock()
        client.configured.return_value = True
        with patch.dict(os.environ, {
            "AMAZON_ASSOCIATE_MODE": "paapi",
            "AMAZON_PAAPI_ENABLED": "true",
            "AMAZON_RECOMMENDATION_MAX_EXTRA_CALLS_PER_ARTICLE": "0",
        }):
            rows, mode, reason = amazon.build_items(self.selection, client)
        client.search_items.assert_not_called()
        self.assertEqual(rows, [])
        self.assertEqual(mode, "skipped")
        self.assertEqual(reason, "amazon_recommendation_budget_unavailable")

    def test_43_creators_api_v2_token_endpoint(self):
        self.assertIn(
            "amazoncognito.com",
            amazon.CreatorsApiClient._token_endpoint("2.3"))

    def test_44_creators_api_v3_token_endpoint(self):
        self.assertEqual(
            amazon.CreatorsApiClient._token_endpoint("3.3"),
            "https://api.amazon.co.jp/auth/o2/token",
        )

    def test_45_unsupported_api_version_is_rejected(self):
        with self.assertRaises(ValueError):
            amazon.CreatorsApiClient._token_endpoint("1.0")

    def test_46_api_search_is_books_only_and_capped(self):
        response = Mock()
        response.json.side_effect = [
            {"access_token": "token", "expires_in": 3600},
            {"searchResult": {"items": []}},
        ]
        response.raise_for_status.return_value = None
        session = Mock()
        session.post.return_value = response
        with patch.dict(os.environ, {
            "AMAZON_CREATORS_API_CREDENTIAL_ID": "id",
            "AMAZON_CREATORS_API_CREDENTIAL_SECRET": "secret",
            "AMAZON_CREATORS_API_CREDENTIAL_VERSION": "3.3",
            "AMAZON_PAAPI_PARTNER_TAG": "example-22",
        }):
            amazon.CreatorsApiClient(session=session).search_items(
                "政治", 99)
        request = session.post.call_args_list[1].kwargs["json"]
        self.assertEqual(request["searchIndex"], "Books")
        self.assertEqual(request["itemCount"], 10)

    def test_47_cli_status_output_has_no_tracking_id(self):
        self.folder_metadata()
        output = io.StringIO()
        with redirect_stdout(output):
            code = local_bot.cmd_amazon_links_status(None)
        self.assertEqual(code, 0)
        self.assertNotIn("example-22", output.getvalue())
        self.assertNotIn("https://", output.getvalue())

    def test_48_database_contains_all_amazon_tables(self):
        counts = metrics_db.table_counts(self.db)
        for table in (
            "amazon_associate_items", "amazon_link_events",
            "amazon_link_import_quarantine",
        ):
            self.assertIn(table, counts)

    def test_49_prohibited_copy_fails_quality(self):
        result = self.draft()
        folder, metadata = self.folder_metadata()
        article = (
            folder / "article.md").read_text(encoding="utf-8")
        article += "\n絶対に買うべき"
        checked = free_note.quality_check(
            article, metadata["title"], metadata["primary_sources"],
            metadata["secondary_sources"], metadata["amazon_items"])
        self.assertIn("prohibited_promotional_phrase", checked["reasons"])

    def test_50_source_has_no_browser_or_scraping_dependency(self):
        source = (ROOT / "src" / "amazon_associate.py").read_text(
            encoding="utf-8").lower()
        for forbidden in ("selenium", "playwright", "beautifulsoup"):
            self.assertNotIn(forbidden, source)

    def test_51_no_auto_purchase_or_note_publish_command(self):
        source = (ROOT / "local_bot.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("amazon-purchase", source)
        self.assertNotIn("note-auto-publish", source)

    def test_52_no_candidates_adds_no_disclosure_or_section(self):
        article = amazon.append_or_replace_section("# 記事\n\n本文", [])
        self.assertNotIn("## 関連書籍", article)
        self.assertNotIn(amazon.DISCLOSURE, article)

    def test_53_budget_skip_does_not_stop_note_generation(self):
        with patch.dict(os.environ, {
            "AMAZON_ASSOCIATE_MODE": "paapi",
            "AMAZON_PAAPI_ENABLED": "true",
            "AMAZON_RECOMMENDATION_MONTHLY_BUDGET_USD": "0",
        }):
            result = free_note.generate_free_note(
                "evergreen_institutional_explainer",
                dry_run=True,
                path=self.db,
                now=datetime(2026, 7, 25, 14, tzinfo=JST),
            )
        article = Path(result["path"], "article.md").read_text(
            encoding="utf-8")
        self.assertEqual(result["status"], "draft")
        self.assertGreaterEqual(result["character_count"], 1800)
        self.assertNotIn("## 関連書籍", article)


if __name__ == "__main__":
    unittest.main()
