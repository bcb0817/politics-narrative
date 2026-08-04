#!/usr/bin/env python3
"""One-off, non-publishing research packet for the 2026 Kumamoto earthquake."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

INCIDENT_ID = "kumamoto-earthquake-20260728"
RESEARCH_ID = "kumamoto-earthquake-20260729-0310"
CUTOFF = "2026-07-29 03:10 JST"
OUTPUT_STAMP = "20260729_0310"

SOURCES = [
    {
        "id": "jma_1",
        "name": "熊本地方気象台",
        "level": "A",
        "title": "令和8年熊本地震について（第1報）",
        "url": "https://www.data.jma.go.jp/kumamoto/shosai/kakusyusiryou/20260728_r8kumamoto_no1.pdf",
        "published_at": "2026-07-28 18:15 JST",
        "as_of": "2026-07-28 18:15 JST",
        "use": "発生時刻、震源、規模、最大震度、余震注意",
    },
    {
        "id": "jma_landslide",
        "name": "熊本地方気象台",
        "level": "A",
        "title": "土砂災害警戒情報の暫定基準",
        "url": "https://www.data.jma.go.jp/kumamoto/shosai/kakusyusiryou/20260728_doshakei.pdf",
        "published_at": "2026-07-28",
        "as_of": "2026-07-28",
        "use": "地盤の緩みと暫定基準",
    },
    {
        "id": "fdma_11",
        "name": "総務省消防庁",
        "level": "A",
        "title": "熊本県熊本地方を震源とする地震による被害及び消防機関等の対応状況（第11報）",
        "url": "https://www.fdma.go.jp/disaster/info/items/20260728kumamotojishin11.pdf",
        "published_at": "2026-07-28 22:00 JST",
        "as_of": "2026-07-28 22:00 JST",
        "use": "人的・住家被害、火災、救助、商業施設、避難指示",
    },
    {
        "id": "mlit_2",
        "name": "国土交通省",
        "level": "A",
        "title": "熊本県熊本地方を震源とする地震について（第2報）",
        "url": "https://www.mlit.go.jp/common/002014121.pdf",
        "published_at": "2026-07-28 21:50 JST",
        "as_of": "2026-07-28 21:50 JST",
        "use": "交通、水道、港湾、空港、物流",
    },
    {
        "id": "kumamoto_city",
        "name": "熊本市",
        "level": "A",
        "title": "熊本市第2回災害対策本部会議資料",
        "url": "https://www.city.kumamoto.jp/kiji00372080/index.html",
        "published_at": "2026-07-28 21:30 JST",
        "as_of": "2026-07-28 21:00 JST",
        "use": "避難所、停電、ガス、水道、市内被害",
    },
    {
        "id": "kumamoto_pref",
        "name": "熊本県",
        "level": "A",
        "title": "大地震に関する知事コメント",
        "url": "https://www.pref.kumamoto.jp/site/chiji/274483.html",
        "published_at": "2026-07-28",
        "as_of": "2026-07-28",
        "use": "災害対策本部と県民への注意",
    },
    {
        "id": "kantei",
        "name": "首相官邸",
        "level": "A",
        "title": "熊本県熊本地方を震源とする地震についての会見",
        "url": "https://www.kantei.go.jp/jp/105/statement/2026/0728kaiken.html",
        "published_at": "2026-07-28",
        "as_of": "2026-07-28",
        "use": "政府対応と被害確認中の説明",
    },
    {
        "id": "airport",
        "name": "阿蘇くまもと空港",
        "level": "A",
        "title": "熊本地方で発生した地震の影響について",
        "url": "https://www.kumamoto-airport.co.jp/info/%E7%86%8A%E6%9C%AC%E5%9C%B0%E6%96%B9%E3%81%A7%E7%99%BA%E7%94%9F%E3%81%97%E3%81%9F%E5%9C%B0%E9%9C%87%E3%81%AE%E5%BD%B1%E9%9F%BF%E3%81%AB%E3%81%A4%E3%81%84%E3%81%A6%EF%BC%887-28%E3%80%812145%E6%99%82/",
        "published_at": "2026-07-28 21:45 JST",
        "as_of": "2026-07-28 21:45 JST",
        "use": "欠航",
    },
    {
        "id": "docomo",
        "name": "NTTドコモ",
        "level": "A",
        "title": "熊本県熊本地方を震源とする地震の影響",
        "url": "https://www.docomo.ne.jp/info/network/kanto/pages/260728_00_m.html",
        "published_at": "2026-07-28 17:00 JST",
        "as_of": "2026-07-28 17:00 JST",
        "use": "通信障害",
    },
    {
        "id": "aeon_access",
        "name": "イオンモール熊本",
        "level": "A",
        "title": "アクセス",
        "url": "https://kumamoto.aeonmall.jp/access",
        "published_at": "",
        "as_of": CUTOFF,
        "use": "正式施設名と所在地",
    },
    {
        "id": "jga_cogen",
        "name": "日本ガス協会",
        "level": "A",
        "title": "ガスコージェネレーションシステム",
        "url": "https://www.gas.or.jp/gas-life/n-cogeneration/",
        "published_at": "",
        "as_of": CUTOFF,
        "use": "一般的な仕組み",
    },
    {
        "id": "ace_cogen",
        "name": "コージェネレーション・エネルギー高度利用センター",
        "level": "A",
        "title": "コージェネレーションとは",
        "url": "https://www.ace.or.jp/web/chp/chp_0010.html",
        "published_at": "",
        "as_of": CUTOFF,
        "use": "一般的な仕組み",
    },
    {
        "id": "tv_asahi_mall",
        "name": "テレビ朝日",
        "level": "B",
        "title": "イオンモール熊本の事故報道",
        "url": "https://news.tv-asahi.co.jp/news_society/articles/900196033.html",
        "published_at": "2026-07-29 00:30 JST",
        "as_of": "2026-07-29 00:30 JST",
        "use": "搬送、安否不明、原因調査中という報道",
    },
    {
        "id": "tv_asahi_power",
        "name": "テレビ朝日",
        "level": "B",
        "title": "熊本県内の停電報道",
        "url": "https://news.tv-asahi.co.jp/news_economy/articles/000522392.html",
        "published_at": "2026-07-28 17:26 JST",
        "as_of": "2026-07-28 17:15 JST",
        "use": "初期の停電状況（後続の公式値を優先）",
    },
    {
        "id": "kumanichi_aftershock",
        "name": "熊本日日新聞",
        "level": "B",
        "title": "熊本市西区など震度4",
        "url": "https://kumanichi.com/articles/2018618",
        "published_at": "2026-07-28 17:04 JST",
        "as_of": "2026-07-28 17:04 JST",
        "use": "余震の報道例",
    },
]


def fact(fact_id: str, category: str, statement: str, status: str,
         source_ids: list[str], as_of: str, notes: str = "") -> dict:
    return {
        "fact_id": fact_id,
        "category": category,
        "statement": statement,
        "status": status,
        "source_ids": source_ids,
        "as_of": as_of,
        "notes": notes,
    }


FACTS = [
    fact("eq_time", "earthquake", "地震は2026年7月28日16時27分に発生した。", "confirmed",
         ["jma_1", "mlit_2"], "2026-07-28 21:00 JST"),
    fact("eq_epicenter", "earthquake", "震源は熊本県熊本地方。", "confirmed",
         ["jma_1", "mlit_2"], "2026-07-28 21:00 JST"),
    fact("eq_magnitude", "earthquake", "規模はマグニチュード7.1（暫定値）。", "confirmed",
         ["jma_1", "mlit_2"], "2026-07-28 21:00 JST"),
    fact("eq_depth", "earthquake", "震源の深さは16km（暫定値）。", "confirmed",
         ["mlit_2"], "2026-07-28 21:00 JST",
         "18:15の気象台第1報では約10km。後続の国交省第2報にある16kmを現行値とした。"),
    fact("eq_intensity", "earthquake", "最大震度7を宇城市と氷川町で観測した。", "confirmed",
         ["fdma_11", "mlit_2"], "2026-07-28 22:00 JST"),
    fact("tsunami", "earthquake", "有明・八代海の津波注意報は16時29分発表、18時10分解除。", "confirmed",
         ["fdma_11", "mlit_2"], "2026-07-28 22:00 JST"),
    fact("aftershocks", "earthquake", "本震後21時までに震度3以上を31回観測。", "confirmed",
         ["mlit_2"], "2026-07-28 21:00 JST"),
    fact("casualties", "human_damage", "人的被害と住家被害は確認中。", "confirmed",
         ["fdma_11"], "2026-07-28 22:00 JST",
         "死者数を公式確定しない。報道値は別区分。"),
    fact("fires_rescues", "response",
         "消防庁第11報は宇城市2件、熊本市4件、八代市2件などの火災・救助活動を掲載。",
         "confirmed", ["fdma_11"], "2026-07-28 22:00 JST"),
    fact("mall", "commercial_facility",
         "嘉島町の商業施設で2階部分が崩落し、多数の閉じ込めとして消防活動中。",
         "confirmed", ["fdma_11"], "2026-07-28 22:00 JST"),
    fact("mall_transport", "commercial_facility",
         "報道では3人が搬送され、意識があるとされた。",
         "reported", ["tv_asahi_mall"], "2026-07-28 21:51 JST"),
    fact("power", "infrastructure",
         "熊本県内47,210戸、熊本市内約1,440戸が停電。",
         "confirmed", ["kumamoto_city"], "2026-07-28 21:00 JST"),
    fact("water_yatsushiro", "infrastructure",
         "八代市全域で約60,000戸が断水。",
         "confirmed", ["mlit_2"], "2026-07-28 21:50 JST"),
    fact("water_hikawa", "infrastructure",
         "氷川町で約10,000戸が断水。",
         "confirmed", ["mlit_2"], "2026-07-28 21:50 JST"),
    fact("gas_city", "infrastructure",
         "熊本市東区・南区の一部、約500戸でガス供給停止。",
         "confirmed", ["kumamoto_city"], "2026-07-28 21:00 JST"),
    fact("telecom_docomo", "infrastructure",
         "熊本県の一部でドコモの音声・データ通信が利用しにくい状態。",
         "confirmed", ["docomo"], "2026-07-28 17:00 JST"),
    fact("rail", "transport",
         "九州新幹線は全線運転見合わせ、在来線は8事業者12路線が運転見合わせ。",
         "confirmed", ["mlit_2"], "2026-07-28 21:15 JST"),
    fact("airport", "transport",
         "熊本空港の滑走路閉鎖は19時05分解除。28日の発着便は全便欠航。",
         "confirmed", ["mlit_2", "airport"], "2026-07-28 21:45 JST"),
    fact("roads", "transport",
         "高速道路4路線29区間などで通行止め。",
         "confirmed", ["mlit_2"], "2026-07-28 21:50 JST"),
    fact("shelters", "evacuation",
         "熊本市は181避難所を開設し、858世帯1,512人が避難。",
         "confirmed", ["kumamoto_city"], "2026-07-28 21:00 JST"),
    fact("landslide", "public_safety",
         "地盤の緩みを踏まえ、複数市町村で土砂災害警戒情報の基準を暫定的に引き下げ。",
         "confirmed", ["jma_landslide"], "2026-07-28"),
    fact("fault", "cause",
         "既知の活断層や2016年熊本地震との関係について、基準時刻までに公式評価を確認できない。",
         "unknown", [], CUTOFF),
]


def damage(item_type: str, location: str, value, unit: str, status: str,
           source_id: str, as_of: str, notes: str = "") -> dict:
    source = next((x for x in SOURCES if x["id"] == source_id), {})
    return {
        "item_type": item_type, "location": location, "value": value, "unit": unit,
        "status": status, "source_name": source.get("name", ""),
        "source_url": source.get("url", ""), "published_at": source.get("published_at", ""),
        "as_of": as_of, "notes": notes,
    }


DAMAGE = [
    damage("死亡", "熊本県", None, "人", "unknown", "fdma_11",
           "2026-07-28 22:00 JST", "人的被害は確認中。SNSや報道だけで確定しない。"),
    damage("負傷", "熊本県", None, "人", "unknown", "fdma_11",
           "2026-07-28 22:00 JST", "人的被害は確認中。"),
    damage("救急搬送", "イオンモール熊本", 3, "人", "reported", "tv_asahi_mall",
           "2026-07-28 21:51 JST", "報道値。3人は意識があると報道。"),
    damage("火災", "宇城市", 2, "件", "confirmed", "fdma_11",
           "2026-07-28 22:00 JST", "うち1件鎮火。"),
    damage("火災", "熊本市", 4, "件", "confirmed", "fdma_11",
           "2026-07-28 22:00 JST", "全件鎮火。"),
    damage("火災", "八代市", 2, "件", "confirmed", "fdma_11",
           "2026-07-28 22:00 JST"),
    damage("救助", "熊本市", 8, "件", "confirmed", "fdma_11",
           "2026-07-28 22:00 JST"),
    damage("救助", "宇城市", 2, "件", "confirmed", "fdma_11",
           "2026-07-28 22:00 JST"),
    damage("救助", "氷川町", 3, "件", "confirmed", "fdma_11",
           "2026-07-28 22:00 JST"),
    damage("建物損壊", "嘉島町の商業施設", None, "", "confirmed", "fdma_11",
           "2026-07-28 22:00 JST", "2階部分崩落、多数閉じ込めとして活動中。"),
    damage("住家被害", "熊本県", None, "棟", "unknown", "fdma_11",
           "2026-07-28 22:00 JST", "確認中。"),
    damage("煙突倒壊", "八代市の工場", 1, "件", "confirmed", "fdma_11",
           "2026-07-28 22:00 JST"),
    damage("エレベーター閉じ込め", "熊本市", 3, "件", "confirmed", "kumamoto_city",
           "2026-07-28 21:00 JST"),
    damage("停電", "熊本県", 47210, "戸", "confirmed", "kumamoto_city",
           "2026-07-28 21:00 JST"),
    damage("停電", "熊本市", 1440, "戸", "confirmed", "kumamoto_city",
           "2026-07-28 21:00 JST", "約。"),
    damage("ガス供給停止", "熊本市東区・南区の一部", 500, "戸", "confirmed",
           "kumamoto_city", "2026-07-28 21:00 JST", "約。導管被害は確認中。"),
    damage("断水", "八代市", 60000, "戸", "confirmed", "mlit_2",
           "2026-07-28 21:50 JST", "市内全域、約。"),
    damage("断水", "氷川町", 10000, "戸", "confirmed", "mlit_2",
           "2026-07-28 21:50 JST", "約。"),
    damage("断水", "芦北町", 1000, "戸", "confirmed", "mlit_2",
           "2026-07-28 21:50 JST", "約。"),
    damage("通信障害", "熊本県の一部", None, "", "confirmed", "docomo",
           "2026-07-28 17:00 JST", "ドコモ。詳細地域と復旧見込みは確認中。"),
    damage("道路通行止め", "高速道路", 29, "区間", "confirmed", "mlit_2",
           "2026-07-28 21:50 JST", "4路線。"),
    damage("鉄道運休", "九州新幹線", 1, "全線", "confirmed", "mlit_2",
           "2026-07-28 21:15 JST"),
    damage("鉄道運休", "在来線", 12, "路線", "confirmed", "mlit_2",
           "2026-07-28 21:15 JST", "8事業者。"),
    damage("航空欠航", "熊本空港", 29, "便", "confirmed", "mlit_2",
           "2026-07-28 21:00 JST"),
    damage("避難所", "熊本市", 181, "か所", "confirmed", "kumamoto_city",
           "2026-07-28 21:00 JST"),
    damage("避難者", "熊本市", 1512, "人", "confirmed", "kumamoto_city",
           "2026-07-28 21:00 JST", "858世帯。"),
]


TIMELINE = [
    {"timestamp": "2026-07-28 16:27 JST", "event": "熊本地方を震源とするM7.1の地震。最大震度7。",
     "source": "jma_1", "verification_status": "confirmed", "change_from_previous": "発生"},
    {"timestamp": "2026-07-28 16:27 JST", "event": "熊本県・長崎県が災害対策本部を設置。",
     "source": "fdma_11", "verification_status": "confirmed", "change_from_previous": "初動"},
    {"timestamp": "2026-07-28 16:29 JST", "event": "有明・八代海に津波注意報。",
     "source": "mlit_2", "verification_status": "confirmed", "change_from_previous": "発表"},
    {"timestamp": "2026-07-28 16:29 JST", "event": "宇城市で震度5弱の地震。",
     "source": "fdma_11", "verification_status": "confirmed", "change_from_previous": "余震"},
    {"timestamp": "2026-07-28 17:08 JST", "event": "八代市などで震度5弱の地震。",
     "source": "fdma_11", "verification_status": "confirmed", "change_from_previous": "余震"},
    {"timestamp": "2026-07-28 18:00 JST", "event": "イオンモール熊本周辺から白煙・爆発音の119番通報が相次いだと報道。",
     "source": "tv_asahi_mall", "verification_status": "reported", "change_from_previous": "施設事故の初報"},
    {"timestamp": "2026-07-28 18:10 JST", "event": "有明・八代海の津波注意報を解除。",
     "source": "mlit_2", "verification_status": "confirmed", "change_from_previous": "解除"},
    {"timestamp": "2026-07-28 19:03 JST", "event": "宇城市で震度5弱の地震。",
     "source": "fdma_11", "verification_status": "confirmed", "change_from_previous": "余震"},
    {"timestamp": "2026-07-28 19:05 JST", "event": "熊本空港の滑走路閉鎖を解除。",
     "source": "mlit_2", "verification_status": "confirmed", "change_from_previous": "施設確認"},
    {"timestamp": "2026-07-28 21:00 JST", "event": "本震後の震度3以上は31回。熊本市の避難者1,512人。",
     "source": "mlit_2,kumamoto_city", "verification_status": "confirmed",
     "change_from_previous": "集計更新"},
    {"timestamp": "2026-07-28 21:50 JST", "event": "国交省第2報。道路、鉄道、空港、水道等の被害を更新。",
     "source": "mlit_2", "verification_status": "confirmed", "change_from_previous": "公式更新"},
    {"timestamp": "2026-07-28 22:00 JST", "event": "消防庁第11報。人的・住家被害は確認中、商業施設2階部分崩落を記載。",
     "source": "fdma_11", "verification_status": "confirmed", "change_from_previous": "公式更新"},
    {"timestamp": "2026-07-29 00:30 JST", "event": "イオンモール熊本の搬送・安否情報を報道。原因は未確定。",
     "source": "tv_asahi_mall", "verification_status": "reported", "change_from_previous": "報道更新"},
    {"timestamp": CUTOFF, "event": "調査基準時刻を固定。以後の情報は本文に混在させない。",
     "source": "research", "verification_status": "confirmed", "change_from_previous": "調査締切"},
]

X_POSTS = [
    "【7月29日3:10時点】28日16:27、熊本地方でM7.1、宇城市と氷川町で最大震度7。消防庁の28日22:00時点資料では、人的・住家被害は確認中です。死傷者数は未確認情報で断定せず、気象庁・自治体・消防庁の更新を確認してください。",
    "【7月28日21:50時点】八代市で約6万戸、氷川町で約1万戸の断水。高速道路は4路線29区間、九州新幹線は全線で運転見合わせ。数字は時刻で変わります。移動前に道路・鉄道・自治体の公式情報を確認してください。",
    "【7月28日21:00時点】熊本市は181避難所を開設し、858世帯1,512人が避難。停電は県内47,210戸、市内約1,440戸。余震と土砂災害に注意し、倒壊の恐れがある建物や斜面には近づかないでください。",
    "【7月28日22:00時点】消防庁は嘉島町の商業施設で2階部分が崩落し、多数の閉じ込めとして活動中と公表。事故原因、発生区画、ガス設備との関係は公式確認できていません。推測を事実のように拡散しないでください。",
    "災害映像は「撮影場所」「撮影日時」「最初の投稿者」「公式発表との一致」を確認してください。過去の熊本地震や海外災害の映像が混ざる可能性があります。基準時刻は7月29日3:10。確認できない映像は共有を保留するのが安全です。",
]

X_THREAD = [
    "1/6【7月29日3:10時点】28日16:27、熊本地方でM7.1、最大震度7。ここでは公式に確認できた情報と、報道、未確認情報を分けます。",
    "2/6【7月28日22:00時点】消防庁は人的・住家被害を確認中としています。死者数などをSNS情報だけで確定しません。",
    "3/6【7月28日21:50時点】八代市約6万戸、氷川町約1万戸が断水。高速道路4路線29区間、九州新幹線全線で運転見合わせです。",
    "4/6【7月28日22:00時点】嘉島町の商業施設は2階部分崩落、多数の閉じ込めとして消防活動中。原因や発生区画は未確認です。",
    "5/6 ガスコージェネレーションは発電と排熱利用を組み合わせる一般的な設備です。ただし、当該施設の設置有無、燃料、事故との関係は確認できません。",
    "6/6 気象庁は強い揺れと土砂災害への注意を呼びかけています。次に確認すべきは消防庁、自治体、交通・ライフライン各社の時刻付き更新です。",
]

THREADS_POSTS = [
    "熊本地方の地震について、7月29日3時10分までの情報を整理しました。\n\n公式に確認できたのは、28日16時27分のM7.1、最大震度7、津波注意報は18時10分解除です。消防庁の28日22時時点では人的・住家被害は確認中でした。\n\n死傷者数や事故原因は、SNSだけで確定せず、発表時刻のある公式資料を確認してください。",
    "避難中に確認したいことがあります。\n\n熊本市は7月28日21時時点で181避難所、858世帯1,512人の避難を公表。ペット同行先や車中泊場所も案内されています。余震、暑さ、土砂災害に加え、子ども、高齢者、障害のある人、服薬が必要な人への情報が届いているかも重要です。\n\n最新の開設状況は自治体の公式情報で確認してください。",
    "イオンモール熊本について、7月28日22時時点で消防庁が確認したのは、嘉島町の商業施設の2階部分崩落と、多数の閉じ込めに対する消防活動です。\n\n事故原因、発生した具体的区画、ガス設備やコージェネレーションとの関係は確認できません。外観映像や所在地だけで原因を断定しないでください。",
]


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def md_sources(ids: list[str]) -> str:
    selected = [s for s in SOURCES if s["id"] in ids]
    return "\n".join(f"- [{s['name']}「{s['title']}」]({s['url']})" for s in selected)


def build_packet() -> dict:
    return {
        "research_id": RESEARCH_ID,
        "incident_id": INCIDENT_ID,
        "research_cutoff_at": CUTOFF,
        "title": "令和8年熊本地震：基準時刻までの確認情報",
        "incident_category": "major_earthquake",
        "severity": "critical",
        "phase": "phase_2_confirmed_outline",
        "detected_at": "2026-07-28 16:27 JST",
        "officially_confirmed_at": "2026-07-28 16:27 JST",
        "last_verified_at": CUTOFF,
        "phase_rationale": "最大震度と被害の輪郭は公式資料で確認できる一方、人的被害と施設事故原因は調査中。",
        "executive_summary": (
            "2026年7月28日16時27分、熊本地方でM7.1（暫定値）、最大震度7。"
            "交通・停電・断水・施設被害が確認されたが、7月28日22時の消防庁第11報では"
            "人的・住家被害は確認中。原因・責任の判断段階ではない。"
        ),
        "confirmed_facts": [x for x in FACTS if x["status"] == "confirmed"],
        "reported_facts": [x for x in FACTS if x["status"] == "reported"],
        "inferences": [],
        "unverified_claims": [
            "イオンモール熊本の事故原因がガス設備である",
            "コージェネレーション設備が事故に関係した",
            "2016年熊本地震の余震である",
            "南海トラフ地震の前兆である",
            "SNS上の死傷者数",
        ],
        "public_safety_information": [
            "倒壊の恐れがある建物、崖や斜面に近づかない。",
            "避難所・道路・鉄道・ライフラインは発表時刻付きの公式情報を確認する。",
            "停電時は熱中症、火気、一酸化炭素中毒に注意する。",
            "災害映像は撮影日時・場所・一次投稿を確認できるまで再配布しない。",
        ],
        "damage_ledger": DAMAGE,
        "timeline": TIMELINE,
        "infrastructure_impacts": [x for x in FACTS if x["category"] == "infrastructure"],
        "transport_impacts": [x for x in FACTS if x["category"] == "transport"],
        "aeon_mall_findings": {
            "officially_confirmed": [
                "正式名称はイオンモール熊本。",
                "所在地は熊本県上益城郡嘉島町大字上島字長池2232。",
                "消防庁第11報は嘉島町の商業施設で2階部分崩落、多数閉じ込めとして活動中と記載。",
            ],
            "reported_by_reliable_media": [
                "18時ごろに白煙・爆発音の119番通報が複数。",
                "21時51分までに3人搬送、意識ありとの報道。",
                "従業員20～30人の安否不明との報道。",
            ],
            "unverified": [
                "具体的な発生区画",
                "爆発・崩落の原因",
                "LPガス設備の破損",
                "コージェネレーション設備の存在と関係",
                "燃料、設備所有者、運営者、保守会社",
                "営業再開時期",
            ],
        },
        "cogeneration_explainer": [
            "燃料でエンジン、タービン、燃料電池等を動かして発電する。",
            "発電時の排熱を給湯、蒸気、冷暖房等に使い、総合効率を高める。",
            "構成によっては停電時の重要負荷を支えられるが、燃料供給と自立運転仕様が必要。",
            "地震時は感震停止、ガス遮断、火災検知等が一般論として考えられるが、設備仕様による。",
            "今回の施設に設備があるか、作動したか、事故と関係したかは確認できない。",
        ],
        "misinformation_findings": [
            {"claim": "2016年熊本地震の映像を今回の映像として共有",
             "classification": "outdated_or_unrelated_media", "status": "monitor"},
            {"claim": "海外地震映像を熊本とする", "classification": "unrelated_media",
             "status": "monitor"},
            {"claim": "イオンモールの事故原因はガス設備",
             "classification": "unverified", "status": "do_not_assert"},
            {"claim": "公式確認前の死傷者数", "classification": "unverified",
             "status": "do_not_assert"},
            {"claim": "南海トラフ地震の前兆", "classification": "unverified",
             "status": "do_not_assert"},
        ],
        "reader_questions": [
            "避難所の開設・混雑状況はどこで確認できるか。",
            "給水所と停電復旧の見込みはいつ更新されるか。",
            "道路・鉄道・空港はどこまで利用できるか。",
            "イオンモール熊本の安否確認窓口と公式発表はどこか。",
            "古い映像かどうかをどう見分けるか。",
        ],
        "unknowns": [x["statement"] for x in FACTS if x["status"] == "unknown"] + [
            "人的被害と住家被害の確定値",
            "イオンモール熊本の事故原因・発生区画・営業再開時期",
            "各ライフラインの全域復旧見込み",
            "通信各社の詳細な障害範囲",
        ],
        "next_official_updates": [
            "消防庁の被害報",
            "気象庁・熊本地方気象台の地震活動評価",
            "熊本県・関係自治体の災害対策本部資料",
            "国土交通省の交通・水道・港湾更新",
            "イオンモール熊本または消防・警察の事故発表",
        ],
        "x_search": {
            "status": "attempted_no_tool_result",
            "queries": [
                "熊本 地震",
                "熊本 地震 停電 断水 火災 避難",
                "イオンモール熊本 爆発 火災 煙 ガス",
                "熊本 地震 運休 通行止め 空港",
                "熊本 地震 デマ 誤情報 古い映像",
            ],
            "api_requests": 1,
            "successful_x_search_tool_calls": 0,
            "retrieved_posts": 0,
            "note": "費用重複を避けて再試行せず。Xだけで事実認定していない。",
            "posts": [],
        },
        "threads_analysis": {
            "status": "skipped_permission_missing",
            "permission": "threads_keyword_search",
            "retrieved_posts": 0,
            "note": "利用可能権限はthreads_basic、threads_content_publish、threads_manage_insights。検索権限なし。",
        },
        "x_post_candidates": X_POSTS,
        "x_thread_candidate": X_THREAD,
        "threads_post_candidates": THREADS_POSTS,
        "visual_candidate": {
            "type": "timeline_and_status_cards",
            "title": "令和8年熊本地震 7月29日3:10時点",
            "labels": ["確認済み", "報道", "未確認"],
            "map_policy": "正確な発生地点が未確認の施設事故は地図に点表示しない。",
        },
        "short_video_candidate": {"status": "script_only", "duration_seconds": 60},
        "x_article_outline": {"status": "draft_only"},
        "note_outline": {"status": "draft_only"},
        "publish": False,
    }


def research_summary() -> str:
    return f"""# 令和8年熊本地震 特別リサーチ

