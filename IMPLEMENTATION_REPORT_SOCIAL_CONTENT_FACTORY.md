# ソーシャル・コンテンツ工場 Phase A 実装報告

作成日: 2026-07-26
対象: `D:\SNS Bot\politics-narrative`

## 1. バックアップ先

`D:\SNS Bot\politics-narrative\backups\politics-narrative-backup-social-factory-20260726-130450`

バックアップ自身の再帰コピーを避けるため `backups` だけを除外し、17,407ファイル、
約153.38 MBを保存した。

## 2. 停止したタスク

なし。調査・実装・dry-runのための停止や再起動は不要と判断した。

## 3. 現行仕様の調査結果

- Xは05:00〜23:00の毎時監視、1実行1件、通常上限8件、総上限10件。
- Threadsは投稿ON、08:30・13:00・20:30、上限3件、180分間隔。
- X/Threads、note、動画、Discord、SQLite、予算管理は既存モジュールとして分離済み。
- 稼働中は `PoliticsNarrativeBot` と `PoliticsNarrativeThreadsOAuth`。
- X daemon PID系統は PowerShell 7732 → Python 15212 → Python 15228。
- Threads OAuth PID系統は PowerShell 7692 → Python 15072 → Python 15096。

## 4. 主な問題点

- 1ニュース1案を前提とし、論点・主張・媒体別候補の在庫が不足。
- topicとclaimが十分に分離されておらず、別角度の検証回数が不足。
- 15分・6時間の測定窓、Short/記事への早期昇格が未整備。
- persona、Threads設定、README、Gmail記述に仕様上の曖昧さがあった。
- 新しい投稿量を本番へ入れる前の候補生産・費用試算・段階導入手段がなかった。

## 5. 設定不整合の修正内容

- personaを `EMOJI_MODE=selective`、最大1個、重大事案は原則なしへ統一。
- `THREADS_POST_ENABLED` を定期公開スイッチと明記。
- `THREADS_AUTO_PUBLISH_TEXT=false` を「コンテナ作成後に明示publishする安全なAPI方式」と明記。
- READMEのX専用表記をX/Threads対応へ修正。
- Gmail転送は未実装であることを明記。
- 優先順位を `.env → typed configuration → runtime status → audit → docs` に統一。
- `config-audit` の本番値監査結果は不整合0件、文書不整合0件。

## 6. 変更ファイル一覧

`.env.example`、`OPERATIONS_FREE_NOTE_DISCORD.md`、`README.md`、
`config/bot_persona.md`、`docs/AMAZON_ASSOCIATE_NOTE_WORKFLOW.md`、
`local_bot.py`、`src/free_note.py`、`src/post_metrics.py`、
`src/threads_api.py`、`src/threads_full_api.py`、
`tests/test_free_note.py`、`tests/test_phases_1_3.py`、
`tests/test_threads_api.py`、`tests/test_threads_full_api.py`。

## 7. 新規ファイル一覧

- `IMPLEMENTATION_PLAN_SOCIAL_CONTENT_FACTORY.md`
- `IMPLEMENTATION_REPORT_SOCIAL_CONTENT_FACTORY.md`
- `config/social_content_sources.json`
- `docs/SOCIAL_CONTENT_FACTORY.md`
- `production/register_social_growth_tasks.ps1`
- `production/social_growth_start.ps1`
- `production/social_growth_stop.ps1`
- `production/social_growth_status.ps1`
- `src/runtime_config.py`
- `src/social_content_factory.py`
- `tests/test_social_content_factory.py`

## 8. コンテンツパケット構造

content_id、topic_key、source_news_ids、primary_sources、verified_facts、
main_event、stakeholders、financial_impact、legal_or_policy_impact、
supporting/opposing arguments、misunderstandings、reader_questions、
content/visual/short angles、longform/note/X article potentialを保持する。

## 9. topic、claim、angle管理

- テーマ、主張、角度を別テーブルへ保存。
- `(topic_key, claim_key)` と `(content_id, claim_key, content_angle)` を一意化。
- 同一事実の言い換えではなく、異なる質問へ答える候補を生成。
- 在庫選定では未確認、高リスク、期限切れ、同一主張、同一人物への批判集中を除外。

