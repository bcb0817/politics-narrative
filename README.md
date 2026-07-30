# politics-narrative

## Current Affairs Expansion Phase A

久世ゆい Social Content Factory は、政治を中核に、経済・企業、重大事件、
AI・テクノロジー、サイバー、社会・生活、安全保障、災害・インフラを
「制度・お金・責任・安全」の視点で整理する時事解説AIへ拡張しました。

Phase Aはカテゴリ分類、brand fit評価、候補在庫、Short・記事・長尺候補、
カテゴリ別レポートだけをローカル生成します。既存のX/Threads投稿数、
プロフィール、重大事案処理、note・動画公開経路は変更しません。
SNSは需要発見だけに使い、事実確認には一次資料と信頼できる報道を使います。

```powershell
.\.venv\Scripts\python.exe local_bot.py current-affairs-status
.\.venv\Scripts\python.exe local_bot.py category-list
.\.venv\Scripts\python.exe local_bot.py category-classify --dry-run
.\.venv\Scripts\python.exe local_bot.py current-affairs-full-cycle --dry-run
```

詳細は `IMPLEMENTATION_PLAN_CURRENT_AFFAIRS_EXPANSION.md`、
`config/current_affairs_categories.json`、
`config/current_affairs_sources.json` を参照してください。

## 社会的怒りの構造化

「社会の怒りを届ける」を、感情の増幅ではなく、負担・受益・意思決定・
責任・説明不足を事実で整理する候補生成・安全評価機能として追加しています。
Phase Bはシャドー比較、Phase Cは制度・予算・行政プロセスなど低リスク対象
だけをX・Threads生成へ接続します。重要テーマは最大5案、通常テーマは最大3案を
比較し、既存の安全・品質・間隔・日次上限・予算判定を通った1案だけを投稿します。

```powershell
.\.venv\Scripts\python.exe local_bot.py social-anger-status
.\.venv\Scripts\python.exe local_bot.py social-anger-full-cycle --dry-run
```

詳細は `docs/SOCIAL_ANGER_PHASE_A.md` を参照してください。



## 現在の投稿方針

- Xへの投稿はテキスト専用。画像生成・画像アップロード機能はありません。
- 投稿は「ニュース事実 → 文章図解 → 保守・右派寄りの批判的意見」で構成します。
- 批判軸は減税・小さな政府、財政規律、安全保障、エネルギー安保、法秩序、少子化、国内産業、行政透明性です。
- 特定政党の無条件な擁護ではなく、同じ原則で与野党を評価します。

日本の政治・政策ニュースを「争点の構造」として X に自動投稿する Bot。

**このBotはローカル運用に移行しました。** GitHub Actions では動かしません。
ローカルPC / ローカルサーバー上で `local_bot.py daemon` として常駐させます。

- 本番配信先は **XとMeta公式Threads API** です。
- Phase AのSocial Content Factoryは、SNSを需要発見装置として扱い、
  topic・claim・angle単位の候補、図解、スレッド、Short、記事原料をローカル生成します。
- 動画クロス投稿は準備機能まで実装済みで、自動公開は既定OFFです。
- `POST_ENABLED=true` にしない限り **X への実投稿は一切行われません**

> テキスト投稿への移行と、catch-up 詰まり対策（attempted_slots.json）の詳しい方針は
> [`README_運用方針.md`](./README_運用方針.md) を参照してください。

## 全体像

```
local_bot.py daemon          ← 常駐。JST 毎時07分・37分に起動
   └── src/post.py diagram   ← 1回分の投稿処理（既存ロジックそのまま）
        ├── src/news.py     ← RSS取得と選択式トピックレーダーの接続
        ├── src/xai_radar.py ← xAI X Search（事実認定には不使用）
        ├── src/engagement_queue.py ← 人間承認用の引用・返信候補
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

### トピック探索プロバイダー

`X_TOPIC_DISCOVERY_PROVIDER=xai|native_x|none` で選択します。有料プロバイダーは同時実行せず、
失敗時に別の有料プロバイダーへ自動切替しません。`xai` はX上の伸び始めを検出するだけで、
事実はRSS・公式資料・信頼できる報道で別途確認します。

```dotenv
X_TOPIC_DISCOVERY_PROVIDER=xai
X_NATIVE_SEARCH_ENABLED=false
XAI_ENABLED=true
XAI_MODEL=grok-4.5
XAI_SEARCH_SCHEDULE=06:00,09:00,12:00,15:00,18:00,21:00
```

## 設定の正本

設定の優先順位は次のとおりです。

```text
.env
  ↓
