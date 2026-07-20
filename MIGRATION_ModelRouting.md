# OpenAIモデルルーティング移行ガイド 🧠

## 変更内容

- 通常投稿: `gpt-5.4-mini`
- 重要投稿と日次レビュー: `gpt-5.6-luna`
- 分類器: `gpt-5.4-nano`（初期状態は無効）
- 週次レポート: `gpt-5.6-terra`（初期状態は無効）
- 手動プレミアム分析: `gpt-5.6-sol`（初期状態は無効）
- 月額上限: `$8.00`、予備費: `$0.50`

## 安全な移行手順 🔒

PowerShellをリポジトリ直下で開き、最初に差分だけ確認します。

```powershell
.\production\update_openai_models.ps1 -Profile latest -WhatIf
```

問題がなければ適用します。スクリプトは現在の `.env` を日時付きでバックアップし、APIキーなど対象外の値を維持します。

```powershell
.\production\update_openai_models.ps1 -Profile latest
```

## 旧設定との互換性

`OPENAI_MODEL_DEFAULT` と `OPENAI_MODEL_IMPORTANT` はそのまま利用できます。新しい用途別設定がない場合、日次レビューなどはこの2値へ安全にフォールバックします。価格は `config/openai_model_pricing.json` で一元管理します。

## 任意機能 ⚙️

週次とプレミアムはXへ投稿せず、ローカルファイルだけを生成します。初期状態では無効です。

```powershell
$env:WEEKLY_REPORT_ENABLED="true"
.\.venv\Scripts\python.exe .\local_bot.py weekly-report

$env:OPENAI_PREMIUM_ENABLED="true"
.\.venv\Scripts\python.exe .\local_bot.py premium-report
```
