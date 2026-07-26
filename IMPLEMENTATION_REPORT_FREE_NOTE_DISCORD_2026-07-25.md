# 無料note自動生成・Discord通知 実装報告

## 1. バックアップ

```text
D:\SNS Bot\politics-narrative\backups\politics-narrative-backup-20260725-044215
```

## 2. 既存コード調査

- 週次レビューはローカルJSONとSQLiteへ保存
- ニュース候補、公開X投稿、投稿指標、レビュー、API費用はSQLiteに集約
- 既存content pipelineには短いnoteプレビューがあったが、完成記事、承認状態、4ファイル保存、専用Discord添付通知は未実装
- Discord一般通知とnote下書き用Webhookの準備実装を確認
- WindowsタスクはPowerShell登録スクリプトで分離管理
- `prompt_version`、投稿タイプ、評価、費用履歴は既存DBに保存
- 完成形の無料note原稿履歴はなかった

詳細は`IMPLEMENTATION_PLAN_FREE_NOTE_DISCORD.md`を参照。

## 3. 主な変更ファイル

- `local_bot.py`
- `src/api_budget.py`
- `src/discord_notify.py`
- `src/free_note.py`
- `src/growth_analytics.py`
- `src/growth_tracking.py`
- `src/metrics_db.py`
- `src/report_ai.py`
- `.env`
- `.env.example`
- `README.md`

## 4. 主な新規ファイル

- `config/free_note_primary_sources.json`
- `production/run_free_note.ps1`
- `production/register_free_note_tasks.ps1`
- `tests/test_free_note.py`
- `OPERATIONS_FREE_NOTE_DISCORD.md`
- `IMPLEMENTATION_PLAN_FREE_NOTE_DISCORD.md`
- 本報告書

## 5. 生成フロー

テーマ選定 → 出典収集 → 1回生成 → 品質審査 → 最大1回再生成 → 4ファイル保存 → Discord通知 → 人間承認 → 人間によるnote公開。

note自動公開、ブラウザ自動化、無料note処理からのX投稿はない。

## 6. テーマ選定

直近7日の確認済み候補と未使用制度テーマを利用し、政治的重要度25%、読者価値25%、出典充足20%、X実績10%、エバーグリーン性15%、多様性5%で並べる。訂正対象、高怒り、低信頼、同一URL、同一topicは除外する。

## 7. 記事タイプ

- `weekly_top5`
- `legislative_process`
- `cabinet_decision_vs_law`
- `social_insurance_burden`
- `party_policy_comparison`
- `evergreen_institutional_explainer`
- `weekly_deep_dive`

## 8. 記事構成

タイトル、3行要約、導入、事実、制度背景、賛成側の最強論拠、反対側の最強論拠、久世ゆいの評価、今後見る点、一次資料・参考資料。

## 9. 保存構造

`outputs/note/drafts/YYYY/YYYY-MM-DD_slug/`を正本とし、`article.md`、`metadata.json`、`sources.md`、`review.md`を保存。状態に応じて`approved`、`published`、`failed`へ移動する。

## 10. metadata

仕様指定項目に加え、品質・安全スコア、警告・失敗理由、生成回数、生成モード、状態履歴、自動公開なし、X書込み0を保存する。

## 11. 出典

政府、国会、e-Gov等のHTTPS公式URLを一次資料として登録。X投稿やxAIだけを一次資料にしない。一次資料が2件未満なら生成をスキップする。

## 12. 公開前チェック

`review.md`に事実確認、編集品質、安全性、公開作業のチェックリストを生成する。

## 13. Discord通知

記事全文は本文に貼らず、タイトル、タイプ、文字数、読了時間、候補日、確認事項、状態、ローカル保存先を通知し、3つのMarkdownを添付する。

## 14. Discord未設定・障害時

ローカル保存を継続。添付失敗時は概要のみ再送。送信失敗はBot本体を停止させない。Webhookはログでマスクする。

## 15. 承認・公開状態

`draft`、`reviewing`、`approved`、`revision_required`、`published`、`rejected`をCLIで管理。状態履歴をmetadataへ残す。`published`は`https://note.com/` URL必須。