src/runtime_config.py の型付き設定
  ↓
実行時設定
  ↓
local_bot.py config-audit
  ↓
README・運用レポート
```

`.env.example`は設定例であり、本番値ではありません。Gmailへのnoteドラフト転送は
現行実装に含まれません。noteドラフトの外部通知先はDiscordだけです。

Phase Aの候補生産を外部投稿なしで確認できます。

```powershell
.\.venv\Scripts\python.exe local_bot.py config-audit
.\.venv\Scripts\python.exe local_bot.py growth-full-cycle --dry-run
.\.venv\Scripts\python.exe local_bot.py growth-status
```

xAIレーダーは`tool_choice=required`、`max_turns=1`、`parallel_tool_calls=false`で実行します。
実際の`server_side_tool_usage_details.x_search_calls`と`cost_in_usd_ticks`を保存し、
複数検索が報告された場合は再試行せず停止します。

### ネイティブX Searchを有効にする

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

検索は固定政策語とRSS見出しから抽出した固有語を組み合わせ、1回最大3クエリ・各6件（合計18件）です。
集計は `data/x_search_latest.json` と `data/x_search_history/YYYY-MM-DD.jsonl` に保存します。
X API障害やレート制限時はRSSだけで継続します。投稿形式は常に久世ゆい独自の
通常投稿で、X上の意見や文章の模倣・引用ポストは行いません。

RSS・官公庁公式情報は05:00〜23:00の毎時00分（1日19枠）に監視し、
X Searchは06:00・12:00・18:00だけ更新します。
それ以外の監視では6時間キャッシュ済みの集計値だけを参照します。
キャッシュは集計値だけを含み、他者の文章、画像、動画を再投稿する機能はありません。

## 初回だけ: init-state（重要）

```bash
python local_bot.py init-state
```

この Bot には「過去 `CATCH_UP_HOURS`（既定24時間）の未処理スロットを古い順に回収する」
catch-up 仕様があります。GitHub Actions 運用では意図された挙動でしたが、
ローカル移行の初回起動時に `posted_slots.json` が空だと、
**過去24時間分（最大48スロット）のバックログ投稿が始まってしまいます。**

`init-state` は、過去24時間以内に開始済みのスロットを「処理済み」として登録し
（実投稿はしません）、以後は未来の毎時00分スロットから通常運用にします。

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

Botは05:00〜23:00の毎時00分にニュースを監視しますが、全枠で投稿しません。JST基準で次を適用します。

- `ORIGINAL_DAILY_POST_MIN=6`: 目標値（投稿ノルマではない）
- `ORIGINAL_DAILY_POST_MAX=8`: 通常投稿の上限
- `BREAKING_DAILY_POST_LIMIT=2` / `MAX_DAILY_AUTOMATED_POSTS=10`: 重大速報・総投稿上限
- `MIN_POST_INTERVAL_MINUTES=60`: 成功投稿間の最短間隔
- `TOPIC_COOLDOWN_HOURS=4`: 同一テーマの冷却時間
- `LOW_QUALITY_FALLBACK_ENABLED=false`: 無投稿でも品質基準を緩和しない
- `EVERGREEN_MIN_SILENCE_HOURS=3`: 3時間無投稿なら公式資料に基づく制度解説を最大1件検討
- `QUALITY_GATE_ENABLED=true` / `MIN_POST_SCORE=7.0`: 品質スコアゲート

低品質フォールバック中も、RSS確認、政治関連性、重複URL、未確認情報、BANリスク、
1日10件の上限は緩和しません。絵文字は選択式で、約25％・最大1個です。重大事件では使いません。

投稿タイプは `breaking_news`、`issue_diagram`、`strong_opinion`、
`comparison_factcheck`、`digest` の5種類です。内部ラベルは本文へ出しません。

## Phase 1〜3・APIコスト管理 🛡️

共通SQLiteは `data/bot_metrics.db` です。既存JSONを維持しながら、ニュース候補、生成投稿、
公開投稿、1h/24h/72h指標、日次・週次レビュー、API費用を二重記録します。

```powershell
.\.venv\Scripts\python.exe local_bot.py db-status
.\.venv\Scripts\python.exe local_bot.py collect-metrics
.\.venv\Scripts\python.exe local_bot.py daily-review
.\.venv\Scripts\python.exe local_bot.py weekly-review
.\.venv\Scripts\python.exe local_bot.py preview-extensions
.\.venv\Scripts\python.exe local_bot.py budget-status
.\.venv\Scripts\python.exe local_bot.py cost-forecast
```

OpenAI `$8`、xAI `$2`、X `$13`、合計 `$23` の月額上限は、API呼び出し前にSQLiteトランザクションで
最大予想額を予約して判定します。通常投稿へURLが含まれる場合は投稿しません。🔒
OpenAI予算は投稿生成 `$5`、分類 `$0.5`、日次レビュー `$1.5`、週次レビュー `$0.5`、
予備 `$0.5` に分離し、レビューが投稿生成枠を消費しないようにしています。
`cost-forecast` は監視・X利用件数、モデル別費用、月末予測、円換算残額を表示します。
月末予測が4,500円超で警告、4,800円超でxAI X Searchと低優先LLM処理を停止します。

人間承認用キューとプロフィール監査はXへ書き込みません。

```powershell
.\.venv\Scripts\python.exe local_bot.py engagement-queue
.\.venv\Scripts\python.exe local_bot.py queue-status
.\.venv\Scripts\python.exe local_bot.py queue-update --type quote --id 1 --status approved
.\.venv\Scripts\python.exe local_bot.py profile-audit
```
テーマ履歴は `data/recent_topics.json`、最新レビューは
`data/daily_review_latest.json`、日別レビューは `data/daily_reviews/YYYY-MM-DD.json` に保存します。

毎日04:40のレビューは、上位・成長上位・下位・品質エラーに加え、投稿時のX注目度、
投稿タイプ・フック・批判軸・時間別の成績、構文の反復傾向を集計し、
`knowledge/viral_patterns/` の `winning_patterns.md`、`losing_patterns.md`、
`avoid_patterns.md` を更新します。次回生成では成功形式を最大3件、失敗・禁止ルールを
最大5件だけ読み込み、プロンプトコストを制限します。

さらにChatGPT（OpenAI Responses API）が、24時間の投稿指標と匿名化した運用ログ集計を
解析し、インプレッション最大化の翌日方針を構造化JSONで返します。根拠Tweet ID、
計測指標、信頼度がある提案だけを48時間有効にし、投稿型・フック・文字数・文章構造へ
自動反映します。安全基準、事実確認、政治的評価原則、投稿上限、API予算は変更できません。

```powershell
.\.venv\Scripts\python.exe local_bot.py review-strategy-status
```

## OpenAI Batch API

日次レビューは翌日の投稿へ即時反映するため同期Responses APIを使用します。
週次レビューなど遅延可能な処理は、OpenAI Batch API（`/v1/responses`、完了枠`24h`）へ
非同期送信できます。速報・通常投稿の生成も同期Responses APIのままです。

```dotenv
OPENAI_BATCH_ENABLED=true
OPENAI_BATCH_TASKS=weekly_report,quality_eval
```

デーモンは起動時と毎時処理後に完了結果を回収します。手動操作も可能です。

```powershell
.\.venv\Scripts\python.exe local_bot.py batch-status
.\.venv\Scripts\python.exe local_bot.py batch-collect
```

入力JSONLとジョブ状態は`data/openai_batches/`およびSQLiteの`openai_batch_jobs`へ保存されます。
同じ日次・週次処理は`custom_id`で重複送信を防止します。Batchの利用額は同期単価の50%として
既存のOpenAI月額予算と用途別レビュー予算へ計上されます。取得エラーでもX投稿処理は停止しません。

## OpenAIモデル設定の安全な更新

用途別の中央ルーター、月額予算、利用量履歴、日次・週次分析については
[MIGRATION_ModelRouting.md](MIGRATION_ModelRouting.md) を参照してください。🧠

`.env` のAPIキーや他の設定を維持したまま、モデル名・推論設定・料金単価だけを更新できます。
実行前に `.env.backup.YYYYMMDD-HHMMSS` が自動作成されます。

```powershell
# 変更内容だけ確認
.\production\set_current_openai_models.ps1

