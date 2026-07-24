# xAIレーダー・人間承認キュー実装

## 役割分担

- xAI X Search: X上の急上昇テーマ、対立軸、代表Post ID、投稿速度の検出
- RSS・公式資料: 事実確認
- OpenAI: 久世ゆいのオリジナル投稿、品質評価、コメント案
- X API: オリジナル投稿と自分の投稿指標取得のみ

xAI由来テーマは単独で投稿候補にならず、既存RSS・公式候補と一致した場合だけ注目度として付加される。

## 公式仕様

- X Search: https://docs.x.ai/developers/tools/x-search
- Tool usage: https://docs.x.ai/developers/tools/tool-usage-details
- Exact cost tracking: https://docs.x.ai/developers/cost-tracking
- Structured outputs: https://docs.x.ai/developers/model-capabilities/text/structured-outputs

OpenAI互換Responses APIを `base_url=https://api.x.ai/v1` で使用し、`x_search`、
`max_tool_calls=1`、`parallel_tool_calls=false`、JSON Schemaを指定する。実費は
`usage.cost_in_usd_ticks / 10_000_000_000` で記録する。

## 自動送信しない機能

`engagement-queue` はMarkdown・JSON・SQLiteへ候補を保存するだけで、X書き込みクライアントを持たない。
引用、返信、リポスト、いいね、フォロー、投票、スレッド、画像の自動送信は無効。

## 本番前確認

1. `.env` の `XAI_API_KEY` を設定する。
2. xAIコンソールで `XAI_MODEL` が利用可能か確認する。
3. `profile-audit` の未チェック項目を人間が確認する。
4. 承認キューの初回出力を目視する。
5. `cost-forecast` が予算内であることを確認する。
