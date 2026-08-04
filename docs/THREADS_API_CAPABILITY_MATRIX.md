# Threads公式API 機能対応表 🧵

最終確認日: 2026-07-26
根拠: [Meta公式Threads Postman workspace](https://www.postman.com/meta/threads/overview) と同workspace内の各リクエスト。

「現在利用可」はローカル設定に記録された3スコープを基準とする。`threads-permissions` の公式 `debug_token` 成功時は、その結果を優先する。App Review欄の「条件付き」は、テストユーザー外へ公開する際にMeta管理画面で確認が必要という意味であり、審査要否を推測で断定しない。

| 機能 | 公式エンドポイント / メソッド | 必要権限 | Review | 現在利用可 | 主要パラメータ・フィールド | ページング / 枠 | Bot用途 | 自動実行 | 実装 |
|---|---|---|---|---|---|---|---|---|---|
| 自分のプロフィール | `GET /me` | `threads_basic` | 条件付き | A: 可 | `fields=id,username,name,is_verified,threads_profile_picture_url,threads_biography,recently_searched_keywords,is_eligible_for_geo_gating` | なし | 接続・所有者確認 | 可 | 完了 |
| 自分の投稿 | `GET /me/threads` | `threads_basic` | 条件付き | A: 可 | `fields`, `since`, `until`, `limit`, `before`, `after` | cursor | 増分同期 | 可 | 完了 |
| 投稿詳細 | `GET /{thread-id}` | `threads_basic` | 条件付き | A: 可 | 投稿、子要素、投票、タグ、位置等 | なし | 再確認 | 可 | Client対応 |
| 投稿Insights | `GET /{thread-id}/insights` | `threads_manage_insights` | 条件付き | A: 可 | `views,likes,replies,reposts,quotes,shares` | なし | 1h/6h/24h/72h/7d測定 | 可 | 完了 |
| Account Insights | `GET /me/threads_insights` | `threads_manage_insights` | 条件付き | A: 可 | `views,likes,replies,reposts,quotes,clicks,followers_count,follower_demographics`; `breakdown=country,city,age,gender` | response paging | 日次集計 | 可 | 完了 |
| 返信・会話 | `GET /{thread-id}/replies`, `GET /{thread-id}/conversation`, `GET /me/replies` | `threads_read_replies` | 条件付き | B: 追加認証 | `fields`, `reverse`, `since`, `until`, cursor | cursor | 返信ツリー同期 | 可 | 完了 |
| メンション | `GET /me/mentions` | `threads_manage_mentions` | 条件付き | B: 追加認証 | `fields`, `since`, `until`, `limit`, cursor | cursor | メンション同期 | 可 | 完了 |
| キーワード/タグ検索 | `GET /keyword_search` | `threads_keyword_search` | 条件付き | B: 追加認証 | `q`, `search_type=TOP/RECENT`, `search_mode=KEYWORD/TAG`, `since`, `until`, `limit` | cursor。結果は全体件数ではない | 相対トレンド標本 | 可 | 完了 |
| 公開プロフィール検索 | `GET /profile_lookup`, `GET /profile_posts` | `threads_profile_discovery` | 条件付き | B: 追加認証 | 完全一致`username`, `fields` | profile_postsはcursor相当 | 公式アカウント照合 | 既定無効 | Client対応予定 |
| コンテナ作成 | `POST /me/threads` | `threads_content_publish` | 条件付き | A: 可 | 下記投稿形式のパラメータ | 公開枠対象 | preview後の作成 | 不可 | 完了 |
| コンテナ状態 | `GET /{container-id}?fields=id,status,error_message` | `threads_content_publish` | 条件付き | A: 可 | `IN_PROGRESS/FINISHED/PUBLISHED/ERROR/EXPIRED` | 有限ポーリング | 曖昧状態解消 | 可 | 完了 |
| 公開 | `POST /me/threads_publish` | `threads_content_publish` | 条件付き | A: 可 | `creation_id` | 公開枠対象 | 人間承認後公開 | 不可 | 完了 |
| テキスト | `POST /me/threads` | `threads_content_publish` | 条件付き | A: 可 | `media_type=TEXT`, `text`, `auto_publish_text` | 公開枠 | 投稿 | 不可 | 完了 |
| 画像/動画 | `POST /me/threads` | `threads_content_publish` | 条件付き | A: 可 | `IMAGE/image_url`, `VIDEO/video_url`, `alt_text`, `is_spoiler_media`; 公開HTTPS URL | 公開枠 | メディア投稿 | 不可 | 完了・既定無効 |
| カルーセル | `POST /me/threads` | `threads_content_publish` | 条件付き | A: 可 | 子を`is_carousel_item=true`、親を`media_type=CAROUSEL`, `children`; 2〜20件 | 公開枠 | 複数媒体 | 不可 | 完了・既定無効 |
| リンク/投票/GIF/テキスト添付 | `POST /me/threads` | `threads_content_publish` | 条件付き | A: 可 | `link_attachment`, `poll_attachment`, `gif_attachment`, `text_attachment`; pollとlink/text添付は排他 | 公開枠 | 会話型投稿 | 不可 | 完了・既定無効 |
| トピックタグ/位置/ゴースト | `POST /me/threads` | content publish。位置は`threads_location_tagging` | 条件付き | タグ/ghost=A、位置=B | `topic_tag`, `location_id`, `is_ghost_post`（24時間） | 公開/位置枠 | 文脈付与 | 不可 | 完了・既定無効 |
| スポイラー/alt/reply control | `POST /me/threads` | `threads_content_publish` | 条件付き | A: 可 | `text_entities`, `is_spoiler_media`, `alt_text`, `reply_control` | 公開枠 | 安全・アクセシビリティ | 不可 | 完了 |
| 引用 | `POST /me/threads`→`POST /me/threads_publish` | `threads_content_publish` | 条件付き | A: 可 | `quote_post_id` | 公開枠 | 引用案 | 不可 | 完了・confirm必須 |
| 返信投稿 | `POST /me/threads`→publish | `threads_content_publish` | 条件付き | A: 可 | `reply_to_id` | reply枠 | 返信案 | 不可 | 完了・confirm必須 |
| リポスト | `POST /{thread-id}/repost` | `threads_content_publish` | 条件付き | A: 可 | 投稿ID | 公開枠 | 選択的拡散 | 不可 | 完了・confirm必須 |
| 削除 | `DELETE /{thread-id}` | `threads_delete` | 条件付き | B: 追加認証 | 自分の投稿ID | delete枠 | 手動訂正 | 不可 | 完了・confirm必須 |
| 返信非表示/再表示 | `POST /{reply-id}/manage_reply` | `threads_manage_replies` | 条件付き | B: 追加認証 | `hide=true/false` | reply枠 | 明白なスパム等 | 不可 | 完了・confirm必須 |
| 位置取得 | `GET /{location-id}` | `threads_location_tagging` | 条件付き | B: 追加認証 | `id,address,city,country,name,latitude,longitude,postal_code` | location枠 | 投稿前確認 | 既定無効 | 完了 |
| 位置検索 | 未確認 | 未確認 | 未確認 | C | 公式リクエストURLを確認できず | 未確認 | なし | 不可 | 未実装 |
| 公開枠 | `GET /me/threads_publishing_limit` | 認証済みユーザー | 条件付き | A: 可 | `quota_usage/config`, reply/delete/location系 | duration/total | 80%警告、90%停止 | 可 | 完了 |
| Token debug | `GET /debug_token` | 有効な認証 | なし | A: 可 | `input_token`。秘密は出力しない | なし | 権限確認 | 手動/起動診断 | 完了 |

## 確認した公式リクエスト

- [プロフィール](https://www.postman.com/meta/threads/request/34203612-3996f0a2-f2ee-4af7-9b66-3dbbe80b8d7d)
- [自分の投稿](https://www.postman.com/meta/threads/request/34203612-fea6c131-986c-4bf0-9908-fa38a77fa2d2)
- [投稿Insights](https://www.postman.com/meta/threads/request/34203612-385abc7d-b3cc-4e5d-9937-ebbe7174e041)
- [Account Insights](https://www.postman.com/meta/threads/request/34203612-d9a6d950-99ab-45a6-bbd2-899ff61b562c)
- [返信](https://www.postman.com/meta/threads/request/34203612-11c959db-21cb-4303-b717-73b661b3579c)
- [メンション](https://www.postman.com/meta/threads/request/34203612-fc3f21da-0a53-44ab-80e2-8cd8c376a42a)
- [検索](https://www.postman.com/meta/threads/request/34203612-b3b2c12a-7ce6-4d86-a3c6-6d31e3b66ea1)
- [単一投稿形式](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api?entity=folder-34203612-995a4593-61de-4558-9b26-6c8becae85e4)
- [カルーセル](https://www.postman.com/meta/threads/request/34203612-b0861a47-db0e-4940-a692-2304669603b3)
- [引用](https://www.postman.com/meta/threads/request/34203612-66db67fa-9b5b-496a-853c-9992db423f3b)
- [リポスト](https://www.postman.com/meta/threads/request/u28ndkk/repost-threads-post)
- [削除](https://www.postman.com/meta/threads/request/34203612-0a51cfc6-6407-4aec-974f-5bcbfbbf9da3)
- [返信管理](https://www.postman.com/meta/threads/request/34203612-b819fb2c-8315-461f-8f30-365f7a32d1b1)
- [公開枠](https://www.postman.com/meta/threads/request/34203612-f4590341-9bee-44f5-901b-606078a03c96)
- [Token debug](https://www.postman.com/meta/threads/request/34203612-e9a7f46e-e48c-4987-a203-22fb25a4b604)

## 公式APIで補完しない機能

全投稿取得、全体トレンド順位、推薦ロジック、プロフィール閲覧者、完全なフォロー一覧、他人の非公開Insights、非公式ないいね/フォロー、Cookie操作は提供を確認できない。Botはこれらをスクレイピングで代替しない。