# 推奨構成: gpt-5.4-nano + gpt-5.4-mini
.\production\set_current_openai_models.ps1 -Apply

# 最新世代重視: gpt-5.4-nano + gpt-5.6-luna
# Legacy model profiles were quarantined. See:
# .\production\DEPRECATED_MODEL_SCRIPTS.md
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
# Growth / cost / quality v2

The production defaults keep hourly RSS and official-source monitoring while
reducing xAI X Search to `06:00,12:00,18:00` (or `06:00,18:00` on locally
detected low-volatility days). X-only claims are never treated as verified.

Daily review runs synchronously at 04:40 JST with a 04:55 deadline and a
local-only fallback. OpenAI Batch is reserved for weekly review and explicit
offline quality evaluation. Automatic use of `gpt-5.6-sol` is disabled.

Human-operated and quality commands:

```powershell
.\.venv\Scripts\python.exe local_bot.py eval-quality --mode rule-only
.\.venv\Scripts\python.exe local_bot.py eval-quality --mode sample --limit 10
.\.venv\Scripts\python.exe local_bot.py engagement-queue
.\.venv\Scripts\python.exe local_bot.py engagement-brief
.\.venv\Scripts\python.exe local_bot.py queue-update --type quote --id 1 --status posted_manually
.\.venv\Scripts\python.exe local_bot.py import-human-review --file .\reports\human_review\reviewed.csv
.\.venv\Scripts\python.exe local_bot.py import-conversions --file .\data\imports\conversions.csv
.\.venv\Scripts\python.exe local_bot.py follower-status
.\.venv\Scripts\python.exe local_bot.py quality-dashboard
```

