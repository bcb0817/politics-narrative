# 無料note原稿・Discord編集フロー

## 概要

この機能は、政治・制度解説の無料note原稿を週1〜2本生成し、ローカルMarkdownを正本として保存します。Discordは通知と人間確認だけに使います。

次の操作は行いません。

- noteへの自動投稿
- noteの非公式API利用
- ブラウザ、Playwright、Seleniumによる公開操作
- 無料note処理からのX投稿
- Discord以外への外部送信

## 生成フロー

1. 直近7日間の確認済みニュースと未使用の制度解説テーマを取得
2. 重要度、読者価値、出典、X実績、エバーグリーン性、多様性で選定
3. 公式一次資料を2件確認
4. ISBN確認済み書籍カタログから、記事種別・タイトルに合う関連書籍を2冊自動選定
5. Terraで1回生成し、失敗時はLunaへ切り替え
6. 文字数、構成、4リンク、出典、数値、内部ラベル、安全性を審査
7. 不合格時は最大1回だけ再生成
8. 1280×670 px（1.91:1）の見出し画像をローカル生成
9. 原稿・画像を含む5ファイルをローカル保存
10. 合格原稿だけDiscordへ結果概要と4ファイルを通知
11. 人が確認し、CLIで状態を更新
12. 人がnoteへ手動公開し、公開URLをCLIで記録

## 保存先

```text
outputs/note/
├─ drafts/YYYY/YYYY-MM-DD_slug/
│  ├─ article.md
│  ├─ cover.png
│  ├─ metadata.json
│  ├─ sources.md
│  └─ review.md
├─ approved/
├─ published/
├─ failed/
└─ note_state.json
```

SQLite障害時は`note_state.json`へフォールバックします。

記事末尾のリンク構成は次のとおりです。

- 一次資料：正確に2件
- 関連書籍：正確に2件
- 関連書籍リンク：Amazon.co.jpのISBN検索
- 書籍選定：`config/free_note_related_books.json`から記事種別・タイトルの一致度で自動選定
- Amazonアフィリエイトタグ：標準では付与しない

## CLI

```powershell
.\.venv\Scripts\python.exe local_bot.py generate-free-note
.\.venv\Scripts\python.exe local_bot.py generate-free-note --type weekly_top5
.\.venv\Scripts\python.exe local_bot.py generate-free-note --topic "閣議決定と法律成立の違い"
.\.venv\Scripts\python.exe local_bot.py generate-free-note --dry-run
.\.venv\Scripts\python.exe local_bot.py note-drafts
.\.venv\Scripts\python.exe local_bot.py note-pipeline-status
.\.venv\Scripts\python.exe local_bot.py note-discord-send --content-id note-YYYYMMDD-001
.\.venv\Scripts\python.exe local_bot.py note-generate-cover --content-id note-YYYYMMDD-001
.\.venv\Scripts\python.exe local_bot.py note-status --content-id note-YYYYMMDD-001 --status reviewing
.\.venv\Scripts\python.exe local_bot.py note-status --content-id note-YYYYMMDD-001 --status approved
.\.venv\Scripts\python.exe local_bot.py note-status --content-id note-YYYYMMDD-001 --status revision_required
.\.venv\Scripts\python.exe local_bot.py note-mark-published --content-id note-YYYYMMDD-001 --url "https://note.com/..."
```

## Discord

実Webhookは追跡対象外の`.env`だけに保存します。

```dotenv
DISCORD_NOTE_ENABLED=true
DISCORD_NOTE_WEBHOOK_URL=
DISCORD_NOTE_CHANNEL_NAME=note-drafts
DISCORD_NOTE_MENTION=
```

現在の実装は、準備済みの`NOTE_DRAFT_DISCORD_WEBHOOK_URL`も後方互換で利用できます。Webhookが空または送信に失敗しても、原稿生成とローカル保存は成功扱いです。Discordには`cover.png`、`article.md`、`sources.md`、`review.md`を添付し、添付失敗時は概要だけを再送します。

## Windowsタスク

登録は自動では行いません。人が確認後、次を実行します。

```powershell
Set-Location "D:\SNS Bot\politics-narrative"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\production\register_free_note_tasks.ps1
```

登録されるタスクは次の2つです。

- `PoliticsNarrativeFreeNoteWed`：水曜20:30
- `PoliticsNarrativeFreeNoteSun`：日曜20:30

週1本にする場合は`.env`の`FREE_NOTE_POSTS_PER_WEEK=1`に変更し、日曜タスクだけを使います。`StartWhenAvailable`により、PC停止中に時刻を過ぎた場合は次回利用可能時に回収されます。`IgnoreNew`と週単位の生成履歴で二重生成を防ぎます。

## Bot本体の再起動

今回の実装完了時点では再起動していません。人間が必要性を確認してから実行してください。

```powershell
Stop-ScheduledTask -TaskName "PoliticsNarrativeBot" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "PoliticsNarrativeBot"
```

対象タスクがない場合、Pythonプロセスを一括停止しないでください。

## ロールバック

1. 無料noteタスクを登録済みなら停止・登録解除
2. バックアップから変更ファイルを戻す
3. `.env`の`FREE_NOTE_ENABLED=false`で機能を無効化
4. `note_drafts`と`note_generation_runs`は履歴保全のため削除しない

バックアップ：

```text
D:\SNS Bot\politics-narrative\backups\politics-narrative-backup-20260725-044215
```
# Amazonアソシエイト付き無料note運用 📚

無料noteのAmazon関連書籍は初期値`manual`で処理します。ドラフト生成後は
`amazon-links-status`で待ち件数を確認し、正規に作成したリンクを
`amazon-link-set`で登録してください。リンク未登録または開示文不足の原稿は
承認ゲートで停止します。noteへの公開は引き続き手動です。

詳細は[`docs/AMAZON_ASSOCIATE_NOTE_WORKFLOW.md`](docs/AMAZON_ASSOCIATE_NOTE_WORKFLOW.md)
を参照してください。
