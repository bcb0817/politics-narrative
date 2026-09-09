# Social Content Factory 再設計計画 🏭

## A. 現行仕様

- Xは05:00〜23:00の毎時監視、通常6〜8件、通常上限8件、速報込み10件。
- Threadsは08:30、13:00、20:30の最大3件。
- 事実確認はRSS・公式資料、SNSは補助的な注目度として利用。
- OpenAI、xAI、X API、Threads、note、動画、Discord、SQLiteの既存機能がある。
- 動画クロス投稿はPhase Aで外部自動公開OFF。

## B. 問題点

- ニュース、投稿、動画、記事が1本の再利用可能な`content_id`で結び付いていない。
- topicとclaimが分離されず、別角度でもtopic単位の冷却を受ける。
- 投稿時に候補を探すため、ニュースが弱い日は候補生産量が不足する。
- 15分・6時間の初動評価と、当日中のShort昇格がない。
- 候補生産量、企画仮説、媒体横断再利用率がKPIになっていない。
- persona、Threads公開スイッチ、README、Gmail説明に不整合があった。

## C. 廃止する仕様

- 候補生産層における「1ニュース1案」。
- topic_keyだけによる候補重複・冷却。
- 単一の総合スコアだけで全形式を評価する方式。
- Xを唯一の完成配信先とするREADME上の説明。
- `THREADS_AUTO_PUBLISH_TEXT`を定期公開スイッチと解釈する曖昧さ。

## D. 維持する仕様

- 本番`.env`、X通常上限8件・総上限10件、Threads上限3件。
- 公式情報とSNS需要シグナルの分離。
- BANリスク、未確認情報、重複、予算、安全ゲート。
- 既存SQLiteテーブル、JSON状態、note、動画、Discord、OAuth。
- Phase Aでは返信・引用・スレッド・動画・図解の自動公開を行わない。

## E. 新規実装

- typed configurationと`config-audit`。
- content packet、topic、claim、angle、inventory、hypothesis、variant。
- 32件の公式資料付きEvergreen在庫。
- X 10〜14件、Threads 4〜7件を供給可能な候補生成。
- 図解1〜2件、Xスレッド、低リスク返信、公式資料引用の承認候補。
- 15m、1h、6h、24h、72hの測定予定。
- Short 3〜5件、X記事・note・長尺候補の昇格。
- 成長日次・週次レポートと費用シミュレーション。

## F. マイグレーション

- 既存`data/bot_metrics.db`へ`CREATE TABLE IF NOT EXISTS`だけを使用する。
- 既存テーブルの削除・変更・名前変更は行わない。
- 全テーブルを`content_id`、`topic_key`、`claim_key`で関連付ける。
- dry-runでもローカル候補と検証結果は保存するが、外部送信しない。

## G. 段階導入

### Phase A

候補生産、在庫、初動測定、図解・スレッド・返信・引用・Short・記事候補だけを有効化する。本番投稿数は変更しない。

### Phase B

人間が`.env`を明示変更し、X 10〜12件、Threads 3〜4件、画像1件/日、スレッド週2〜3件、承認済み返信・引用2件/日を段階導入する。

### Phase C

X 10〜14件、Threads 4〜6件、Short候補3〜5件、Short完成1件、クロス投稿を媒体別に有効化する。

### Phase D

実測結果と予算を確認し、X最大16件、Threads最大7件、Short最大2件へ最適化する。

## H. ロールバック

1. Social Growthタスクを停止する。
2. `SOCIAL_CONTENT_FACTORY_PHASE=disabled`相当として新CLIを実行しない。
3. Gitコミットをrevertする。
4. 必要な場合だけバックアップからコードを復元する。
5. 加算テーブルは履歴保全のため削除しない。既存投稿系は新テーブルに依存しない。

バックアップ：

`D:\SNS Bot\politics-narrative\backups\politics-narrative-backup-social-factory-20260726-130450`