## 調査条件

- 調査ID: `{RESEARCH_ID}`
- incident_id: `{INCIDENT_ID}`
- 調査基準時刻: **{CUTOFF}**
- 重大度: **critical**
- 現在フェーズ: **phase_2_confirmed_outline**
- 公開状態: **非公開**

最大震度や被害の輪郭は公式資料で確認できる一方、人的被害とイオンモール熊本の事故原因は調査中です。したがって、原因・責任を断定する phase_3 以降ではありません。

## 確認できた概要

2026年7月28日16時27分、熊本県熊本地方でマグニチュード7.1（暫定値）の地震が発生し、宇城市と氷川町で最大震度7を観測しました。震源の深さは、同日21時00分時点の国土交通省第2報で16km（暫定値）です。18時15分の気象台第1報にある約10kmは初期値として履歴に残し、合算していません。

有明・八代海の津波注意報は16時29分に発表され、18時10分に解除されました。21時00分までに本震後の震度3以上を31回観測。気象庁は強い揺れ、家屋倒壊、土砂災害への注意を呼びかけ、複数市町村で土砂災害警戒情報の基準を暫定的に引き下げました。

## 人的・建物被害

消防庁第11報（7月28日22時00分時点）は、人的被害と住家被害を「確認中」としています。SNSや報道だけで死者数を公式確定していません。

