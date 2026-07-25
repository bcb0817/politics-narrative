# 動画クロス投稿 Phase A 運用手順

## 現在の安全状態

- 外部自動公開は `CROSSPOST_AUTO_PUBLISH_ENABLED=false`。
- Instagramは `CROSSPOST_INSTAGRAM_ENABLED=false`。
- Phase Aでは公開HTTPSストレージへアップロードしない。
- Windowsタスクは自動登録・自動起動しない。
- `crosspost-publish` は全体スイッチ、媒体スイッチ、`--confirm` の三条件が必要。
- Phase B承認前は、条件を満たしてもオーケストレーターが実投稿を拒否する。

## Phase A dry-run

```powershell
$env:POST_ENABLED="false"
$env:CROSSPOST_ENABLED="true"
$env:CROSSPOST_AUTO_PUBLISH_ENABLED="false"
$env:YOUTUBE_UPLOAD_ENABLED="false"
$env:YOUTUBE_AUTO_PUBLISH_ENABLED="false"
$env:X_POST_ENABLED="false"
$env:THREADS_POST_ENABLED="false"
$env:INSTAGRAM_AUTO_PUBLISH_ENABLED="false"

.\.venv\Scripts\python.exe local_bot.py crosspost-generate-copy --dry-run
.\.venv\Scripts\python.exe local_bot.py crosspost-render-renditions --dry-run
.\.venv\Scripts\python.exe local_bot.py crosspost-validate --dry-run
.\.venv\Scripts\python.exe local_bot.py crosspost-prepare --dry-run
.\.venv\Scripts\python.exe local_bot.py crosspost-publish --dry-run
.\.venv\Scripts\python.exe local_bot.py crosspost-report --dry-run
```

## Instagram準備

Instagram Login方式ではBusinessまたはCreatorアカウントが必要。OAuthには
`instagram_business_basic` と `instagram_business_content_publish` を使用する。
個人アカウントは投稿対象にしない。

```powershell
.\.venv\Scripts\python.exe local_bot.py instagram-auth-url
.\.venv\Scripts\python.exe local_bot.py instagram-token-status
.\.venv\Scripts\python.exe local_bot.py instagram-profile --dry-run
```

## YouTube準備

Google CloudでYouTube Data API v3を有効化し、OAuth同意画面と
`youtube.upload` スコープを設定する。未監査APIプロジェクトは
`YOUTUBE_PRIVACY_STATUS=private` のまま運用する。

## X準備

既存OAuth 1.0aユーザー認証に動画アップロード権限があることを確認する。
実装は公式v2の `INIT → APPEND → FINALIZE → STATUS` と
`POST /2/tweets` を使用する。

## Threads準備

既存OAuthと `threads_content_publish` を再利用する。VIDEOコンテナ作成、
処理状態確認、`threads_publish` の順で実行する。

## 公開HTTPSメディア

`MEDIA_PUBLICATION_PROVIDER` は初期状態で未設定。Funnel方式を使う場合も、
動画専用ポートで1ファイルだけをトークン付きURLから配信する。OAuth用Funnelと
動画配信用Funnelは分離する。

## 緊急停止

```powershell
.\.venv\Scripts\python.exe local_bot.py crosspost-emergency-stop
.\production\crosspost_stop.ps1
```

成功済みの外部投稿は削除しない。停止マーカーを解除する場合は、原因を確認後に
`outputs/crosspost/.crosspost-emergency-stop` を人間が明示的に削除する。

## Windowsタスク

今回のPhase Aでは登録しない。構成確認だけ行う場合：

```powershell
.\production\register_crosspost_tasks.ps1 -WhatIf
```

Phase B以降も、人間の承認なしに登録・開始しない。
