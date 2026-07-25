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
