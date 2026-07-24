# 久世ゆい 差分改修計画・既存実装調査

調査日: 2026-07-24  
対象: `D:\SNS Bot\politics-narrative`

## 改修方針

- 現行の毎時監視、投稿上限、品質ゲート、SQLite/JSON記録、予算ガードを維持する。
- 本番投稿、返信、引用、リポスト、いいね、フォローは実行しない。
- `.env`は指定キーだけを差分変更し、APIキーを表示・変更しない。
- SQLiteマイグレーションは `CREATE TABLE IF NOT EXISTS` と列存在確認で再実行可能にする。
- 本番タスクは改修後も停止状態を維持する。

## 既存実装の接続点

| 項目 | 現在の場所 | 調査結果 |
|---|---|---|
| xAIクライアント | `src/xai_radar.py` | Responses APIとX Searchを使用。検索1回制限、実費ticks記録、RSSへの非致命フォールバックあり |
| xAIスケジュール | `src/xai_radar.py:should_run` | 6時刻を直接判定。適応型スケジュールは未実装 |
| xAIコスト | `src/xai_radar.py`, `src/api_budget.py` | `cost_in_usd_ticks`をUSDへ変換。警告・当日ハード停止は未実装 |
| モデルルーター | `src/model_router.py` | mini/Luna/nano/Terra/Solの経路あり。日次はLuna、Luna上限4、nano上限8 |
| Batch投入・回収 | `src/openai_batch.py` | Responses Batch、24h、custom_id重複防止、完了回収あり |
| 日次レビュー | `local_bot.py:cmd_report` | ローカル集計後 `report_ai` を呼ぶ。現在Batch対象で04:45 |
| 週次レビュー | `local_bot.py:_run_long_report` | Terra、Batch対応、週単位dedupe keyあり |
| 品質ゲート | `src/post.py`, `src/publishing_policy.py`, `src/phase2.py` | 品質7.0、BANリスク3、URL・内部ラベル・未確認X・文脈不一致を拒否 |
| 学習反映 | `local_bot.py:cmd_report`, `src/review_scoring.py` | 4軸評価と安全フィルターあり。用途別winnerと転換データは不足 |
| 引用・返信キュー | `src/engagement_queue.py` | SQLite/JSON/Markdown保存、状態更新、自動送信なし。時刻運用とbriefは未実装 |
| SQLite移行 | `src/metrics_db.py` | `CREATE TABLE IF NOT EXISTS`とJSON移行。今回要求テーブルは未実装 |
| フォロワー・転換 | なし | スナップショット、外部転換CSV取込とも未実装 |
| テスト | `tests/` | 7ファイル、158テスト。機能・予算・安全中心 |
| 意味品質Evals | なし | `evals/`、100件fixture、CLIとも未実装 |
| プロンプト版 | `src/post.py` | `PROMPT_VERSION`を保存。現在既定`v1` |

## 実装差分

1. xAIを基本3回、低変動日2回へ削減し、360分キャッシュ・実費警告・当日停止を追加する。
2. 日次レビューを04:40の同期miniへ変更し、04:55を期限としてローカル結果へフォールバックする。
3. Batchを週次レビュー・明示的オフラインEvalsに限定する。
4. Luna上限2、nano上限6、Sol自動経路なしへ簡素化する。
5. 100件以上の政治品質fixtureと `eval-quality` CLIを追加する。
6. 人間評価出力・CSV/JSON取込、品質ダッシュボードを追加する。
7. 引用・返信候補を12:20/20:20に生成し、`engagement-brief`を追加する。
8. フォロワースナップショット、外部転換イベント、用途別winnerを追加する。
9. 怒り・個人攻撃・党派性・訂正・削除対象を学習成功例から除外する。
10. prompt versionを `x-growth-quality-v2` に変更し、版別比較を保存する。

## 既存データ保護

- `data/bot_metrics.db`、JSON履歴、投稿履歴、レビュー履歴、API使用履歴は削除しない。
- 新規テーブル・列は追加のみとする。
- SQLiteが利用できない場合は既存どおりJSONへフォールバックする。

