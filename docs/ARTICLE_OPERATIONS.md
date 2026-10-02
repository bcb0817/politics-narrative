# 政治解説記事の運用

## 現状と接続範囲（2026-10-01）

調査時HEADは`aafb244`、ブランチは`codex/threads-full-api`。
SNS関連ソース・DB・ニュース収集・スケジューラは以前の依頼で削除済みでした。
14個のPoliticsNarrativeタスクはDisabledでした。独立した自動Botは作らず、
既存の`local_bot.py`という入口に`article`サブコマンドだけを追加しました。
旧`once`/`force`/`daemon`等は実行できません。タスクや自動化には接続しません。

旧版`4887e56`から再利用したのは、公開IP検査付きHTTP取得とリポジトリ内バックアップの仕組み、
明文化済み編集方針です。旧APIクライアントや公開パイプラインは復元していません。
作業前バックアップは`backups/politics-narrative-backup-xai-article-drafts-20261001-113530`。
既存`.env`、未コミットの`.env.example`、音声ファイルは変更対象外です。

## 入力と実行

```powershell
# APIもURL取得も行わない。状態はneeds_research（完成稿ではない）
python -X utf8 local_bot.py article --theme '架空の動作確認' --dry-run

# A: 一次資料、背景・反論の資料を繰り返し指定
python -X utf8 local_bot.py article --theme '制度改正の対象と条件' `
  --url 'https://example.org/primary' --url 'https://example.org/background' `
  --format '政策解説' --budget 0.50

# B: 既存の収集結果を読み取り専用で選定（手動実行）
python -X utf8 local_bot.py article --candidates data/news_candidates.json

# 明示的にWeb検索を使う。検索結果は根拠にせず、URLを直接再取得する
python -X utf8 local_bot.py article --theme '調べたい政策' --research web

# 完了済み工程を再実行せず再開
python -X utf8 local_bot.py article --resume outputs/articles/<run-id>
```

URL例の`example.org`は説明用であり、実資料に置き換えます。
`--region`は日本の国政、`--audience`は政治に詳しくない一般読者、
`--min-chars 2500 --max-chars 4000`が初期値です。
形式は政策解説／発言検証／政策比較／ニュースの背景解説／根拠付き論評。
`--editorial`で今回の関心を追加できますが、事実検証・非差別等の共通規約は維持します。
`--output`は保存先、`--env-file`は明示的な環境設定、`--config`は料金等の設定です。
重複履歴の取りこぼしを防ぐため、CLI保存先は`outputs/articles/`直下の実行フォルダに限定します。
資料は提供URLだけを使う`provided`が既定。反論・全文を見つけられない場合は欠測として記録します。

候補はJSON配列または`{"candidates": [...]}`、SQLiteの`reach_inventory`または
`news_candidates`テーブルに対応します。稼働中の収集機能・実データは現時点で存在しないため、
この連携はテストデータによる検証です。別のコレクタやスケジュールは追加しません。
以下はすべて架空のテスト例です。

```json
{
  "candidates": [{
    "title": "架空制度の改正", "url": "https://fiction.example/policy",
    "published_at": "2026-10-01T00:00:00Z",
    "expires_at": "2026-10-03T00:00:00Z",
    "primary_urls": ["https://fiction.example/primary"],
    "article_assessment": {
      "impact": {"score": 2, "reason": "架空資料に対象世帯の記載"},
      "misunderstanding": {"score": 1, "reason": "施行日と適用年が異なる"},
      "added_value": {"score": 1, "reason": "二つの条件を併せて説明できる"}
    }
  }]
}
```

各評価は0〜2。未取得の影響・誤解・補足価値はnullで、人気や需要を捏造しません。
鮮度は公表日時、一次資料候補はURLと提供一覧に基づく暫定評価です。
取得可能性や内容の真実性を保証するスコアではありません。採否は`selection.json`に記録。
期限切れ、未来日付、同一URL/テーマの重複を除外します。再取得で期限を延長しません。
再記事化は`substantive_update`に`description/source_url/event_date/evidence_quote`を指定し、
新資料の原文と一致する根拠を必須にします。実質的更新の意味判断は独立レビューと人間確認が必要です。
削除済み過去記事との重複は判定できません。

## 工程と出力

公開URLの取得 → 論点・根拠整理 → 記事生成 → 別API呼び出しでの検証 → 最大2回修正。
単にモデルが同意しただけでは根拠にしません。原文の完全一致引用と資料番号を機械照合します。
政策・司法段階などの既知の取り違えは機械検査も行いますが、完全な意味検証ではありません。
正しい事実に基づく批判・評価は、判断基準と評価であることの表示を必須として許容します。

各実行フォルダには以下を保存します。失敗時・予算停止時も途中まで残ります。

