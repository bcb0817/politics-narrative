# 無料note・Discord実装計画 📝

## 現状調査

- 週次レビューはMarkdown、JSON、SQLite `weekly_reviews`へ保存される。
- ニュース候補は`news_candidates`、公開X投稿は`published_posts`、
  指標は`post_metrics`に保存される。
- ニュース候補には`source_url`、`source_name`、確認状態が保存される。
- 既存`content_pipeline.py`はShortsと短いnoteプレビューを生成するが、
  完成原稿、出典台帳、承認状態、Discord添付通知は持たない。
- Discordにはnote draft専用Webhookと接続テストが準備済み。
- OpenAI費用はSQLite予約方式で、用途別上限を適用できる。
- WindowsタスクはPowerShell 5.1スクリプトで登録し、
  `MultipleInstances IgnoreNew`を使用する。
- 過去の完成済み無料note原稿は存在しない。

## 実装方針

1. `src/free_note.py`へ保存・選定・生成・品質・状態管理を集約する。
2. 原稿正本を`outputs/note/`配下の4ファイルとして保存する。
3. 一次資料は政府・国会・法令・裁判所等の公式URLだけを認定する。
4. SQLiteへ`note_drafts`と`note_generation_runs`を加算移行する。
5. SQLite障害時も記事フォルダとJSON状態へ保存する。
6. OpenAI費用を`free_note_generation`として月1.50ドルに分離する。
7. Discordは概要と3ファイルを専用Webhookへ送り、失敗を非致命にする。
8. 承認・公開URL・一覧・再送・状態確認CLIを追加する。
9. 水曜・日曜の独立タスク登録スクリプトを追加するが、自動登録しない。
10. dry-runでは外部API・Discord・X・note公開を一切行わない。
