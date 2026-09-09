# 日次投稿目標（20件） 🎯

Xで成功保存されたユニーク投稿を1日20件の正本として数えます。Threadsの自動
クロスポストは到達率として別表示し、同じ内容を2件とは数えません。日付境界は
JSTです。

```powershell
.\.venv\Scripts\python.exe local_bot.py daily-post-goal
.\.venv\Scripts\python.exe local_bot.py daily-post-goal --save
.\.venv\Scripts\python.exe local_bot.py daily-post-goal --save --apply-remediation
```

既定は読み取り専用です。`--save`は`data/daily_post_goal/`へJSONを保存します。
`--notify`は既存Discord設定が有効な場合だけ結果概要を通知します。いずれもSNSへ
投稿しません。

目標は`.env`の`DAILY_POST_TARGET=20`で変更できます。未達時は当日の投稿試行ログを
集計し、候補不足、品質不合格、スロット競合、投稿停止、API・予算などを分類して、
残り時間に必要なペースと対策を表示します。投稿数を増やすためでも、品質・安全・
重複・一次情報確認の基準は自動的に下げません。`--apply-remediation`は候補の事前選別数、
一時的API失敗の再試行、検証済み常緑候補の上限だけを安全な範囲で調整します。

毎日04:40の日次レビューでは前日分を自動集計し、未達なら是正ポリシーを保存します。
ChatGPTの日次分析にも達成率・未達理由・是正内容を渡し、次回投稿から反映します。📊

Windowsタスクは既定でWhatIfです。

```powershell
.\production\register_daily_post_goal_task.ps1
.\production\register_daily_post_goal_task.ps1 -Apply
```

`-Apply`時のみ毎日18:00 JST相当のWindowsローカル時刻に登録します。当日中に
安全な補完策を取れるよう、締切直前ではなく夕方に監視します。登録処理は
タスクを即時起動しません。18:00の進捗監視も是正ポリシーを更新します。
