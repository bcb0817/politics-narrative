# 熊本地震 朝夕定点更新

## 現在の段階

Phase Aです。公式情報の取得、スナップショット、差分、画像、X・Threads候補、品質検査、Discord通知までを行います。外部投稿は行いません。

incident_id は `kumamoto-earthquake-20260728` に固定し、重複作成しません。

## 時刻

- 朝: 07:00締切、07:30投稿候補時刻
- 夜: 19:00締切、19:30投稿候補時刻
- タイムゾーン: Asia/Tokyo

Windowsタスクは07:00と19:00にPhase A一括処理を開始し、投稿時刻まで待機しません。候補をSQLiteと出力フォルダへ保存します。

## 公式データ

公式ページの到達性と更新ヘッダーは `config/kumamoto_disaster_sources.json` から確認します。Web本文から被害数を推測抽出しません。

構造化した公式値を本番候補へ使う場合は、次へレビュー済みJSONを配置します。

`data/disaster_inputs/<incident_id>_YYYY-MM-DD_morning.json`

`data/disaster_inputs/<incident_id>_YYYY-MM-DD_evening.json`

公式値がない場合はnullとし、レビュー済み入力がなければ品質不足として投稿を見送ります。異なる機関の数字は合算しません。

## dry-run

```powershell
$env:POST_ENABLED="false"
$env:X_POST_ENABLED="false"
$env:THREADS_POST_ENABLED="false"
$env:KUMAMOTO_DISASTER_AUTO_POST_ENABLED="false"

.\.venv\Scripts\python.exe local_bot.py disaster-update-full-cycle `
  --incident-id kumamoto-earthquake-20260728 `
  --snapshot-type morning `
  --dry-run
```

## Windowsタスク

登録スクリプトは作成済みですが、自動実行していません。

```powershell
.\production\register_kumamoto_disaster_update_tasks.ps1 -WhatIf
.\production\register_kumamoto_disaster_update_tasks.ps1
.\production\kumamoto_disaster_updates_start.ps1
.\production\kumamoto_disaster_updates_status.ps1
```

## Phase B

Phase Bへは自動移行しません。候補と画像を人間が確認し、X・Threads各1件を既存の手動承認投稿経路で公開します。`KUMAMOTO_DISASTER_AUTO_POST_ENABLED=false` は維持します。

## Phase C

Phase Cは実装済みです。確認済み公式情報、品質合格、重大差分、媒体別上限、
緊急停止の全条件を満たした後に限り有効化します。訂正自動投稿も独立スイッチで
管理し、公式訂正、鮮度、品質、重複防止をすべて通過した場合だけ実行します。

## 緊急停止

タスクが未登録の場合:

```powershell
$env:KUMAMOTO_DISASTER_UPDATES_ENABLED="false"
```

登録後:

```powershell
.\production\kumamoto_disaster_updates_stop.ps1
```

## Bot再起動

本機の通常Botを再起動する場合だけ、明示確認後に次を使用します。

```powershell
.\production\stop.ps1
.\production\start.ps1
```

災害更新のPhase A実装だけでは通常Bot再起動は不要です。

## ロールバック

通常Botと別パイプラインのため、まず環境変数をfalseにして停止します。SQLiteと出力を削除せず保持したうえで、今回変更したファイルだけをGitの通常のrevertで戻します。`git reset --hard` と `git clean` は使用しません。

## Phase B：人間承認投稿

Phase Bは実装済みですが、初期状態では無効です。候補を確認後、次の順に
承認と投稿を別操作で行います。

```powershell
.\.venv\Scripts\python.exe local_bot.py disaster-update-approve `
  --snapshot-id <snapshot_id> --platform x --decision approved

.\.venv\Scripts\python.exe local_bot.py disaster-update-publish `
  --snapshot-id <snapshot_id> --platform x --confirm
```

Threadsも`--platform threads`で同様です。投稿には災害専用スイッチに加えて、
Xでは`POST_ENABLED`と`X_POST_ENABLED`、Threadsでは
`THREADS_POST_ENABLED`が必要です。承認操作だけでは外部投稿しません。

## Phase C：確認済み情報限定の自動投稿

Phase Cの経路は実装済みですが、初期状態では無効です。次のすべてを明示的に
有効化した場合だけ動作します。

```dotenv
KUMAMOTO_DISASTER_PHASE=C
KUMAMOTO_DISASTER_PUBLISH_ENABLED=true
KUMAMOTO_DISASTER_AUTO_POST_ENABLED=true
KUMAMOTO_DISASTER_AUTO_PUBLISH_VERIFIED_ONLY=true
```

媒体別スイッチと全体スイッチも必要です。公式情報、情報鮮度、品質、意味のある
差分のいずれかが不足する場合は投稿を見送ります。訂正自動投稿は
`KUMAMOTO_DISASTER_CORRECTION_AUTO_POST=true`の場合だけ有効です。
通常候補とは別の投稿レコードに保存され、二重投稿を防止します。

## Phase D：復旧期・終了処理

復旧期レポートと終了時パッケージはローカル下書きだけを生成します。

```powershell
.\.venv\Scripts\python.exe local_bot.py disaster-recovery-brief
.\.venv\Scripts\python.exe local_bot.py disaster-closure-package
```

終了時パッケージには、保存済み公式値だけを使う総括記事候補と、一般的な
防災確認事項を含みます。推計値、異なる機関の合算、外部投稿は行いません。

## 頻度変更の承認

推薦だけではモードもWindowsタスクも変更しません。人間が確認した後だけ、
次を実行します。

```powershell
.\production\set_kumamoto_disaster_frequency.ps1 `
  -Mode active_daily -ConfirmChange
```

`active_twice_daily`は朝夕、`active_daily`は夜のみです。
`recovery_periodic`は夜のタスクを3日ごとに変更し、通常のスナップショットに
加えて復旧報告候補を作成します。`closed`は朝夕タスクを停止します。
スクリプトはタスクを即時実行しません。
