# Windows セットアップ手順（politics-narrative）

このBotはWindows対応済みです（日本語フォント自動検出 / タイムゾーン / 文字コード対応）。
以下はWindows PCでオンプレミス（ローカル常駐）稼働させる手順です。
コマンドはすべて **PowerShell** に打ちます。

> **重要（二重投稿防止）**: Macなど別のPCで同じBotを動かしている場合は、
> **必ず片方だけ**にしてください。状態ファイルはPCごとに別なので、
> 両方でdaemonを動かすと同じニュースに二重投稿します。

## 0. 準備

`politics-narrative` フォルダ一式をPCに置く。この手順では例として
`C:\X bot\politics-narrative` に置いた前提で書きます。

フォルダ構成が以下になっていることを確認:

```
politics-narrative\
  local_bot.py
  requirements.txt
  .env.example
  src\
    post.py
    news.py
  config\
  knowledge\
```

## 1. Python 3.12 を入れる（未インストールの場合）

1. https://www.python.org/downloads/ から「Download Python 3.12.x」
2. インストーラーの最初の画面で **「Add python.exe to PATH」に必ずチェック**
3. 「Install Now」

確認（PowerShellで）:

```powershell
python --version
```

`Python 3.12.x` と出ればOK。

## 2. フォルダをPowerShellで開く

エクスプローラーで `C:\X bot\politics-narrative` を開き、
**アドレスバーに `powershell` と打ってEnter**。
そのフォルダの場所でPowerShellが開きます（`cd` 不要で確実）。

別の場所から移動する場合はパスをダブルクォートで囲む:

```powershell
cd "C:\X bot\politics-narrative"
```

## 3. 仮想環境を作って有効化（初回のみ作成）

```powershell
python -m venv .venv
.venv\Scripts\activate
```

左端に `(.venv)` が付けばOK。

**「running scripts is disabled」エラーが出た場合**（初回によくある）:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

`Y` で確定してから、もう一度 `.venv\Scripts\activate`。

※ PowerShellを開き直すたびに `.venv\Scripts\activate` は打ち直します。

## 4. パッケージを入れる

```powershell
pip install -r requirements.txt
```

（Windows用のタイムゾーンデータ tzdata も自動で入ります）

## 5. .env を作る

```powershell
copy .env.example .env
notepad .env
```

メモ帳で開くので、キー5種を設定:

```
API_KEY=...
API_KEY_SECRET=...
ACCESS_TOKEN=...
ACCESS_TOKEN_SECRET=...
OPENAI_API_KEY=...
```

まずは安全のため `POST_ENABLED=false` のままにして、Ctrl+S で保存。

主な設定（.env.example に既定値入り）:

| 変数 | 意味 |
|---|---|
| `POST_ENABLED` | true にしない限りXへ実投稿しない |
| `QUALITY_GATE_ENABLED` | false=審査なしでどんどん投稿（実績学習運用）。BANリスク判定は常時有効 |
| `THREAD_ENABLED` | false=単発投稿のみ（返信分割なし） |
| `SLOT_INTERVAL_MINUTES` | 投稿間隔（分）。20なら20分刻み |
| `ACTIVE_HOURS` | 投稿する時間帯（例 6-9,12,18-23） |

## 6. 初期化（必ず1回）

```powershell
python local_bot.py init-state
```

`newly marked as attempted = ...` と出ればOK（過去分のバックログ暴発を防ぐ）。

## 7. テスト（Xには投稿されない）

```powershell
python local_bot.py status
python local_bot.py force
```

`exit=0` で終わればセットアップ成功。

## 8. 本番稼働

1. `notepad .env` で `POST_ENABLED=true` に変更・保存
2. 実投稿を1回テスト:
   ```powershell
   python local_bot.py force
   ```
   `Posted tweet id: ...` が出て、Xに投稿が出れば成功
3. 常駐スタート:
   ```powershell
   python local_bot.py daemon
   ```
   `next run at ...` が出れば本番稼働中。

**常駐中の注意:**
- PowerShellウィンドウは閉じない（閉じるとBotも止まる）
- PCをスリープさせない（設定 → システム → 電源 → スリーブを「なし」に）
- 止めるときは `Ctrl + C`

## 9. 毎日1回: 実績レポート（学習ループの生命線）

daemonとは**別のPowerShellウィンドウ**を開いて:

```powershell
cd "C:\X bot\politics-narrative"
.venv\Scripts\activate
python local_bot.py report
```

実際のインプレッションを取得し、伸びた/伸びなかったパターンを
`knowledge\` に記録。次回の生成からBotが実績を読んで改善します。

## 10. PC再起動後も自動起動させたい場合（任意・タスクスケジューラ）

1. スタートメニューで「タスクスケジューラ」を検索して開く
2. 右側の「タスクの作成」
3. 全般タブ: 名前 `politics-narrative`
4. トリガータブ: 新規 → 「ログオン時」
5. 操作タブ: 新規 →
   - プログラム: `C:\X bot\politics-narrative\.venv\Scripts\python.exe`
   - 引数: `local_bot.py daemon`
   - 開始（作業フォルダ）: `C:\X bot\politics-narrative`
6. 条件タブ: 「AC電源接続時のみ」のチェックは好みで
7. OKで保存

これでログオンすると自動でdaemonが立ち上がります。
停止したいときはタスクスケジューラで無効化するか、タスクマネージャーで
python.exe を終了します。

## トラブル時

- ログ: `logs\bot.log` / `logs\post_attempts.jsonl` / `logs\errors.jsonl`
  ```powershell
  Get-Content logs\bot.log -Tail 50
  ```
- 投稿されない理由は `Skip reason:` を確認
- `zsh` や `command not found` 系のエラーはMac用コマンドを打っている可能性
  （Windowsは `copy`・`notepad`・`.venv\Scripts\activate`）
- 詳しい運用方針は `README_運用方針.md` を参照
