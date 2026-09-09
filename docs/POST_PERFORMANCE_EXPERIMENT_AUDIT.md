# 投稿実績・実験基盤監査

## 利用可能データ

```json
{
  "generated_candidates": 108,
  "published_x": 84,
  "published_threads_rows": 25,
  "metric_rows_x": 306,
  "follower_snapshots": 6,
  "measurement_windows": [
    "15m",
    "1h",
    "24h",
    "6h",
    "72h"
  ],
  "profile_clicks_available": true,
  "follows_available": false,
  "reply_text_available": false,
  "limitations": [
    "15分窓は既存DBに保存されている場合だけ分析対象",
    "投稿時フォロワー数は最寄りスナップショットで近似",
    "返信本文がないためspecific_reply_rateは算出不能(null)",
    "Threadsにはprofile_clicks/follows/bookmarksがない",
    "バックテストは完全な当時時点の情報再現ではない"
  ]
}
```

## 方針

欠損分母はnullのまま保存し、推測で補完しません。需要は真偽・重要性・安全性と分離します。Phase Aは外部投稿しません。
