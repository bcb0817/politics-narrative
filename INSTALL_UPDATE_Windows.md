# Windows差し替え手順

このZIPは `D:\SNS Bot\politics-narrative` に上書き展開できます。
ZIPには `.env` を含めていないため、既存のAPIキー設定は上書きされません。

```powershell
Expand-Archive `
  -Path "$HOME\Downloads\politics-narrative-text-only-conservative.zip" `
  -DestinationPath "D:\SNS Bot" `
  -Force

cd "D:\SNS Bot\politics-narrative"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe local_bot.py status
```

旧Pillowを削除する場合のみ実行します。

```powershell
.\.venv\Scripts\python.exe -m pip uninstall pillow -y
```

テスト生成は次です。`POST_ENABLED=false` のままならXには投稿されません。

```powershell
.\.venv\Scripts\python.exe local_bot.py force
Get-Content .\logs\bot.log -Tail 100
```
