# 起動経路・予算・xAI台帳監査

監査日: 2026-07-24 JST  
対象: `D:\SNS Bot\politics-narrative`

## 結論

- 正式な自動起動経路は Windowsタスク `PoliticsNarrativeBot`。
- 正式タスクは現在 `Ready`（停止中）で、今回の作業では起動していない。
- Startupの旧政治Botは `Politics-X-Bot.cmd.disabled` で既に無効。
- Runキー、PowerShellプロファイル、Windowsサービスに政治Bot経路はない。
- 旧 `PoliticsNarrativeDailyReview` は管理者所有で、無効化操作がアクセス拒否になった。
  ただし、呼び出し先 `production\run_daily_review.ps1` を no-op に変更したため、
  API呼び出し・レビュー生成・X操作は一切実行しない。
- 日次レビューの実処理は正式タスク内のデーモンに統合されている。

## プロセス監査

監査時、リポジトリをコマンドラインに含むPythonデーモンは0件だった。
監査コマンド自身のPowerShell以外に、政治Bot関連PowerShellはなかった。

## 正式タスク

| 項目 | 値 |
|---|---|
| タスク名 | `PoliticsNarrativeBot` |
| 状態 | `Ready` |
| トリガー | 対象ユーザーのログオン |
| 実行 | `powershell.exe` |
| 引数 | `-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "...production\run_bot.ps1"` |
| 作業フォルダ | `D:\SNS Bot\politics-narrative` |
| 多重起動 | `IgnoreNew` |
| 異常終了時 | 60秒間隔、最大99回再試行 |
| 日次レビュー | デーモン内に統合 |

`production\register_task.ps1` は登録後に
`Get-ScheduledTask` で確認し、タスクを勝手に開始しない。

## 検出した全起動経路

| 経路 | 状態 | 処置 |
|---|---|---|
| `PoliticsNarrativeBot` | 正式・停止中 | 再登録して設定確認 |
| `PoliticsNarrativeDailyReview` | 旧経路・Ready | 停止済み。所有権により無効化拒否。呼び出し先をno-op化 |
| Startup `Politics-X-Bot.cmd.disabled` | 無効 | 維持 |
| Registry Run | 該当なし | 変更なし |
| PowerShell profiles | 該当ファイルなし | 変更なし |
| Windows services | 該当なし | 変更なし |
| その他の政治Botタスク | 該当なし | 変更なし |

## xAI台帳の監査結果

従来表示の `$1.892638` は二重加算ではない。
汎用 `api_usage_events` と専用 `xai_usage_events` に同じレーダーイベントが
複製記録されていたが、従来集計は汎用テーブルだけを集計していた。

費用の実体は次の10件。

| 種別 | 件数 | 実費USD |
|---|---:|---:|
| xAIレーダー | 7 | 1.8222996 |
| 接続・互換性診断 | 3 | 0.0703384 |
| 合計 | 10 | 1.8926380 |

主因は旧レーダーの失敗リクエストである。1リクエストあたり
12〜15回のX Search、約16万〜32万入力トークンが発生し、失敗でも
実費が約 `$0.18`〜`$0.43` 計上された。

`cost_in_usd_ticks` は **10,000,000,000 ticks = 1 USD**。
保存値とAPI診断時の既知実費を照合し、この換算で一致した。

## 修正後の台帳

- 正本: SQLite `xai_usage_events`
- JSONL: 監査・バックアップ専用
- 新規xAI費用を `api_usage_events` へ鏡像記録しない
- `request_id` に一意制約
- 実費がある場合は `actual_cost_usd` を優先
- 実費がない場合のみ `estimated_cost_usd`
- `cost_source` は `actual` または `estimated`
- 各行は累積額ではなく、そのリクエスト単体の費用
- 旧汎用台帳だけにあった診断3件は、履歴を削除せず専用台帳へ移行
- 移行後: 10件、request_id 10件、合計 `$1.892638`

## 予算ガード

| API | 月額枠 |
|---|---:|
| OpenAI | $8 |
| xAI | $3 |
| X API | $12 |
| 合計 | $23 |

予備費は OpenAI `$0.75`、xAI `$0.25`、X `$0.50`、全体 `$1.00`。

`XAI_COST_LEDGER_VERIFIED=false` の間はxAI実効上限を `$2` に固定する。
人間が台帳を確認して `true` に変更した場合だけ `$3` を利用できる。

## xAI実行制約

- 基本枠: `06:00,12:00,18:00`
- 低変動日: `06:00,18:00`
- 1枠1リクエスト
- 1リクエスト最大1ツール呼び出し
- 再試行による同一枠の追加課金なし
- 警告: `$0.012`
- 当日停止: `$0.020` 超過
- xAI停止時もRSS、公式情報監視、OpenAI投稿処理は継続

## 文字コード監査

- Python、設定、テスト、Markdown、PowerShellをUTF-8として検査。
- PowerShell 18本をUTF-8 BOMへ統一。
- 文字化けした重複READMEファイル名2件を除去し、正常名の原本を維持。
- SQLite全TEXT列を検査し、Unicode置換文字および代表的な文字化け列は0件。
- 比較前にNFKC正規化を追加し、全角表記による安全判定回避を防止。

## バックアップ

`D:\SNS Bot\politics-narrative\backups\politics-narrative-backup-20260724-013738`

