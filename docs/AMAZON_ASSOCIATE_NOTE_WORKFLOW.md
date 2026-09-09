# 無料note・Amazonアソシエイト運用手順 📚

## 目的

無料noteドラフトへ、記事内容と関連する書籍候補を0〜3件追加します。
本文生成、候補選定、リンク登録、承認、公開の責任範囲を分離し、
架空リンクや未確認リンクの公開を防ぎます。

この機能はAmazon商品の購入、noteへの投稿、ブラウザ操作を自動化しません。

## 初期設定

初期値は安全な`manual`モードです。

```dotenv
AMAZON_ASSOCIATE_ENABLED=true
AMAZON_ASSOCIATE_MODE=manual
AMAZON_ASSOCIATE_TRACKING_ID=
AMAZON_ASSOCIATE_DISCLOSURE_ENABLED=true
AMAZON_RELATED_ITEMS_MIN=1
AMAZON_RELATED_ITEMS_MAX=3
AMAZON_RELATED_ITEMS_REQUIRE_RELEVANCE=true
AMAZON_MIN_RELEVANCE_SCORE=7.0
AMAZON_MANUAL_LINK_PLACEHOLDER=true
AMAZON_REQUIRE_LINKS_BEFORE_APPROVAL=true
AMAZON_REQUIRE_DISCLOSURE_BEFORE_APPROVAL=true
AMAZON_PAAPI_ENABLED=false
AMAZON_PAAPI_MARKETPLACE=www.amazon.co.jp
AMAZON_PAAPI_REGION=us-west-2
AMAZON_RECOMMENDATION_MONTHLY_BUDGET_USD=0.20
AMAZON_RECOMMENDATION_MAX_EXTRA_CALLS_PER_ARTICLE=1
AMAZON_PRODUCT_SCRAPING_ENABLED=false
AMAZON_AUTO_PURCHASE_ENABLED=false
```

認証情報とトラッキングIDは、Git管理されない`.env`だけに保存してください。

## manualモードの流れ

1. 無料noteドラフトを生成します。
2. `amazon-links-status`でリンク待ち候補を確認します。
3. Amazonアソシエイトの正規手段で、候補商品のリンクを人が作成します。
4. `amazon-link-set`またはCSVでリンクを登録します。
5. `article.md`、開示文、`review.md`を人が確認します。
6. 全候補が`ready`になった後、通常の承認操作を行います。
7. noteへの貼り付けと公開は人が行い、公開URLをBotへ記録します。

```powershell
.\.venv\Scripts\python.exe local_bot.py amazon-links-status

.\.venv\Scripts\python.exe local_bot.py amazon-link-set `
  --content-id note-YYYYMMDD-001 `
  --item-id amazon-001 `
  --url "https://www.amazon.co.jp/..."
```

リンク登録前に`article.md`のバックアップを同じ原稿フォルダへ作成します。
同じリンクの再登録は重複として扱い、二重更新しません。

ISBNで商品を指定することもできます。

```powershell
.\.venv\Scripts\python.exe local_bot.py amazon-link-set `
  --content-id note-YYYYMMDD-001 `
  --isbn 9780000000000 `
  --url "https://www.amazon.co.jp/..."
```

## CSV一括登録

UTF-8 CSVに次のヘッダーを使用します。`item_id`または`isbn`のどちらかを
指定してください。

```csv
content_id,item_id,isbn,affiliate_url
note-YYYYMMDD-001,amazon-001,,https://www.amazon.co.jp/...
note-YYYYMMDD-001,,9780000000000,https://www.amazon.co.jp/...
```

```powershell
.\.venv\Scripts\python.exe local_bot.py import-amazon-links `
  --file .\amazon-links.csv
```

不正な行は原稿を変更せず、SQLiteの隔離テーブルへ記録します。隔離記録には
アソシエイトURL自体を保存しません。入力CSVはBotが変更しません。

## 承認ゲート

次のいずれかに該当すると、`approved`または`published`への状態変更を拒否します。

- リンク待ち、無効、未確認の商品がある
- 規定のAmazonアソシエイト開示文が本文にない
- `review.md`にAmazon確認チェックリストがない

記事ごとにAmazon欄を使わない判断をした場合は、次の明示的な操作で無効化します。
元の`article.md`はバックアップされます。

```powershell
.\.venv\Scripts\python.exe local_bot.py amazon-links-disable `
  --content-id note-YYYYMMDD-001
```