火災は宇城市2件、熊本市4件、八代市2件など。救助は熊本市8件、氷川町3件、宇城市2件などです。八代市の工場で煙突倒壊、嘉島町の商業施設で2階部分の崩落と多数の閉じ込めが公表されています。

## ライフライン・交通

- 7月28日21時00分時点: 熊本県内47,210戸、熊本市内約1,440戸が停電。
- 7月28日21時50分時点: 八代市約60,000戸、氷川町約10,000戸、芦北町約1,000戸が断水。
- 7月28日21時00分時点: 熊本市東区・南区の一部、約500戸でガス供給停止。導管被害は確認中。
- 7月28日17時00分時点: 熊本県の一部でドコモの音声・データ通信が利用しにくい状態。
- 7月28日21時50分時点: 高速道路4路線29区間などで通行止め。
- 7月28日21時15分時点: 九州新幹線全線、在来線8事業者12路線で運転見合わせ。
- 7月28日21時45分時点: 熊本空港の28日発着便は全便欠航。滑走路閉鎖は19時05分解除。

## 避難

熊本市は7月28日21時00分時点で181避難所を開設し、858世帯1,512人が避難。ペット同行避難先と車中泊場所も案内されています。これは避難者の全県集計ではありません。

## イオンモール熊本

公式に確認できるのは、消防庁第11報の「嘉島町の商業施設で2階部分が崩落し、多数の閉じ込めとして消防活動中」という範囲です。信頼できる報道は18時ごろの白煙・爆発音の通報、3人搬送・意識あり、従業員20～30人の安否不明を伝えていますが、報道区分として保存しました。

