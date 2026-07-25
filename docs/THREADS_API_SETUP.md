# Meta公式Threads API セットアップ 🧵

## 安全な初期状態

初期導入はPhase A（プレビューのみ）です。

```dotenv
THREADS_ENABLED=true
THREADS_POST_ENABLED=false
```

この状態ではThreadsへの書き込みは発生しません。ブラウザ自動操作、Cookie、
非公式API、プロフィール変更、返信、引用、再投稿、いいね、フォローは使用しません。

## Metaアプリの準備

1. Meta for Developersでアプリを作成し、Threads APIを追加します。
2. OAuthリダイレクトURIを登録し、`.env` の
   `THREADS_APP_ID`、`THREADS_APP_SECRET`、`THREADS_REDIRECT_URI` を設定します。
3. 初期権限は次の3つだけにします。

```text
threads_basic
threads_content_publish
threads_manage_insights
```

## OAuth

OAuth開始前に、公開HTTPSベースURLを設定します。

```dotenv
THREADS_PUBLIC_BASE_URL=https://threads-bot.example.com
THREADS_REDIRECT_URI=https://threads-bot.example.com/threads/callback
THREADS_CALLBACK_TRUST_PROXY=true
```

`THREADS_REDIRECT_URI`はMeta管理画面のOAuth Redirect URLと完全一致させます。
HTTP、localhost、変動する一時トンネルURLは本番登録に使用しません。

```powershell
.\.venv\Scripts\python.exe local_bot.py threads-auth-url
.\.venv\Scripts\python.exe local_bot.py threads-exchange-code --code "<authorization-code>"
.\.venv\Scripts\python.exe local_bot.py threads-token-status
.\.venv\Scripts\python.exe local_bot.py threads-profile
```

認可コード交換後、長期トークン、ユーザーID、ユーザー名、有効期限は
Git管理対象外の `.env` に原子的に保存されます。トークンやApp Secretは
ログ、SQLite、JSONフォールバック、GitHubへ保存しません。

認証URLには暗号学的に生成した`state`が含まれます。平文stateはDBへ保存せず、
SHA-256ハッシュ、有効期限、一回限りの使用状態だけを保存します。

## 公開HTTPS callback

ローカルサーバーはWaitressで`127.0.0.1:8787`にだけ待ち受けます。

```powershell
.\production\run_threads_oauth_server.ps1
```

Windowsログオン時にcallbackサーバーを自動起動する場合は、次を一度実行します。

```powershell
.\production\register_threads_oauth_task.ps1
.\production\threads_oauth_status.ps1
```

登録直後にもタスクから起動する場合は
`.\production\register_threads_oauth_task.ps1 -Start` を使用します。

外部公開には、固定ホスト名を持つリバースプロキシまたは名前付きCloudflare
Tunnel等が別途必要です。TLS終端側から`X-Forwarded-Proto: https`を渡す場合だけ
`THREADS_CALLBACK_TRUST_PROXY=true`にします。プロキシのアクセスログでは
`/threads/callback`のクエリ文字列を記録しない設定にしてください。

公開URL設定後、次のコマンドでMeta登録用URLを確認できます。

```powershell
.\.venv\Scripts\python.exe local_bot.py threads-endpoints
```

公開するエンドポイントは次の3つです。

- `GET /threads/callback`
- `POST /threads/deauthorize`
- `POST /threads/data-deletion`

解除・削除要求はMetaの`signed_request`をApp SecretによるHMAC-SHA256で
検証します。署名不正の場合はトークンや履歴を変更しません。

## Phase Aの確認

```powershell
$env:THREADS_ENABLED="true"
$env:THREADS_POST_ENABLED="false"
.\.venv\Scripts\python.exe local_bot.py threads-generate --dry-run
.\.venv\Scripts\python.exe local_bot.py threads-status
.\.venv\Scripts\python.exe local_bot.py threads-drafts
Remove-Item Env:THREADS_ENABLED -ErrorAction SilentlyContinue
Remove-Item Env:THREADS_POST_ENABLED -ErrorAction SilentlyContinue
```

Phase Bへの移行は、OAuth、ドラフト品質、重複防止、予算、Insights収集を
確認した後、人間が `.env` の `THREADS_POST_ENABLED=true` を明示して行います。

## Windowsタスク

次のスクリプトは用意されますが、導入作業では登録・開始しません。

```powershell
.\production\register_threads_tasks.ps1
.\production\threads_start.ps1
.\production\threads_stop.ps1
.\production\threads_status.ps1
```
