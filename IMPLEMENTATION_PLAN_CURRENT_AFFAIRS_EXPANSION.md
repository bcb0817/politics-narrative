# Current Affairs Expansion Phase A 実装計画

作成日: 2026-07-28

## A. 現在の政治専用ロジック

- `src/news.py` がRSS・公式情報を取得し、X/xAIは需要レーダーとしてのみ利用する。
- `src/post.py` が政治関連性、一次情報、品質、重複、投稿間隔、重大事案を判定する。
- `src/social_content_factory.py` がtopic、claim、angle、packet、X/Threads/Short/記事候補をローカル生成する。
- 本番X/Threads、note、動画、Discord、予算は独立した既存モジュールが担当する。

## B. そのまま維持するロジック

- X/Threadsの投稿経路、投稿数、スケジュール、品質ゲート、安全弁。
- 既存の重大事案専用処理と被害者配慮。
- SNS需要と検証済み事実の分離。
- note、動画、Discord、SQLite、API予算管理。
- `POST_ENABLED`等の外部書込スイッチと人間承認。

## C. 一般化するロジック

- 政治関連性を「制度・お金・責任・安全・生活への影響」という共通brand fitへ拡張する。
- content packetへカテゴリ、共通評価軸、寿命、対象読者、推奨媒体・形式を付加する。
- 候補選定をカテゴリ配分、政治比率下限、単一カテゴリ上限に対応させる。
- Short・記事・長尺候補をカテゴリ内相対評価と複合テーマで評価する。

## D. 新しいカテゴリ

`politics_policy`、`economy_business`、`major_incidents`、`technology_ai`、
`cybersecurity`、`society_living`、`security_defense`、
`disaster_infrastructure`をprimary categoryとする。
`governance_accountability`は原則secondary categoryとして横断付与する。

## E. カテゴリ別情報源

情報源の正本は`config/current_affairs_sources.json`とする。公式RSS、公式API、
公開フィードだけを自動取得対象にし、HTMLスクレイピング、ブラウザ自動操作、
非公式API、有料記事全文取得は行わない。報道は背景・現場・複数視点の補助であり、
一次資料の代替にしない。

## F. カテゴリ別安全基準

- 政治、経済、AI、社会は品質7.0以上かつ公式・一次資料を要求する。
- サイバーは品質7.5以上とし、攻撃手法を不要に詳細化しない。
- 重大事案は既存専用処理を優先し、通常品質スコアだけで判断しない。
- 災害は更新時刻と公共安全情報を優先する。
- 私人晒し、未確認犯罪の断定、残虐な詳細、芸能ゴシップを除外する。
- brand fit 6.5未満は原則候補化しない。

## G. カテゴリ別投稿基準

Phase Aは分類・評価・候補・在庫・レポートだけを実装し、外部投稿しない。
週次目標は政治35%、経済20%、重大事案15%、AI10%、社会10%、安全保障5%、
災害5%。通常週の政治比率は25%以上、単一カテゴリは45%以下を目安とする。
比率はノルマではなく、重大事案・災害では既存criticalルールを優先する。

## H. 移行方法

1. 加算的SQLiteマイグレーションを実行する。
2. 既存content packetを分類し、拡張packetとカテゴリ割当を保存する。
3. テストfixtureでdry-runし、カテゴリ配分・除外・安全性を検証する。
4. Phase Aでは既存投稿経路へ接続しない。
5. Phase B以降は人間レビュー後、カテゴリごとに明示的に接続する。

## I. ロールバック方法

対象タスクを特定して停止し、`backups/`内の実装前バックアップからソースを復元する。
SQLiteは加算的テーブルのため既存テーブルを削除しない。必要なら新テーブルの利用だけを
`CURRENT_AFFAIRS_EXPANSION_ENABLED=false`で停止する。`.env`、runtime data、
投稿履歴は上書きしない。検証後、対象タスクだけを人間が再起動する。
