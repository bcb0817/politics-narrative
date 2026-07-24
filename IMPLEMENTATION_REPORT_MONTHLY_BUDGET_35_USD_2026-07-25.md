# 月額API予算35ドル化 実装報告 💰

## 1. バックアップ先

- リポジトリ全体:
  `D:\SNS Bot\politics-narrative-backup-20260725-041802`
- `.env`:
  `D:\SNS Bot\politics-narrative\archive\config_backups\.env.20260725-042115.backup`

## 2. Bot停止結果 ⚠️

`PoliticsNarrativeBot` の停止は実行環境の安全審査で拒否された。
迂回停止やPythonプロセスの一括終了は実施していない。
最終確認時点でタスクは `Running`、日次レビュータスクは `Ready`。
本作業ではタスクを再起動していない。

## 3. 変更した主なファイル

- `.env`
- `.env.example`
- `README.md`
- `local_bot.py`
- `src/api_budget.py`
- `src/model_router.py`
- `src/news.py`
- `src/xai_radar.py`
- `src/post.py`
- `src/content_pipeline.py`
- `src/quality_evals.py`
- `src/metrics_db.py`
- `tests/test_hourly_budget_policy.py`
- `tests/test_xai_engagement_style.py`

## 4. 新規作成ファイル

- `config/api_budget.json`
- `AUDIT_MONTHLY_BUDGET_35_USD.md`
- `reports/budget_changes/20260725-042115.json`
- `tests/test_monthly_budget_35.py`
- `IMPLEMENTATION_REPORT_MONTHLY_BUDGET_35_USD_2026-07-25.md`

## 5. `.env`変更項目

- `OPENAI_MONTHLY_BUDGET_USD=15.0`
- `XAI_MONTHLY_BUDGET_USD=4.0`
- `X_MONTHLY_BUDGET_USD=16.0`
- `TOTAL_MONTHLY_API_BUDGET_USD=35.0`
- `OPENAI_BUDGET_RESERVE_USD=1.00`
- `XAI_BUDGET_RESERVE_USD=0.25`
- `X_BUDGET_RESERVE_USD=0.75`
- `TOTAL_BUDGET_RESERVE_USD=2.00`
- `MONTHLY_BUDGET_JPY=5775`（互換表示用）
- `BUDGET_USD_JPY_RATE=165`
- `BUDGET_WARNING_RATIO=0.85`
- `BUDGET_RESTRICT_RATIO=0.93`
- `BUDGET_HARD_STOP_RATIO=1.00`
- OpenAI用途別予算を15ドル構成へ更新

APIキー・X認証情報・Discord Webhookなど、対象外の秘密情報は保持した。
対象予算キーはすべて重複なしで1件ずつ存在する。

## 6. 変更前後の予算

| Provider | 変更前 | 変更後 |
|---|---:|---:|
| OpenAI | $8.00 | $15.00 |
| xAI | $3.00 | $4.00 |
| X API | $12.00 | $16.00 |
| Total | $23.00 | $35.00 |

変更前の実環境値を履歴へ保存した。過去の利用イベントは書き換えていない。

## 7. reserveと実効利用可能額

- OpenAI reserve: `$1.00`
- xAI reserve: `$0.25`
- X reserve: `$0.75`
- Total reserve: `$2.00`
- 実効利用可能総額: `$35.00 - $2.00 = $33.00`

reserveは35ドルへ追加される別枠ではなく、35ドル内の保留額。
重大速報でも総上限・reserveを超える新規有料処理は許可しない。

## 8. 円換算

正本はUSD予算であり、円表示は次の式で動的計算する。

`TOTAL_MONTHLY_API_BUDGET_USD × BUDGET_USD_JPY_RATE`

現在は `35 × 165 = 5,775円`。
固定5,000円は判定の正本として使用しない。

## 9. 警告・制限・停止

- 85%: 警告、予測再計算
- 93%以降:
  1. コンテンツパイプライン停止
  2. 品質EvalsのAPI評価停止・ローカル化
  3. 週次LLMレビュー停止
  4. 日次LLMレビュー停止・ローカル化
  5. xAI探索を低変動日の2回相当へ縮小
  6. nano追加分類停止
  7. 通常投稿上限を8件から6件へ縮小
- 100%: 重大速報を含む新規有料API処理を停止

RSS・公式情報の監視、既存キャッシュ、ローカル集計は継続する。
プロセスを予算到達だけでクラッシュさせない。

## 10. xAI台帳検証

結果:

