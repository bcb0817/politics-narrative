# 自動起動・xAI費用・文字コード監査

監査日: 2026-07-24 JST  
対象: `D:\SNS Bot\politics-narrative`

## 結論

- 正式な自動起動経路はWindowsタスク `PoliticsNarrativeBot`。
- 正式タスクは未起動で、監査時の政治Botプロセスは0件。
- 実行は非表示PowerShell、作業フォルダは対象リポジトリ、
  多重起動は `IgnoreNew`、異常終了後は60秒で再試行。
- Startupの旧政治Botファイルは `.disabled`。
- Registry Run、PowerShellプロファイル、Windowsサービスに該当経路なし。
- 管理者所有の旧 `PoliticsNarrativeDailyReview` はWindows ACLにより
  タスク無効化が拒否されたが、実行先は監査ログだけを残して終了するno-op。
  APIおよびX操作は実行できないため、機能上は無効。

## xAI費用

- 監査対象イベント: 10件
- レーダー: 7件、`$1.8222996`
- 接続・互換性診断: 3件、`$0.0703384`
- 合計: `$1.892638`
- 表示額はSQLiteとJSONの二重加算ではない。
- 高額化原因は旧実装で失敗リクエスト内に12〜15回のツール呼び出しと、
  約16万〜32万入力トークンが発生したこと。
- SQLite `xai_usage_events` を正本とし、JSONLは監査専用。
- request_idは一意、実費優先、推定費用は別列。

## ticks換算

換算処理は `src/xai_cost.py` の次の関数だけを使用する。

```text
actual_cost_usd = cost_in_usd_ticks / 10,000,000,000
```

## 文字コード

- Python、JSON、YAML、MarkdownはUTF-8。
- 日本語を含むPowerShellはUTF-8 BOM。
- JSONは `ensure_ascii=False`。
- 安全判定前にNFKC正規化。
- Unicode置換文字と代表的文字化け列のソース検査: 0件。
- SQLite全TEXT列の同検査: 0件。

## 詳細

履歴、タスクXML、費用イベント内訳、予算ガードは
`AUDIT_BUDGET_STARTUP_XAI.md` と
`IMPLEMENTATION_REPORT_STARTUP_XAI_LEDGER_2026-07-24.md` を参照。

