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

```powershell
.\.venv\Scripts\python.exe local_bot.py threads-auth-url
.\.venv\Scripts\python.exe local_bot.py threads-exchange-code --code "<authorization-code>"
.\.venv\Scripts\python.exe local_bot.py threads-token-status
.\.venv\Scripts\python.exe local_bot.py threads-profile
```

認可コード交換後、長期トークン、ユーザーID、ユーザー名、有効期限は
Git管理対象外の `.env` に原子的に保存されます。トークンやApp Secretは
ログ、SQLite、JSONフォールバック、GitHubへ保存しません。

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