- `article.md`：3タイトル候補、推奨タイトル、出典番号付き本文
- `article.txt`：X記事へ貼り付ける本文・出典（先頭の状態表示は確認後に人間が判断）
- `promo_posts.txt`：短い紹介投稿2案
- `sources.json`：取得日時、公開/更新/出来事日時、主体、種別、根拠、限界、取得失敗
- `claims.json`：主張ごとの分類、重大度、資料番号と原文
- `review.json`：検証結果・修正履歴
- `usage.json`：実測/推定使用量、費用、未精算予約
- `run.json`：入力、設定版、状態、日時、エラー

加えて`plan.json/draft.json/review_0.json/revision_1.json`等がチェックポイントです。
`ready_for_review`は人間レビュー待ち、`needs_research`は未解決のcritical/major等、
`budget_exceeded`は呼び出し・費用上限、`failed`はAPI/形式/通信等の失敗です。
本文文字数の範囲外はminorとして明示し、水増しを避けます。完成を装う自動公開はありません。

## モデル・料金・予算

2026-10-01にxAI公式資料を確認し、構造化出力と推論に対応する`grok-4.7`を選定。
同じモデルへの独立呼び出しによるレビューであり、異なるモデルの一致による検証ではありません。
`reasoning_effort=low`、Responses API、`store=false`、公式グローバルエンドポイントを使用。

