# 久世ゆい 差分改修 最終報告

実施日: 2026-07-24  
対象: `D:\SNS Bot\politics-narrative`

## 1. バックアップ

- `D:\SNS Bot\politics-narrative\backups\politics-narrative-backup-20260724-010132`
- 16,742ファイル

## 2. 既存コード調査

調査結果と差分計画は `IMPROVEMENT_PLAN_X_GROWTH_COST_QUALITY.md` に保存した。
既存の投稿上限、毎時監視、品質ゲート、SQLite/JSON、予算ガード、
自動エンゲージメント禁止を維持した。

## 3. 主な変更ファイル

- `.env`（指定キーだけを差分変更、APIキーは未変更）
- `.env.example`
- `README.md`
- `local_bot.py`
- `src/api_budget.py`
- `src/engagement_queue.py`
- `src/metrics_db.py`
- `src/model_router.py`
- `src/openai_batch.py`
- `src/phase2.py`
- `src/post.py`
- `src/publishing_policy.py`
- `src/report_ai.py`
- `src/review_scoring.py`
- `src/xai_radar.py`
- `tests/test_model_router.py`
- `tests/test_openai_batch.py`
- `tests/test_xai_engagement_style.py`

## 4. 新規ファイル

- `IMPROVEMENT_PLAN_X_GROWTH_COST_QUALITY.md`
- `IMPLEMENTATION_REPORT_X_GROWTH_COST_QUALITY_2026-07-24.md`
- `src/growth_tracking.py`
- `src/quality_evals.py`
- `tests/test_growth_quality_v2.py`
- `evals/fixtures/political_quality.jsonl`
- `evals/expected/criteria.json`
- `evals/results/*.json`
- `reports/engagement/2026-07-24.md`

作業開始前から存在した未コミット変更・未追跡ファイルは削除していない。

## 5. xAI

- 基本スケジュール: `06:00,12:00,18:00`
- 1日上限: 3回
- 低変動日: `06:00,18:00`
- 高変動日も3回を超えない
- キャッシュ・lookback: 360分
- ローカル変動指標: 新規候補、重大候補、同一topic増加、前回attention
- 実費: `cost_in_usd_ticks`からUSD換算して保存
- 警告: 1回 `$0.012` 超
- 当日停止: 1回 `$0.020` 超
- 月間予測 `$1.80` 超で2回運用
- 月 `$2.00` 到達で停止し、RSS・公式情報は継続
- xAI情報は外部確認済みRSS/公式候補のattention補助だけに使用

## 6. 日次・週次レビュー

- 日次: 04:40、同期Responses API、`gpt-5.4-mini`
- 日次期限: 04:55
- 日次失敗・期限超過: 保存済みパターンとローカル集計を使用
- 日次API上限: 1回
- 日次レビューはBatch対象外
- 週次: 日曜22:30にTerra Batch投入
- 週次回収: 月曜04:15
- 週単位の一意custom_idで重複投入を防止
- Batch未完了・失敗で投稿デーモンを止めない

## 7. モデル構成

| 用途 | モデル |
|---|---|
| 通常投稿 | gpt-5.4-mini |
| 重要ニュース | gpt-5.6-luna |
| 曖昧時分類 | gpt-5.4-nano |
| 日次レビュー | gpt-5.4-mini |
| 週次レビュー | gpt-5.6-terra |
| 手動プレミアム | gpt-5.6-sol |

- Luna上限: 2回/日
- nano上限: 6回/日
- `OPENAI_PREMIUM_ENABLED=false`
- Solは自動投稿・分類・安全審査・日次・週次fallback・Batch fallback候補に含めない

## 8. 品質Evals

- fixture: 112件、16カテゴリ
- 長文転載なし。短い架空・公開事実ベースの入力
- 採点: 事実性、関連性、論理性、独自性、自然な日本語、安全性
- 合格: 24/30以上、事実性4以上、関連性4以上、安全性5
- URL、内部ラベル、未確認X、記事にない数字、属性攻撃、犯罪断定、
  再審への無関係な財源論などを自動失格
- rule-only: 112件実行、期待結果一致112/112
- sample: 10件実行、期待結果一致10/10
- full: `--confirm-full`がない限り拒否
- API評価は独立予算 `$0.25`。予算不足時はrule-only

## 9. 人間評価・手動キュー

- 週間サンプル: 公開10、拒否5、生成後未投稿5
- 人間評価CSV/JSON取込: `import-human-review`
- 引用候補: 1回3、1日5
- 返信候補: 1回5、1日10
- 生成時刻: 12:20、20:20
- `engagement-brief`で上位候補、期限切れ、未処理、対応済み、推奨時間を出力
- `posted_manually`状態を保存可能
- Xへの引用・返信送信処理、ブラウザ自動操作は追加していない

