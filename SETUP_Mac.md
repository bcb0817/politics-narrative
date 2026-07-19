# Mac セットアップ手順（politics-narrative）

Windows で作ったコードはそのまま Mac で動きます（フォントも macOS のヒラギノを自動で探します）。
以下は Mac 用のセットアップ手順です。コマンドはすべて **ターミナル** アプリに打ちます。

## 0. 準備

このフォルダ一式（`local_bot.py` / `src/` / `.env.example` など）を Mac にコピーしておく。
この手順では例として `~/Documents/politics-narrative` に置いた前提で書きます。

## 1. ターミナルを開く

`command + スペース` → `ターミナル` と打って Enter。

## 2. Python 3.12 を入れる

1. https://www.python.org/downloads/macos/ から macOS installer をダウンロード
2. `.pkg` を開いてインストール

確認:

```
python3 --version
```

`Python 3.12.x` と出れば OK。

## 3. フォルダに移動

```
cd ~/Documents/politics-narrative
```

（別の場所に置いた場合は、Finder でフォルダを掴んで `cd ` の後ろにドラッグ&ドロップするとパスが入る）

確認:

```
ls
ls src
```

`local_bot.py` と、`src` の中に `post.py` / `news.py` が見えれば OK。

## 4. 仮想環境を作って有効化

```
python3 -m venv .venv
source .venv/bin/activate
```

左端に `(.venv)` が付けば OK。**ターミナルを開き直すたびに `source .venv/bin/activate` は打ち直す。**

## 5. パッケージを入れる

```
pip install -r requirements.txt
```

## 6. .env を作る

```
cp .env.example .env
open -e .env
```

テキストエディットで開くので、キー5種を設定:

```
API_KEY=...
API_KEY_SECRET=...
ACCESS_TOKEN=...
ACCESS_TOKEN_SECRET=...
OPENAI_API_KEY=...
```

投稿形式と安全弁（まずは false で安全にテスト）:

```
POST_ENABLED=false
```

`command + S` で保存して閉じる。

## 7. 初期化（必ず1回）

```
python local_bot.py init-state
```

`newly marked as attempted = 48` と出れば OK（過去分の暴発を防ぐ）。

## 8. テスト（Xには投稿されない）

```
python local_bot.py status
python local_bot.py force
```

`exit=0` で終わればセットアップ成功。

## 9. 本番稼働

1. `.env` を開いて `POST_ENABLED=true` に変更・保存
2. 実投稿を1回だけテスト:
   ```
   python local_bot.py force --bypass-score
   ```
   `Posted tweet id: ...` が出て、@example_account に投稿が出れば成功
3. 常駐スタート:
   ```
   python local_bot.py daemon
   ```
   `next run at ...` が出れば本番稼働中。

**注意:** ターミナルのウィンドウは閉じない。Mac をスリープさせない
（システム設定 → バッテリー / ロック画面 でスリープを無効化）。止めるときは `control + C`。

## 10. 再起動しても自動起動させたい場合（任意・launchd）

ターミナル常駐で十分ですが、Mac 再起動後も自動で立ち上げたい場合は launchd を使います。
`~/Library/LaunchAgents/com.politics-narrative.plist` を作成:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.politics-narrative</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/takashisekimoto/Documents/politics-narrative/.venv/bin/python</string>
        <string>local_bot.py</string>
        <string>daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/takashisekimoto/Documents/politics-narrative</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/takashisekimoto/Documents/politics-narrative/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/takashisekimoto/Documents/politics-narrative/logs/launchd.err.log</string>
</dict>
</plist>
```

パス（`/Users/takashisekimoto/Documents/politics-narrative`）は実際の置き場所に合わせて修正。
読み込み・開始:

```
launchctl load ~/Library/LaunchAgents/com.politics-narrative.plist
```

停止・解除:

```
launchctl unload ~/Library/LaunchAgents/com.politics-narrative.plist
```

## トラブル時

- ログ: `logs/bot.log` / `logs/post_attempts.jsonl` / `logs/errors.jsonl`
- 投稿されない理由は `logs/bot.log` の `Skip reason:` を確認
- 詳しい運用方針は `README_運用方針.md` を参照
