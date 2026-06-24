# politics-narrative 運用方針

## 投稿運用
- GitHub Actions は10分おきに起動する
- Python側で JST 現在時刻を取得して投稿可否を判定する
- 投稿許可スロットは JST 05:07〜23:17 の1日40回
- 各スロットには12分の許容幅を持たせる
- 1スロット1投稿まで
- 定期投稿はすべて diagram モード
- 手動実行も diagram モード
- linkモードなし
- test投稿なし
- ランダムスケジュール生成なし
- ランダム投稿数なし
- post.ymlの自動書き換えなし
- 投稿内容は政策・データ・図解中心
- 過度な煽り、陰謀論、差別表現、個人攻撃は禁止

## 時刻判定
GitHub Actions の schedule は UTC 基準で実行が数分〜十数分遅延することがある。
そのため cron の完全一致では制御せず、`post.py` 側で JST 現在時刻を取得し、
各スロット開始から +12分以内かどうかで投稿可否を判定する。

JST取得は2段構え:
1. 第一候補: `https://worldtimeapi.org/api/timezone/Asia/Tokyo`
2. フォールバック: `datetime.now(ZoneInfo("Asia/Tokyo"))`

## 重複防止
- `src/posted_slots.json` … 投稿済みスロット（例: `2026-06-24_05:07`）
- `src/posted_urls.json` … 投稿済みのURL・タイトル・キーワード・型・ジャンル
- どちらも GitHub Actions cache で復元・保存する
- 投稿APIが成功した後にだけ記録する（失敗時は未投稿扱い）

## 手動実行
- 通常の手動実行: 時刻判定あり
- workflow_dispatch の `force_post=true`（または `FORCE_POST=true`）: 時刻判定を無視して diagram 投稿
- test モードは復活させない

---

## このリポジトリで「やめたこと」と移行手順

### 削除するファイル
1. `.github/workflows/reset.yml` を削除する
   - 削除できない場合は `schedule:` トリガーを消し、`workflow_dispatch:` のみにする
   - 理由: `reset.yml` が `generate_schedule.py` を実行して `post.yml` をランダム生成・上書きすると固定運用が壊れるため
2. `src/generate_schedule.py` を削除する
   - 残す場合でも、どの workflow からも呼ばれない状態にする

### 必要な Secrets（Settings → Secrets and variables → Actions）
- `API_KEY`
- `API_KEY_SECRET`
- `ACCESS_TOKEN`
- `ACCESS_TOKEN_SECRET`
- `ANTHROPIC_API_KEY`

### 依存ライブラリ
`requirements.txt` がある場合はそれを使う。無い場合は workflow が以下を入れる:
`tweepy / anthropic / pillow / requests / feedparser`

### 日本語フォント
図解画像のため workflow で `fonts-noto-cjk` を導入している。
ローカル検証する場合も Noto CJK を入れること。

### ニュース取り込み
`post.py` の `gather_candidate_news()` は最小フォールバック実装。
既存の RSS ingestion がある場合は、その出力（title / summary / url / source_name の配列）を
この関数に差し込むこと。

---

## 投稿型（A〜L）

図解投稿の「型」を A〜L に拡張した。狙いは炎上ではなく、保存・引用リポスト・返信。

- A=対比型 / B=数字インパクト型 / C=誤解訂正型 / D=争点整理型 / E=未来警告型
- F=争点問いかけ型
- G=誰が得するか型 / H=負担の見える化型 / I=海外比較型
- J=世代間構造型 / K=政策の副作用型 / L=ニュースの裏にある制度型

`POST_TYPES`（`src/post.py`）のキー順がそのまま生成ツールの `type` enum になる。
型を増減するときは `POST_TYPES` を編集すれば enum も自動で揃う。

## F=争点問いかけ型の安全ルール

F型は「最後に1つだけ問いを置く」型。返信・引用を増やすのが目的。

- 単なる「あなたはどう思いますか？」は禁止。必ず争点（線引き・優先順位・負担配分）を提示してから問う。
- 良い問いの例:「これは支援なのか、将来世代への負担移転なのか。あなたはどう見ますか？」
- 政党・政治家・個人への攻撃に誘導しない。
- 投票先・投票行動・選挙手続きに関する問いは禁止。
- 災害・事件事故・民族/国籍/宗教対立を招きやすいテーマでは F型を使わない。
- F型は採点で増えすぎないよう抑制している（直近10投稿に3回以上で減点）。

## 投稿本文・数字・スコアの方針

- 投稿本文に URL は入れない（画像と本文で完結させる）。
- ソース不明（ニュース本文に根拠がない）数字は使わない。使った場合は `uses_unverified_number=true` とし、採点で大きく減点する。
- 本文の狙いは「怒らせる」ことではなく「争点を一言で言い直したくなる状態」を作ること。
- スコアは反応の取りやすさ（引用・保存・初速）を重く見た加重平均で評価する。
  追加項目: early_reaction_likelihood / quote_angle_strength / visual_clarity /
  policy_structure_value / evergreen_value / source_trust。
- **低スコア投稿はスキップする。** 40スロットが空いていても、実効スコアが投稿閾値
  （`SCORE_POST`）未満なら投稿しない。スロットを埋めること自体は目的ではない。
- ニュースの事前選別（`prefilter_news`）では、投稿済みURL・重複タイトル・除外テーマ
  （`EXCLUDED_TOPICS`）を外し、ソース信頼度で軽く補正する。
  選別件数は環境変数 `PREFILTER_TOP_N`（未指定なら4）で調整できる。

## 図解画像サイズ（A/Bテスト）

図解画像は `DIAGRAM_PRESET` で切り替えられる。

- `landscape`（既定）: 1200x675（X タイムライン向け 16:9）
- `vertical`: 1080x1350（保存・縦長シェア向け 4:5）

未指定時は landscape のまま。将来 `DIAGRAM_PRESET=vertical` で縦長図解の反応を
A/Bテストできる。文字はピクセル幅で折り返すため、長文でもはみ出しにくい
（タイトル・結論は最大2行、対比カラムは各4項目・1項目2行まで）。

## ニュース取り込み

`src/post.py` の `gather_candidate_news()` は `src/news.py` の `fetch_all_items()` を使う
（NHK 政治/経済/国際・Yahoo! 政治/経済/国際）。
`fetch_news()` は取得時に投稿済みURLを記録してしまうため **使わない**。
投稿済み記録は、投稿API成功後に `save_post_record()` / `mark_slot_posted()` でのみ更新する。
