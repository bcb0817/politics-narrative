# 運用方針（politics-narrative）

このドキュメントは、テキスト投稿への移行と、catch-up の詰まり対策（attempted_slots.json）に
関する運用方針をまとめたものです。基本的なセットアップ・起動方法は `README.md` を参照してください。

## 1. 投稿形式：デフォルトはテキスト投稿

- さらに背景・争点の対比・見るべきポイントは、**スレッド（X の返信チェーン）**として
  親投稿にぶら下げ、情報量を担保します。これは X ネイティブの返信機能であり、
  Threads 連携ではありません。

### 文字数ルール（X）

- 親投稿は 280字以内（安全目安 260字以内）。
- 文の途中で切らない／「…」で終わらせない。
- スレッド返信も各 260字以内の目安。

### 関連する環境変数

| 変数 | 既定 | 意味 |
|---|---|---|
| `POST_ENABLED` | `false` | `true` にしない限り X へ実投稿しない |
| `MARK_DISABLED_RUN_AS_ATTEMPTED` | `false` | `POST_ENABLED=false` の run を attempted に記録するか |

## 2. スロット管理：posted と attempted の2本立て

catch-up が同じ低スコア slot を何度も選び続ける詰まりを解消するため、
状態ファイルを2つに分けています。

- `posted_slots.json` … **投稿に成功した** slot
- `attempted_slots.json` … **投稿をトライした** slot（成功＋低スコアskip等）

### catch-up の基準

- catch-up は `attempted_slots.json` を基準に**未トライ(unattempted)** slot を探します。
- 以前は `posted_slots.json` を基準にしていましたが、低スコアskip時は posted に
  記録しないため、同じ低スコア slot が毎回選ばれ続けていました。attempted 基準に
  することで、低スコアskipした slot も「処理済み」とみなして次へ進みます。

### 記録ルール

| ケース | attempted | posted | post_history |
|---|---|---|---|
| 投稿成功 | ○ 記録 | ○ 記録 | ○ 記録 |
| `effective_score_below_threshold`（低スコア） | ○ 記録 | × | × |
| `ban_risk_or_unverified_block` | ○ 記録 | × | × |
| `no_news` | ○ 記録 | × | × |
| `candidate_generation_failed` | ○ 記録 | × | × |
| `post_disabled`（POST_ENABLED=false） | ×（既定）※ | × | × |
| `post_to_x_failed`（一時失敗） | × | × | × |
| ネットワーク／レート制限等の一時エラー | × | × | × |

※ `post_disabled` は、既定では attempted に記録しません（ローカルテストで本番用の
slot を消費しないため）。`MARK_DISABLED_RUN_AS_ATTEMPTED=true` のときだけ記録します。

**attempted にも posted にも記録しません**。本来投稿できたはずの slot を失わないためです。
ただし `logs/post_attempts.jsonl` と `logs/errors.jsonl` には失敗として記録します。

### サイズ制限

- `attempted_slots.json` は直近500件、`posted_slots.json` は直近300件に丸めます。

## 3. 実装後の期待挙動

- **低スコアskp後**: `attempted_slots.json` に slot_key が記録され、`posted_slots.json`
  には記録されない。次回 run では同じ slot を選ばず、次の未トライ slot に進む。
- **投稿成功後**: `attempted_slots.json` と `posted_slots.json` の両方に記録され、
  `posted_urls.json`（post_history）も更新される。
- **post_disabled**: 既定では attempted にも posted にも記録しない。
  `post_attempts.jsonl` には失敗として記録する。

## 4. ログ（post_attempts.jsonl）

各 attempt は `logs/post_attempts.jsonl` に1行1JSONで記録されます。主な項目:

`ts_jst` / `decision` / `reason` / `slot_key` / `selected_slot` / `title` / `type` /
`genre` / `effective_score` / `overall` / `ban_risk` / `tweet_id` / `post_format` /
`image_post_enabled` / `image_path` / `attempted_recorded` / `posted_recorded`

## 5. init-state について（catch-up 基準の変更に伴う注意）

catch-up の基準が `attempted_slots.json` に変わったため、`init-state` は過去スロットを
**attempted_slots.json** に登録します（以前は posted_slots.json でした）。
ローカル移行の初回や、状態をリセットしたときは、通常運用を始める前に
`python local_bot.py init-state` を実行してください。バックログの暴発を防げます。