## 10. X候補供給量

dry-runで14件を供給。コード上は目標10〜14件、hard max 16件に対応するが、
Phase Aでは現行本番上限を変更していない。

## 11. Threads候補供給量

dry-runで6件を供給。x_reuse、threads_native、reply_driven、evergreen、
video_promotionを区別する。Phase Aでは現行本番上限を変更していない。

## 12. 投稿形式

X候補はtext、visual、video、thread、reply/quoteを持つ。比率は週次目標として
`.env.example` へ追加した。候補は公開権限を持たず、1回最大2件かつ30分差で計画する。

## 13. 図解生成

2件生成可能。X 16:9、Threads 4:5、動画9:16のbriefを保存する。
HTML/CSS/SVG/Pillowを優先し、数字には一次資料照合フラグを必須とした。

## 14. スレッド生成

3件生成可能。各候補は結論、事実、争点、賛否、今後の確認点からなる3〜5投稿。
自動公開はOFF。

## 15. 返信、引用候補

低リスク返信5件、公式一次資料の引用5件をローカル承認キューへ保存可能。
人物意見の批判引用、自動返信、自動引用はOFF。

## 16. Evergreen在庫

税、社会保険、予算、国債、選挙、国会、内閣、地方自治、安全保障、
エネルギー、外国人政策、司法、行政監視、企業統治など32件を構築。

## 17. 新スコアリング

テーマ価値は需要25%、公共性20%、新規性15%、動画化15%、議論10%、
一次資料10%、persona適合5%。投稿品質は正確性30%、フック20%、
明瞭さ20%、独自論点15%、会話性10%、安全性5%。

## 18. 投稿タイプ別閾値

breaking 7.5、news_explainer 7.0、conversation 6.5、evergreen 7.0、
visual 7.0、thread 7.5、short 8.0、longform 8.5。

## 19. 測定窓

X/Threadsに15分、1時間、6時間、24時間、72時間を実装。
Threads full APIは任意の7日窓も維持する。

## 20. Short昇格条件

相対views、返信率、共有系指標、質問3件、媒体横断反応、図解適性、
明確な数字、60秒1論点、長尺余地、一次資料の10条件中3件以上。

## 21. X記事、note、長尺昇格条件

質問3件以上、論点6件以上、一次資料2件以上を最低条件とし、
短文で不足するテーマを記事・note・長尺候補へ保存する。

## 22. 情報源追加

- e-GovパブリックコメントRSS: 有効。
- 国会会議録検索API: 有効。
- EDINET API: キー登録が必要なため無効の準備状態。
- 日本銀行: 正確な対象フィード確定まで無効の準備状態。
- Phase A dry-runではネットワーク取得を行わない。

## 23. SNS需要発見

Threadsトレンド・返信分析と既存xAI結果を需要シグナルとして集約。
`verified_fact=0` を固定し、SNS上の話題性から事実を生成しない。

## 24. SQLiteマイグレーション

要求された23テーブルを追加的かつ再実行可能に作成した。
既存テーブルやデータを削除・再作成していない。

## 25. CLI一覧

config-audit、content-packet-generate、content-inventory-status/build、
content-variants-generate、content-hypotheses、growth-status、
growth-daily/weekly-report、short/article/visual/thread/reply/quote-candidates、
source-health、growth-budget-simulation、growth-full-cycle。

## 26. Windowsタスク用スクリプト

登録、start、stop、statusの4本を追加した。登録は `-Apply` 必須で、
既定はWhatIf。今回、新規タスクは登録・起動していない。

## 27. 追加環境変数

X/Threads候補量、hard max、形式比率、主張別冷却、最大2候補、
返信・引用・スレッドの安全スイッチ、各候補目標、Phase B/C予算例を
`.env.example` のみに追加した。

## 28. 予算シミュレーション

14 X、6 Threads、2図解、5 Short候補、30日で実行。
本番予算値や利用実績は変更していない。

