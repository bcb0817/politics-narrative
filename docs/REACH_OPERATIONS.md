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
3実験（冒頭・説明順・形式選定）は異なる候補へ振り分ける。候補のURLごとに割当を固定し、テーマ由来の
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

上記はv1の適用記録。v2で未実装だった項目を本番経路へ追加した。
現在の動作、切り替え、外部条件は次節を参照。成果の増加は未観測。

## v2: 形式配分・学習・統合集計

既存daemonの毎時10分・40分の非投稿イベントから既存collect-metricsを実行。
24h取得を先に行い、事故監査、暦日取得、集計、日次学習へ進む。
収集・投稿の有効設定とは独立して計測を行う。新たな投稿ジョブや承認は追加しない。

- 形式選定実験だけで6形式を選ぶ。本文素材の適合条件に合う形式に限定し、
  treatmentの20%を均等探索、残りは過去の実績と最近の使用数で重み付け。
  決定をURL単位で保存し、適格形式・選択確率・探索フラグ・モデル版を記録。
  controlと冒頭/説明順の実験にはこの形式指示を付けず、同一ニュースの再投稿もしない。
- 説明価値の順位値は数字・条件・時点・出典リンクの有無による構造的代理指標。
  真偽や品質を測定した点数ではない。欠けている需要はnullのまま。
- `learning.enabled`で自動学習を切り替える。学習60件・時間順holdout20件以上、
  7日・5テーマ以上、欠測35%以下が必要。対数変換と上側クリップ、ridge縮小を使い、
  holdout誤差が定数予測より5%以上改善した場合だけ初期重みへ最大20%混合する。
  形式別実績はジャンル・時刻・鮮度で層別化し、形式30件・7日・5テーマ以上でのみ使用。
  形式重みは0.67〜1.5倍。探索は残す。安全閾値の学習・全面採用はしない。
- 投稿時に本文特徴、URL、ハッシュタグ、段落、媒体、ニュース年齢、投稿間隔、
  テーマ連投、冒頭の完全反復、フォロワー規模、設定版を保存。
  不明な媒体や公開日時は推定で埋めない。既存投稿は読取時に復元可能な特徴だけ抽出。
- 全特徴の集計と、ジャンル・6時間帯・鮮度12h帯・フォロワー50人帯を合わせた
  比較を出力。各セル5件未満は比較しない。複数要因で条件を揃えても因果とは呼ばない。
- 訂正、実際の同文重複、結果不明、予算超過、API障害、スキップ理由を冪等に集計。
  品質維持と技術的機会損失を分離。未解決の公開事故は実験・新しい学習更新を停止する。
  既存の解決証跡があればambiguousを解決済みとして反映する。投稿再送はしない。
- 費用予約へ候補キーを付け、公開ID・送信キー・計測IDに結合する。
  既存動画IDと研究要約run_idも、それぞれの公開台帳へ結合する。
  複数投稿の読取費は均等配賦という計算規則を明記。
  共通取得費と未投稿候補費、実行中予約、料金見積、確認済み実費を別々に表示。
  xAI専用台帳と共通台帳のミラーを二重計上しない。過去の帰属キーは捏造しない。

動画・災害・研究要約・送信台帳で確認済みのX投稿IDが共通投稿表にない場合は、
確認から10分以上経ってから冪等に取り込み、同じ24h計測の対象へ含める。
結果不明は取り込まない。本文を回収できない動画等は本文不明のままで、タイトルを
投稿本文として捏造しない。これは計測台帳の同期であり投稿操作ではない。

SQLiteの追加テーブルはreach_features/reach_models/reach_incidents/reach_daily_posts/
reach_daily_runs/reach_runtime_snapshots/reach_collection_runs。既存テーブルは削除しない。
集計先は引き続きdata/reach_report_latest.json。学習モデルはDB内に保存し、
投稿候補のconfig_jsonに使用版を残す。API秘密値は設定スナップショットに含めない。

## 暦日Analyticsとフォロワーの取得条件

[公式Posts Analytics](https://docs.x.com/x-api/posts/get-post-analytics)に従い、
GET /2/tweets/analyticsを使用。JSTの前日00:00〜翌00:00をUTCへ変換し、
granularity=totalで指定区間そのものを取得する。累積値の差分や24h投稿群合計で代用しない。
[公式料金案内](https://docs.x.com/x-api/getting-started/pricing)と実際の契約料金を確認してから使う。

安全な有効化条件（秘密値は.envだけに設定し、コミットしない）:

- X_ANALYTICS_ENTITLED=true: 対象エンドポイントの契約権限を確認済み。
- X_ANALYTICS_OAUTH2_ACCESS_TOKEN: 認可されたOAuth 2.0アクセストークン。
- X_ANALYTICS_PRICE_PER_POST_USD: 契約上の1投稿リソース当たり最大費用。
- X_ANALYTICS_PRICE_VERIFIED=true: 上の費用を確認済み。

現在はこれらが未設定。取得コードは本番へ接続するが、資格不足を記録して外部呼出しを
しない。料金0を仮定せず、既存OAuth 1.0秘密値を別方式のトークンとして流用しない。
権限不足401/403では当日中の再試行を止め、課金不明なら最大予約を保持。
24h用に20リソースを優先確保し、暦日取得は残予算内・最大20IDずつ。

`calendar_analytics.account_inventory_complete`は既定false。
全アカウント投稿の在庫を証拠で確認できた場合だけtrueにする。
未収録投稿があるか不明な場合はknown_posts_calendar_impressionsだけを表示し、
アカウント暦日総表示数はnull。APIの省略されたID・項目は0にせず欠測とする。

既存のフォロワー取得を再利用し、両日境界±10分以内のスナップショットの差を
observed_follower_changeと実際の取得区間で表示。厳密なJST日界と一致しない値を
daily_net_followersと呼ばない。欠けたフォロワー数を0へ変換する旧処理も修正。
日次費用/1,000表示は暦日総数と費用の条件が揃う場合のみ計算し、
見積費用による値と確認済み実費の値を別フィールドにする。

## v2の停止・復旧と検証

学習だけ戻すにはlearning.enabled=false。企画全体はREACH_POLICY_ENABLED=false。
暦日APIだけ止めるにはcalendar_analytics.enabled=false。
既存データを消さず設定変更後に対象daemonだけ安全に再起動する。
候補割当済みの設定版は不変。max_assignments/終了日/安全停止条件は引き続き適用。
ソース全体を戻す場合も送信台帳・予算予約を削除しない。

検証コマンドはtests/run_reach_checks.py。追加テストはtests.test_reach_completion。
既存計測・成長系はtests.test_measurement_jobs、tests.test_growth_quality_v2、
tests.test_growth_revenue_p0_p2。設定契約テストは本番.envではなく.env.exampleを読む。
任意のローカルarchiveがない新規checkoutでも、危険な旧スクリプトが本番にないことを検査する。

2026-09-07 12:18 JSTにv2で再起動。PoliticsNarrativeBotはRunning、
対象supervisorは1つ、Python親子1系統を確認。本番DBで集計を実行し、
data/reach_report_latest.jsonの保存を確認した。学習はinsufficient_data、
暦日APIはanalytics_entitlement_unconfirmed、未解決の公開事故は0件。
検証のための投稿・API資格の追加・予算増額は行っていない。
公開予定のGit状態で289テスト、未コミット試作を含む作業ツリーで153テスト成功。
バックアップ: backups/politics-narrative-backup-reach-completion-20260907-114157。
