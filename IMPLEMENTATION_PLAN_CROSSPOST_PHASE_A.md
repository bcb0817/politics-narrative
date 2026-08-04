# 動画クロス投稿 Phase A 実装計画

## 境界

Phase Aはローカル生成、媒体別レンディション、媒体別投稿文、公式API要求計画、
モックテスト、dry-runまでとする。外部アップロード、外部投稿、タスク登録は行わない。

## アーキテクチャ

1. `publication_id` を発行し、1本のマスター動画へ関連付ける。
2. FFmpegでYouTube、X、Threads、Instagram向けレンディションを生成する。
3. ffprobe、blackdetect、silencedetect、安全領域で品質を確認する。
4. 媒体別投稿文を個別生成する。
5. 公開HTTPSメディアはプロバイダー抽象化を通す。
6. prepare、stage、commit、reconcileをSQLiteで追跡する。
7. APIタイムアウトで結果不明の場合は`ambiguous`とし、盲目的に再投稿しない。
8. 成功済み媒体は一部失敗時も削除しない。

## 公式API

- X API v2 chunked media uploadとPosts API。
- Threads APIのVIDEO containerとthreads_publish。
- Instagram API with Instagram LoginのREELS containerとmedia_publish。
- YouTube Data API v3のresumable videos.insertとprocessingDetails。

## 段階導入

- Phase A：外部書き込みなし。
- Phase B：媒体ごとに人間承認付きテスト1件。
- Phase C：4媒体の限定テスト。
- Phase D：1日1本の本番運用。