The engagement queue never sends replies or quote posts. `follower-status
--capture` performs an owned-account read only; the scheduled daemon uses it at
00:05 and 23:55 JST. Post-level follower attribution is explicitly reported as
a time-window estimate.

## Discord notifications

The webhook URL must be stored only in the ignored `.env` file. Discord receives
result summaries only. Detailed models, scores, risk diagnostics, slots, stack
traces, and raw log lines remain in the local `logs/` directory.

```dotenv
DISCORD_NOTIFICATIONS_ENABLED=true
DISCORD_WEBHOOK_URL=
DISCORD_NOTIFY_STARTUP=true
DISCORD_NOTIFY_POST_SUCCESS=true
DISCORD_NOTIFY_ERROR=true
DISCORD_NOTIFY_RUN_LOG=true
DISCORD_NOTIFY_SKIP=false
DISCORD_NOTIFY_THREADS_RESEARCH=true
DISCORD_LOG_MODE=result_only
```

通知は「投稿完了」「今回は投稿なし」「処理失敗」「ログ確認結果」に整理されます。
投稿成功は重複通知せず、`discord-log`もログ本文ではなく、エラー・警告件数と
正常／異常の結果だけを送ります。

Threads公式API検索後の相対トレンド分析は、検索語、取得件数、
代表的な公開投稿（最大3件）、上位トピック、公式・報道との照合結果を
1件のDiscord通知にまとめます。アクセストークン、ユーザー識別ハッシュ、
生レスポンス、内部ログは送信しません。同じ検索結果の再分析だけでは
再通知せず、新しい検索実行が保存された場合だけ通知します。

```dotenv
THREADS_DISCORD_RESEARCH_ENABLED=true
```

Send a manual connection test:

```powershell
.\.venv\Scripts\python.exe local_bot.py discord-test
.\.venv\Scripts\python.exe local_bot.py discord-log --source bot --lines 40
.\.venv\Scripts\python.exe local_bot.py discord-log --source attempts --lines 20
```

note draftは通常ログとは別のWebhookへ通知できます。Webhook URLは`.env`の
`NOTE_DRAFT_DISCORD_WEBHOOK_URL`だけに保存し、GitHubへコミットしません。

```dotenv
NOTE_DRAFT_DISCORD_ENABLED=true
NOTE_DRAFT_DISCORD_WEBHOOK_URL=
NOTE_DRAFT_DISCORD_WEBHOOK_USERNAME=久世ゆい note Bot
```

接続テストは次のコマンドで実行します。記事作成や公開は行いません。

```powershell
.\.venv\Scripts\python.exe local_bot.py discord-note-draft-test
```
# 2026-07-24 起動・xAI台帳差分

- 自動起動は `PoliticsNarrativeBot` へ統一しました。
- `production\register_task.ps1` は登録のみ行い、自動開始しません。
- xAI費用の正本はSQLite `xai_usage_events` です。
- `XAI_COST_LEDGER_VERIFIED=false` の間も、xAI実効月額上限は5ドルです。
- xAIは原則06:00・12:00・18:00、低変動日は06:00・18:00に実行します。
- `xai-roi` と `openai-usage-breakdown` で費用対効果と用途別費用を確認できます。
- 詳細は `AUDIT_BUDGET_STARTUP_XAI.md` を参照してください。
# API予算 💰

現行の月額API予算は、OpenAI `$15`、xAI `$5`、X API `$16`、
合計 `$36` です。reserve `$2` は総額に追加せず、36ドル内の保留額として扱うため、
通常の実効利用可能額は `$34` です。