## 29. API費用予測

OpenAI 3.21ドル、xAI 1.80ドル、X API 1.05ドル、画像/TTS 0.45ドル、
合計6.51ドル。候補生成単価0.0054ドル、公開投稿相当単価0.0071ドル、
Short候補単価0.0090ドル。36ドル枠内で超過予測日はなし。

## 30. 既存テスト結果

全体714テストが合格。既存機能テストを含む。

## 31. 新規テスト結果

`test_social_content_factory.py` の新規55テストが合格。

## 32. dry-runで使用したテーマ

直近の公式ニュース候補15テーマに加え、企業統治、行政監視、司法制度など
32件のEvergreenを使用した。代表例は適時開示制度、EDINET、情報公開制度、
パブリックコメント、刑事裁判と適正手続。

## 33. 生成したコンテンツ角度

速報、変更、受益者、負担、財源、制度問題、歴史比較、国際比較、支持、
反対、編集評価、誤解、Short、長尺、X記事、noteの16角度。

## 34. 生成したX候補数

利用可能候補14件。

## 35. 生成したThreads候補数

利用可能候補6件。

## 36. 生成した図解候補数

2件。

## 37. 生成したShort候補数

5件。

## 38. 生成した記事候補数

2件。各候補の一次資料は2件。

## 39. 外部投稿がなかったこと

dry-run結果はX 0、Threads 0、動画 0、note 0、外部write 0。

## 40. 本番.envを変更していないこと

実装前バックアップと実装後 `.env` のSHA-256は一致した。

## 41. Phase B有効化手順

人間レビュー後に `.env.example` から必要項目だけを `.env` へ移し、
まずX 10〜12、Threads 3〜4、画像1日1件、スレッド週2〜3件、
返信・引用1日2件を設定する。`config-audit`、dry-run、費用試算、
24〜72時間の監視を経て個別スイッチを有効化する。

## 42. Phase C有効化手順

Phase Bの品質・BANリスク・費用・重複率が基準内の週だけ、
X 10〜14、Threads 4〜6、Short候補3〜5へ段階拡大する。
クロス投稿は媒体ごとの認証・公開テスト後に別々に有効化する。

## 43. 投稿数を増やす際の監視項目

BANリスク、削除/訂正、重複、同一人物批判集中、反応の相対値、
返信感情、Short転用率、媒体横断勝率、API費用、投稿間隔、
一次資料充足率を15分・1時間・6時間・24時間・72時間で監視する。

## 44. 緊急停止方法

新成長タスクだけなら
`powershell -ExecutionPolicy Bypass -File production\social_growth_stop.ps1`。
既存Botは対象タスクを特定してから個別停止し、Python一括停止は行わない。

## 45. Bot再起動コマンド

今回実行していない。必要時は管理者PowerShellで
`Restart-ScheduledTask -TaskName PoliticsNarrativeBot`。
Threads OAuthは必要な場合だけ
`Restart-ScheduledTask -TaskName PoliticsNarrativeThreadsOAuth`。

## 46. ロールバック方法

1. 対象タスクだけ停止。
2. 現在のruntimeデータを別場所へ保全。
3. 上記バックアップからソースを復元。
4. `.env` のハッシュと秘密情報を確認。
5. compileallと既存テストを実行。
6. 対象タスクだけ再開。

Gitでは本変更コミットのrevertも可能だが、既存データを削除する
SQLiteダウングレードは行わない。

## 47. 残存リスク

- Phase Aは候補生産であり、新しい投稿量の実運用実績はまだない。
- SNS需要シグナルはサンプル偏りがあり、事実確認には使用できない。
- EDINETはAPIキー、日銀は利用する個別フィードの確定が必要。
- 図解はbriefまでで、最終レンダリングと視認性確認が必要。
- 返信・引用・スレッドは人間承認で安全性を検証する必要がある。
- 費用予測は設定単価による試算で、実請求を保証しない。
- 稼働中プロセスは再起動していないため、変更コードは次回の通常再起動まで
  既存daemonへ反映されない。