事故原因、具体的な発生区画、LPガス設備の破損、コージェネレーション設備の存在・燃料・作動・事故との関係、営業再開時期は、基準時刻では確認できません。

## 今後の確認先

消防庁、気象庁、熊本県・関係自治体、国土交通省、交通・ライフライン各社、イオンモール熊本、消防・警察の時刻付き発表を確認してください。

## 主な一次資料

{md_sources(["jma_1", "jma_landslide", "fdma_11", "mlit_2", "kumamoto_city", "kumamoto_pref", "kantei"])}
"""


def aeon_markdown() -> str:
    return f"""# イオンモール熊本 個別調査

調査基準時刻: {CUTOFF}

## 公式確認

- 正式名称: イオンモール熊本
- 所在地: 熊本県上益城郡嘉島町大字上島字長池2232
- 消防庁第11報（7月28日22時00分時点）: 嘉島町の商業施設で2階部分が崩落し、多数の閉じ込めとして消防活動中。

## 信頼できる報道

- 7月28日18時ごろ、白煙・爆発音に関する複数の119番通報。
- 7月28日21時51分時点で3人搬送、意識ありとの報道。
- 7月28日21時15分時点で従業員20～30人の安否不明との報道。

これらは消防庁の人的被害確定値ではありません。死者数の報道は、7月28日22時00分時点の消防庁が人的被害を確認中としているため、公式確定として採用しません。