- [モデル仕様](https://docs.x.ai/developers/models/grok-4.7)
- [料金](https://docs.x.ai/developers/pricing)
- [使用量と実費](https://docs.x.ai/developers/cost-tracking)
- [構造化出力](https://docs.x.ai/developers/model-capabilities/text/structured-outputs)
- [Web検索](https://docs.x.ai/developers/tools/web-search)
- [X検索](https://docs.x.ai/developers/tools/x-search)

短いコンテキストの100万トークン単価は入力$2、キャッシュ入力$0.50、出力$6。
ローカルの送信上限80,000 UTF-8バイトは長文料金の閾値より十分小さく制限します。
Responsesの出力トークンは推論分を含むため、推論を再加算しません。
`cost_in_usd_ticks / 10^10`を実費として保存し、欠測はnull。
トークン単価による推定値は別項目です。実費不明の場合、保守的に予約額を消費扱いにします。

Web検索は$0.005/呼び出し、最大2回＋追加予約$0.15。
X検索は2026-09-21以降、取得ポスト$0.005/件・プロフィール$0.01/件の単位です。
従来の「検索呼び出し回数」だけでは費用を制御できないため、本実装では無効です。
検索ツール内部の入力増加等はAPI側の厳密な金額上限を保証できません。
入力バイト数・出力上限による予約は安全側の見積もりであり、プロバイダの請求保証ではありません。
実APIでは出力上限4500指定に対して、推論を含むoutput_tokensが7769となる応答を観測しました。
このため推論等の不確実性に8000トークン分を追加予約します（実費の二重加算はしません）。
HTTP応答待ちは360秒に制限し、上限時間後は結果不明として停止します。

全呼び出し前にSQLite `data/bot_metrics.db`の`article_calls`へBEGIN IMMEDIATEで永続予約。
月次の既存ENV上限・予備費を尊重し、旧`api_usage_events/xai_usage_events`が存在すれば集計に含めます。
同時起動はファイルロックでも抑止。上限は9呼び出し、修正2回、429/5xx再試行1回。
認証エラーは即停止。応答不明・実行中の予約は自動再送も自動解放もしません。
別アプリのxAI請求や削除済み台帳は把握できません。旧Botを将来復元する場合は、旧側にも
この予約を読ませる共通化が必要です。この変更では旧Botを動かしません。

## 再開と復旧

入力・設定ハッシュが同一の`--resume`だけを認め、成功工程を再課金しません。
通常の再開ではrun.json内の保存済み設定を使います。明示的に--configを指定する場合も同一設定が必要です。
一時的な月次不足なら枠が戻った後に再開できます。1記事枠の不足は上限を自動引き上げず停止を維持。
課金実績が上限を超えた場合もキャッシュ再生で完了扱いにしません。
`ambiguous_api_call_requires_manual_reconciliation`はコンソールの使用量と予約を人間が照合するまで停止。
DBや実行フォルダを削除して再試行することは、重複課金・証拠消失になるため避けてください。
取得失敗もチェックポイントに残すため、再開で勝手に新しい資料へ置換しません。
資料・設定を変更する必要がある場合は、既存結果を保持し、新たな調査として入力を整理してください。

機能を使わない場合はCLIを実行しないだけで停止します。Windowsタスクの再開は不要です。
コードの巻き戻しはこの変更のコミットだけを`git revert`し、`.env`・音声・台帳・下書きは保持します。
ソース変更時はAGENTSに従いバックアップ・テスト・秘密情報確認を行ってください。

## 取得・検証の制約

HTTP/HTTPSのみ、公開IPのみ、DNS結果を実接続へ固定、転送先も再検査。
最大1MB、4ホップ、HTMLのみ、8秒タイムアウト、資料最大6件・各10,000文字。
接続はfinallyで閉じ、SQLiteも確実に閉じます。ログインやアクセス制限は迂回しません。
PDF/公式動画の全文抽出、画像OCR、Xログイン資料取得は未実装です。
HTMLの代替資料がなければ取得失敗として要調査になります。取得日を公表日にはしません。
本文中の主体や日付はモデル抽出であることを記録し、独立確認済みとは呼びません。
サーバー側Web検索の接続先はxAI側で管理され、ローカル取得と同等のネットワーク検査は保証しません。
APIキーは入力資料・プロンプト・生成物に含めず、エラー本文も保存しません。

## 検証

テスト用資料はすべて架空と明記。政策段階、予算期間、切り取り、裁判、肩書、SNS世論、
タイトル過大断定、根拠付き論評、取得失敗、予約競合、応答消失、予算停止と再開を確認します。
実APIの結果はローカルの`outputs/articles/sample-20261001/`参照。実データはGitに含めません。

2026-10-01の実API試験では国税庁の令和7年度基礎控除改正のHTML取得、plan生成、
原文照合、構造化出力と費用取得が成功しました。plan実費は$0.055990。
次のdraft呼び出しで応答を受信できずambiguousとなり、$0.124884の予約を保持しました。
合計の保守的計上額は$0.180874ですが、合計実費はnull（未確定）です。
同じ呼び出しは再送していません。本文・独立レビューまでの実API通し確認は未完了です。
HTTP時間上限を120秒から360秒へ調整後の経路はモック検証のみです。
架空資料の通しテストは完成・保存・再開まで確認済みですが、これを実記事の成功と扱いません。
現在の回帰テストは29件（政治的な取り違え6例のsubtestを含む）です。
構文確認、dry-run、既存SQLite候補の読み取り専用性、実API失敗実行の再開時に
台帳の呼び出し件数が2件のまま増えないことも確認しています。

## 1回限りの通常X投稿テスト（2026-10-02）

ユーザーの実投稿テスト依頼に限り、`src.manual_x_smoke`を追加しました。
記事生成・通常Botからの呼び出しはなく、停止中の14タスクは変更していません。
X Articlesの公開確認ではありません。

```powershell
# 初期状態はdry-run。APIや.envへアクセスしない
python -X utf8 -m src.manual_x_smoke --expected-user goudakazundo931

# 明示的に依頼された1回だけ。既に完了済みなので現在は保存結果の表示のみ
python -X utf8 -m src.manual_x_smoke --send --expected-user goudakazundo931
```

本文は「動作確認のテスト投稿です。」に固定。`.env`のAPI_KEY、API_KEY_SECRET、
ACCESS_TOKEN、ACCESS_TOKEN_SECRETでOAuth 1.0a認証します。xAIキーではありません。
ライブ経路だけ、既にインストール済みのrequests/requests_oauthlibを使います。
GET /2/users/meでユーザー名を一致確認してからPOST /2/tweetsを1回だけ実行。
POST_ENABLEDとX_POST_ENABLEDがtrueでない場合は停止し、設定を変更・迂回しません。

送信前の状態と費用予約をSQLiteのx_manual_smokeに永続化し、記事生成と同じ実行ロックを使用。
成功、失敗、応答不明、途中停止のいずれも同じスロットを自動再送しません。
旧自動投稿のDBが復元されている場合は、旧上限の検証不足を避けるため停止します。
キー・応答中の認証情報・エラー本文は保存しません。
記録は`outputs/manual_x_smoke/result.json`（Git対象外）。予約は記事側の総予算計算にも含めます。

公式の[料金表](https://docs.x.com/x-api/getting-started/pricing)を2026-10-02に確認。
User Read $0.010 + URLなし投稿 $0.015 = $0.025を保守的に予約し、実費はnull。
外部アプリの利用や削除済みの台帳までは把握できず、アカウント全体の請求上限保証ではありません。
購入、契約変更、予算増額は行いません。

実投稿は2026-10-02 11:43 JSTにHTTP 201で成功し、投稿IDを確認しました。
同一コマンド再実行も保存結果の再生だけで、API要求・投稿は追加されませんでした。
追加7テストを含む合計36件が成功。元の記事生成APIの応答不明問題は別件であり、
今回の投稿成功は生成記事の完成や自動記事公開の成功を意味しません。
