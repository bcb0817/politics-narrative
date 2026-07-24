# OpenAI API移行メモ 🤖

このBotはOpenAI Responses APIと用途別モデルルーティングへ移行済みです。
新規環境では `.env.example` を参考に `.env` を作成し、
`OPENAI_API_KEY` を設定してください。

## Windowsセットアップ

```powershell
cd "D:\SNS Bot\politics-narrative"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe local_bot.py status
```

モデル名だけを現行構成に合わせる場合は、次の安全なスクリプトを使います。

```powershell
.\production\set_current_openai_models.ps1
.\production\set_current_openai_models.ps1 -Apply
```

旧 `migrate_to_openai.ps1` は廃止・隔離済みです。使用しないでください。⚠️

利用実績はSQLiteを正本として記録し、`budget-status` と
`cost-forecast` で確認できます。