## 10. 転換・学習

- フォロワースナップショット: 00:05、23:55
- 投稿単位のフォロー転換は時間窓推定と明記
- `conversion_events`へnote、YouTube、ニュースレター、購入等をCSV取込可能
- 拡散・信頼・会話・転換を別スコアで保存
- `viral_winner`、`trust_winner`、`conversation_winner`、
  `conversion_winner`へ用途別分類
- 怒り、個人攻撃、党派性、訂正、削除、高claim risk、
  低信頼・低転換の煽り投稿を成功例から除外
- 実験枠: 20%、1日最大2件
- prompt version: `x-growth-quality-v2`

## 11. SQLiteマイグレーション

追加テーブル:

- `follower_snapshots`
- `conversion_events`
- `quality_eval_runs`
- `human_reviews`
- `post_quality_dimensions`

`generated_posts`と`daily_reviews`へ品質・4軸・winner・prompt比較列を追加。
マイグレーションは再実行可能で、既存67投稿、レビュー、API使用履歴を維持した。

## 12. テスト・dry-run

- 既存テスト: 158/158成功
- 新規テスト: 48/48成功
- 合計: 206/206成功
- Python compileall: 成功
- PowerShell 5.1 parser: 全17スクリプト成功
- `force` dry-run: `POST_ENABLED=false`、ニュース0件で安全にskip
- 日次レビュー: ネットワーク制限時に保存済み指標へフォールバック
- engagement queue/brief: 成功、X writes 0
- Evals rule-only/sample: 成功
- quality-dashboard/budget-status/cost-forecast: 成功
- dry-run開始後の `post_create`: 0件

## 13. 費用

現在:

- OpenAI: `$0.6704`
- xAI: `$1.8926`
- X API: `$0.2730`
- 合計: `$2.8361`

既存7日傾向の未制限予測:

- OpenAI: `$1.0252`
- xAI: `$3.7853`
- X API: `$0.5460`
- 合計: `$5.3565`、約884円

改修後のxAI `$2.00`上限を適用した予測:

- 合計: 約`$3.5712`、約589円

xAIは既に月額上限に近く、予約費を含む予算ガードにより現在は実質停止状態。
RSS・公式監視は継続する。

## 14. 品質比較

- 改修前prompt `v1`の保存済み生成品質平均: 約7.25
- 改修後prompt `x-growth-quality-v2`: 本番投稿を実施していないため実績比較は未確定
- ルールEvals: 112/112件で期待結果一致

本番投稿なしであるため、v2のインプレッション・信頼・転換の優劣は今後の実績で評価する。

## 15. 残存リスク

- xAI既存実績1件の実費 `$1.892638`、tool call数0は異常値の可能性があり、
  xAI請求画面との照合が必要
- 実行環境のネットワーク制限により、dry-runではRSS/X/OpenAIの実通信を検証していない
- `daily_review_state.json`は管理者所有。非昇格dry-runでは
  `daily_review_state.local.json`へ安全にフォールバックする
- v2は本番投稿前のため、フォロー転換・品質の実績比較データがまだない
- 作業開始前からGit作業ツリーに多数の未コミット変更が存在する

## 16. 本番起動前の人間確認

1. xAIの請求画面で `$1.892638` の実費を確認
2. `.env`のモデル名とAPIキーを確認
3. `quality-dashboard`とEvals結果を確認
4. タスクの実行ユーザーと多重起動がないことを確認
5. 最初の1投稿をX上で目視確認

## 17. 本番再起動

```powershell
Start-ScheduledTask -TaskName "PoliticsNarrativeBot"
Get-ScheduledTask -TaskName "PoliticsNarrativeBot" |
  Format-List TaskName,State
```

本作業では再起動していない。最終状態はタスク`Ready`、政治Pythonデーモン0件。

## 18. ロールバック

本番タスクを停止した状態で、現在のリポジトリを別名へ退避し、次のバックアップを
元のパスへ戻す。

```powershell
Stop-ScheduledTask -TaskName "PoliticsNarrativeBot" -ErrorAction SilentlyContinue
Rename-Item "D:\SNS Bot\politics-narrative" "politics-narrative-failed-20260724"
Copy-Item "D:\SNS Bot\politics-narrative\backups\politics-narrative-backup-20260724-010132" `
  "D:\SNS Bot\politics-narrative" -Recurse
```

ロールバック後も人間確認なしに本番タスクを再起動しない。