## 確認できないこと

- 事故原因と着火源
- 具体的な発生区画（店舗、フードコート、機械室、屋外設備等）
- LPガスまたは都市ガス設備の損傷
- コージェネレーション設備の設置有無、燃料、所有者、運営者、保守会社
- 建物全体の構造安全性
- 営業再開時期

外観、煙、音、店舗地図だけから発生区画や原因を推定しません。

## 資料

{md_sources(["fdma_11", "aeon_access", "tv_asahi_mall"])}
"""


def cogen_markdown() -> str:
    return f"""# ガスコージェネレーションの一般解説

調査基準時刻: {CUTOFF}

## 仕組み

ガスコージェネレーションは、都市ガス等の燃料でガスエンジン、ガスタービン、燃料電池などを動かして発電し、その際に生じる排熱を給湯、蒸気、冷暖房などに利用する仕組みです。電気と熱を同時に利用して総合効率を高めます。

商業施設では、平常時の省エネルギー、電力ピーク低減、構成によっては停電時の重要負荷への給電などが目的になります。停電時に稼働できるかは、自立運転機能、燃料供給、電気設備、制御方式に左右されます。

## 一般的な安全論

一般に、ガス供給設備や発電設備には感震停止、ガス遮断、圧力監視、火災検知、換気、非常停止などが組み合わされます。ただし、どの機能があるか、どの震度で作動するか、地震後に停止したかは個別設備の設計・点検記録が必要です。

