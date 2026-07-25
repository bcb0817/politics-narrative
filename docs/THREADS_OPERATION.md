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
- callback停止: 公開トンネルを停止後、`run_threads_oauth_server.ps1`のプロセスを停止します。
- callback自動起動登録: `.\production\register_threads_oauth_task.ps1`
- callbackタスク確認: `.\production\threads_oauth_status.ps1`
- 自動投稿開始: `.\production\enable_threads_automation.ps1`
- 自動投稿停止: `.\production\threads_stop.ps1`
