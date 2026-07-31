# 統合リサーチDB・分析投稿 📊

## 目的

公式情報・RSSを事実確認の中心に置き、X API Search、xAI X Search、
Threads Search・Insightsを反応シグナルとして統合する。
検索結果をそのまま投稿せず、事実とSNS上の主張を分離して保存する。

## SQLite

正本は `data/bot_metrics.db`。

- `integrated_research_runs`
  - 統合処理の実行単位、対象期間、利用できたプロバイダーを保存する。
- `integrated_research_topics`
  - 論点、公式情報の要約、賛成論・反対論、確信度、進展、
    投稿可否と判断理由を保存する。
- `integrated_research_evidence`
  - 公式URL、代表X Post ID、Threads Post IDを根拠単位で保存する。
- `integrated_research_decisions`
  - 投稿候補・見送りの判断、理由、使用スコア、判断時刻を保存する。
- `integrated_research_corrections`
  - 訂正前後の事実要約と適用状態を保存する。
- `integrated_research_audits`
  - DB整合性、欠落一次資料、重複URL、削除済み根拠の監査結果を保存する。

既存の `xai_discovery_*`、`threads_search_*`、`news_candidates`、
`published_posts` は削除・置換しない。

## 投稿までの流れ

1. 公式情報・RSS候補を取得する。
2. xAI X Searchの論点を、検証済み候補へ意味的に関連付ける。
3. 利用可能なThreads検索結果を同じ論点へ関連付ける。
4. 統合結果と根拠をSQLiteへ保存する。
5. 事実・意見・推測、賛否、明示的な事実矛盾、社会的怒りを分離する。
6. 根拠数、情報源数、確信度、投稿価値、キャッシュ鮮度、
   前回からの進展を判定する。
7. 合格した最大1件を通常のニュース候補へ追加する。
8. 通常投稿パイプラインが品質、BANリスク、予算、投稿間隔、
   1日上限、72時間の意味的重複を再検査する。
9. Xで公開された後、既存のThreads生成・投稿処理へ渡す。
10. 投稿IDとX/Threads指標を結び、日次ChatGPTレビューへ渡す。

統合処理は投稿枠を追加しない。通常投稿上限の内数として扱う。

## 投稿ゲート

既定値:

```dotenv
INTEGRATED_RESEARCH_ENABLED=true
INTEGRATED_RESEARCH_POST_ENABLED=true
INTEGRATED_RESEARCH_MIN_SOURCE_FAMILIES=2
INTEGRATED_RESEARCH_MIN_EVIDENCE=2
INTEGRATED_RESEARCH_MIN_CONFIDENCE=0.65
INTEGRATED_RESEARCH_MIN_POSTING_VALUE_SCORE=6.0
INTEGRATED_RESEARCH_UNCHANGED_SIMILARITY=0.82
INTEGRATED_RESEARCH_MAX_POST_CANDIDATES_PER_RUN=1
INTEGRATED_RESEARCH_DISCORD_ENABLED=true
INTEGRATED_RESEARCH_EVIDENCE_RETENTION_DAYS=180
INTEGRATED_RESEARCH_EXPORT_DIR=outputs/integrated_research
INTEGRATED_RESEARCH_BACKFILL_AUTO=false
```

以下はDBへ保存するが投稿候補にしない。

- 公式情報・RSSによる事実の土台がない
- 情報源、根拠、確信度が基準未満
- 前回の統合結果から実質的な変化がない
- 72時間以内に同じ論点を投稿済み
- 統合分析の投稿が無効

## 表示

```powershell
.\.venv\Scripts\python.exe .\local_bot.py integrated-research-status
```

表示内容は、機能の有効状態、全統合テーブルの件数、最新実行状態。

## 分析・監査コマンド

すべてローカル処理です。`--apply`がない移行・保持期限・訂正・削除処理は
プレビューのみで、外部投稿を行いません。

```powershell
.\.venv\Scripts\python.exe .\local_bot.py integrated-research-history --limit 20
.\.venv\Scripts\python.exe .\local_bot.py integrated-research-outcomes --days 30
.\.venv\Scripts\python.exe .\local_bot.py integrated-research-source-contribution --days 30
.\.venv\Scripts\python.exe .\local_bot.py integrated-research-export --format markdown --days 30
.\.venv\Scripts\python.exe .\local_bot.py integrated-research-dashboard --days 30
.\.venv\Scripts\python.exe .\local_bot.py integrated-research-audit
.\.venv\Scripts\python.exe .\local_bot.py integrated-research-backup-check
.\.venv\Scripts\python.exe .\local_bot.py integrated-research-backfill --limit 500
.\.venv\Scripts\python.exe .\local_bot.py integrated-research-retention --days 180
.\.venv\Scripts\python.exe .\local_bot.py integrated-research-reuse --topic-id 123
.\.venv\Scripts\python.exe .\local_bot.py integrated-research-mitigations
```

訂正適用と情報源削除の墓標化も、`--apply`を明示した場合だけ反映する。
公式の事実根拠が削除されたテーマは自動的に投稿対象外となり、再検証を要求する。

## 可視化と再利用

- X SearchのDiscord通知は、次の順序で表示する。
  1. 今回の結論
  2. 検索回数・分析対象・公式照合済み件数
  3. 注目度順のトピックカード
  4. 主な見方・反対または補足の見方・代表X投稿
  5. Botの投稿判断
- Discordで一覧性を優先し、上位4トピックをカード表示する。
  全トピックは`x-search-research-report.md`として同じ通知へ添付する。
- Discordには内部プロンプト、トークン、APIキー、内部ユーザー識別子を含めない。
- 日次レビューには統合テーマ、判断理由、平均信頼度、平均投稿価値、成果指標を追加する。
- JSON、CSV、Markdownは `outputs/integrated_research/` に保存する。
- HTMLダッシュボードはローカル専用で、GitHubには保存しない。
- 検証済み一次資料があるテーマだけを、note・Shorts・長尺動画向け
  コンテンツパケットへ昇格できる。

## 外部制約

- Threadsキーワード検索はMetaの権限審査と実際のAPI応答に依存する。
  使えない場合は欠落を記録し、公式資料とxAIの照合を続ける。
- 削除済みSNS投稿や保存されなかった過去レスポンスは復元できない。
  ID・ハッシュ・削除日時だけを保持する。
- 検索結果は標本であり、世論全体の推計ではない。投稿にもその限界を明記する。
- noteはドラフト生成・通知まで。公式自動公開APIを利用していないため、人間確認後に公開する。
- YouTube・Instagram投稿は有効なOAuth権限と動画素材が必要。
  未準備時はコンテンツパケットと候補生成までで停止する。

## 安全な停止

分析投稿だけ止め、リサーチDBを継続する:

```dotenv
INTEGRATED_RESEARCH_ENABLED=true
INTEGRATED_RESEARCH_POST_ENABLED=false
```

`data/`はGit管理対象外。検索結果、Post ID、SQLiteはGitHubへ公開しない。