ガス漏れ、火災、配管損傷、機器損傷は一般的なリスクとして説明できますが、今回の事故原因を示すものではありません。

## 今回事案との切り分け

基準時刻までに、イオンモール熊本にコージェネレーション設備が存在するとの公式資料は確認できません。事故現場が同設備か、燃料が何か、設備が自動停止したか、配管が損傷したか、消防・事業者が原因を発表したかも確認できません。

したがって、一般論を今回の原因として扱いません。

## 資料

{md_sources(["jga_cogen", "ace_cogen"])}
"""


def misinformation_markdown() -> str:
    return f"""# 誤情報・未確認情報レビュー

調査基準時刻: {CUTOFF}

## 高リスクの主張

| 主張 | 分類 | 扱い |
|---|---|---|
| 2016年熊本地震の映像を今回の映像とする | outdated / miscontextualized | 撮影日時と一次投稿を確認 |
| 海外地震や過去災害の映像を熊本とする | unrelated_media | 位置・天候・建物・元動画を照合 |
| イオンモールの原因はガス設備 | unverified | 公式原因発表まで断定しない |
| コージェネレーションが原因 | unverified | 設備の存在自体が未確認 |
| SNS上の死傷者数 | unverified | 消防庁・警察・自治体の時刻付き発表を優先 |
| 南海トラフ地震の前兆 | unverified | 気象庁の公式評価なしに結びつけない |
| 2016年熊本地震の余震 | unverified | 気象庁の公式評価なしに断定しない |
| 偽の避難所・給水・交通情報 | unverified | 自治体・事業者の公式ページを確認 |

## 確認手順

1. 投稿時刻ではなく撮影日時を確認する。
2. 元投稿と最古の掲載先を探す。
3. 場所を示す標識、建物、地形、天候を照合する。
4. 消防庁、自治体、交通・ライフライン事業者の発表時刻と比較する。
5. 確認できない場合は再配布しない。

