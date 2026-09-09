# Threads API 運用手順 🛡️

## 通常確認

```powershell
.\.venv\Scripts\python.exe local_bot.py threads-status
.\.venv\Scripts\python.exe local_bot.py threads-token-status
.\.venv\Scripts\python.exe local_bot.py threads-drafts
.\.venv\Scripts\python.exe local_bot.py platform-comparison
```

投稿は通常2件、最大3件を想定し、08:30・20:30を標準、13:00を任意枠と
します。最低件数はノルマではなく、適格な一次情報がない場合は投稿しません。
X投稿から30分以上空け、同一トピックは8時間、投稿間隔は180分を守ります。

## 明示投稿

```powershell
.\.venv\Scripts\python.exe local_bot.py threads-publish --draft-id <id>
```

この操作にも `THREADS_POST_ENABLED=true` が必要です。投稿は公式APIの
コンテナ作成とpublishの2段階で行い、タイムアウト時は `ambiguous` として
保存します。不明な成功状態を自動再試行しません。

## トークン

有効期限7日前から更新対象です。

```powershell
.\.venv\Scripts\python.exe local_bot.py threads-refresh-token
```

更新失敗時はThreads投稿だけを無効化します。X Botには影響しません。

## Deauthorize・データ削除

Metaからの`POST /threads/deauthorize`は署名検証後、該当ユーザーの保存済み
Access Token、User ID、Username、有効期限を削除し、Threads投稿を停止します。

`POST /threads/data-deletion`は同じ署名検証に加え、該当ユーザーと投稿履歴の
紐付けを匿名化し、削除確認コードと確認URLを返します。確認コードとOAuth
stateはハッシュだけをSQLiteへ保存します。

App Secret、認証コード、アクセストークン、signed_requestはログへ出しません。
公開プロキシ側でもOAuth callbackのクエリ文字列をログから除外してください。

## Insightsと比較

1時間、24時間、72時間の未取得窓だけを収集します。views、likes、replies、
reposts、quotes、sharesを保存し、取得不能な指標は0ではなくNULLにします。
XとThreadsは指標定義が異なるため単純な勝者判定は行わず、同一
`source_content_id` の十分なサンプルがある場合だけ傾向を示します。

## 障害時

- 認証エラー: 投稿OFFを維持し、権限と期限を確認します。
- タイムアウト: `ambiguous` を確認し、盲目的に再投稿しません。
- SQLite障害: 投稿はfail closedとし、機密情報を除くJSONLへ事象を保存します。
- OpenAI予算不足: ローカルプレビューへ縮退し、Threads API投稿は勝手に行いません。
- 緊急停止: `THREADS_POST_ENABLED=false` にして `threads_stop.ps1` を実行します。

## 公式APIの取得・分析コマンド 🧵

追加権限を含む再認証URLは、ブラウザを自動操作せず次で表示します。

```powershell
.\.venv\Scripts\python.exe local_bot.py threads-permissions
.\.venv\Scripts\python.exe local_bot.py threads-auth-url --scope-profile full-analysis
```

キーワード検索だけを追加する場合は、不要な返信管理・削除権限を要求しない
最小権限プロファイルを使用します。

```powershell
.\.venv\Scripts\python.exe local_bot.py threads-auth-url --scope-profile keyword-search
```

読み取り・ローカル分析:

```powershell
.\.venv\Scripts\python.exe local_bot.py threads-profile-sync
.\.venv\Scripts\python.exe local_bot.py threads-sync-posts
.\.venv\Scripts\python.exe local_bot.py threads-sync-replies
.\.venv\Scripts\python.exe local_bot.py threads-sync-mentions
.\.venv\Scripts\python.exe local_bot.py threads-collect-post-insights
.\.venv\Scripts\python.exe local_bot.py threads-collect-account-insights
.\.venv\Scripts\python.exe local_bot.py threads-search --query "社会保険料" --search-type RECENT --hours 24
.\.venv\Scripts\python.exe local_bot.py threads-search --query "選挙" --search-mode TAG --search-type TOP
.\.venv\Scripts\python.exe local_bot.py threads-trends
.\.venv\Scripts\python.exe local_bot.py threads-quota-status
.\.venv\Scripts\python.exe local_bot.py threads-daily-report
.\.venv\Scripts\python.exe local_bot.py threads-weekly-report
.\.venv\Scripts\python.exe local_bot.py threads-x-comparison --days 30
```

`threads-full-sync` は取得と分析だけを実行し、投稿、返信、引用、リポスト、削除、返信管理を行いません。

## 人間承認が必須の操作 🔒

返信・引用・リポスト・削除・返信管理・各投稿形式は、対応する `THREADS_AUTO_*` が明示的に有効で、`THREADS_POST_ENABLED=true` で、CLIに `--confirm` がある場合だけ実行できます。

```powershell
.\.venv\Scripts\python.exe local_bot.py threads-reply-draft --reply-to-id <id>
.\.venv\Scripts\python.exe local_bot.py threads-reply-publish --draft-id <id> --confirm
.\.venv\Scripts\python.exe local_bot.py threads-quote-draft --post-id <id>
.\.venv\Scripts\python.exe local_bot.py threads-quote-publish --draft-id <id> --confirm
.\.venv\Scripts\python.exe local_bot.py threads-repost --post-id <id> --confirm
.\.venv\Scripts\python.exe local_bot.py threads-delete --post-id <owned-id> --reason "manual correction" --confirm
```

タイムアウトで結果が不明な書き込みは `ambiguous` として保存し、盲目的に再試行しません。

## 分析タスクスクリプト

次のスクリプトは作成済みですが、自動登録・開始はされません。

```powershell
.\production\register_threads_analytics_tasks.ps1 -WhatIf
.\production\register_threads_analytics_tasks.ps1
.\production\threads_analytics_start.ps1
.\production\threads_analytics_stop.ps1
.\production\threads_analytics_status.ps1
```

政治キーワードは `config\threads_political_keywords.yaml`、トレンド重みは `config\threads_trend_weights.json` で変更できます。Threads検索結果は一次資料として扱わず、既存の公式資料・RSS・報道と照合します。

## Discordへのリサーチ結果通知 🔎

`threads-search` の後に `threads-trends` を実行すると、新しい検索結果が
保存されている場合だけ、次の結果を1件にまとめてDiscordへ通知します。

- 検索語、検索回数、API取得件数、重複除外後の件数
- 代表的な公開投稿（最大3件）
- 相対トレンド上位5件と状態・スコア
- 公式資料・報道との照合を満たした話題数
- 標本分析でありThreads全体順位ではないという注意書き

```dotenv
THREADS_DISCORD_RESEARCH_ENABLED=true
DISCORD_NOTIFY_THREADS_RESEARCH=true
```

アクセストークン、ユーザー識別ハッシュ、生APIレスポンス、内部ログは
Discordへ送信しません。定期タスクの `search` モードでも同じ通知が行われます。
- callback停止: 公開トンネルを停止後、`run_threads_oauth_server.ps1`のプロセスを停止します。
- callback自動起動登録: `.\production\register_threads_oauth_task.ps1`
- callbackタスク確認: `.\production\threads_oauth_status.ps1`
- 自動投稿開始: `.\production\enable_threads_automation.ps1`
- 自動投稿停止: `.\production\threads_stop.ps1`
