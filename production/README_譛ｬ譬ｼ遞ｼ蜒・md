# 本格稼働手順（Windows）

配置先:

`D:\SNS Bot\politics-narrative`

## 1. インストール・安全テスト

PowerShellを通常権限で開きます。

```powershell
cd "D:\SNS Bot\politics-narrative"
powershell -ExecutionPolicy Bypass -File .\production\install.ps1
```

`.env` が開いた場合、OpenAIとXの5個のキーを設定します。
本番設定の推奨値:

```env
POST_ENABLED=true
THREAD_ENABLED=true
QUALITY_GATE_ENABLED=true
CATCH_UP_HOURS=3
MAX_NEWS_AGE_HOURS=12
SLOT_INTERVAL_MINUTES=45
ACTIVE_HOURS=7-8,12,18-23
```

## 2. 自動起動して本番開始

```powershell
powershell -ExecutionPolicy Bypass -File .\production\go_live.ps1
```

Windowsログオン時に自動起動します。異常終了した場合は監視スクリプトが60秒後に再起動します。

## 3. 状態確認

```powershell
powershell -ExecutionPolicy Bypass -File .\production\status.ps1
```

## 4. 停止・再開

```powershell
powershell -ExecutionPolicy Bypass -File .\production\stop.ps1
powershell -ExecutionPolicy Bypass -File .\production\start.ps1
```

## 5. 自動起動登録の削除

```powershell
powershell -ExecutionPolicy Bypass -File .\production\uninstall_task.ps1
```

## 注意

- PCがスリープ中は投稿できません。電源設定で稼働時間帯のスリープを無効化してください。
- 自動起動は「Windowsログオン時」です。再起動後、ユーザーがログオンすると稼働します。
- Xアプリには投稿権限（Read and write）と、その権限で再生成したAccess Token/Secretが必要です。
- `.env` は公開・共有・Gitコミットしないでください。