## 公式APIモード

互換性のため設定名は`paapi`ですが、廃止済みPA-API 5.0へは接続しません。
Amazon公式のCreators APIをOAuth 2.0で使用します。

```dotenv
AMAZON_ASSOCIATE_MODE=paapi
AMAZON_PAAPI_ENABLED=true
AMAZON_PAAPI_PARTNER_TAG=
AMAZON_CREATORS_API_CREDENTIAL_ID=
AMAZON_CREATORS_API_CREDENTIAL_SECRET=
AMAZON_CREATORS_API_CREDENTIAL_VERSION=
```

認証情報不足、API障害、関連候補不足、追加呼び出し上限または予算不足の場合は、
スクレイピングせず`manual`モードへ戻ります。本文ドラフト生成は継続します。

公式資料:

- [PA-API 5.0 Resources](https://webservices.amazon.com/paapi5/documentation/resources.html)
- [Creators API Introduction](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/introduction)
- [Migrating from PA-API](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/migrating-to-creatorsapi-from-paapi)
- [Creators API curl examples](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/get-started/using-curl)

## Discord通知

通知するのは次の結果だけです。

- 関連書籍候補の件数
- 手動リンク待ち件数
- 公式API取得済み件数
- 手動作業が必要な場合のCLI案内

Webhook、API認証情報、トラッキングID、アソシエイトURLは通知しません。

## リンク切れ時の対応

公開前の人間確認でリンク切れや商品違いを見つけた場合は、原稿を承認せず、
正しいリンクを作り直してください。公開済み記事ではnote側のリンクを人が修正し、
Bot側は該当原稿を`revision_required`へ戻してから、修正リンクを再登録します。
価格、在庫、レビュー状態をBotが推測してリンクの有効性を判断することはありません。

## 保存データと成果計測

SQLiteへ次を加算保存します。

- `amazon_associate_items`: 原稿・商品ごとの候補とリンク状態
- `amazon_link_events`: 候補生成、リンク登録、検証、公開の履歴
- `amazon_link_import_quarantine`: CSV不正行の安全な隔離記録

成果イベントは`amazon_link_click`、`amazon_purchase`、
`amazon_commission`に対応し、既存の成果CSV取込とダッシュボードで
記事別・商品別に集計します。

購入と紹介料は、Amazonの正式なレポートまたは人が用意したCSVだけを取り込みます。
既存の成果CSVに`item_id`列を任意で追加できます。

```csv
occurred_at,source,campaign,content_id,event_type,value,item_id
2026-08-01T10:00:00+09:00,note,free-note-001,note-20260726-001,amazon_link_click,1,amazon-001
```

```powershell
.\.venv\Scripts\python.exe local_bot.py import-conversions `
  --file .\data\imports\conversions.csv
```

## 公開前チェック

- `amazon-links-status`で`manual_required`と`invalid`が0件
- 本文にアソシエイト開示文がある
- 書名、著者、ISBNまたはASIN、リンク先商品が一致
- 紹介文が記事テーマと関連し、過度な広告表現を含まない
- Amazonアソシエイト参加者として必要な表示を確認
- Amazonの最新規約、商標ルール、リンク利用条件を人が確認
- `review.md`のAmazon確認項目を人が確認

## ロールバック

1. 本番タスクを停止し、対象プロセスだけが停止したことを確認します。
2. 作業前バックアップからコードを別フォルダへ復元します。
3. `.env`は全面上書きせず、追加した`AMAZON_`設定を無効化します。
4. SQLiteの新規テーブルは既存機能へ影響しないため、履歴保全のため削除しません。
5. Amazon欄を個別に外す場合は`amazon-links-disable`を使います。
6. 全テストとdry-run後、人の判断でスケジュールタスクを再開します。

最小の機能停止は次の設定です。

```dotenv
AMAZON_ASSOCIATE_ENABLED=false
AMAZON_PAAPI_ENABLED=false
```

## 禁止事項

- 商品ページのスクレイピング、非公式API、Cookie利用
- Selenium、Playwright等によるAmazon・noteのブラウザ操作
- Amazon画像、価格、在庫、レビューの無断取得
- 商品の自動購入
- noteへの自動投稿
- 未確認URLや架空URLの自動生成
- 認証情報、トラッキングID、リンクをログやDiscordへ表示
