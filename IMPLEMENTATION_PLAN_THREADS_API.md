# Meta公式Threads API連携 実装計画 🧵

作成日: 2026-07-25

## 現行コード調査

- `src/news.py`がRSS・公式情報・xAIレーダーを統合し、確認済み候補をSQLiteへ保存する。
- `src/post.py`が確認済みニュースからX専用Structured Outputを生成し、品質・重複・BANリスクを検査する。
- `published_posts`、`generated_posts`、`news_candidates`を結合すると、公開済みX投稿と元の確認済みニュースを追跡できる。
- `src/model_router.py`、`src/openai_usage.py`、`src/api_budget.py`がOpenAIモデル選択、費用記録、月額予算予約を担当する。
- `src/metrics_db.py`は`CREATE TABLE IF NOT EXISTS`と加算カラム方式で既存データを保持する。
- `src/post_metrics.py`がX投稿の1h・24h・72h指標を重複なしで回収する。
- `local_bot.py`の日次レビューはSQLiteの実績をローカル集計した後、必要時だけOpenAI分析を行う。
- `production/register_*.ps1`はタスク登録と確認を行い、登録時に勝手に開始しない。
- 現時点でThreads API、Threads OAuth、Threads投稿、Threads Insights専用コードは存在しない。
- 共通の独立した`content_id`はX投稿にはないため、Threadsでは`x-<tweet_id>`を共通IDとして保存する。

## Meta公式仕様の確認結果

- 投稿はThreads User Access Tokenを使用する。
- テキスト投稿は`POST /{threads-user-id}/threads`でコンテナを作り、
  `POST /{threads-user-id}/threads_publish`で公開する。
- `reply_control`は`everyone`、`accounts_you_follow`、`mentioned_only`に対応する。
- Insightsは`/{threads-media-id}/insights`から`views`、`likes`、`replies`、
  `reposts`、`quotes`、`shares`を取得できる。
- Threads本文の現行上限は500文字のため、`THREADS_PLATFORM_LIMIT_CHARS=500`を維持する。

## 実装方針

1. Phase Aを初期状態とし、`THREADS_POST_ENABLED=false`を維持する。
2. X本文を再利用せず、同じ確認済みニュースからThreads専用文を別生成する。
3. OAuth URL、コード交換、長期トークン交換、プロフィール確認、期限前更新をCLI化する。
4. トークン本体とApp Secretは`.env`だけへ保存し、SQLite・JSON・ログ・CLI表示へ出さない。
5. 投稿はコンテナ作成と公開の2段階に分け、状態を各段階で保存する。
6. タイムアウト等で結果不明の場合は`ambiguous`とし、自動再投稿しない。
7. 1日最大3件、180分間隔、topic 8時間クールダウン、X本文との類似度0.80以下を適用する。
8. Insightsは1h・24h・72hを一意制約付きで保存し、欠損指標は`null`のまま許容する。
9. Threads障害・トークン更新失敗はThreadsだけを停止し、X BotとRSS監視へ波及させない。
10. WindowsタスクはX Botから分離した登録用スクリプトだけを作り、登録・開始は実行しない。

## 安全条件

- Metaアプリ作成、App Review申請、プロフィール変更を自動化しない。
- ブラウザ自動操作、Cookie流用、非公式API、返信・引用・再投稿・いいね・フォローを実装しない。
- dry-runではOpenAI、Threads投稿、X投稿、Discord送信を行わず、ローカル生成と保存だけを検証する。
- 本番Threads投稿とWindowsタスク登録は人間がPhase B以降へ進めるまで行わない。
