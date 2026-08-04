from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import metrics_db
import social_anger as anger


class SocialAngerPhaseATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "social-anger.db"
        metrics_db.init_db(self.db)
        anger.apply_migrations(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def fixture(content_id):
        return dict(next(row for row in anger.FIXTURES
                         if row["content_id"] == content_id))

    def test_01_tax_burden_classifies_as_unfair_burden(self):
        self.assertEqual(
            anger.classify_anger_axis(self.fixture("anger-tax")),
            "unfair_burden",
        )

    def test_02_management_avoiding_responsibility_is_gap(self):
        item = {
            **self.fixture("anger-governance"),
            "summary": "経営者が責任を取らない一方、現場だけを処分した。",
        }
        self.assertEqual(anger.classify_anger_axis(item), "accountability_gap")

    def test_03_explanation_failure_classification(self):
        self.assertEqual(
            anger.classify_anger_axis(self.fixture("anger-admin")),
            "explanation_failure",
        )

    def test_04_double_standard_classification(self):
        item = {
            **self.fixture("anger-policy"),
            "summary": "同様の事案で基準の不一致が確認された。",
        }
        self.assertEqual(anger.classify_anger_axis(item), "double_standard")

    def test_05_internal_control_is_governance_failure(self):
        self.assertEqual(
            anger.classify_anger_axis(self.fixture("anger-governance")),
            "governance_failure",
        )

    def test_06_victim_relief_gap_classification(self):
        self.assertEqual(
            anger.classify_anger_axis(self.fixture("anger-victim")),
            "victim_abandonment",
        )

    def test_07_target_is_policy_or_process(self):
        target = anger.classify_target_type(self.fixture("anger-tax"))
        self.assertIn(target, anger.ANGER_TARGET_TYPES)
        self.assertNotIn(target, anger.PROTECTED_ATTRIBUTES)

    def test_08_nationality_is_not_target_type(self):
        target = anger.classify_target_type(self.fixture("anger-attribute"))
        self.assertEqual(target, "unknown")

    def test_09_workers_and_management_are_separate(self):
        roles = anger.extract_roles(self.fixture("anger-governance"))
        self.assertIn("従業員", roles["affected_group"])
        self.assertIn("経営陣", roles["decision_maker"])
        self.assertNotEqual(roles["affected_group"], roles["responsible_entity"])

    def test_10_decision_maker_and_executor_can_be_separate(self):
        roles = anger.extract_roles(self.fixture("anger-tax"))
        self.assertIn("政府", roles["decision_maker"])
        self.assertIn("所管省庁", roles["responsible_entity"])

    def test_11_cost_bearer_and_beneficiary_are_separate(self):
        roles = anger.extract_roles(self.fixture("anger-tax"))
        self.assertNotEqual(roles["cost_bearer"], roles["beneficiary"])

    def test_12_important_topic_has_fact_candidate(self):
        rows = anger.generate_candidates(self.fixture("anger-governance"), path=self.db)
        self.assertIn("fact", {row["angle"] for row in rows})

    def test_13_important_topic_has_burden_candidate(self):
        rows = anger.generate_candidates(self.fixture("anger-governance"), path=self.db)
        self.assertIn("burden", {row["angle"] for row in rows})

    def test_14_important_topic_has_responsibility_candidate(self):
        rows = anger.generate_candidates(self.fixture("anger-governance"), path=self.db)
        self.assertIn("responsibility", {row["angle"] for row in rows})

    def test_15_important_topic_has_structure_candidate(self):
        rows = anger.generate_candidates(self.fixture("anger-governance"), path=self.db)
        self.assertIn("structure", {row["angle"] for row in rows})

    def test_16_important_topic_has_improvement_candidate(self):
        rows = anger.generate_candidates(self.fixture("anger-governance"), path=self.db)
        self.assertIn("improvement", {row["angle"] for row in rows})

    def test_17_duplicate_paraphrases_are_removed(self):
        row = {
            "platform": "x", "post_type": "one", "post_text": "同じ 文",
        }
        duplicate = {
            "platform": "x", "post_type": "two", "post_text": "同じ文",
        }
        self.assertEqual(len(anger._deduplicate([row, duplicate])), 1)

    def test_18_rejects_everyone_is_angry_generalization(self):
        violations = anger.safety_violations(
            "国民全員が怒っている。",
            self.safe_metadata(),
        )
        self.assertIn("unsupported_generalization", violations)

    def test_19_rejects_unsupported_interest_claim(self):
        violations = anger.safety_violations(
            "これは利権です。",
            self.safe_metadata(),
        )
        self.assertIn("unsupported_term:利権", violations)

    def test_20_rejects_unsupported_intermediary_skimming(self):
        violations = anger.safety_violations(
            "中抜きが行われた。",
            self.safe_metadata(),
        )
        self.assertIn("unsupported_term:中抜き", violations)

    def test_21_rejects_unsupported_concealment(self):
        violations = anger.safety_violations(
            "組織的な隠蔽だ。",
            self.safe_metadata(),
        )
        self.assertIn("unsupported_term:隠蔽", violations)

    def test_22_allows_internal_control_collapse_with_evidence(self):
        metadata = self.safe_metadata()
        metadata["term_evidence"] = {
            "内部統制崩壊": ["申請と承認が同一アカウントで完結"],
        }
        violations = anger.safety_violations(
            "確認された事実は内部統制崩壊を示します。",
            metadata,
        )
        self.assertNotIn("unsupported_term:内部統制崩壊", violations)

    def test_23_rejects_personal_attack(self):
        violations = anger.safety_violations(
            "社長は無能だ。",
            self.safe_metadata(),
        )
        self.assertIn("personal_attack", violations)

    def test_24_rejects_protected_attribute_attack(self):
        violations = anger.safety_violations(
            "特定の国籍を責め、排除すべきだ。",
            self.safe_metadata(),
        )
        self.assertIn("protected_attribute_targeting", violations)

    def test_25_rejects_violence_or_harassment(self):
        violations = anger.safety_violations(
            "責任者の住所を晒せ。",
            self.safe_metadata(),
        )
        self.assertIn("violence_or_harassment", violations)

    def test_26_rejects_sensational_only_candidate(self):
        violations = anger.safety_violations(
            "大炎上。ヤバすぎる。完全終了。",
            self.safe_metadata(),
        )
        self.assertIn("sensational_language", violations)

    def test_27_missing_improvement_is_penalized(self):
        metadata = self.safe_metadata()
        metadata["proposed_improvement"] = ""
        self.assertIn(
            "improvement_missing",
            anger.safety_violations("確認済み事実です。", metadata),
        )

    def test_28_evidence_strength_score(self):
        scores = anger.score_assessment(self.fixture("anger-tax"))
        self.assertGreaterEqual(scores["evidence_strength_score"], 8)

    def test_29_public_harm_score(self):
        scores = anger.score_assessment(self.fixture("anger-tax"))
        self.assertGreaterEqual(scores["public_harm_score"], 6.5)

    def test_30_unfairness_score(self):
        scores = anger.score_assessment(self.fixture("anger-tax"))
        self.assertGreaterEqual(scores["unfairness_score"], 6.5)

    def test_31_accountability_gap_score(self):
        scores = anger.score_assessment(self.fixture("anger-governance"))
        self.assertGreaterEqual(scores["accountability_gap_score"], 6.5)

    def test_32_constructive_value_score(self):
        scores = anger.score_assessment(self.fixture("anger-incident"))
        self.assertGreaterEqual(scores["constructive_value_score"], 6.5)

    def test_33_social_only_has_high_exploitation_risk(self):
        scores = anger.score_assessment(self.fixture("anger-social-only"))
        self.assertGreater(scores["anger_exploitation_risk"], 3)

    def test_34_attribute_attack_has_high_mob_targeting_risk(self):
        scores = anger.score_assessment(self.fixture("anger-attribute"))
        self.assertGreater(scores["mob_targeting_risk"], 2)

    def test_35_unverified_accusation_has_high_defamation_risk(self):
        item = {
            **self.fixture("anger-social-only"),
            "summary": "この人物が犯人で犯罪者だ。",
        }
        scores = anger.score_assessment(item)
        self.assertGreater(scores["defamation_risk"], 2)

    def test_36_target_concentration_is_detected(self):
        anger.record_target("one", "management", "same", "同じ組織", path=self.db)
        anger.record_target("one", "management", "same", "同じ組織", path=self.db)
        anger.record_target("two", "policy", "other", "別対象", path=self.db)
        self.assertGreater(anger.target_concentration("same", path=self.db), .3)

    def test_37_phase_1_has_no_anger_candidate(self):
        item = {
            **self.fixture("anger-incident"),
            "incident_phase": "phase_1_emergency",
        }
        rows = anger.generate_candidates(item, path=self.db)
        self.assertTrue(all(row["status"] == "rejected" for row in rows))

    def test_38_phase_2_prioritizes_confirmed_facts(self):
        item = {
            **self.fixture("anger-incident"),
            "incident_phase": "phase_2_confirmed_outline",
        }
        rows = anger.generate_candidates(item, path=self.db)
        fact = next(row for row in rows if row["angle"] == "fact")
        other = next(row for row in rows if row["angle"] != "fact")
        self.assertNotIn("major_incident_phase_2_facts_only", fact["decision_reason"])
        self.assertIn("major_incident_phase_2_facts_only", other["decision_reason"])

    def test_39_phase_3_allows_structure_and_responsibility(self):
        rows = anger.generate_candidates(self.fixture("anger-incident"), path=self.db)
        reasons = {
            row["angle"]: row["decision_reason"] for row in rows
            if row["angle"] in {"structure", "responsibility"}
        }
        self.assertTrue(all("major_incident_phase" not in reason
                            for reason in reasons.values()))

    def test_40_victim_count_is_not_used_as_hook(self):
        item = {
            **self.fixture("anger-incident"),
            "summary": "被害者数は公式資料で確認済み。",
        }
        rows = anger.generate_candidates(item, path=self.db)
        self.assertTrue(all("被害者数" not in row["hook"] for row in rows))

    def test_41_threads_is_not_x_copy(self):
        item = self.fixture("anger-governance")
        assessment = anger.assess(item)
        x = anger.build_candidate(item, assessment, "responsibility", platform="x")
        threads = anger.build_candidate(
            item, assessment, "responsibility", platform="threads")
        self.assertNotEqual(x["post_text"], threads["post_text"])

    def test_42_major_incident_threads_has_no_light_question_or_emoji(self):
        item = self.fixture("anger-incident")
        rows = anger.generate_candidates(item, platform="threads", path=self.db)
        self.assertTrue(all("皆さんはどう思いますか" not in row["post_text"]
                            for row in rows))
        self.assertTrue(all(not any(symbol in row["post_text"]
                                    for symbol in ("😀", "🚨", "🌷", "🔥"))
                            for row in rows))

    def test_43_threads_has_specific_accountability_question(self):
        rows = anger.generate_candidates(
            self.fixture("anger-governance"), platform="threads", path=self.db)
        self.assertTrue(any("実行期限" in row["public_question"] for row in rows))

    def test_44_threads_contains_improvement(self):
        rows = anger.generate_candidates(
            self.fixture("anger-governance"), platform="threads", path=self.db)
        self.assertTrue(all(row["proposed_improvement"] for row in rows))

    def test_45_hostile_replies_are_not_success(self):
        metrics = anger.learning_metrics([
            {"hostile_or_abusive": True, "constructive_reply": 1},
        ])
        self.assertFalse(metrics["hostile_replies_counted_as_success"])
        self.assertEqual(metrics["constructive_reply_rate"], 0)

    def test_46_saves_and_specific_questions_are_valued(self):
        metrics = anger.learning_metrics([
            {"bookmarks": 4, "quotes": 2, "specific_question": 1},
        ])
        self.assertEqual(metrics["quality_signals"]["bookmarks"], 4)
        self.assertGreater(metrics["specific_question_rate"], 0)

    def test_47_anger_to_solution_rate(self):
        metrics = anger.learning_metrics([], [
            {"status": "paired"}, {"status": "solution_missing"},
        ])
        self.assertEqual(metrics["anger_to_solution_completion_rate"], .5)

    def test_48_same_target_concentration_metric(self):
        metrics = anger.learning_metrics([
            {"target_key": "a"}, {"target_key": "a"}, {"target_key": "b"},
        ])
        self.assertGreater(metrics["target_concentration"], .6)

    def test_49_production_post_count_is_not_changed(self):
        self.assertFalse(anger.status(self.db)["production_posting_limits_changed"])

    def test_50_profile_is_not_changed(self):
        self.assertFalse(anger.status(self.db)["profile_changed"])

    def test_51_live_env_is_not_rewritten_by_full_cycle(self):
        env = ROOT / ".env"
        before = hashlib.sha256(env.read_bytes()).hexdigest()
        anger.full_cycle(path=self.db)
        after = hashlib.sha256(env.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_52_no_x_or_threads_publish_client(self):
        source = (SRC / "social_anger.py").read_text(encoding="utf-8")
        for forbidden in ("tweepy", "ThreadsClient", "post_to_x"):
            self.assertNotIn(forbidden, source)

    def test_53_major_incident_pipeline_is_not_replaced(self):
        source = (SRC / "social_anger.py").read_text(encoding="utf-8")
        self.assertNotIn("def publish_major_incident", source)
        self.assertEqual(anger.PHASE_ORDER["phase_3_cause_investigation"], 3)

    def test_54_factory_tables_remain_available(self):
        import social_content_factory as factory
        factory.apply_migrations(self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("content_packets", names)
        self.assertIn("social_anger_candidates", names)

    def test_55_migration_is_idempotent(self):
        self.assertTrue(anger.apply_migrations(self.db))
        self.assertTrue(anger.apply_migrations(self.db))
        with closing(metrics_db.connect(self.db)) as conn:
            names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("social_anger_assessments", names)

    def test_56_no_task_registration(self):
        source = (SRC / "social_anger.py").read_text(encoding="utf-8")
        self.assertNotIn("Register-ScheduledTask", source)
        self.assertNotIn("Start-ScheduledTask", source)

    def test_57_all_required_tables_exist(self):
        required = {
            "social_anger_assessments", "social_anger_candidates",
            "social_anger_targets", "anger_solution_links",
            "social_anger_weekly_reviews",
        }
        with closing(metrics_db.connect(self.db)) as conn:
            actual = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue(required <= actual)

    def test_58_full_cycle_has_no_external_publish(self):
        result = anger.full_cycle(path=self.db)
        self.assertTrue(all(value == 0 for value in result["safety"].values()))

    def test_59_structured_output_has_required_fields(self):
        item = self.fixture("anger-governance")
        row = anger.generate_candidates(item, path=self.db)[0]
        required = {
            "post_text", "title", "hook", "verified_facts", "affected_group",
            "decision_maker", "beneficiary", "cost_bearer",
            "responsible_entity", "anger_axis", "anger_target_type",
            "unfairness_explanation", "accountability_gap", "public_question",
            "proposed_improvement", "fact_opinion_boundary",
            "evidence_strength_score", "public_harm_score", "unfairness_score",
            "accountability_gap_score", "constructive_value_score",
            "anger_exploitation_risk", "mob_targeting_risk",
            "defamation_risk", "oversimplification_risk", "ban_risk",
            "decision_reason",
        }
        self.assertTrue(required <= set(row))

    def test_60_phase_b_is_shadow_only(self):
        with patch.dict(os.environ, {
            "SOCIAL_ANGER_PRODUCTION_ENABLED": "true",
            "SOCIAL_ANGER_PRODUCTION_PHASE": "B",
        }):
            result = anger.evaluate_production_candidate(
                self.fixture("anger-governance"),
                "確認済みの内部統制上の問題です。\n\n"
                "経営陣には、再発防止策の期限と第三者検証を示す責任があります。",
                platform="x", path=self.db,
            )
        self.assertFalse(result["production_publish_connected"])
        self.assertIn("phase_shadow_only", result["decision_reasons"])

    def test_61_phase_c_connects_eligible_low_risk_candidate(self):
        with patch.dict(os.environ, {
            "SOCIAL_ANGER_PRODUCTION_ENABLED": "true",
            "SOCIAL_ANGER_PRODUCTION_PHASE": "C",
            "SOCIAL_ANGER_MIN_PRODUCTION_EFFECTIVE_SCORE": "0",
        }):
            result = anger.evaluate_production_candidate(
                self.fixture("anger-governance"),
                "確認済みの内部統制上の問題です。\n\n"
                "経営陣には、再発防止策の期限と第三者検証を示す責任があります。",
                platform="x", path=self.db,
            )
        self.assertTrue(result["production_publish_connected"])
        self.assertEqual(result["safety_violations"], [])

    def test_62_phase_c_does_not_connect_unknown_target(self):
        item = self.fixture("anger-attribute")
        with patch.dict(os.environ, {
            "SOCIAL_ANGER_PRODUCTION_ENABLED": "true",
            "SOCIAL_ANGER_PRODUCTION_PHASE": "C",
            "SOCIAL_ANGER_MIN_PRODUCTION_EFFECTIVE_SCORE": "0",
        }):
            result = anger.evaluate_production_candidate(
                item,
                "確認済みの事実と制度上の論点を分けて確認します。\n\n"
                "責任主体は、期限と検証方法を公開する必要があります。",
                platform="threads", path=self.db,
            )
        self.assertFalse(result["production_publish_connected"])
        self.assertIn(
            "target_not_in_guarded_rollout", result["decision_reasons"])

    def test_63_major_incident_phase_two_blocks_responsibility_angle(self):
        item = {
            **self.fixture("anger-governance"),
            "major_incident": True,
            "incident_phase": "phase_2_confirmed_outline",
        }
        with patch.dict(os.environ, {
            "SOCIAL_ANGER_PRODUCTION_ENABLED": "true",
            "SOCIAL_ANGER_PRODUCTION_PHASE": "C",
        }):
            result = anger.evaluate_production_candidate(
                item,
                "確認済み事実だけを整理します。\n\n調査結果を待ちます。",
                platform="x", path=self.db,
            )
        self.assertFalse(result["production_publish_connected"])
        self.assertIn(
            "major_incident_phase_2_facts_only",
            result["safety_violations"],
        )

    def test_64_prompt_context_contains_roles_and_improvement(self):
        context = anger.production_prompt_context(
            self.fixture("anger-tax"), path=self.db)["prompt_context"]
        self.assertTrue(context["affected_group"])
        self.assertTrue(context["responsible_entity"])
        self.assertTrue(context["proposed_improvement"])
        self.assertTrue(context["fact_opinion_boundary"])

    def test_65_status_reports_guarded_production_connection(self):
        with patch.dict(os.environ, {
            "SOCIAL_ANGER_PRODUCTION_ENABLED": "true",
            "SOCIAL_ANGER_PRODUCTION_PHASE": "C",
        }):
            result = anger.status(self.db)
        self.assertEqual(result["phase"], "C")
        self.assertTrue(result["production_publish_connected"])

    def test_66_consumption_tax_is_low_risk_budget_target(self):
        item = {
            "content_id": "tax-current",
            "topic_key": "消費税減税",
            "title": "食料品の消費税減税案",
            "summary": "税率と実施期間、家計負担への影響を政府が公表した。",
            "verified": True,
            "source_type": "official",
            "source_name": "政府",
        }
        assessment = anger.assess(item)
        self.assertEqual(
            assessment["anger_target_type"], "budget_allocation")
        self.assertGreaterEqual(
            assessment["scores"]["public_harm_score"], 6.5)

    @staticmethod
    def safe_metadata():
        return {
            "term_evidence": {},
            "proposed_improvement": "期限と検証方法を公開する。",
            "responsible_entity": ["責任主体"],
            "fact_opinion_boundary": "事実と評価を分離",
        }


if __name__ == "__main__":
    unittest.main()
