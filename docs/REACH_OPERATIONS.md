# 自動運用・検証・復旧

通常の候補収集、選定、生成、適格性検査、投稿、計測、集計に人間承認は追加しない。
投稿許可、費用上限、契約条件、事実性、重複・公開ゲートは引き続き適用する。
根拠が足りなければ枠を空ける。未測定動画の公開を停止してもテキスト運用は継続する。

## 本番経路

PoliticsNarrativeBotの既存daemonに15分の候補収集イベントを追加。
同じ既存RSS/X/xAI収集関数を呼び、SQLite reach_inventoryへ保存。
有料取得は既存のスケジュール・共通費用上限に従う。
投稿タイミングは既存の45分間隔・5〜23時を維持し、在庫の適格候補を読む。
在庫が空なら既存収集をフォールバック実行。投稿件数目標で品質を緩和しない。
収集間隔はREACH_COLLECTION_INTERVAL_MINUTES（5〜60、既定15）、
投稿時刻は既存ACTIVE_HOURS/SLOT_INTERVAL_MINUTESで独立して管理する。

既存collect-metricsは既定で24hのみ収集する。
取得可能時間は投稿24時間後から26時間後まで。遅れた値で過去の枠を埋めない。
POST_METRIC_WINDOWSで15m,1h,6h,24h,72hを追加できるが、まず24h取得率を優先。
API件数と費用はユニーク投稿ID単位で予約し、100IDを超えるバッチを作らない。
読み取り上限が足りなければ欠測として残す。
REACH_PRIORITIZE_24H_METRICS=true（既定）では投稿ごとの重複したフォロワー取得を省き、
既存の日次取得を残す。投稿時フォロワー数を推測で埋めない。計測対象はSQLiteの公開ID。

集計はdata/reach_report_latest.json。日別投稿件数、24〜26h中央値・p75、
欠測率、形式・冒頭・時刻・曜日・文字数別集計、実験群別集計を保存。
日次総表示数は対応する実測がないのでnull。投稿群累積値の合計と混同しない。
本番の記事本文・候補在庫・メトリクス・ログ・秘密情報はGitに追加しない。

## 実験

config/reach_experiments.jsonに仮説、比較、期間、割当、上限、停止条件を記録。
2実験は異なる候補へ振り分ける。候補のURLごとに割当を固定し、テーマ由来の
hashで実験を選び、ジャンル・6時間帯内でcontrol/treatmentを交互に割り当てる。
同じ候補の両版を投稿しない。追加LLM呼出しは0、通常の生成費用内。
割当時の設定全文と公開tweet_idを保存。生成キャッシュは割当別に分離。
既存の品質・事実性・重複検査は両群へ同じように適用する。

9月14日に取得率と運用障害を確認、10月5日に28日集計を評価。
各群30件の計測を目安とし、足りなければ結論保留。最大120候補の割当または
終了日に到達すると実験指示を停止する。単発の最高値や内部スコアで全面採用しない。
形式別の相関は観測であり、テーマ・時刻・鮮度・フォロワー規模が交絡しうる。
日次投稿数が増えただけかは、投稿件数と1投稿あたり中央値/p75を分けて判定する。

## 切り替えと復旧

検証: `.venv/Scripts/python.exe tests/run_reach_checks.py`
このrunnerは本番.envを読まず、一時DBを使い、外部ネットワーク接続を拒否する。
ローカルHTTPサーバーのテストだけloopbackを許可する。送信はmock。

元の企画順位へ戻すにはREACH_POLICY_ENABLED=false。
実験だけ止める場合はconfig/reach_experiments.jsonのenabled=false。
元の収集経路へ戻すにはREACH_COLLECTION_ENABLED=false。
送信記録・厳密な計測・予算予約の安全修正は保持する。
設定変更後は進行中の送信がないことを確認し、PoliticsNarrativeBotだけを
Stop-ScheduledTask/Start-ScheduledTaskで再起動する。強制投稿コマンドは使わない。
既存の親子プロセスとdaemon.lockを確認し、孤児プロセスがあれば対象を特定してから停止。
バックアップはproduction/create_backup.ps1のみ、リポジトリ内backupsに作成する。

## 結果不明と予算

x_deliveryのsending/ambiguousは削除・タイムアウト解除・自動再送をしない。
既存の所有者タイムライン取得時に完全一致した本文と送信時刻以降の作成時刻・IDが
見つかればpublishedへ照合し、公開履歴にも補完する。古い同文投稿は照合しない。
見つからなければ保留を維持。動画はmedia IDも含むので本文だけで自動照合しない。
再送せず、認証済みの所有者側結果・課金台帳の証拠がある場合だけ運用で照合する。
API予約をゼロに戻して再送を誘発しない。

暦日総表示数、厳密なフォロワー純増、全経路の費用帰属は取得証拠が揃うまでnull。
未確認料金は最大値を保持するため、余分に停止する可能性がある。
これは実測費用とは別の予約であり、同じ費用として成果比較に使わない。

## 既存の未コミット試作との互換性

politics_news_pipeline、political_sentiment_agent、shorts_factoryへの小修正は
現在の作業ツリーへ適用済み。これらの試作全体を無関係な変更として取り込むことを避け、
差分をproduction/reach_draft_compatibility.patchに保存した。
既存試作の元版へ適用する場合は `git apply --check production/reach_draft_compatibility.patch`
が成功した場合のみ `git apply production/reach_draft_compatibility.patch` を実行。
既に適用済みなら再適用しない。未コミット試作がない通常のcheckoutでは適用不要。
確認: `.venv/Scripts/python.exe tests/run_reach_checks.py tests.test_politics_news_pipeline tests.test_political_sentiment_agent tests.test_shorts_factory`

## 適用記録と現在の範囲

2026-09-07 11:36 JSTにPoliticsNarrativeBotを再起動。タスクはRunning、
Pythonの親子1系統とdaemon.lockの保持を確認。候補在庫・編集実験は有効、
次の通常投稿判定は12:00 JST。検証のためのforce/onceや外部公開は実行していない。
Gitの公開対象だけを取り出した独立チェックアウトで264テスト成功。
未コミット試作の互換修正は別途32テスト成功。秘密情報・ランタイムはGit対象外。

今回は冒頭・説明順の2実験に限定する。6形式の辞書とexploration_fractionは
次段階の設定予約であり、6形式を自動配分する機能はまだ有効化していない。
過去成果に応じた重みの自動学習・形式の全面採用も実装していない。
説明価値の数値はnull、関連性を読者影響の代理指標として使う初期順位は仮説。
URL・段落・メディア・テーマ間隔など全特徴の多変量比較、訂正等の統合集計、
暦日表示数・費用帰属の取得は今後の拡張事項。現行レポートの取得済み値だけで
原因や改善成果を断定しない。今回の再起動後の取得成功率・成果はまだ未観測。
