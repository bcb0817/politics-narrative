# xAI X Search 月30ドル運用仕様 💰🔎

## 目的

xAI X Searchを、X上の公開反応を事実認定するためではなく、RSS・政府・省庁・国会・
報道機関から取得した検証可能なニュース候補の注目度と拡散速度を測る補助レーダーとして使う。
得られたシグナルは候補の優先順位へ反映し、既存の品質・安全・重複・投稿間隔・日次上限を
通過した場合だけXとThreadsの自動投稿経路へ渡す。

## 予算

- xAI月額上限: 30.00 USD
- xAI予約額: 1.50 USD
- OpenAI月額上限: 15.00 USD
- X API月額上限: 16.00 USD
- 全API合計上限: 61.00 USD
- 全体予約額: 3.25 USD
- 表示用換算レート: 1 USD = 165 JPY
- 表示上の合計上限: 10,065 JPY

`XAI_MONTHLY_BUDGET_USD`と`XAI_VERIFIED_EFFECTIVE_LIMIT_USD`は30.0とする。
台帳検証フラグがfalseの場合の実効上限は7.5ドルで、30ドルを自動開放しない。
運用者が`XAI_ALLOW_UNVERIFIED_FULL_BUDGET=true`を明示した場合のみ例外とする。
実費はSQLiteの`xai_usage_events`を正本として記録する。

## 検索スケジュール

- 通常: 06:00、09:00、12:00、15:00、18:00、21:00 JST
- 最大: 1日6リクエスト
- 低ボラティリティ時: 06:00、12:00、18:00 JST
- 予算制限時: 06:00、18:00 JST
- 検索対象期間: 直近240分
- キャッシュ有効期間: 240分

固定金額ではなく、xAI実効上限に対する実績・月末予測の比率で頻度を縮小する。
85%到達時は低頻度、93%到達時は制限頻度とし、全体予算ガードも優先する。

## 1回の調査

- 最大8トピック
- 1トピック最大5件の代表Post ID
- 最大3回のX Searchツール呼び出し
- 最大3ターン
- 画像理解: 有効
- 動画理解: 無効
- 1回の目標警告額: 0.200 USD
- 1回の停止判定額: 0.300 USD

出力するシグナルは注目度、拡散速度、主な主張、反対意見、代表Post IDである。
Post本文の転載や、個人を対象にした評価は行わない。

## 自動投稿までの経路

1. RSS・公式情報からニュース候補を取得する。
2. xAI X Searchが候補と現在のX上の注目を比較する。
3. 独立したニュース候補と一致したトピックだけにxAIシグナルを付与する。
4. `x_attention_score`を既存のニューススコアへ加える。
5. 品質、安全性、重複、topic cooldown、投稿間隔、日次上限、予算を検査する。
6. `POST_ENABLED=true`ならXへ自動投稿する。
7. 公開済みX投稿はThreads用に再構成され、`THREADS_POST_ENABLED=true`かつ
   Threads側のスケジュール・上限・安全条件を満たした場合に自動投稿する。
8. 投稿記録へxAI一致、注目度、速度、調査時刻、配賦コストを保存する。

xAIだけで発見した未確認情報からニュース候補を新規作成しない。xAIが停止・失敗しても、
RSS・公式情報による監視と既存キャッシュで運用を継続する。

## 運用確認

```powershell
.\.venv\Scripts\python.exe local_bot.py budget-status
.\.venv\Scripts\python.exe local_bot.py cost-forecast
.\.venv\Scripts\python.exe local_bot.py discovery-provider-status
.\.venv\Scripts\python.exe local_bot.py xai-roi
.\.venv\Scripts\python.exe local_bot.py status
.\.venv\Scripts\python.exe local_bot.py threads-status
```
