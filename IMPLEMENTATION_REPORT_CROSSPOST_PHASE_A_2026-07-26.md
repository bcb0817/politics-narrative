# 動画クロス投稿 Phase A 実装報告

1. **バックアップ先**：`D:\SNS Bot\politics-narrative-backup-crosspost-20260726-042844`
2. **既存実装調査**：X/Threadsテキスト投稿は存在。YouTubeアップロード、Instagram Reels、動画レンディションは未実装だった。
3. **変更ファイル**：`.env.example`、`local_bot.py`、`src/discord_notify.py`。
4. **新規ファイル**：`src/crosspost.py`、`src/media_publication.py`、テスト、設定、ドキュメント、PowerShellスクリプト。
5. **アーキテクチャ**：prepare、stage、commit、reconcileを媒体別状態で追跡する非分散トランザクション。
6. **publication_id**：`video-20260726-politics-api-safety-short-b741cc`。
7. **マスター動画**：1080×1920、30fps、H.264、AAC 48kHz、45秒。
8. **YouTubeレンディション**：1080×1920、30fps、字幕表示、SRT併設。
9. **Xレンディション**：確実性優先の720×1280、30fps、H.264/AAC。
10. **Threadsレンディション**：1080×1920、H.264/AAC、alt text生成。
11. **Instagramレンディション**：1080×1920、30fps、25Mbps・1GB上限内。
12. **媒体別投稿文**：4媒体で異なる本文、CTA、alt text、タグを生成。
13. **公開順序**：YouTube、X、Threads、Instagram。
14. **目標公開ウィンドウ**：120秒。厳密な同一秒公開は保証しない。
15. **X動画方式**：公式v2 INIT、APPEND、FINALIZE、STATUS、POST /2/tweets。
16. **Threads動画方式**：VIDEOコンテナ、処理確認、threads_publish。
17. **Instagram方式**：プロアカウント確認、REELSコンテナ、FINISHED、media_publish。
18. **YouTube方式**：resumable videos.insert、processingDetails、未監査時private。
19. **公開HTTPS方式**：s3/r2/funnel/customの抽象化。Phase Aはdry-run URLのみ。
20. **Funnel安全策**：1ファイル限定、トークン、期限、一覧禁止、走査拒否、Range対応、専用ポート。
21. **冪等性**：`platform:publication_id` の一意キー。
22. **一部失敗**：成功済み投稿を維持し、失敗媒体だけを再照合する。
23. **ambiguous**：結果不明時は再投稿せず、状態確認を要求する。
24. **SQLite**：5テーブルと3インデックスを加算的・再実行可能に作成。
25. **CLI**：crosspost系11件、Instagram系5件、YouTube token statusを追加。
26. **Windowsスクリプト**：登録、開始、停止、状態確認を追加。未登録・未起動。
27. **Discord**：準備・結果の要約通知を追加。URL、トークン、秘密鍵は除外。
28. **Analytics**：同じpublication_idへ媒体別指標を保存し、定義差を保持。
29. **環境変数**：自動公開OFF、Instagram対象OFFを初期値として追加。
30. **既存テスト**：既存592件を4分割して実行し、全件成功。
31. **新規テスト**：要求項目に対応する67件を追加し、全件成功。合計659件成功。
32. **dry-run動画パス**：`outputs/crosspost/video-20260726-politics-api-safety-short-b741cc/`
33. **動画仕様**：YouTube/Threads/Instagramは1080×1920、Xは720×1280、全て約45.013秒。
34. **投稿文**：dry-run JSONとしてSQLiteへ保存済み。
35. **外部投稿**：X、Threads、Instagram、YouTubeの外部書き込み0件。
36. **Meta設定**：Instagram Login、Business/Creator、必要スコープ、redirect URIが必要。
37. **プロアカウント確認**：`/me?fields=id,username,account_type,media_count`のaccount_typeを確認。
38. **Google設定**：YouTube Data API、OAuth同意、youtube.upload、監査状態確認が必要。
39. **X権限確認**：既存OAuthユーザーにmedia uploadとPost作成権限が必要。
40. **媒体別テスト**：Phase Bでprivate/テスト投稿を媒体ごと1件、人間承認後に行う。
41. **Phase Bコマンド**：現段階ではロックされており、人間承認後の差分で有効化する。
42. **本番有効化**：Phase B/C合格後に全体・媒体スイッチを個別に有効化する。
43. **緊急停止**：`local_bot.py crosspost-emergency-stop` または `production/crosspost_stop.ps1`。
44. **ロールバック**：Gitコミットを戻すか、上記完全バックアップから復元する。
45. **残存リスク**：各社API審査・契約差、公開ストレージ未設定、実アカウントテスト未実施。
