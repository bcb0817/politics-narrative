# 差分改修 実装報告

対象: 政治ニュースX Bot「久世ゆい」  
実施日: 2026-07-24 JST  
基準: 作業開始時点のCodex直近実装

## 1. バックアップ先

`D:\SNS Bot\politics-narrative-backup-20260724-013738`

未コミット差分、`.env`、SQLite、JSON履歴、ログを含むリポジトリ全体を
変更前にコピーした。

## 2. 実際の起動経路

正式経路はWindowsタスク `PoliticsNarrativeBot`。
ログオンをトリガーに、非表示PowerShellで
`production\run_bot.ps1` を実行する。

旧 `PoliticsNarrativeDailyReview` は管理者所有で無効化ACLが拒否された。
タスクは停止し、呼び出し先をno-op化した。よって実レビューは正式タスク内の
04:40統合レビューだけである。

Startupの旧政治Botは `.disabled`、Runキー・PowerShellプロファイル・
Windowsサービスに該当経路はなかった。

## 3. 統一後の起動方式

- タスク: `PoliticsNarrativeBot`
- 作業フォルダ: `D:\SNS Bot\politics-narrative`
- `MultipleInstances=IgnoreNew`
- 60秒後に再起動
- `register_task.ps1` は登録のみで開始しない
- 登録確認は `Get-ScheduledTask`
- 現在状態: `Ready`

## 4. 変更ファイル

- `.env`
- `.env.example`
- `README.md`
- `local_bot.py`
- `src/api_budget.py`
- `src/metrics_db.py`
- `src/post.py`
- `src/publishing_policy.py`
- `src/quality_evals.py`
- `src/xai_radar.py`
- `production/register_task.ps1`
- `production/start.ps1`
- `production/stop.ps1`
- `production/status.ps1`
- `production/go_live.ps1`
- `production/run_daily_review.ps1`
- `tests/test_xai_engagement_style.py`

## 5. 新規ファイル

- `AUDIT_BUDGET_STARTUP_XAI.md`
- `IMPLEMENTATION_REPORT_STARTUP_XAI_LEDGER_2026-07-24.md`
- `src/usage_reports.py`
- `tests/test_startup_xai_ledger_v3.py`

文字化けした重複READMEファイル名2件は削除し、正常名の原本を維持した。

## 6. xAI台帳の不整合原因

旧実装は専用台帳と汎用台帳の両方へ同じレーダー利用を記録していた。
集計自体は汎用台帳だけだったため、表示額の二重加算はなかった。

`$1.892638` の内訳はレーダー7件 `$1.8222996` と診断3件
`$0.0703384`。失敗したレーダーが12〜15ツール呼び出しと
約16万〜32万入力トークンを消費したことが高額化の主因。

## 7. ticksからUSDへの換算

`actual_cost_usd = cost_in_usd_ticks / 10,000,000,000`

既知の診断実費と保存値を照合して一致した。

## 8. 重複加算の防止

- xAI集計の正本を `xai_usage_events` に統一
- 新規xAI利用を汎用台帳へ鏡像記録しない
- `request_id` に一意制約
- JSONLは集計対象外
- 旧汎用台帳は削除せず保持
- 旧専用台帳にない診断3件だけを冪等移行

移行後は10行、異なるrequest_id 10件、合計 `$1.892638`。

## 9. 修正前後のxAI使用額

| 状態 | 件数 | 合計 |
|---|---:|---:|
| 修正前表示 | 汎用10件 | $1.892638 |
| 修正後正本 | 専用10件 | $1.892638 |

金額を改ざんせず、正本と重複経路だけを修正した。

## 10. xAI検索スケジュール

- 通常: 06:00 / 12:00 / 18:00
- 低変動: 06:00 / 18:00
- 高変動でも1日3回以下
- 指定時刻外はキャッシュ

## 11. 適応スケジュール

ローカルRSS候補数、重大更新、同一topic急増、国会重要議決、
選挙、外交危機、災害、直前attentionを入力にする。
外部APIを増やさず判定する。

## 12. xAI 1回あたり実費

履歴平均は `$0.1892638 / request`。
レーダーだけに限定した従来平均は約 `$0.270377 / run`。
新設ガードは警告 `$0.012`、当日停止 `$0.020`。

## 13. xAI ROI測定