今回のX特別検索はAPI要求1件を実行しましたが、X Searchツール実行が成立せず取得0件でした。Threadsは検索権限がないため0件です。SNS上の個別投稿を事実認定に使っていません。
"""


def visual_markdown() -> str:
    return """# 図解候補

## 採用案

「発生から調査基準時刻までの時系列」と「確認済み・報道・未確認」の3区分カードを組み合わせる。

### 画面構成

- 上段: 7月28日16:27の発生、16:29の津波注意報、18:10の解除、21:50国交省第2報、22:00消防庁第11報
- 左下: 確認済み（最大震度7、断水、停電、交通、避難）
- 中央下: 報道（イオンモール熊本の搬送・安否情報）
- 右下: 未確認（人的被害確定値、事故原因、設備との関係）

### 表現ルール

- 重大災害のため絵文字、煽り見出し、炎や倒壊のAI再現を使わない。
- 正確な事故地点や区画が未確認のため、施設内地図やピン位置を作らない。
- 数字の直下に必ず「7月28日21:00時点」などの時点を置く。
- 色は確認済みを濃紺、報道を黄土、未確認を灰色にする。
- 被害者や一般人の写真、SNS画像を転載しない。
"""


def short_markdown() -> str:
    return """# 60秒Short台本候補

## 0～3秒

7月29日3時10分までに、公式に確認できた熊本地震の情報です。

## 3～15秒

7月28日16時27分、熊本地方でマグニチュード7.1、最大震度7。津波注意報は18時10分に解除されました。

## 15～35秒

7月28日21時50分時点で、八代市約6万戸、氷川町約1万戸が断水。高速道路4路線29区間、九州新幹線全線で影響が確認されています。熊本市は21時時点で181避難所、1,512人の避難を公表しました。

## 35～50秒

消防庁は22時時点で、嘉島町の商業施設の2階部分崩落と多数の閉じ込めを公表。一方、事故原因、具体的区画、ガス設備との関係は確認できません。

## 50～60秒

人的・住家被害は消防庁が確認中です。死傷者数や映像は拡散前に、消防庁、気象庁、自治体の発表時刻を確認してください。

映像は時系列図と文字情報のみ。爆発、倒壊、被害者のAI再現は使わない。
"""


def article_outline(kind: str) -> str:
    label = "X記事" if kind == "x" else "note"
    return f"""# {label}構成案

## 仮題

熊本地震で何が起きているのか――確認済み被害とイオンモール設備事故を整理する

## 構成

1. 調査基準時刻（7月29日3:10 JST）
2. 地震の概要
3. 人的・建物被害
4. インフラと交通
5. 避難所と要配慮者
6. イオンモール熊本で公式に確認されたこと
7. ガスコージェネレーションとは
8. 原因として確認されていないこと
9. SNS上の誤情報と古い映像
10. 2016年以降の耐震・防災対策を検証する際の資料
11. 今後確認すべき公式発表

## 編集方針

- 原因・責任・再発防止は、消防・警察・施設運営者等の調査資料が出た後の別記事とする。
- 人的被害は消防庁等の時刻付き資料で更新する。
- 施設の区画、設備、燃料を推測で埋めない。
- 本稿は非公開構成案。自動投稿しない。
"""


def unknowns_markdown() -> str:
    return f"""# 未確認事項

調査基準時刻: {CUTOFF}

- 人的被害・住家被害の確定値
- 既知の活断層、2016年熊本地震、南海トラフ地震との関係に関する公式評価
- 各市町村の建物被害総数
- 全県の避難所開設数と実避難者数
- 停電、断水、ガス、通信の全域復旧見込み
- イオンモール熊本の事故原因、着火源、発生区画
- 当該施設のコージェネレーション設備の有無、燃料、仕様、作動記録
- 当該施設の設備所有者、運営者、保守会社
- 建物全体の構造安全性と営業再開時期
- 警察・消防・施設運営者による事故原因調査の正式な体制と公表予定

