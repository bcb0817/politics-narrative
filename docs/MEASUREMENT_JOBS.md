# 投稿計測ジョブ

`measurement-cycle` は、公開投稿ごとに投稿時・15分・1時間・6時間・
24時間・72時間のフォロワー計測計画を作り、X投稿指標と返信を
読み取り専用で収集するためのジョブです。SNSへの書き込みは行いません。

状態確認とdry-run（外部APIリクエストなし）:

```powershell
.\.venv\Scripts\python.exe local_bot.py measurement-status
.\.venv\Scripts\python.exe local_bot.py measurement-reconcile
.\production\run_measurement_cycle.ps1
.\production\register_measurement_task.ps1
```

`working_cached` はDBに過去データがあるだけで、現在のAPI疎通を保証しません。
認証値が存在しても未疎通なら `configured_not_verified`、不足時は
`missing_credentials` と表示します。ThreadsについてもInsights用scopeだけでは
keyword search・返信・メンション取得はできないため、各機能のscopeを個別に
確認してください。空テーブルはAPIの空結果ではなく「実データ未確認」です。

フォロワー計測は、実際の取得日時が予定時刻の前後
`MEASUREMENT_FOLLOWER_TOLERANCE_MINUTES`（既定10分、1〜180分）以内にある場合
だけ紐付けます。状態表示の `eligible_now` は現在取得可能な窓、
`overdue_unrecoverable` は許容時間を過ぎて復元できない窓です。明示実行時、
後者は新しいAPI取得値を流用せず `missed` として記録します。

計画だけを安全に整合する場合は、まず既定のdry-runで作成予定数と
`missed`予定数を確認します。適用してもSQLite以外にはアクセスせず、
投稿指標・フォロワー・返信APIは一切呼びません。

```powershell
.\.venv\Scripts\python.exe local_bot.py measurement-reconcile
.\.venv\Scripts\python.exe local_bot.py measurement-reconcile --apply
```

人間が予算・権限を確認した後の読み取り実行:

```powershell
.\production\run_measurement_cycle.ps1 -Execute
```

タスク登録は既定でWhatIfです。明示的な承認後のみ次を実行します。

```powershell
.\production\register_measurement_task.ps1 -Apply
```