`local_bot.py xai-roi` はxAI由来と非xAI投稿を分離し、
インプレッション、プロフィールクリック、ブックマーク・引用、
信頼、訂正、削除、1投稿あたり費用を比較する。

各群10件未満なら `insufficient_data`。増額判定はプロフィールクリック率
20%以上改善、品質悪化なし、訂正増加なしが必要で、表示回数だけでは増額しない。

現状はxAI由来公開投稿0件のため `insufficient_data`。

## 14. OpenAI呼び出し内訳

| task_type | calls | USD |
|---|---:|---:|
| classification | 1 | 0.00009085 |
| daily_review | 5 | 0.0190375 |
| legacy_json_import | 1 | 0.31567667 |
| post_generation | 33 | 0.29030325 |
| weekly_review | 1 | 0.04532 |

合計41 calls、`$0.67042827`。公開投稿67件に対して
0.6119 calls/post、`$0.01000639/post`。失敗2、再生成0。

## 15. 文字化けの原因と修正

原因はWindows PowerShell 5.1の既定文字コードとUTF-8 BOMなしファイルの混在、
過去に文字化け名で複製されたREADME。

- PowerShell 18本をUTF-8 BOM化
- Python/JSON読み書きはUTF-8
- JSONは `ensure_ascii=False`
- 比較前にNFKC正規化
- 置換文字・代表的な文字化け列をソースとDB全TEXT列で検査
- 検査結果0件

## 16. 修正した安全キーワード

- 明確な個人攻撃: 政治家役職と侮辱語の組み合わせ
- 政党・支持者への属性攻撃
- 未確認の犯罪断定
- 既存の外国人・高齢者・若者等への属性攻撃
- 全角文字をNFKC正規化してから判定

政策判断や公的行為への批判は個人攻撃として誤判定しない。

## 17. 日次レビュー

04:40開始、gpt-5.4-mini同期1回、04:55期限。
失敗時はローカル集計を保存し、投稿Botを停止しない。
xAI/非xAIの1日・7日比較をレビューJSONへ追加。

## 18. 週次Batch

週次レビューと大量オフラインEvalsだけがBatch対象。
日次レビューをBatchへ戻していない。

## 19. モデル構成

- default: `gpt-5.4-mini`
- important: `gpt-5.6-luna`
- classifier: `gpt-5.4-nano`
- daily: `gpt-5.4-mini`
- weekly: `gpt-5.6-terra`
- premium: false
- Luna: 1日2回
- nano: 1日6回
- Sol: 自動経路なし

## 20. Sol非自動利用の証拠

`.env` の `OPENAI_PREMIUM_ENABLED=false` とモデルルーターの
自動候補除外を既存テストで確認。Solは手動premium要求以外では選択されない。

## 21. フォロワー保存

既存 `follower_snapshots` を維持。00:05と23:55にOwned Read上限内で保存する。
投稿単位の転換は時間窓推定であることを明示する。

## 22. 転換イベント保存

既存 `conversion_events` と `import-conversions` を維持。
profile_visit、note_click、youtube_click、newsletter_signup、paid_purchaseに対応。
URL付き自動投稿は有効化していない。

## 23. SQLiteマイグレーション

追加列:

- xAI request_id、schedule、cached tokens、tool counts、actual/estimated cost、
  cost_source、cache_used
- news_candidates / published_posts の discovered_via、xAI一致、
  attention、velocity、発見時刻、費用配賦

既存行は削除せず、再実行可能。

## 24. 環境変数

主な変更:

- xAI `$2 → $3`
- X `$13 → $12`
- 合計 `$23` 維持
- xAI reserve `$0.25`
- X reserve `$0.50`
- total reserve `$1.00`
- `XAI_COST_LEDGER_VERIFIED=false`
- `XAI_MAX_TOOL_CALLS_PER_REQUEST=1`
- `XAI_ATTRIBUTION_MIN_SAMPLE_SIZE=10`

## 25. 既存テスト

全239件成功。既存158件基準に対して合計81件増。

## 26. 新規テスト

今回の専用ファイルで33件を追加。直前実装で追加済みの48件と合わせて、
依頼書基準から81件増。台帳、重複、換算、予算、スケジュール、ROI、
NFKC、安全判定、PowerShell起動仕様を検証した。

