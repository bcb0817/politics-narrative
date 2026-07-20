# politics-narrative


## 現在の投稿方針

- Xへの投稿はテキスト専用。画像生成・画像アップロード機能はありません。
- 投稿は「ニュース事実 → 文章図解 → 保守・右派寄りの批判的意見」で構成します。
- 批判軸は減税・小さな政府、財政規律、安全保障、エネルギー安保、法秩序、少子化、国内産業、行政透明性です。
- 特定政党の無条件な擁護ではなく、同じ原則で与野党を評価します。

日本の政治・政策ニュースを「争点の構造」として X に自動投稿する Bot。

**このBotはローカル運用に移行しました。** GitHub Actions では動かしません。
ローカルPC / ローカルサーバー上で `local_bot.py daemon` として常駐させます。

- 対象プラットフォームは **X のみ**（Threads対応はありません）
- `POST_ENABLED=true` にしない限り **X への実投稿は一切行われません**

> テキスト投稿への移行と、catch-up 詰まり対策（attempted_slots.json）の詳しい方針は
> [`README_運用方針.md`](./README_運用方針.md) を参照してください。

## 全体像

```
local_bot.py daemon          ← 常駐。JST 毎時07分・37分に起動
   └── src/post.py diagram   ← 1回分の投稿処理（既存ロジックそのまま）
         ├── src/news.py     ← RSS取得とX Searchレーダーの接続
         ├── src/x_attention.py ← X注目度集計・スパム補正・RSS照合
         ├── OpenAI API   ← 投稿候補の生成・スコアリング
         └── tweepy          ← X へ投稿（既定はテキスト＋スレッド返信）

data/     posted_slots.json / attempted_slots.json / posted_urls.json（状態）
logs/     bot.log / post_attempts.jsonl / errors.jsonl（ログ）
```

## セットアップ

前提: Python 3.12

```bash
# 1. 仮想環境
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

# 2. 依存パッケージ
pip install -r requirements.txt

# 3. .env 作成
cp .env.example .env        # Windows: copy .env.example .env
# .env をエディタで開き、X APIキー4種と OPENAI_API_KEY を設定する
```

### .env の作り方

`.env.example` をコピーして `.env` を作り、以下を埋めます。

| 変数 | 内容 |
|---|---|
| `API_KEY` / `API_KEY_SECRET` | X API の Consumer Keys |
| `ACCESS_TOKEN` / `ACCESS_TOKEN_SECRET` | X API の Access Token |
| `X_BEARER_TOKEN` | X API v2 Recent Search用のBearer Token |
| `X_SEARCH_ENABLED` | `true`でX Searchを注目度レーダーとして使用（既定 `false`） |
| `OPENAI_API_KEY` | OpenAI API キー（候補生成に必須） |
| `POST_ENABLED` | **`true` にしない限り実投稿されない**（既定 false） |

`.env` は `.gitignore` 済みです。**絶対にコミットしないでください。**

### X Searchを有効にする

X Developer PortalでBearer Tokenを取得し、`.env`へ次を追加します。

```dotenv
X_SEARCH_ENABLED=true
X_BEARER_TOKEN=ここにBearer Token
X_SEARCH_MAX_QUERIES_PER_RUN=5
X_SEARCH_MAX_RESULTS_PER_QUERY=20
X_SEARCH_LOOKBACK_MINUTES=90
X_SEARCH_MIN_UNIQUE_ACCOUNTS=3
X_SEARCH_WEIGHT=0.25
X_SEARCH_MIN_POST_COUNT=3
X_SEARCH_MAX_TOPIC_RESULTS=10
SOURCE_SCHEDULE_SPLIT=true
```

X Searchは事実情報源ではなく、複数アカウントにまたがる注目度を測るレーダーです。
X投稿を直接候補にせず、RSS・公式情報で確認済みの候補にだけ注目度を付与します。
いいね、リポスト、返信、引用を経過時間で補正し、単一アカウント、コピー投稿、
返信スパム、反応誘導、新規・低活動アカウントなどには保守的な減点を行います。

検索は固定政策語とRSS見出しから抽出した固有語を組み合わせ、1回最大5クエリです。
集計は `data/x_search_latest.json` と `data/x_search_history/YYYY-MM-DD.jsonl` に保存します。
X API障害やレート制限時はRSSだけで継続します。投稿形式は常に久世ゆい独自の
通常投稿で、X上の意見や文章の模倣・引用ポストは行いません。

`SOURCE_SCHEDULE_SPLIT=true` の場合、毎時00分はRSS・官公庁公式情報を基にした
独自テキスト、毎時30分は外部確認済みのX Search話題を基にした独自テキストを
生成します。他者の文章、画像、動画をダウンロードして再投稿する機能はありません。

## 初回だけ: init-state（重要）

```bash
python local_bot.py init-state
```

