-- Phase A post-performance experiment tables.
-- Idempotent; no existing table or production posting configuration is changed.
CREATE TABLE IF NOT EXISTS post_performance_features (
 id INTEGER PRIMARY KEY, post_id TEXT, content_id TEXT, platform TEXT,
 topic_key TEXT, category TEXT, post_type TEXT, hook_type TEXT,
 angle_type TEXT, structure_type TEXT, ending_type TEXT,
 character_count INTEGER, line_count INTEGER, sentence_count INTEGER,
 media_present INTEGER, breaking_flag INTEGER, source_quality_score REAL,
 topic_demand_score REAL, news_freshness_hours REAL,
 specific_number_present INTEGER, specific_actor_present INTEGER,
 cost_bearer_present INTEGER, decision_maker_present INTEGER,
 human_stake_present INTEGER, contrast_present INTEGER, surprise_present INTEGER,
 comparison_present INTEGER, future_scenario_present INTEGER,
 counterargument_present INTEGER, concrete_question_present INTEGER,
 memorable_line_present INTEGER, abstract_term_ratio REAL,
 average_sentence_length REAL, generic_conclusion_flag INTEGER,
 same_hook_recent_count INTEGER, same_structure_recent_count INTEGER,
 same_ending_recent_count INTEGER, semantic_similarity_recent REAL,
 noun_density REAL, previous_post_interval_hours REAL,
 previous_24h_normalized_impressions REAL, major_news_flag INTEGER,
 topic_competition_score REAL, topic_saturation_score REAL,
 feature_json TEXT, created_at TEXT, updated_at TEXT, UNIQUE(post_id,platform));
CREATE TABLE IF NOT EXISTS post_performance_outcomes (
 id INTEGER PRIMARY KEY, post_id TEXT, captured_at TEXT,
 measurement_window TEXT, followers_at_publish INTEGER,
 impressions INTEGER, views INTEGER, likes INTEGER, reposts INTEGER,
 quotes INTEGER, replies INTEGER, bookmarks INTEGER, shares INTEGER,
 profile_clicks INTEGER, link_clicks INTEGER, follows INTEGER,
 like_rate REAL, repost_rate REAL, quote_rate REAL, reply_rate REAL,
 bookmark_rate REAL, share_rate REAL, profile_click_rate REAL,
 follow_conversion_rate REAL, engagement_rate REAL,
 specific_reply_rate REAL, outcome_json TEXT, created_at TEXT,
 UNIQUE(post_id,measurement_window));
CREATE TABLE IF NOT EXISTS post_experiments (
 id INTEGER PRIMARY KEY, experiment_id TEXT UNIQUE, topic_key TEXT,
 content_id TEXT, platform TEXT, fact_packet_hash TEXT,
 control_candidate_id TEXT, variant_candidate_ids_json TEXT,
 assignment_group TEXT, prediction_version TEXT, selection_status TEXT,
 published_candidate_id TEXT, published_candidate_type TEXT,
 result_status TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS post_candidate_predictions (
 id INTEGER PRIMARY KEY, experiment_id TEXT, candidate_id TEXT,
 candidate_type TEXT, hook_strength REAL, specificity REAL,
 new_information_value REAL, quoteability REAL, fact_strength REAL,
 predicted_performance_score REAL, prediction_json TEXT, created_at TEXT,
 UNIQUE(experiment_id,candidate_id));
CREATE TABLE IF NOT EXISTS post_experiment_results (
 id INTEGER PRIMARY KEY, experiment_id TEXT, candidate_id TEXT,
 candidate_type TEXT, sample_group TEXT,
 normalized_impression_score REAL, normalized_profile_click_score REAL,
 normalized_follow_score REAL, normalized_quote_score REAL,
 result_json TEXT, evaluated_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS post_feature_correlations (
 id INTEGER PRIMARY KEY, feature_name TEXT, outcome_name TEXT,
 sample_size INTEGER, pearson_correlation REAL, spearman_correlation REAL,
 confidence_low REAL, confidence_high REAL,
 analysis_period_start TEXT, analysis_period_end TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS post_experiment_recommendations (
 id INTEGER PRIMARY KEY, analysis_period_start TEXT, analysis_period_end TEXT,
 recommendation_type TEXT, feature_name TEXT, control_metric REAL,
 variant_metric REAL, sample_size INTEGER, confidence REAL,
 recommendation TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS post_experiment_snapshots (
 id INTEGER PRIMARY KEY, experiment_id TEXT, snapshot_type TEXT,
 snapshot_hash TEXT, payload_json TEXT, source_timestamp TEXT,
 captured_at TEXT, created_at TEXT,
 UNIQUE(experiment_id,snapshot_type));
CREATE TABLE IF NOT EXISTS post_reply_events (
 id INTEGER PRIMARY KEY, platform TEXT, root_post_id TEXT, reply_id TEXT,
 author_hash TEXT, reply_text TEXT, reply_classification TEXT,
 replied_at TEXT, source TEXT, collected_at TEXT,
 UNIQUE(platform,reply_id));
