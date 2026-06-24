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