円表示は固定5,000円ではなく、`TOTAL_MONTHLY_API_BUDGET_USD` と
`BUDGET_USD_JPY_RATE` から動的に計算します。標準レート165円では5,940円です。

xAIは `XAI_COST_LEDGER_VERIFIED=false` の場合も
`XAI_UNVERIFIED_EFFECTIVE_LIMIT_USD=5.0`を適用し、実効上限を5ドルにします。
台帳検証失敗は引き続き警告として表示し、RSS・公式情報へのフォールバックを維持します。

予算ステージは85%で警告、93%で補助機能を順次縮小、100%で新規の有料API処理を
停止します。RSS・公式情報のローカル監視と既存キャッシュは継続します。
# 無料note原稿パイプライン 📝

政治・制度解説を通常週2本、強い勝ちテーマがある週は最大3本まで候補生成し、`outputs/note`へMarkdownで保存できます。記事末尾は一次資料2〜5件と関連書籍候補0〜3件に対応し、関連書籍はISBN確認済みカタログから関連性スコア7.0以上の候補を選定します。初期設定の`manual`モードでは架空のAmazonリンクを作らず、`AMAZON_LINK_PENDING:`プレースホルダーを置きます。人がSiteStripe等で作成したアソシエイトリンクを登録し、開示文とリンクの確認が済むまで承認を止められます。同時に、記事タイトル入りの見出し画像`cover.png`を1280×670 px（1.91:1）でローカル生成します。合格原稿は専用Discordへ結果概要、見出し画像、確認用ファイルを通知しますが、noteへの公開は必ず人が行います。

```powershell
.\.venv\Scripts\python.exe local_bot.py generate-free-note --dry-run
.\.venv\Scripts\python.exe local_bot.py note-drafts
.\.venv\Scripts\python.exe local_bot.py note-pipeline-status
.\.venv\Scripts\python.exe local_bot.py note-generate-cover --content-id note-YYYYMMDD-001
.\.venv\Scripts\python.exe local_bot.py amazon-links-status
.\.venv\Scripts\python.exe local_bot.py amazon-link-set --content-id note-YYYYMMDD-001 --item-id amazon-001 --url "https://www.amazon.co.jp/..."
.\.venv\Scripts\python.exe local_bot.py import-amazon-links --file .\amazon-links.csv
.\.venv\Scripts\python.exe local_bot.py amazon-links-disable --content-id note-YYYYMMDD-001
```

詳しい運用手順、安全条件、CSV形式、Creators APIへの移行方針は
[`docs/AMAZON_ASSOCIATE_NOTE_WORKFLOW.md`](docs/AMAZON_ASSOCIATE_NOTE_WORKFLOW.md)
を参照してください。

## Meta公式Threads API連携 🧵

Threads連携は、公式Graph APIだけを使うプレビュー優先設計です。初期値は
`THREADS_POST_ENABLED=false` で、Xの検証済みトピックを会話向けに再構成した
下書きだけを保存します。返信・引用・再投稿・いいね・フォロー・プロフィール変更は
自動化しません。

- セットアップ: [`docs/THREADS_API_SETUP.md`](docs/THREADS_API_SETUP.md)
- 運用・障害対応: [`docs/THREADS_OPERATION.md`](docs/THREADS_OPERATION.md)
- 状態確認: `python local_bot.py threads-status`
- 安全な確認: `python local_bot.py threads-generate --dry-run`
- OAuth公開URL確認: `python local_bot.py threads-endpoints`
- OAuth callback自動起動登録: `powershell -ExecutionPolicy Bypass -File production/register_threads_oauth_task.ps1`
- OAuth callbackタスク確認: `powershell -ExecutionPolicy Bypass -File production/threads_oauth_status.ps1`
- Threads自動投稿を安全確認後に有効化: `powershell -ExecutionPolicy Bypass -File production/enable_threads_automation.ps1`
- Threads自動投稿を緊急停止: `powershell -ExecutionPolicy Bypass -File production/threads_stop.ps1`

OAuth callback、Deauthorize、Data DeletionはWaitressでローカル待受し、
固定ホスト名のHTTPSリバースプロキシを通して公開します。OAuth stateは
一回限りで、Metaの解除・削除要求はHMAC-SHA256署名を検証します。

設定、承認CLI、Windowsタスク登録、ロールバックは[OPERATIONS_FREE_NOTE_DISCORD.md](OPERATIONS_FREE_NOTE_DISCORD.md)を参照してください。