## 16. 追加CLI

- `generate-free-note`
- `note-drafts`
- `note-discord-send`
- `note-status`
- `note-mark-published`
- `note-pipeline-status`
- `free-note-due`

## 17. Windowsタスク

`production/register_free_note_tasks.ps1`を人が明示実行する。水曜・日曜20:30、非表示、作業ディレクトリ固定、`IgnoreNew`、`StartWhenAvailable`、ログ保存。今回は登録・開始していない。

## 18. OpenAIモデル

主モデル`gpt-5.6-terra`、フォールバック`gpt-5.6-luna`、reasoningはmedium、最大出力6,000 tokens。分類とテーマ選定はローカル中心。

## 19. 月間予算

無料noteの処理上限は月`$1.50`、1記事`$0.25`。既存OpenAIプロバイダ上限`$15`と総額`$35`の内側で制御し、X APIの予算イベントを生成しない。

## 20. 1記事推定費用

最大想定7,000入力・6,000出力では、Terra約`$0.1075`、Luna約`$0.0430`。いずれも`$0.25`未満。実費は実token数で確定する。

## 21. SQLite

加算型、再実行可能な`note_drafts`と`note_generation_runs`を追加。既存テーブル・履歴は削除していない。障害時は`outputs/note/note_state.json`へフォールバック。

## 22. 環境変数

`FREE_NOTE_*`、`OPENAI_MODEL_FREE_NOTE*`、`OPENAI_REASONING_EFFORT_FREE_NOTE`、`OPENAI_MAX_OUTPUT_TOKENS_FREE_NOTE`、`DISCORD_NOTE_*`を追加。`.env`は差分追記で、全面上書きしていない。実Webhookは追跡対象外の`.env`のみ。

## 23. 既存テスト

全回帰テスト成功。合計396件。

## 24. 新規テスト

無料note専用83件成功。保存、テーマ、品質、Discord、状態、予算、安全、回帰を検証。

## 25〜27. dry-run

- タイトル：`今週の政治ニュース5選`
- 文字数：3,184
- 品質：10.0
- 安全性：10.0
- 一次資料：5件
- API費用：`$0.00`
- 保存先：`outputs/note/drafts/2026/2026-07-25_今週の政治ニュース5選`

## 28. 外部送信・公開

dry-runでは`POST_ENABLED=false`、`DISCORD_NOTE_ENABLED=false`をプロセス環境に設定。X投稿0、Discord送信なし、note公開なしを結果とmetadataで確認。

## 29. 残存リスク

- 公式サイトのURL・構造変更時はレジストリ更新が必要
- 実生成記事は必ず人が事実・日付・制度名を再確認する
- Terra/Lunaの利用可否や価格が変わった場合はpricing設定を更新する
- タスク未登録のため、自動スケジュールはまだ開始していない
- GitHub更新は有効なGitHub CLI認証が必要

## 30. 本番有効化前の人間確認

- `.env`のモデル、予算、専用Webhookを確認
- dry-run原稿と一次資料を確認
- Discordチャンネルの閲覧権限を確認
- タスク登録後に`Get-ScheduledTask`で2タスクを確認

## 31. Discord Webhook

`.env`だけに`DISCORD_NOTE_WEBHOOK_URL`を設定する。既存の`NOTE_DRAFT_DISCORD_WEBHOOK_URL`も後方互換で利用可能。URLをGitへ追加しない。

## 32. 無料noteタスク登録

```powershell
Set-Location "D:\SNS Bot\politics-narrative"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\production\register_free_note_tasks.ps1
```

## 33. Bot本体の再起動

今回は未実施。必要時のみ人が次を実行する。

```powershell
Stop-ScheduledTask -TaskName "PoliticsNarrativeBot" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "PoliticsNarrativeBot"
```

## 34. ロールバック

`FREE_NOTE_ENABLED=false`で機能を停止し、必要な変更ファイルをバックアップから戻す。履歴保全のためSQLiteテーブルや生成原稿は削除しない。
