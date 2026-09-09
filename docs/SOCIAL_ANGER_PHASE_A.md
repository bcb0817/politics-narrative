# 「社会の怒りを届ける」段階導入ガイド 🧭

## 目的

生活者が抱く不公平感を煽るのではなく、確認可能な負担、意思決定、
受益、責任、説明不足へ分解し、改善可能な制度・組織課題へ変換します。

Phase Aは候補生成、Phase Bは既存候補とのシャドー比較です。Phase Cでは、
制度・予算・行政プロセスなどの低リスク対象に限り、既存のX・Threads
投稿生成へ接続できます。Phase Dは個人行為を含む拡張段階ですが、既定では
無効です。投稿そのものは既存の安全判定、投稿間隔、日次上限、API予算を
必ず通り、社会的怒りの判定だけで投稿を強制しません。

## 処理

1. 既存の確認済みニュースまたはテストフィクスチャを取得
2. 怒りの原因を20分類
3. 当事者、負担者、受益者、決定者、責任主体を分離
4. 妥当性8指標と危険性4指標を採点
5. 重要案件は事実・負担・責任・構造・改善の5角度を生成
6. 属性攻撃、扇動、無根拠な一般化・意図推測・強い断定を検査
7. XとThreadsの候補、Short、記事、note候補をローカル保存
8. 批判候補と改善候補の接続率、対象集中度を集計

## 投稿候補の最低条件

- 実効スコア7.0以上
- evidence strength 8.0以上
- public harm 6.5以上
- constructive value 6.5以上
- anger exploitation risk 3以下
- mob targeting risk 2以下
- defamation risk 2以下
- oversimplification risk 3以下

90分救済へ将来接続する場合も、この怒り関連の安全基準は緩和しません。

## 重大事案

- phase_0: 候補生成禁止
- phase_1: 公共安全情報のみ（本モジュールの怒り候補は禁止）
- phase_2: 確認済み事実角度のみ
- phase_3以降: 原因・組織要因・責任・再発防止候補を許可

被害者数、映像、悲鳴、残虐性をフックにしません。

## CLI

```powershell
.\.venv\Scripts\python.exe local_bot.py social-anger-status
.\.venv\Scripts\python.exe local_bot.py social-anger-assess --dry-run
.\.venv\Scripts\python.exe local_bot.py social-anger-candidates --dry-run
.\.venv\Scripts\python.exe local_bot.py social-anger-targets
.\.venv\Scripts\python.exe local_bot.py social-anger-risk-report
.\.venv\Scripts\python.exe local_bot.py social-anger-solution-gaps
.\.venv\Scripts\python.exe local_bot.py social-anger-daily-report --dry-run
.\.venv\Scripts\python.exe local_bot.py social-anger-weekly-report --dry-run
.\.venv\Scripts\python.exe local_bot.py social-anger-full-cycle --dry-run
```

## Phase B/Cの有効化手順

- Phase B: `SOCIAL_ANGER_PRODUCTION_PHASE=B`（シャドー比較のみ）
- Phase C: `SOCIAL_ANGER_PRODUCTION_ENABLED=true` と
  `SOCIAL_ANGER_PRODUCTION_PHASE=C`（低リスク対象だけ接続）
- 重要テーマは最大5案、通常テーマは最大3案を比較し、投稿は1案だけ
- 事件・災害・司法などは、原因調査前に責任追及型へ移行しない

## ロールバック

機能全体を止めるなら `SOCIAL_ANGER_CONCEPT_ENABLED=false`、本番接続だけを
止めるなら `SOCIAL_ANGER_PRODUCTION_ENABLED=false` にします。コードを戻す場合は
`src/social_anger.py`、本書、プロフィール候補、CLI差分、環境変数例、
専用テストだけを対象にします。既存DB表は残しても本番へ影響しません。
