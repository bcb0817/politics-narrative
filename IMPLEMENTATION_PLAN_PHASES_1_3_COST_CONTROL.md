# Phase 1〜3・コスト制御 実装計画 📋

## 既存コード調査結果

- `local_bot.py`: once/force/daemon/status/reportを提供。04:45の日次レビューをデーモンへ統合済み。
- `src/news.py`: RSS 7系統とX Recent Searchを実装。ただし検索は投稿スロット連動で、1日3回制御・予算予約は未実装。
- `src/post.py`: Structured Outputs、品質ゲート、重複・topic cooldown、X投稿、JSON履歴を実装。返信スレッド経路が残存。
- `src/model_router.py`: 通常mini、重要Luna、日次Luna、週次TerraのルーティングとOpenAI予算ガードを実装済み。
- `src/openai_usage.py`: OpenAIのJSON使用量・月別JSONLを記録。SQLiteと合算予算は未実装。
- `src/report_ai.py`: 日次・週次向けの限定入力LLM分析を実装済み。
- `src/publishing_policy.py`: 投稿間隔、日次上限、topic cooldown、重大続報判定、分類をローカル実装。
- 状態は `data/*.json`、ログは `logs/*.log/jsonl`。SQLiteは未使用。
- 画像生成・画像添付は廃止済み。出典返信専用処理はないが、一般スレッド返信経路は残存。
- Windowsタスクは本体と日次レビューが存在。メトリクス・週次レビューの専用登録は未実装。
- テストは49件。ニュース選定、安全性、X注目度、モデルルーティングをカバー。

## 実装方針

1. 標準`sqlite3`でWAL・busy timeout付き共通DBを作り、障害時はJSON運用を継続する。
2. API呼び出し前に`BEGIN IMMEDIATE`で最大予想額を予約し、完了時に実額へ更新する。
3. RSS監視は30分、X Searchは06:00/12:00/18:00だけ更新し、その他は6時間キャッシュを使う。
4. 投稿は通常最大4、重大速報追加2、合計6、間隔60分、topic cooldown 4時間にする。
5. URL・内部ラベル・関連性・未確認情報・重複・BANリスクをローカルで遮断する。
6. nano分類はローカル判定が曖昧な場合のみ、1日8回まで利用する。
7. 拡張案は`outputs/previews/`へ保存するだけで、自動投稿経路を持たせない。
8. 1h/24h/72h指標を各窓1回だけ取得し、SQLiteへ保存する。
9. 日次Luna・週次Terraで分析し、失敗・予算不足時は段階降格またはローカル集計へ移行する。
10. 既存JSONは削除せず、移行期間はSQLiteと二重書き込みする。

## コスト停止順序

拡張案 → 週次LLM → 日次LLM → X Search → 通常投稿 → 重大速報投稿。RSS監視とローカル集計は常に継続する。

## 安全な完了条件 🔒

構文検査、単体テスト、投稿無効dry-run、日次・週次モック試験、DB・予算表示を確認する。本番タスクは停止したまま人間の確認を待つ。