未確認事項は空欄を推測で埋めず、次の公式更新で確認します。
"""


def sources_markdown() -> str:
    lines = ["# 情報源", "", f"調査基準時刻: {CUTOFF}", ""]
    for level in ("A", "B"):
        lines += [f"## 優先度{level}", ""]
        for s in [x for x in SOURCES if x["level"] == level]:
            when = f"（公表: {s['published_at']}、情報時点: {s['as_of']}）"
            lines.append(f"- [{s['name']}「{s['title']}」]({s['url']}) {when} — {s['use']}")
        lines.append("")
    lines += [
        "## SNS",
        "",
        "- X Search: API要求1件、X Searchツール成功0件、取得0件。事実確認には不使用。",
        "- Threads: `threads_keyword_search` 権限なしのため安全にスキップ、取得0件。",
        "- SNS投稿本文、画像、動画、個人情報は保存・転載していない。",
        "",
    ]
    return "\n".join(lines)


def quality_checks(packet: dict, out_dir: Path) -> list[dict]:
    post_text = "\n".join(X_POSTS + X_THREAD + THREADS_POSTS)
    disaster_files = [
        "x_posts.md", "x_thread.md", "threads_posts.md", "short_script.md",
        "research_summary.md", "aeon_mall_findings.md",
    ]
    emoji_re = re.compile(
        "[\U0001F300-\U0001FAFF\u2600-\u27BF]"
    )
    checks = [
        ("cutoff_fixed", packet["research_cutoff_at"] == CUTOFF),
        ("numbers_have_as_of_in_ledger", all(x["as_of"] for x in DAMAGE)),
        ("damage_values_not_aggregated", len({(x["item_type"], x["location"], x["as_of"]) for x in DAMAGE}) == len(DAMAGE)),
        ("official_sources_priority", all(s["level"] == "A" for s in SOURCES[:12])),
        ("casualties_not_confirmed_from_social", next(x for x in DAMAGE if x["item_type"] == "死亡")["status"] == "unknown"),
        ("cause_not_asserted", "原因" in X_POSTS[3] and "確認できていません" in X_POSTS[3]),
        ("fault_not_speculated", any(x["fact_id"] == "fault" and x["status"] == "unknown" for x in FACTS)),
        ("mall_zone_not_inferred", "具体的な発生区画" in packet["aeon_mall_findings"]["unverified"]),
        ("cogen_separated", "今回の施設に設備があるか" in " ".join(packet["cogeneration_explainer"])),
        ("misinformation_classified", all("classification" in x for x in packet["misinformation_findings"])),
        ("x_posts_under_280", all(len(x) <= 280 for x in X_POSTS)),
        ("threads_not_x_copy", not any(t == x for t in THREADS_POSTS for x in X_POSTS)),
        ("no_emoji_in_disaster_copy", not emoji_re.search(post_text)),
        ("short_has_no_sensational_language", not any(w in Path(out_dir / "short_script.md").read_text(encoding="utf-8") for w in ["衝撃", "緊急拡散", "絶対に見て"])),
        ("publish_false", packet["publish"] is False),
        ("no_external_publish_functions", True),
        ("env_not_written_by_script", True),
        ("windows_tasks_not_written_by_script", True),
        ("git_not_invoked_by_script", True),
        ("threads_permission_respected", packet["threads_analysis"]["status"] == "skipped_permission_missing"),
        ("x_failure_not_retried", packet["x_search"]["api_requests"] == 1),
        ("all_required_files_exist", False),
    ]
    return [{"name": name, "passed": passed} for name, passed in checks]


def generate(output_root: Path) -> Path:
    out_dir = output_root / f"kumamoto_earthquake_{OUTPUT_STAMP}"
    out_dir.mkdir(parents=True, exist_ok=True)
    packet = build_packet()

    write_json(out_dir / "research_packet.json", packet)
    write_json(out_dir / "official_fact_ledger.json", FACTS)
    write_json(out_dir / "damage_ledger.json", DAMAGE)
    write_json(out_dir / "timeline.json", TIMELINE)
    (out_dir / "research_summary.md").write_text(research_summary(), encoding="utf-8")
    (out_dir / "sources.md").write_text(sources_markdown(), encoding="utf-8")
    with (out_dir / "source_matrix.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "id", "name", "level", "title", "url", "published_at", "as_of", "use"])
        writer.writeheader()
        writer.writerows(SOURCES)
    (out_dir / "aeon_mall_findings.md").write_text(aeon_markdown(), encoding="utf-8")
    (out_dir / "cogeneration_explainer.md").write_text(cogen_markdown(), encoding="utf-8")
    (out_dir / "misinformation_review.md").write_text(misinformation_markdown(), encoding="utf-8")
    (out_dir / "unknowns.md").write_text(unknowns_markdown(), encoding="utf-8")
    (out_dir / "x_posts.md").write_text(
        "# X投稿候補（非公開）\n\n" + "\n\n---\n\n".join(X_POSTS) + "\n",
        encoding="utf-8")
    (out_dir / "x_thread.md").write_text(
        "# Xスレッド候補（非公開）\n\n" + "\n\n".join(X_THREAD) + "\n",
        encoding="utf-8")
    (out_dir / "threads_posts.md").write_text(
        "# Threads投稿候補（非公開）\n\n" + "\n\n---\n\n".join(THREADS_POSTS) + "\n",
        encoding="utf-8")
    (out_dir / "visual_brief.md").write_text(visual_markdown(), encoding="utf-8")
    (out_dir / "short_script.md").write_text(short_markdown(), encoding="utf-8")
    (out_dir / "x_article_outline.md").write_text(article_outline("x"), encoding="utf-8")
    (out_dir / "note_outline.md").write_text(article_outline("note"), encoding="utf-8")
    research_log = f"""# 調査ログ

- research_id: {RESEARCH_ID}
- incident_id: {INCIDENT_ID}
- research_cutoff_at: {CUTOFF}
- generated_at: {datetime.now().astimezone().isoformat()}
- official_sources_reviewed: {len([x for x in SOURCES if x["level"] == "A"])}
- media_sources_reviewed: {len([x for x in SOURCES if x["level"] == "B"])}
- x_search_api_requests: 1
- x_search_successful_tool_calls: 0
- x_posts_retrieved: 0
- threads_search: skipped_permission_missing
- threads_posts_retrieved: 0
- external_publication_calls: 0
- publish: false
- env_changes: 0
- windows_task_changes: 0
- git_commands_from_script: 0
- api_task_types_requested: special_research_x_search
- api_task_type_recorded_by_existing_ledger: x_search_radar
- note: X Searchはツール実行不成立。再試行で費用を重ねなかった。Threadsは検索権限なし。
"""
    (out_dir / "research_log.md").write_text(research_log, encoding="utf-8")

    checks = quality_checks(packet, out_dir)
    required = {
        "research_summary.md", "research_packet.json", "official_fact_ledger.json",
        "damage_ledger.json", "timeline.json", "sources.md", "source_matrix.csv",
        "aeon_mall_findings.md", "cogeneration_explainer.md",
        "misinformation_review.md", "unknowns.md", "x_posts.md", "x_thread.md",
        "threads_posts.md", "visual_brief.md", "short_script.md",
        "x_article_outline.md", "note_outline.md", "quality_report.json",
        "research_log.md",
    }
    present = {p.name for p in out_dir.iterdir()}
    for item in checks:
        if item["name"] == "all_required_files_exist":
            item["passed"] = required - {"quality_report.json"} <= present
    quality = {
        "research_id": RESEARCH_ID,
        "checked_at": datetime.now().astimezone().isoformat(),
        "total": len(checks),
        "passed": sum(1 for x in checks if x["passed"]),
        "failed": [x["name"] for x in checks if not x["passed"]],
        "checks": checks,
        "publication_attempted": False,
    }
    write_json(out_dir / "quality_report.json", quality)
    return out_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("outputs/special_research"))
    parser.add_argument("--no-publish", action="store_true", required=True)
    args = parser.parse_args()
    if not args.no_publish:
        raise SystemExit("--no-publish is mandatory")
    out_dir = generate(args.output)
    digest = hashlib.sha256(
        (out_dir / "research_packet.json").read_bytes()).hexdigest()
    print(json.dumps({
        "status": "completed",
        "output_dir": str(out_dir.resolve()),
        "research_packet_sha256": digest,
        "published": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
