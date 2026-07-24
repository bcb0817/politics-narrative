# 月額API予算35ドル化 事前監査 💰

## 監査日時

2026-07-25 04:21 JST

## 変更前の有効設定

- OpenAI: `$8.00`
- xAI: `$3.00`
- X API: `$12.00`
- 合計: `$23.00`
- OpenAI reserve: `$0.75`
- xAI reserve: `$0.25`
- X reserve: `$0.50`
- Total reserve: `$1.00`
- USD/JPY: `165`
- 旧表示用円予算: `5,000円`

## 検出した固定値

- `src/api_budget.py` にプロバイダー・総額の旧デフォルト値が存在した。
- `src/api_budget.py`、`src/news.py`、`src/xai_radar.py` に
  4,500円・4,800円・5,000円の固定判定が存在した。
- `local_bot.py` の `budget-status` と `cost-forecast` に
  `$8`、`$12`、`$23`、5,000円の旧表示・デフォルトが存在した。
- `.env` と `.env.example` に旧予算と旧reserveが存在した。
- 旧予算へ戻す専用PowerShellスクリプトは検出されなかったため、
  `archive/deprecated/budget_migrations/` への移動対象はなかった。

## 方針

- 優先順位を `.env` → `config/api_budget.json` → 安全なデフォルトとする。
- provider合計とtotalが不一致なら警告し、低い方を実効総上限にする。
- reserveは総額に加算せず、総予算内の保留額として扱う。
- 円表示は `TOTAL_MONTHLY_API_BUDGET_USD × BUDGET_USD_JPY_RATE` で算出する。
- 85%・93%・100%を比率ベースで判定する。
- xAI台帳未検証時は設定値にかかわらず実効上限を`$2.00`に保つ。
- 過去のAPI使用履歴・投稿・レビューは変更しない。
