# Amazonアソシエイト・無料note連携 実装計画

作成日: 2026-07-25

## 調査結果

- 無料noteのテーマ選定・本文生成・品質検査・保存は`src/free_note.py`に集約されている。
- 原稿の正本は`outputs/note`配下の`article.md`で、`metadata.json`、`sources.md`、`review.md`を併存させている。
- Discord通知は`src/discord_notify.py`から専用Webhookへ概要と4ファイルを送る。
- SQLiteは`src/metrics_db.py`の再実行可能な加算マイグレーションを使用する。
- OpenAI費用は`src/api_budget.py`で予約・確定され、無料noteは本文生成1呼び出しを基本とする。
- 成果イベントは`src/growth_tracking.py`からCSV取込し、`src/growth_analytics.py`で集計する。
- 既存の関連書籍機能は、ISBN確認済みローカルカタログから記事に合う書籍を選び、Amazon ISBN検索URLを直接掲載していた。
- アソシエイトリンクの手動登録、CSV取込、商品単位のSQLite台帳、リンクイベント、承認ゲートは未実装だった。
- Amazon PA-API 5.0は2026-05-15に廃止され、現行公式手段はOAuth 2.0のCreators APIである。

## 実装方針

1. 初期値を`manual`とし、架空のアソシエイトURLを生成しない。
2. ISBN確認済みカタログから1〜3件を関連性スコアで選び、明示的なリンク待ちプレースホルダーを本文へ入れる。
3. SiteStripe等で人が作成したリンクをCLIまたはCSVで登録し、原稿バックアップ後にプレースホルダーを置換する。
4. 設定名`paapi`は互換のため維持するが、実通信は廃止済みPA-APIではなく、現在の公式Creators API認証情報が揃った場合だけ行う。
5. 正式APIに失敗した場合はスクレイピングせず、`manual`候補へ戻す。
6. `amazon_associate_items`、`amazon_link_events`、取込隔離テーブルを加算作成し、既存行を維持する。
7. リンク待ち・開示不足を任意の承認ゲートで拒否し、Amazon欄を無効化するCLIも用意する。
8. conversion eventへAmazonクリック・購入・紹介料を追加し、記事別・商品別集計を返す。
9. Discordには件数とリンク待ち状態だけを通知し、認証情報・トラッキングID・URLを載せない。
10. 追加のOpenAI呼び出しは行わず、manualモードの追加API費用を0ドルに保つ。

## 安全条件

- 商品ページのスクレイピング、非公式API、Cookie、ブラウザ自動操作を実装しない。
- 自動購入、note自動公開、Amazon画像取得、価格・在庫・レビュー取得を実装しない。
- 認証情報は`.env`にのみ置き、ログ、Discord、GitHubへ出さない。
- PA-API/Creators API実通信テストはモックに限定する。
- 本番Botと無料noteタスクは作業完了後も勝手に再起動しない。

## 検証

- 新規単体テストで候補選定、manual登録、CSV、公式APIフォールバック、開示、承認、Discord、SQLite、成果計測、安全性を確認する。
- 全既存テスト、`compileall`、PowerShell 5.1構文検査を実行する。
- 外部投稿・Discord・公式APIを無効にしたdry-runで生成物とCLI表示を確認する。