## 27. dry-run

`POST_ENABLED=false` と `FORCE_POST=true` で実行。
ネットワークはサンドボックスで拒否され、`no_news` で正常終了。
X投稿、返信、引用、リポスト、いいね、フォローは0件。
公開投稿DBは67件のまま。

## 28. 月末予測

| API | 予測USD |
|---|---:|
| OpenAI | 1.02517987 |
| xAI 生トレンド | 3.785276 |
| X API | 0.546 |
| 合計 | 5.35645587 |

約884円、5000円予算の予測残額は約4116円。
xAIは未検証フラグにより実際には2ドルで停止するため、生トレンドは上限前の参考値。

## 29. 予算増額判断

増額すべきではない。
xAI由来公開投稿が0件でROIサンプル不足、既に月額2ドル安全上限の94.6%を消費している。
台帳確認後も、最低10件ずつの比較データが揃うまで3ドル解放は推奨しない。

## 30. 残存リスク

- 旧日次レビュータスクのWindows登録自体は管理者所有ACLで無効化拒否。
  呼び出し先no-opにより実害は遮断済み。
- xAIの過去失敗費用は実費であり取り消せない。
- xAI由来投稿のROIサンプルはまだない。
- 月末予測は過去7日線形外挿で、実効上限による停止を直接反映しない。
- `.env` は秘密を含むためGitへ追加しないこと。

## 31. 本番起動前の確認事項

1. `AUDIT_BUDGET_STARTUP_XAI.md` の台帳内訳を人間が確認する。
2. xAIを3ドルまで使う場合だけ `XAI_COST_LEDGER_VERIFIED=true` にする。
3. 管理者PowerShellで旧 `PoliticsNarrativeDailyReview` を無効化できれば実施する。
4. `production\status.ps1` で正式タスクがReadyであることを確認する。
5. `.env` のAPIキーを表示・ログ出力しない。

## 32. 本番再起動コマンド

今回の作業では実行していない。

```powershell
Set-Location "D:\SNS Bot\politics-narrative"
.\production\start.ps1
```

## 33. ロールバック

Botを停止し、現リポジトリを別名退避してからバックアップを戻す。

```powershell
Set-Location "D:\SNS Bot\politics-narrative"
.\production\stop.ps1
```

復元元:

`D:\SNS Bot\politics-narrative-backup-20260724-013738`

## 34. 投稿安全仕様

品質7以上、BANリスク3以下、X単独事実禁止、URL自動投稿禁止、
画像・自動返信・自動引用・自動リポスト・自動いいね・自動フォローなしを維持。

## 35. 投稿量仕様

通常6〜8件目標、通常上限8、重大速報追加2、総上限10、
最短60分、topic cooldown 4時間を維持。

## 36. 監視仕様

05:00〜23:00の毎時00分を維持。
RSS、政府・省庁、国会、政党、報道機関監視はxAI停止時も継続。

## 37. 投稿計測

1h / 24h / 72h指標、SQLiteとJSON履歴、ロック、異常終了時再起動を維持。

## 38. 最終状態

- 正式タスク: `Ready`
- 政治Botデーモン: 0
- 本番X投稿: 未実行
- 本番タスク再起動: 未実行
- 人間確認待ち

## 39. 追加差分完了（02:20 JST）

初回報告後に残っていた仕様差分を実装した。

- 指定名 `AUDIT_STARTUP_XAI_COST_ENCODING.md` を追加
- ticks換算を `src/xai_cost.py` の1関数へ完全に集約
- xAI入力を最大5候補、各180文字以内の概要、対象期間、日本語、
  公式・政治家・記者・研究者・報道機関区分へ限定
- xAIへ記事本文、投稿本文、過去レビュー、winning_patternsを渡さない
- ROIへ時間当たりインプレッション、エンゲージメント率、
  ブックマーク率、引用率、品質、フォロー転換推定を追加
- ROIへプロフィールクリック当たり費用と推定フォロー当たり費用を追加
- OpenAI内訳へ失敗率と再生成率を追加
- 旧管理者タスク用の明示的な管理者無効化スクリプトを追加
- PowerShell 19本のWindows PowerShell 5.1構文確認に成功
- Pythonテスト250件すべて成功
- 追加dry-runは `POST_ENABLED=false`、`no_news`、X操作0件
