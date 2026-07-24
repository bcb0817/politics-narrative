# OpenAIモデルルーティング移行ガイド 🤖

## 現行モデル

- 通常投稿: `gpt-5.4-mini`
- 重要投稿: `gpt-5.6-luna`
- 補助分類: `gpt-5.4-nano`
- 日次レビュー: `gpt-5.4-mini`
- 週次レビュー: `gpt-5.6-terra`
- 手動プレミアム分析: `gpt-5.6-sol`

## 安全な設定変更

リポジトリ直下で、まずプレビューします。

```powershell
.\production\set_current_openai_models.ps1
```

表示内容を確認後、対象のモデルキーだけを更新します。

```powershell
.\production\set_current_openai_models.ps1 -Apply
```

適用時は `.env` の日時付きバックアップが作成されます。APIキーや予算など、
対象外の設定は変更しません。

## 廃止したスクリプト ⚠️

旧移行スクリプトは誤実行防止のため
`archive/deprecated/model_migrations/` に隔離されています。
本番環境では使用しないでください。
