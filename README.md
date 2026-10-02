# politics-narrative — 政治解説の記事下書き

現在の入口は **手動実行のxAI記事生成だけ**です。X/Threadsへの公開機能、定期収集、
旧Botのdaemonは復旧していません。既存の公開設定・投稿頻度・月次予算は変更しません。
Python 3.11以降、標準ライブラリのみで実行できます。

例外として、明示的な依頼による通常のX投稿1件だけを確認する
`src.manual_x_smoke`があります。記事公開・任意本文・定期投稿には対応しません。
2026-10-02のテストは成功済みで、再実行しても追加送信しません。

```powershell
Set-Location 'D:\SNS Bot\politics-narrative'
python -X utf8 local_bot.py article --theme '解説したい政策' --url 'https://一次資料のURL'
```

`XAI_API_KEY`を環境変数または既存の`.env`に設定してください。キーの値を引数に渡さないでください。
初期予算は1記事0.50米ドル。生成物は`outputs/articles/<run-id>/`へ保存され、Git対象外です。
`ready_for_review`も公開前の人間確認が必要です。自動公開の設定変更による有効化はできません。

- 詳細な実行・復旧・料金・制限：[記事運用手順](docs/ARTICLE_OPERATIONS.md)
- 編集方針：[記事編集規約](config/article_editorial.md)
- モデル・料金・上限：[設定](config/article_generation.json)

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests local_bot.py
```

テストは架空資料、固定した予算時刻、一時DB、モック応答を使用し、本番`.env`や実APIを読みません。