- request ID: PASS
- ticks換算: PASS
- 重複集計: PASS
- キャッシュ課金: PASS
- 月境界: PASS
- 実費対推定: PASS
- tool-call件数: FAIL

最終値は `XAI_COST_LEDGER_VERIFIED=false`。
設定予算は4ドルだが、実効上限は2ドルのまま。
推測でtrueには変更していない。

## 11. OpenAI用途別予算

- 通常投稿: `$9.00`
- 重要投稿: `$1.50`
- classifier: `$0.75`
- 日次レビュー: `$1.00`
- 週次レビュー: `$0.75`
- Quality Evals: `$0.50`
- コンテンツパイプライン: `$0.50`
- reserve: `$1.00`
- 合計: `$15.00`

通常投稿と重要投稿の使用額はメタデータで分離して集計する。

## 12. X API用途

用途は、URLなしテキスト投稿、自分の投稿指標、フォロワースナップショット、
必要な自分宛メンション取得、および明示的にNative X Searchを選択した場合に限定。
現在は `X_TOPIC_DISCOVERY_PROVIDER=xai`、
`X_NATIVE_SEARCH_ENABLED=false` のため、有料話題探索の二重実行はない。
予算増額を理由に投稿数・指標取得回数・自動対人操作は増やしていない。

## 13. 旧予算設定の調査

固定4,500円・4,800円・5,000円のコード判定を廃止し、
比率ベースへ置換した。
旧予算へ戻す専用PowerShellスクリプトは検出されなかったため、
隔離対象はなかった。
過去の監査・実装報告は履歴資料として変更していない。

## 14. SQLite・JSON履歴

- `budget_change_events` テーブルを追加
- 変更イベント1件を保存
- 同じイベントの再保存は重複しない
- JSON履歴も `reports/budget_changes/` に保存
- 投稿68件、レビュー、OpenAI/xAI/X利用履歴は維持

## 15. `budget-status`結果

- OpenAI: `$0.8185 / $15.00`
- xAI: `$1.8926 / effective $2.00`
- X API: `$0.3250 / $16.00`
- 合計実費: `$3.0361`
- 実効利用可能額: `$33.00`
- 現在stage: `normal`
- 円表示予算: `5,775円`

## 16. `cost-forecast`結果

- OpenAI月末予測: `$1.2494`
- xAI月末予測: `$2.0000`（未検証上限を反映）
- X API月末予測: `$0.6036`
- 合計月末予測: `$3.8530`
- 円換算月末予測: 約636円
- 85%・93%・100%到達予測: 今月中はなし

## 17. テスト・構文検査

- Python compileall: PASS
- PowerShell 5.1 parser: production配下19ファイルすべてPASS
- unittest: 310件すべてPASS
- 新規予算テスト: 33件
- `.env`対象キー重複検査: PASS

## 18. dry-run

実行時環境変数で `POST_ENABLED=false`、`FORCE_POST=true`、
xAI・Native X Search・Discord run logを無効化して実行。
終了コード0、候補0件でskip。

- X投稿: 0
- 返信: 0
- 引用: 0
- リポスト: 0
- いいね: 0
- フォロー: 0

## 19. 本番起動前の確認事項

- 稼働中プロセスは起動時に読み込んだ旧予算を保持している可能性がある。
- 新予算を確実に読み込ませるには、明示承認後にタスク停止を確認し、
  人間の確認後に1回だけ起動する必要がある。
- xAI台帳は未検証のため、実効2ドルを維持する。
- 本作業では再起動していない。

## 20. 本番再起動コマンド

人間が最終確認した後だけ実行する。

```powershell
Start-ScheduledTask -TaskName "PoliticsNarrativeBot"
```

既にRunningの場合は先に停止状態を確認し、二重起動させない。

## 21. ロールバック

1. `PoliticsNarrativeBot` を停止する。
2. `.env`だけ戻す場合は
   `archive\config_backups\.env.20260725-042115.backup` を `.env` へ復元する。
3. 全体を戻す場合は
   `D:\SNS Bot\politics-narrative-backup-20260725-041802` を使用する。
4. `budget_change_events` とJSON履歴は監査証跡として残す。
5. 構文検査・テスト・`budget-status`を確認する。
6. 人間の確認後にだけタスクを1回起動する。

## 22. 残存リスク

- xAI過去台帳のtool-call件数不整合が未解消。
- 本番停止が安全審査で拒否されたため、現在のRunningプロセスへ新設定が
  反映されたとは保証できない。
- API事業者の請求画面は引き続き最終的な実請求の正本。
