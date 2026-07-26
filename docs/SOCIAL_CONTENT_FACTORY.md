# Social Content Factory 運用ガイド 🧩

## Phase Aの安全境界

- 現行X・Threads投稿数は変更しない。
- `.env`を自動更新しない。
- X、Threads、note、動画、Discordへの外部送信を行わない。
- Windowsタスクを自動登録・起動しない。
- SQLiteへ候補、在庫、レポート、測定予定だけを加算保存する。

## 基本フロー

```text
公式資料・確認済みニュース・SNS需要シグナル
  → topic cluster
  → content packet
  → claim / angle
  → X・Threads候補
  → 15m / 1h / 6h / 24h / 72h
  → Short / longform / X article / note候補
```

SNS由来情報の`verified_fact`は常にfalseとして扱い、公式資料または確認済みニュースと
一致するまで投稿事実には昇格させません。

## dry-run

```powershell
$env:POST_ENABLED="false"
$env:X_POST_ENABLED="false"
$env:THREADS_POST_ENABLED="false"
$env:CROSSPOST_AUTO_PUBLISH_ENABLED="false"
$env:YOUTUBE_UPLOAD_ENABLED="false"
$env:INSTAGRAM_AUTO_PUBLISH_ENABLED="false"

.\.venv\Scripts\python.exe local_bot.py config-audit
.\.venv\Scripts\python.exe local_bot.py growth-full-cycle --dry-run
.\.venv\Scripts\python.exe local_bot.py growth-status
```

## Phase B有効化

1. `growth-budget-simulation`で30日費用を確認。
2. `growth-daily-report`を7日分レビュー。
3. X上限、Threads枠、画像、スレッド、返信・引用を一つずつ変更。
4. 24時間ごとにBANリスク、否定反応、重複、フォロー転換、API費用を確認。
5. 自動返信と自動引用は引き続きfalseのまま、人間承認で検証。

## Phase C有効化

1. Phase Bを最低2週間計測。
2. Short候補の一次資料、著作権、音声・画像、60秒構成を人間確認。
3. クロス投稿を媒体単位でprivate/test公開。
4. `CROSSPOST_AUTO_PUBLISH_ENABLED`は全媒体テスト後にのみ変更。

## 緊急停止

```powershell
.\production\social_growth_stop.ps1
.\.venv\Scripts\python.exe local_bot.py crosspost-emergency-stop
```

既存Botを止める必要がある場合だけ、対象を確認して次を実行します。

```powershell
Stop-ScheduledTask -TaskName "PoliticsNarrativeBot"
Stop-ScheduledTask -TaskName "PoliticsNarrativeThreads"
```

## 本番Bot再起動

今回のPhase A実装では再起動しません。人間確認後に限り実行します。

```powershell
Stop-ScheduledTask -TaskName "PoliticsNarrativeBot"
Start-ScheduledTask -TaskName "PoliticsNarrativeBot"
```