この Bot には「過去 `CATCH_UP_HOURS`（既定24時間）の未処理スロットを古い順に回収する」
catch-up 仕様があります。GitHub Actions 運用では意図された挙動でしたが、
ローカル移行の初回起動時に `posted_slots.json` が空だと、
**過去24時間分（最大48スロット）のバックログ投稿が始まってしまいます。**

`init-state` は、過去24時間以内に開始済みのスロットを「処理済み」として登録し
（実投稿はしません）、以後は未来の 07分/37分 スロットから通常運用にします。

**ローカルで初めて動かす前に必ず1回実行してください。**

## 使い方

```bash
# 状態確認（次回実行時刻・件数・設定値・直近投稿）
python local_bot.py status

# 1回だけ通常実行（スロット判定あり）
python local_bot.py once

# 強制投稿（スロット判定なし。スコアゲートは有効）
python local_bot.py force

# 強制投稿＋スコアゲート無視（effective_score < 0 は投稿しない）
python local_bot.py force --bypass-score

# 常駐（JST 毎時07分・37分に実行。Ctrl+C で終了）
python local_bot.py daemon
```

### 動作確認の推奨手順

1. `.env` を作成（`POST_ENABLED=false` のまま）
2. `python local_bot.py init-state`
3. `python local_bot.py status`
4. `python local_bot.py force` → 候補生成・スコア判定・本文組み立てまで動く。
   `logs/bot.log` と `logs/post_attempts.jsonl` を確認。**Xには投稿されない。**
5. 問題なければ `.env` の `POST_ENABLED=true` に変更して運用開始

## POST_ENABLED について

- `POST_ENABLED=false`（既定）: 候補生成・スコア判定・本文組み立てまでは実行し、
  **X への実投稿だけを直前で止めます。** ログに
  `[INFO] POST_ENABLED=false -> X posting skipped` と出ます。
  この場合、スロットは投稿済みになりません。
- `POST_ENABLED=true`: 実投稿します。
- これは旧 dry-run モードの復活ではありません。mode は diagram 固定のまま、
  環境変数による安全弁です。

## 常駐方法（OS別）

### Windows

- 簡単な方法: ターミナル（PowerShell）を開いたままにする
  ```powershell
  .venv\Scripts\activate
  python local_bot.py daemon
  ```
- タスクスケジューラを使う場合: 「タスクの作成」→ トリガー「ログオン時」→
  操作でプログラム `C:\path\to\repo\.venv\Scripts\python.exe`、
  引数 `local_bot.py daemon`、開始（作業）フォルダをリポジトリ直下に設定。

### macOS

- 簡単な方法: ターミナル常駐（`python local_bot.py daemon`）
- launchd を使う場合: `~/Library/LaunchAgents/` に plist を置き、
  `ProgramArguments` に venv の python と `local_bot.py daemon`、
  `WorkingDirectory` にリポジトリ直下を指定して `launchctl load`。

### Linux

systemd の例（`/etc/systemd/system/politics-narrative.service`）:

```ini
[Unit]
Description=politics-narrative X bot
After=network-online.target

[Service]
WorkingDirectory=/path/to/politics-narrative
ExecStart=/path/to/politics-narrative/.venv/bin/python local_bot.py daemon
Restart=on-failure
User=youruser

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now politics-narrative
```

## ログの確認方法

| ファイル | 内容 |
|---|---|
| `logs/bot.log` | 実行ログ全般（起動時刻、モード、選択slot、skip理由、tweet_id、次回実行時刻） |
| `logs/post_attempts.jsonl` | 投稿トライの構造化記録（1行1JSON。decision / reason / score など） |
| `logs/errors.jsonl` | エラーの構造化記録 |

```bash
# 直近のログを見る
tail -50 logs/bot.log          # Windows: Get-Content logs\bot.log -Tail 50
```

## 投稿されないときの確認項目

1. `python local_bot.py status` で `POST_ENABLED` が `true` になっているか
2. `logs/bot.log` の `Skip reason:` を確認
   - `no_unattempted_slot` … その時間帯のスロットはすべてトライ済み（正常）
   - `post_disabled` … `POST_ENABLED=false`
   - `effective_score_below_threshold` … スコアが `MIN_POST_SCORE`（既定6.3）未満
   - `ban_risk_or_unverified_block` … BANリスク/未検証数字による安全弁（仕様どおり）
   - `no_news` … RSS取得失敗。ネットワークを確認
   - `post_to_x_failed` … X APIエラー。`logs/errors.jsonl` を確認
3. X APIキー・`OPENAI_API_KEY` が `.env` に正しく設定されているか
4. daemon が実際に動いているか（`logs/bot.log` に `daemon: next run at ...` が出ているか）

## 状態ファイルの移行（旧 → 新）

状態ファイルの置き場所を `src/` から `data/` に変更しました。

- 新: `data/posted_slots.json` / `data/posted_urls.json`
- 旧: `src/posted_slots.json` / `src/posted_urls.json`

