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

既存の `xai_discovery_*`、`threads_search_*`、`news_candidates`、
`published_posts` は削除・置換しない。

## 投稿までの流れ

1. 公式情報・RSS候補を取得する。
2. xAI X Searchの論点を、検証済み候補へ意味的に関連付ける。
3. 利用可能なThreads検索結果を同じ論点へ関連付ける。
4. 統合結果と根拠をSQLiteへ保存する。
5. 根拠数、情報源数、確信度、前回からの進展を判定する。
6. 合格した最大1件を通常のニュース候補へ追加する。
7. 通常投稿パイプラインが品質、BANリスク、予算、投稿間隔、
   1日上限、72時間の意味的重複を再検査する。
8. Xで公開された後、既存のThreads生成・投稿処理へ渡す。

統合処理は投稿枠を追加しない。通常投稿上限の内数として扱う。

## 投稿ゲート

既定値:

```dotenv
INTEGRATED_RESEARCH_ENABLED=true
INTEGRATED_RESEARCH_POST_ENABLED=true
INTEGRATED_RESEARCH_MIN_SOURCE_FAMILIES=2
INTEGRATED_RESEARCH_MIN_EVIDENCE=2
INTEGRATED_RESEARCH_MIN_CONFIDENCE=0.65
INTEGRATED_RESEARCH_UNCHANGED_SIMILARITY=0.82
INTEGRATED_RESEARCH_MAX_POST_CANDIDATES_PER_RUN=1
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

表示内容は、機能の有効状態、3テーブルの件数、最新実行状態。

## 安全な停止

分析投稿だけ止め、リサーチDBを継続する:

```dotenv
INTEGRATED_RESEARCH_ENABLED=true
INTEGRATED_RESEARCH_POST_ENABLED=false
```

`data/`はGit管理対象外。検索結果、Post ID、SQLiteはGitHubへ公開しない。
