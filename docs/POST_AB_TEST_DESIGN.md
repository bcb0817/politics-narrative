# Phase B plan

- control 50% / variant 50%（初期案）
- 同一ニュースからはどちらか一方だけを公開
- category / topic_demand_bucket / publish_hour_bucket / breaking_flag / media_present で層化
- 最低100公開比較、中央値impressions +15%、profile click rate +15%、followと安全が非悪化で全面採用候補
- 強い成功: impressions +25%、quote +20%、profile click +20%、follow +10%
- 罵倒・党派攻撃・単発バズを成功扱いしない
- 人間承認なしにprompt、投稿比率、数、時刻、強度、テーマ配分を変更しない

## 公平性

同じfact_packet_hashを確認し、同じテーマの両案を公開しません。平均だけでなく中央値、分位点、trimmed mean、信頼区間を併記します。
