# xAI X Search 月30ドル運用監査

監査日: 2026-07-30 JST
対象: `D:\SNS Bot\politics-narrative`
正本台帳: SQLite `xai_usage_events`

## 結論

月30ドル設定は維持しつつ、未検証時は7.5ドル、台帳検証後のみ30ドルを
利用できる構成へ変更した。固定の1回予算ではなく、残額、予約額、月末までの
予定回数、直近実績から1回予算と実行頻度を毎回計算する。

xAI X Searchはニュースの事実確認元ではない。RSS、政府、省庁、国会、政党、
報道機関から得た候補に対して、X上の注目、論点、反対論を定性的に補助する
discovery layerとしてのみ使用する。

## 変更前30日監査

- リクエスト: 19回
- 成功: 11回
- 失敗: 8回
- ツール呼び出し記録: 55回
- 平均費用: 0.11102726 USD
- 中央値: 0.02904080 USD
- 最大値: 0.42822880 USD
- P75: 0.10981200 USD
- P90: 0.42141312 USD
- P95: 0.42309232 USD
- 台帳合計: 2.10951800 USD

旧実装には、1リクエスト当たり12、14、15ツールと記録された履歴があり、
現行上限と整合しなかった。旧履歴は費用監査には残すが、新しいツール上限の
合否判定からは分離する。

## Phase A

### 動的予算

計算式:

```text
remaining_budget =
  effective_limit - actual_monthly_cost - active_reserved_cost

dynamic_target_per_run =
  min(
    configured_warning_limit,
    remaining_budget / max(remaining_planned_runs, 1) * 0.85
  )
```

モードは`normal`、`low_frequency`、`restricted`、`emergency_only`、
`stopped`の5段階。実績比率だけでなく月末予測比率も使用する。

### 台帳安全性

- 設定上限: 30 USD
- 検証済み実効上限: 30 USD
- 未検証実効上限: 7.5 USD
- 未検証30ドルの自動開放: 無効
- `cost_in_usd_ticks / 10_000_000_000`を実費の正本とする
- ticks欠損時は`unverified_estimate`として保存し、実費へ確定しない
- 連続3回の実費未取得でxAI検索を停止する
- RSS・公式情報の監視と通常採点は継続する

### 検索範囲

前回成功時刻の30分前から今回開始時刻までを検索する。初回は240分、
最大24時間。24時間を超えた空白は`coverage_gap_minutes`へ記録する。

### 通常・拡張調査

| 項目 | 通常 | 拡張 |
|---|---:|---:|
| 最大トピック | 5 | 8 |
| 最大X Search | 2 | 3 |
| 最大ターン | 2 | 3 |
| 画像理解 | OFF | 重要案件のみ条件付き |
| 動画理解 | OFF | OFF |

拡張調査は重要度8以上または重大更新、予算モード`normal`、1回予算に余裕が
ある場合だけ使用する。

### シグナルと採点

xAIだけの信号は`qualitative_xai`。X全体の正確なインプレッション、割合、
拡散速度として表示しない。ニュース候補との一致confidenceが0.80未満なら
加点しない。xAI加点は既存スコアから分離し、合計最大2.0点に制限する。

キャッシュ利用時は`x_signal_type=cached`とし、注目推定値を時間減衰させる。
古い速度推定値は`null`にする。

## Phase B

2026-07-30に画像理解OFF、最大3トピック、投稿機能OFFで実APIを2回実行した。

- 成功: 2/2
- ツール呼び出し: 各2回
- 取得トピック: 各1件
- 合計実費: 0.06617360 USD
- coverage gap: 0分
- 外部投稿: 0件
- 台帳検証: 全項目PASS

この実測後、`XAI_COST_LEDGER_VERIFIED=true`として30ドル上限を開放した。

## Phase C

- 通常スケジュール: 06:00、09:00、12:00、15:00、18:00、21:00 JST
- 低頻度: 06:00、12:00、18:00 JST
- 制限: 06:00、18:00 JST
- Windowsタスクはログオン起動1個を維持
- タスク内部で予算モードを判定し、不要な枠をスキップ
- `MultipleInstances=IgnoreNew`
- 異常終了時は1分後に再起動、最大99回

本番疎通ではX投稿1件、Threads投稿1件が成功した。

## Phase D

成功サンプル30件未満では`insufficient_data`とし、自動変更しない。
30件到達後、成功率、費用、取得トピック数、採用可能トピック比率から
実行頻度とツール数を再評価する。

最適化後も次の上限は超えない。

- 1日最大6回
- 通常最大2ツール
- 拡張最大3ツール
- 画像理解は重要案件のみ
- 動画理解OFF

適用した判断は`xai_roi_results`へ保存し、次回実行から読み込む。

## SQLite

追加テーブル:

- `xai_discovery_runs`
- `xai_discovery_topics`
- `xai_budget_mode_history`
- `xai_roi_results`

`xai_usage_events`にはreasoning/image token、service tier、開始・完了時刻、
予約額、実費差額、実費検証状態を追加した。マイグレーションは再実行可能。

## CLI

```powershell
.\.venv\Scripts\python.exe local_bot.py xai-discovery-status
.\.venv\Scripts\python.exe local_bot.py xai-budget-mode
.\.venv\Scripts\python.exe local_bot.py xai-run-budget
.\.venv\Scripts\python.exe local_bot.py xai-coverage-report --days 30
.\.venv\Scripts\python.exe local_bot.py xai-cost-breakdown --days 30
.\.venv\Scripts\python.exe local_bot.py xai-discovery-audit
.\.venv\Scripts\python.exe local_bot.py xai-discovery-dry-run --max-topics 5 --no-api-call
.\.venv\Scripts\python.exe local_bot.py xai-roi-report --dry-run
.\.venv\Scripts\python.exe local_bot.py xai-optimize --days 30
```

実API検証は明示確認が必要:

```powershell
.\.venv\Scripts\python.exe local_bot.py xai-live-validation --runs 2 --confirm
```

## 検証

- Python構文検査: PASS
- 全テスト: 1,065件PASS、1件skip
- fixture dry-run: API 0件、外部投稿0件、全チェックPASS
- 実API台帳検証: PASS
- X本番疎通: PASS
- Threads本番疎通: PASS
- Windowsタスク: Running

## ロールバック

1. `.env`で`XAI_ENABLED=false`へ変更する。
2. Botを再起動する。
3. RSS・公式情報のみで監視と採点を継続する。
4. 必要なら`XAI_COST_LEDGER_VERIFIED=false`へ戻し、実効上限を7.5ドルへ戻す。

既存SQLite行は削除しない。追加テーブルも監査証跡として保持する。

## 残存リスク

- 旧履歴の高ツール回数は正確なツール内訳を再構成できない。
- xAIの定性的推定はX全体の母集団統計ではない。
- 24時間を超える停止期間は検索で完全回収できず、coverage gapが残る。
- Phase Dの効果判定には最低30件、推奨100件の成功サンプルが必要。
- RSSの一部で403/404があり、該当ソースは別URLの保守が必要。