旧ファイルが残っていて新ファイルがまだ無い場合、**初回実行時に自動でコピー移行**されます。
手動で移行する場合は旧ファイルを `data/` にコピーしてください。
GitHub Actions cache に入っていた状態は引き継げないため、代わりに `init-state` を使ってください。

## 安全設計（維持している方針）

- mode は diagram 固定（link / test / normal / dry-run は復活させない）
- catch-up は `attempted_slots.json` 基準。低スコアskipも attempted に記録して詰まりを防ぐ
- 過度な煽り、陰謀論、差別表現、個人攻撃、政党罵倒は禁止（`config/prohibited_expressions.md`）
- スコア判定を維持: `MIN_POST_SCORE` / `FORCE_POST` / `FORCE_BYPASS_SCORE`
- `effective_score < 0` の候補は強制でも投稿しない
- 投稿成功後にだけ投稿済み記録を保存する（失敗時は slot を posted 扱いにしない）
- 1 run の投稿トライは `MAX_POSTS_PER_RUN`（既定1）まで

## ディレクトリ構成

```
local_bot.py            ローカル運用エントリポイント
src/
  post.py               投稿処理本体（diagram固定）
  news.py               RSS取得
  publishing_policy.py  投稿上限・間隔・分類・テーマ冷却・成長スコア
  x_attention.py        X注目度集計・スパム補正・RSS照合
config/
  platform_rules.json   Xの文字数ルール（X専用）
  bot_persona.md        Botのペルソナ（参照用）
  prohibited_expressions.md  禁止表現（参照用）
knowledge/
  viral_patterns/       winning / losing / avoid パターン
data/                   状態（git管理外）
logs/                   ログ（git管理外）
```

## 選別投稿ポリシー

Botは30分ごとにニュースを監視しますが、全枠で投稿しません。JST基準で次を適用します。

- `MAX_DAILY_POSTS=16`: 1日の成功投稿上限
- `MIN_POST_INTERVAL_MINUTES=45`: 成功投稿間の最短間隔
- `TOPIC_COOLDOWN_HOURS=4`: 同一テーマの冷却時間
- `LOW_QUALITY_FALLBACK_HOURS=3`: 3時間以上成功投稿がなければ得点・型・テーマ枠を緩和
- `QUALITY_GATE_ENABLED=true` / `MIN_POST_SCORE=7.0`: 品質スコアゲート

低品質フォールバック中も、RSS確認、政治関連性、重複URL、未確認情報、BANリスク、
1日16件の上限は緩和しません。

投稿タイプは `breaking_news`、`issue_diagram`、`strong_opinion`、
`comparison_factcheck`、`morning_evening_digest` の5種類です。内部ラベルは本文へ出しません。
テーマ履歴は `data/recent_topics.json`、最新レビューは
`data/daily_review_latest.json`、日別レビューは `data/daily_reviews/YYYY-MM-DD.json` に保存します。

毎日04:45のレビューは、上位・成長上位・下位・品質エラーに加え、投稿時のX注目度、
投稿タイプ・フック・批判軸・時間別の成績、構文の反復傾向を集計し、
`knowledge/viral_patterns/` の `winning_patterns.md`、`losing_patterns.md`、
`avoid_patterns.md` を更新します。次回生成では成功形式を最大3件、失敗・禁止ルールを
最大5件だけ読み込み、プロンプトコストを制限します。

## OpenAIモデル設定の安全な更新

用途別の中央ルーター、月額予算、利用量履歴、日次・週次分析については
[MIGRATION_ModelRouting.md](MIGRATION_ModelRouting.md) を参照してください。🧠

`.env` のAPIキーや他の設定を維持したまま、モデル名・推論設定・料金単価だけを更新できます。
実行前に `.env.backup.YYYYMMDD-HHMMSS` が自動作成されます。

```powershell
# 変更内容だけ確認
.\production\update_openai_models.ps1 -Profile recommended -WhatIf

# 推奨構成: gpt-5.4-nano + gpt-5.4-mini
.\production\update_openai_models.ps1 -Profile recommended

# 最新世代重視: gpt-5.4-nano + gpt-5.6-luna
.\production\update_openai_models.ps1 -Profile latest
```

更新後は `PoliticsNarrativeBot` の再起動が必要です。

## 今後の拡張方針

現在、生成方針は `src/post.py` 内のプロンプトに直書きされています。
今後は `config/bot_persona.md`（ペルソナ・トーン）、`config/prohibited_expressions.md`、
`knowledge/viral_patterns/`・`knowledge/failed_patterns/`（伸びた/伸びなかった投稿パターン）を
Bot に読み込ませ、生成品質を実績ベースで改善していく設計へ拡張する予定です。

## GitHub Actions について

GitHub Actions での運用は廃止しました。`.github/workflows/post.yml` は削除済みです。
もしリポジトリに残っている場合は削除してください（scheduleが発火すると二重投稿の原因になります）。
