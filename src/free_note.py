"""Human-approved free note article pipeline.

The local Markdown folder is the source of truth. This module never publishes
to note, controls a browser, or writes to X.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import unicodedata
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from api_budget import estimate_openai, finalize, reserve
from metrics_db import apply_additive_migrations, connect, db_path, init_db, write
from publishing_policy import normalize_topic_key


JST = ZoneInfo("Asia/Tokyo")
ARTICLE_TYPES = {
    "weekly_top5",
    "legislative_process",
    "cabinet_decision_vs_law",
    "social_insurance_burden",
    "party_policy_comparison",
    "evergreen_institutional_explainer",
    "weekly_deep_dive",
}
STATUSES = {
    "draft", "reviewing", "approved", "revision_required",
    "published", "rejected",
}
PROMPT_VERSION = "free-note-v1"
OFFICIAL_DOMAINS = (
    ".go.jp", "shugiin.go.jp", "sangiin.go.jp", "laws.e-gov.go.jp",
    "elaws.e-gov.go.jp", "courts.go.jp", "ndl.go.jp",
)
INTERNAL_TERMS = (
    "post_type", "hook_type", "critique_axis", "decision_reason",
    "quality_score", "prompt_version", "system_prompt", "JSONキー",
    "モデル名", "F型", "A型",
)
REQUIRED_HEADINGS = (
    "## 導入", "## 何が起きたか", "## 制度・政策の背景",
    "## 賛成側の最も強い主張", "## 反対側の最も強い主張",
    "## 久世ゆいの評価", "## 読者が今後見るべきポイント",
    "## 一次資料・参考資料",
)


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def note_root() -> Path:
    raw = os.environ.get("FREE_NOTE_OUTPUT_DIR", "outputs/note")
    path = Path(raw)
    if not path.is_absolute():
        path = _root() / path
    for name in ("drafts", "approved", "published", "failed"):
        (path / name).mkdir(parents=True, exist_ok=True)
    return path


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: dict) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def slugify(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip(" .-")
    return (text or "untitled")[:72].rstrip(" .-")


def _official(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return url.startswith("https://") and any(
            host == domain.lstrip(".") or host.endswith(domain)
            for domain in OFFICIAL_DOMAINS
        )
    except ValueError:
        return False


def _registry_sources() -> list[dict]:
    path = _root() / "config" / "free_note_primary_sources.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return [row for row in rows if _official(str(row.get("url", "")))]
    except Exception:
        return []


def _candidate_rows(path: Path | None = None, days: int = 7) -> list[dict]:
    cutoff = (datetime.now(JST) - timedelta(days=days)).isoformat()
    try:
        with closing(connect(path)) as conn:
            rows = conn.execute("""SELECT n.*,
              COALESCE(MAX(m.impressions),0) impressions,
              COALESCE(MAX(m.bookmarks),0) bookmarks,
              COALESCE(MAX(m.profile_clicks),0) profile_clicks
              FROM news_candidates n
              LEFT JOIN published_posts p ON p.topic_key=n.topic_key
              LEFT JOIN post_metrics m ON m.tweet_id=p.tweet_id
              WHERE n.fetched_at>=? AND n.verified=1 AND n.source_url<>''
              GROUP BY n.id ORDER BY n.final_news_score DESC,
              n.source_reliability_score DESC,n.fetched_at DESC""",
                (cutoff,)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def _dedupe_candidates(rows: list[dict], limit: int = 5) -> list[dict]:
    selected = []
    seen_urls = set()
    seen_topics = set()
    scored = []
    for original in rows:
        row = dict(original)
        importance = min(1.0, float(row.get("final_news_score", 0) or 0) / 10)
        utility = min(
            1.0, float(row.get("source_reliability_score", 0) or 0) / 10)
        source_availability = 1.0 if _official(
            str(row.get("source_url") or "")) else .65
        performance = min(1.0, (
            float(row.get("impressions", 0) or 0) / 10000
            + float(row.get("bookmarks", 0) or 0) / 100
            + float(row.get("profile_clicks", 0) or 0) / 100
        ) / 3)
        evergreen_value = .7 if row.get("genre") in {
            "policy", "law", "economy", "social_security"
        } else .5
        row["note_topic_score"] = round(
            importance * .25
            + utility * .25
            + source_availability * .20
            + performance * .10
            + evergreen_value * .15
            + .05,
            6,
        )
        scored.append(row)
    for row in sorted(
        scored, key=lambda item: item["note_topic_score"], reverse=True
    ):
        url = str(row.get("source_url") or "")
        topic = normalize_topic_key(
            row.get("topic_key") or row.get("title") or "")
        if not url or url in seen_urls or not topic or topic in seen_topics:
            continue
        metadata = {}
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            pass
        if metadata.get("correction_required") or metadata.get("manual_delete_required"):
            continue
        if float(metadata.get("anger_score", 0) or 0) >= 8:
            continue
        if metadata.get("trust_score") is not None and float(
            metadata.get("trust_score") or 0) < 5:
            continue
        seen_urls.add(url)
        seen_topics.add(topic)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _existing_metadata() -> list[tuple[Path, dict]]:
    rows = []
    root = note_root()
    for status_dir in ("drafts", "approved", "published", "failed"):
        for path in (root / status_dir).rglob("metadata.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                rows.append((path, payload))
            except Exception:
                continue
    return rows


def _topic_in_cooldown(article_type: str, topic_key: str,
                       now: datetime | None = None) -> tuple[bool, str | None]:
    now = now or datetime.now(JST)
    days = _int("FREE_NOTE_TOPIC_COOLDOWN_DAYS", 90)
    current_week = now.date() - timedelta(days=now.weekday())
    for _, row in _existing_metadata():
        if (
            row.get("generation_mode") == "dry_run_local"
            or row.get("status") in {
                "failed", "revision_required", "rejected"
            }
        ):
            continue
        generated = str(row.get("generated_at") or "")
        try:
            generated_at = datetime.fromisoformat(generated)
            age = now - generated_at
        except (TypeError, ValueError):
            continue
        if article_type == "weekly_top5":
            generated_week = generated_at.date() - timedelta(
                days=generated_at.weekday())
            if (row.get("article_type") == article_type
                    and generated_week == current_week):
                return True, str(row.get("content_id") or "")
            continue
        previous_topic = normalize_topic_key(
            row.get("primary_topic_key") or row.get("title") or "")
        if age <= timedelta(days=days) and previous_topic == normalize_topic_key(
            topic_key
        ):
            return True, str(row.get("content_id") or "")
    return False, None


def select_topic(article_type: str | None = None, topic: str | None = None,
                 *, path: Path | None = None,
                 now: datetime | None = None) -> dict:
    now = now or datetime.now(JST)
    article_type = article_type or "evergreen_institutional_explainer"
    if article_type not in ARTICLE_TYPES:
        raise ValueError("unsupported article type")
    rows = _dedupe_candidates(_candidate_rows(path), 5)
    evergreen = {
        "evergreen_institutional_explainer": [
            "閣議決定と法律成立の違い",
            "政令・省令・告示の違い",
            "国と地方自治体の役割分担",
            "予算案と法律案の違い",
        ],
        "legislative_process": [
            "法案成立までの流れ",
            "委員会審議と本会議の役割",
            "公布と施行は何が違うか",
        ],
        "cabinet_decision_vs_law": [
            "閣議決定と法律成立の違い",
            "政府方針に法的拘束力が生まれるまで",
        ],
        "social_insurance_burden": [
            "社会保険料は誰が負担しているか",
            "本人負担・事業主負担・公費負担の違い",
        ],
        "party_policy_comparison": [
            "政策を同じ基準で比較する方法",
            "政党公約を財源と実施期限で比べる方法",
        ],
        "weekly_deep_dive": [
            rows[0]["title"] if rows else "今週の制度変更",
        ],
        "weekly_top5": ["今週の政治ニュース5選"],
    }
    candidate_topics = evergreen[article_type]
    selected_topic = str(topic or candidate_topics[0]).strip()
    if not topic and article_type != "weekly_top5":
        for candidate in candidate_topics:
            duplicate, _ = _topic_in_cooldown(
                article_type, normalize_topic_key(candidate), now)
            if not duplicate:
                selected_topic = candidate
                break
    topic_key = normalize_topic_key(selected_topic)
    duplicate, previous_id = _topic_in_cooldown(
        article_type, topic_key, now)
    return {
        "article_type": article_type,
        "topic": selected_topic,
        "topic_key": topic_key,
        "candidates": rows,
        "duplicate": duplicate,
        "previous_content_id": previous_id,
        "selection_reason": (
            "explicit_topic" if topic else
            "weekly_verified_candidates" if article_type.startswith("weekly")
            else "unused_evergreen_institutional_topic"
        ),
    }


def _sources(selection: dict) -> tuple[list[dict], list[dict]]:
    primary = []
    secondary = []
    for row in selection.get("candidates", []):
        source = {
            "name": row.get("title") or row.get("source_name"),
            "publisher": row.get("source_name") or "",
            "published_at": row.get("published_at") or row.get("fetched_at") or "",
            "url": row.get("source_url") or "",
            "fact_used": row.get("title") or "",
            "verified_at": datetime.now(JST).isoformat(),
            "news_candidate_id": row.get("id"),
        }
        (primary if _official(source["url"]) else secondary).append(source)
    for row in _registry_sources():
        types = row.get("article_types") or []
        if types and selection["article_type"] not in types and "all" not in types:
            continue
        source = {
            "name": row.get("name", ""),
            "publisher": row.get("publisher", ""),
            "published_at": row.get("published_at", ""),
            "url": row.get("url", ""),
            "fact_used": row.get("fact_used", ""),
            "verified_at": datetime.now(JST).isoformat(),
            "news_candidate_id": None,
        }
        if source["url"] not in {item["url"] for item in primary}:
            primary.append(source)
    return primary, secondary


def _three_line_summary(selection: dict) -> list[str]:
    if selection["article_type"] == "weekly_top5":
        return [
            "今週の政治ニュースを、見出しの強さではなく制度上の重要性から整理します。",
            "確認済みの報道と公式資料を分け、決定済み事項と今後の手続きを区別します。",
            "来週、国会・予算・施行段階で確認すべき論点を示します。",
        ]
    return [
        f"今回のテーマは「{selection['topic']}」です。",
        "制度上の決定主体と手続を分け、賛成・反対双方の強い論点を確認します。",
        "ニュースを見たときに、読者自身が次の動きを検証できる形で整理します。",
    ]


def _local_article(selection: dict, primary: list[dict],
                   secondary: list[dict]) -> tuple[str, str]:
    title = selection["topic"]
    summary = "\n".join(f"- {line}" for line in _three_line_summary(selection))
    candidates = selection.get("candidates", [])
    if selection["article_type"] == "weekly_top5":
        event_lines = []
        for row in candidates[:5]:
            event_lines.append(
                f"### {row.get('title','確認済みニュース')}\n\n"
                "現時点で確認できるのは、上記の出来事が報じられたことです。"
                "見出しだけから制度の成立、施行、予算執行まで完了したと判断せず、"
                "国会や政府の公式資料で次の手続きを確認する必要があります。\n"
            )
        events = "\n".join(event_lines)
    else:
        events = (
            f"「{title}」を考える際、最初に必要なのは、誰が、どの手続で、"
            "どこまで決めたのかを分けることです。報道で「決定」と表現されても、"
            "法律案の提出、国会での審議と議決、公布、施行は同じ段階ではありません。"
        )
    primary_links = "\n".join(
        f"- [{row['name']}]({row['url']})" for row in primary)
    secondary_links = "\n".join(
        f"- [{row['name']}]({row['url']})" for row in secondary[:8])
    article = f"""# {title}

{summary}

## 導入

政治ニュースでは、短い言葉で「決定」「成立」「実施」が並びます。しかし、それぞれの言葉が示す段階を取り違えると、まだ審議中の案を確定事項と思い込んだり、成立済みの法律が直ちに生活を変えると誤解したりします。今このテーマを見る意味は、賛否を急ぐ前に、権限、費用、責任、検証時期を確認できるようにすることです。

## 何が起きたか

{events}

確認済みの事実と、今後起こり得ることは分けて読む必要があります。日付、決定主体、正式名称、採決結果、公布日、施行日は、報道だけでなく各機関の原資料に戻って確認します。情報が更新された場合は、古い見出しをそのまま前提にせず、最新の公式記録を優先します。

## 制度・政策の背景

内閣の意思決定と、国会による立法は役割が異なります。政府が方針を決めても、それだけで新しい法律が成立するわけではありません。法律案は国会へ提出され、委員会と本会議で審議されます。成立後も、公布と施行の時点が一致するとは限りません。附則や政令で施行時期が定められる場合があるため、生活への影響を判断するときは施行日まで確認する必要があります。

政策の評価では、目的だけでなく実施手段も重要です。財源、行政コスト、地方自治体や事業者の負担、既存制度との整合性、効果測定の方法を確認します。理念に賛成できても手段に問題がある場合があり、逆に制度設計を修正すれば目的を実現しやすくなる場合もあります。

## 賛成側の最も強い主張

賛成側の強い主張は、社会課題への対応を先送りせず、政府や国会が明確な方針と制度を示す必要があるというものです。全国で共通する課題には一定の統一ルールが必要であり、責任主体や期限を定めることで、行政のばらつきを減らせる可能性があります。また、早期に方向性を示すことが、自治体や民間の準備を促すという見方もあります。

この主張を公平に評価するには、政策目的が現実の課題に対応しているか、実施主体に必要な権限と資源があるか、結果を測る指標が公開されるかを確認する必要があります。

## 反対側の最も強い主張

反対側の強い主張は、目的が妥当でも、権限の集中、費用負担、審議時間、地域差への配慮が不十分なら、制度が別の問題を生むというものです。方針を急いで決めることで、国会審議や現場との調整が形式化する懸念もあります。将来負担を伴う政策では、財源の説明が曖昧なままでは持続性を判断できません。

反対論も感情的な拒否だけで終わらせず、代替案、必要な修正、検証可能な条件を示しているかを見るべきです。単に「反対だから反対」ではなく、より小さい費用や少ない権限で同じ目的を達成できるかが重要な比較になります。

## 久世ゆいの評価

評価軸は政党名ではなく、法の支配、権限と責任の一致、費用の透明性、国会審議、地方自治、事後検証です。政府が方針を示すこと自体と、その方針が法的拘束力を持つことは区別しなければなりません。国民に負担を求める制度ほど、誰が決め、誰が支払い、失敗時に誰が説明するのかを明確にする必要があります。

賛成側には効果と期限の提示を、反対側には実行可能な代替案を求めるのが公平です。政治的な言葉の強さより、条文、予算、議事録、施行後の数値を追うほうが、制度を正確に評価できます。

## 読者が今後見るべきポイント

- 正式な決定主体と文書名が公開されているか
- 法律案なら、委員会と本会議の審議経過が確認できるか
- 公布日と施行日が分けて示されているか
- 国と地方、本人と事業者などの費用分担が明確か
- 実施後の評価指標、見直し期限、説明責任が設定されているか

今後の報道では「決まった」という一語だけでなく、どの段階まで進んだのかを確認してください。公式資料が更新されたときは、見出しより新しい原資料を優先することが重要です。

## 一次資料・参考資料

### 一次資料

{primary_links}

### 二次資料

{secondary_links or "- 今回は一次資料を中心に構成"}
"""
    minimum = _int("FREE_NOTE_MIN_CHARS", 1800)
    if len(article) < minimum:
        article += (
            "\n\n## 補足\n\n制度を追うときは、決定文書、議案情報、法令本文を別々に"
            "確認してください。文書が見つからない主張は断定せず、確認待ちとして扱います。"
            "更新履歴を残すことで、後から説明が変わった場合にも検証できます。\n"
        ) * 3
    maximum = _int("FREE_NOTE_MAX_CHARS", 3200)
    if len(article) > maximum:
        # Preserve every required section and source link. Remove prose from
        # the longest non-heading blocks until the document fits instead of
        # blindly cutting the references off the end.
        blocks = article.split("\n\n")
        protected = {
            index for index, block in enumerate(blocks)
            if block.startswith("#") or "](https://" in block
        }
        while len("\n\n".join(blocks)) > maximum:
            candidates_to_shorten = [
                (len(block), index) for index, block in enumerate(blocks)
                if index not in protected and len(block) > 120
            ]
            if not candidates_to_shorten:
                break
            _, index = max(candidates_to_shorten)
            over = len("\n\n".join(blocks)) - maximum
            keep = max(120, len(blocks[index]) - max(40, over))
            blocks[index] = blocks[index][:keep].rstrip("、。 ") + "。"
        article = "\n\n".join(blocks)
    return title, article


def _sources_markdown(primary: list[dict], secondary: list[dict]) -> str:
    def section(title: str, rows: list[dict]) -> str:
        blocks = [f"# {title}", ""]
        if not rows:
            blocks.append("- 該当資料なし")
        for row in rows:
            blocks.extend([
                f"## {row.get('name','資料')}",
                f"- 発行主体: {row.get('publisher','')}",
                f"- 公開日: {row.get('published_at','')}",
                f"- URL: {row.get('url','')}",
                f"- 記事中で使った事実: {row.get('fact_used','')}",
                f"- 確認日時: {row.get('verified_at','')}",
                "",
            ])
        return "\n".join(blocks)
    return section("一次資料", primary) + "\n\n" + section("二次資料", secondary)


REVIEW_MARKDOWN = """# 公開前チェック

## 事実確認

- [ ] 人名・役職は正しい
- [ ] 日付は正しい
- [ ] 法案名・制度名は正しい
- [ ] 数字と単位は正しい
- [ ] 一次資料と本文が一致している
- [ ] 未確認情報を断定していない

## 編集品質

- [ ] X投稿の単純な引き伸ばしになっていない
- [ ] 初心者にも理解できる
- [ ] 賛成側と反対側を公平に説明している
- [ ] 久世ゆいの評価と事実が分離されている
- [ ] 同じ主張を繰り返していない
- [ ] AI的な定型表現を削除した
- [ ] 煽りすぎたタイトルではない

## 安全性

- [ ] 個人への犯罪断定がない
- [ ] 属性攻撃がない
- [ ] 特定政党の宣伝になっていない
- [ ] 他人の記事・投稿の長文転載がない
- [ ] 著作権上問題のある引用がない

## 公開作業

- [ ] 見出し画像が1280×670pxで正しく表示される
- [ ] 見出し画像のタイトルが記事と一致している
- [ ] note上で見出しを確認
- [ ] 改行・箇条書きを確認
- [ ] 必要に応じてアイキャッチを設定
- [ ] 公開URLをmetadata.jsonへ登録
"""


def quality_check(article: str, title: str, primary: list[dict],
                  secondary: list[dict]) -> dict:
    reasons = []
    warnings = []
    length = len(article)
    if length < _int("FREE_NOTE_MIN_CHARS", 1800):
        reasons.append("too_short")
    if length > _int("FREE_NOTE_MAX_CHARS", 3200):
        reasons.append("too_long")
    if len(primary) < _int("FREE_NOTE_MIN_PRIMARY_SOURCES", 2):
        reasons.append("insufficient_primary_sources")
    if title not in article:
        reasons.append("title_body_mismatch")
    first_line = next(
        (line.strip() for line in article.splitlines() if line.strip()), "")
    if first_line != f"# {title}":
        reasons.append("missing_title_heading")
    if any(term.lower() in article.lower() for term in INTERNAL_TERMS):
        reasons.append("internal_label_leak")
    if any(heading not in article for heading in REQUIRED_HEADINGS):
        reasons.append("missing_required_section")
    if "## 賛成側の最も強い主張" not in article or "## 反対側の最も強い主張" not in article:
        warnings.append("unbalanced_arguments")
    known_urls = {row.get("url") for row in primary + secondary}
    article_urls = re.findall(r"https?://[^\s)>\]]+", article)
    if any(url not in known_urls for url in article_urls):
        reasons.append("unknown_or_fabricated_url")
    source_text = " ".join(
        f"{row.get('name','')} {row.get('fact_used','')} {row.get('published_at','')}"
        for row in primary + secondary)
    numeric_claims = re.findall(r"\d[\d,.]*\s*(?:円|%|％|人|件|億|兆)", article)
    if any(claim.replace(" ", "") not in source_text.replace(" ", "")
           for claim in numeric_claims):
        reasons.append("unsupported_numeric_claim")
    safety_patterns = (
        r"(民族|国籍|宗教).{0,8}(劣等|排除|消えろ)",
        r"(犯罪者|逮捕された|死亡した).{0,10}(だ|である)",
    )
    if any(re.search(pattern, article) for pattern in safety_patterns):
        reasons.append("safety_violation")
    score = max(0.0, 10.0 - len(set(reasons)) * 1.5 - len(warnings) * .5)
    safety = 10.0 if "safety_violation" not in reasons else 0.0
    passed = (
        not reasons
        and score >= _float("FREE_NOTE_MIN_QUALITY_SCORE", 8.0)
        and safety >= _float("FREE_NOTE_MIN_SAFETY_SCORE", 9.0)
    )
    return {
        "passed": passed,
        "quality_score": round(score, 2),
        "safety_score": round(safety, 2),
        "reasons": sorted(set(reasons)),
        "warnings": sorted(set(warnings)),
        "character_count": length,
    }


def _next_content_id(now: datetime) -> str:
    prefix = f"note-{now:%Y%m%d}-"
    existing = [
        row.get("content_id", "") for _, row in _existing_metadata()
        if str(row.get("content_id", "")).startswith(prefix)
    ]
    sequence = max(
        [int(value.rsplit("-", 1)[-1]) for value in existing
         if value.rsplit("-", 1)[-1].isdigit()] or [0]
    ) + 1
    return f"{prefix}{sequence:03d}"


def _folder(status: str, generated_at: datetime, slug: str) -> Path:
    bucket = "failed" if status == "failed" else status
    if bucket not in {"drafts", "approved", "published", "failed"}:
        bucket = "drafts"
    if bucket == "drafts":
        return note_root() / bucket / f"{generated_at:%Y}" / f"{generated_at:%Y-%m-%d}_{slug}"
    return note_root() / bucket / f"{generated_at:%Y-%m-%d}_{slug}"


def _save_fallback_state(metadata: dict) -> None:
    path = note_root() / "note_state.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            rows = []
    except Exception:
        rows = []
    rows = [row for row in rows if row.get("content_id") != metadata["content_id"]]
    rows.append(metadata)
    _atomic_json(path, rows[-500:])


def _save_db(metadata: dict, path: Path | None = None) -> bool:
    try:
        init_db(path)
        result = write("""INSERT INTO note_drafts
          (content_id,title,slug,article_type,status,generated_at,target_publish_date,
           published_at,note_url,prompt_version,model,character_count,reading_minutes,
           primary_topic_key,included_topic_keys_json,source_news_ids_json,
           source_x_post_ids_json,primary_sources_json,secondary_sources_json,draft_path,
           discord_notification_status,discord_message_id,input_tokens,output_tokens,
           estimated_cost_usd,quality_score,safety_score,cover_path,cover_status,
           cover_width,cover_height,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(content_id) DO UPDATE SET
           status=excluded.status,published_at=excluded.published_at,
           note_url=excluded.note_url,draft_path=excluded.draft_path,
           discord_notification_status=excluded.discord_notification_status,
           discord_message_id=excluded.discord_message_id,
           cover_path=excluded.cover_path,cover_status=excluded.cover_status,
           cover_width=excluded.cover_width,cover_height=excluded.cover_height,
           updated_at=excluded.updated_at""", (
            metadata["content_id"], metadata["title"], metadata["slug"],
            metadata["article_type"], metadata["status"], metadata["generated_at"],
            metadata["target_publish_date"], metadata.get("published_at"),
            metadata.get("note_url"), metadata["prompt_version"], metadata["model"],
            metadata["character_count"], metadata["estimated_reading_minutes"],
            metadata["primary_topic_key"],
            json.dumps(metadata["included_topic_keys"], ensure_ascii=False),
            json.dumps(metadata["source_news_candidate_ids"], ensure_ascii=False),
            json.dumps(metadata["source_x_post_ids"], ensure_ascii=False),
            json.dumps(metadata["primary_sources"], ensure_ascii=False),
            json.dumps(metadata["secondary_sources"], ensure_ascii=False),
            metadata["draft_path"], metadata["discord_notification_status"],
            metadata.get("discord_message_id"), metadata.get("input_tokens", 0),
            metadata.get("output_tokens", 0), metadata["estimated_cost_usd"],
            metadata["quality_score"], metadata["safety_score"],
            metadata.get("cover_path"), metadata.get("cover_status"),
            metadata.get("cover_width"), metadata.get("cover_height"),
            metadata["created_at"], metadata["updated_at"],
        ), path)
        return result is not None
    except (sqlite3.Error, OSError):
        return False


def _record_run(run: dict, path: Path | None = None) -> None:
    try:
        write("""INSERT INTO note_generation_runs
          (run_at,schedule_type,target_article_type,selected_topic,selection_reason,
           status,content_id,model,input_tokens,output_tokens,estimated_cost_usd,
           error_type,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            run.get("run_at"), run.get("schedule_type"), run.get("target_article_type"),
            run.get("selected_topic"), run.get("selection_reason"), run.get("status"),
            run.get("content_id"), run.get("model"), run.get("input_tokens", 0),
            run.get("output_tokens", 0), run.get("estimated_cost_usd", 0),
            run.get("error_type", ""), json.dumps(run, ensure_ascii=False),
        ), path)
    except sqlite3.Error:
        pass


def _openai_article(selection: dict, primary: list[dict], secondary: list[dict],
                    path: Path | None = None, client_factory=None) -> dict:
    models = [
        os.environ.get(
            "OPENAI_MODEL_FREE_NOTE",
            os.environ.get("OPENAI_MODEL_FREE_NOTE_PRIMARY", "gpt-5.6-terra"),
        ),
        os.environ.get("OPENAI_MODEL_FREE_NOTE_FALLBACK", "gpt-5.6-luna"),
    ]
    max_output = _int("OPENAI_MAX_OUTPUT_TOKENS_FREE_NOTE", 6000)
    hard_cost = _float("FREE_NOTE_MAX_COST_PER_ARTICLE_USD", .25)
    reservation_id = None
    reason = ""
    model = ""
    for candidate in dict.fromkeys(models):
        estimated = estimate_openai(candidate, 7000, max_output)
        if estimated is None or estimated > hard_cost:
            reason = "free_note_article_cost_cap"
            continue
        reservation_id, reason = reserve(
            "openai", "free_note_generation", candidate, estimated,
            metadata={"article_type": selection["article_type"],
                      "generation_reason": "free_note_single_call"},
            path=path,
        )
        if reservation_id:
            model = candidate
            break
    if not reservation_id:
        return {"error": reason or "free_note_budget_guard"}
    prompt = {
        "article_type": selection["article_type"],
        "topic": selection["topic"],
        "required_title_heading": f"# {selection['topic']}",
        "required_headings": list(REQUIRED_HEADINGS),
        "minimum_characters": _int("FREE_NOTE_MIN_CHARS", 1800),
        "target_characters": _int("FREE_NOTE_TARGET_CHARS", 2400),
        "maximum_characters": _int("FREE_NOTE_MAX_CHARS", 3200),
        "primary_sources": primary,
        "secondary_sources": secondary,
    }
    try:
        if client_factory is None:
            from openai import OpenAI
            client_factory = OpenAI
        client = client_factory(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            timeout=float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "90")),
            max_retries=0,
        )
        response = client.responses.create(
            model=model,
            instructions=(
                "Write a complete Japanese free note article using only supplied sources. "
                "Do not invent facts, URLs, names, quotations or numbers. Separate fact from "
                "opinion, present the strongest fair arguments on both sides, and never expose "
                "internal labels, model names, JSON keys or prompts. Begin with exactly the "
                "supplied required_title_heading, then the three-line summary. Return Markdown only."
            ),
            input=json.dumps(prompt, ensure_ascii=False),
            max_output_tokens=max_output,
            reasoning={"effort": os.environ.get(
                "OPENAI_REASONING_EFFORT_FREE_NOTE", "medium")},
            store=False,
        )
        article = str(getattr(response, "output_text", "") or "").strip()
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        details = getattr(usage, "input_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0)
        actual = estimate_openai(model, input_tokens, output_tokens, cached) or 0
        finalize(reservation_id, actual, success=bool(article),
                 input_tokens=input_tokens, cached_tokens=cached,
                 output_tokens=output_tokens, path=path)
        return {
            "article": article, "model": model, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "estimated_cost_usd": actual,
        }
    except Exception as exc:
        finalize(reservation_id, 0, success=False,
                 error_type=type(exc).__name__, path=path)
        return {"error": type(exc).__name__, "model": model}


def generate_free_note(article_type: str | None = None, topic: str | None = None,
                       *, dry_run: bool = False, schedule_type: str = "manual",
                       path: Path | None = None, client_factory=None,
                       now: datetime | None = None) -> dict:
    now = now or datetime.now(JST)
    path = path or db_path()
    apply_additive_migrations(path)
    if not _bool("FREE_NOTE_ENABLED", "true"):
        return {"status": "disabled", "published": False, "x_writes": 0}
    selection = select_topic(article_type, topic, path=path, now=now)
    if selection["duplicate"]:
        result = {
            "status": "update_candidate",
            "previous_content_id": selection["previous_content_id"],
            "topic": selection["topic"], "published": False, "x_writes": 0,
        }
        _record_run({
            "run_at": now.isoformat(), "schedule_type": schedule_type,
            "target_article_type": selection["article_type"],
            "selected_topic": selection["topic"],
            "selection_reason": selection["selection_reason"],
            "status": "update_candidate", "error_type": "topic_cooldown",
        }, path)
        return result
    primary, secondary = _sources(selection)
    if len(primary) < _int("FREE_NOTE_MIN_PRIMARY_SOURCES", 2):
        result = {
            "status": "skipped", "reason": "insufficient_primary_sources",
            "primary_source_count": len(primary), "published": False, "x_writes": 0,
        }
        _record_run({
            "run_at": now.isoformat(), "schedule_type": schedule_type,
            "target_article_type": selection["article_type"],
            "selected_topic": selection["topic"],
            "selection_reason": selection["selection_reason"],
            "status": "skipped", "error_type": result["reason"],
        }, path)
        return result

    generation = {}
    attempts = 1 if dry_run else 1 + max(
        0, min(1, _int("FREE_NOTE_MAX_REGENERATION_ATTEMPTS", 1)))
    title = selection["topic"]
    article = ""
    quality = {}
    for attempt in range(attempts):
        if dry_run:
            title, article = _local_article(selection, primary, secondary)
            generation = {
                "model": "local-dry-run", "input_tokens": 0, "output_tokens": 0,
                "estimated_cost_usd": 0.0,
            }
        else:
            generation = _openai_article(
                selection, primary, secondary, path, client_factory)
            article = generation.get("article", "")
            if not article:
                break
            first_line = next(
                (line.strip() for line in article.splitlines() if line.strip()), "")
            title = (
                first_line[2:].strip()
                if first_line.startswith("# ") and not first_line.startswith("## ")
                else selection["topic"]
            )
        quality = quality_check(article, title, primary, secondary)
        if quality["passed"]:
            break

    content_id = _next_content_id(now)
    slug = slugify(title)
    passed = bool(quality.get("passed"))
    status = "draft" if passed else "failed"
    folder = _folder("drafts" if passed else "failed", now, slug)
    if folder.exists():
        sequence = content_id.rsplit("-", 1)[-1]
        folder = folder.with_name(f"{folder.name}_{sequence}")
    folder.mkdir(parents=True, exist_ok=False)
    cover = {
        "cover_status": "not_generated",
        "cover_path": None,
        "cover_width": 1280,
        "cover_height": 670,
        "cover_aspect_ratio": round(1280 / 670, 4),
        "cover_generator": "local_pillow_v1",
    }
    if passed and _bool("FREE_NOTE_COVER_ENABLED", "true"):
        try:
            from note_cover import generate_cover
            cover = generate_cover(
                title,
                selection["article_type"],
                folder / "cover.png",
                generated_at=now,
            )
        except Exception as exc:
            cover["cover_status"] = "failed"
            cover["cover_error"] = type(exc).__name__
    target_publish = (
        now.date() if now.hour < 20 else now.date() + timedelta(days=1)
    ).isoformat()
    metadata = {
        "content_id": content_id, "title": title, "slug": slug,
        "status": status, "article_type": selection["article_type"],
        "generated_at": now.isoformat(), "target_publish_date": target_publish,
        "prompt_version": PROMPT_VERSION, "model": generation.get("model", ""),
        "character_count": len(article),
        "estimated_reading_minutes": max(1, round(len(article) / 600)),
        "primary_topic_key": selection["topic_key"],
        "included_topic_keys": [
            normalize_topic_key(row.get("topic_key") or row.get("title") or "")
            for row in selection.get("candidates", [])
        ],
        "source_news_candidate_ids": [
            row.get("id") for row in selection.get("candidates", []) if row.get("id")
        ],
        "source_x_post_ids": [],
        "primary_sources": primary, "secondary_sources": secondary,
        "discord_notification_status": "pending" if passed else "not_eligible",
        "discord_message_id": None, "note_url": None, "published_at": None,
        "input_tokens": generation.get("input_tokens", 0),
        "output_tokens": generation.get("output_tokens", 0),
        "estimated_cost_usd": generation.get("estimated_cost_usd", 0.0),
        "quality_score": quality.get("quality_score", 0),
        "safety_score": quality.get("safety_score", 0),
        "quality_reasons": quality.get("reasons", [generation.get("error", "generation_failed")]),
        "quality_warnings": quality.get("warnings", []),
        "selection_reason": selection["selection_reason"],
        "generation_attempts": attempts if not passed else attempt + 1,
        "generation_mode": "dry_run_local" if dry_run else (
            "openai" if article else "outline_only"),
        "draft_path": str(folder),
        "created_at": now.isoformat(), "updated_at": now.isoformat(),
        "status_history": [{"status": status, "at": now.isoformat()}],
        "published": False, "automatic_note_publish": False, "x_writes": 0,
        **cover,
    }
    _atomic_text(folder / "article.md", article or (
        f"# {selection['topic']}\n\n## 構成案\n\n"
        "API予算または生成エラーのため、本文生成を停止しました。"
    ))
    _atomic_text(folder / "sources.md", _sources_markdown(primary, secondary))
    _atomic_text(folder / "review.md", REVIEW_MARKDOWN)
    _atomic_json(folder / "metadata.json", metadata)
    if not _save_db(metadata, path):
        _save_fallback_state(metadata)

    discord_sent = False
    if passed and not dry_run and _bool("DISCORD_NOTE_ENABLED", "false"):
        discord_sent = send_note_discord(content_id, path=path)
        metadata = load_note(content_id)[1]
    _record_run({
        "run_at": now.isoformat(), "schedule_type": schedule_type,
        "target_article_type": selection["article_type"],
        "selected_topic": selection["topic"],
        "selection_reason": selection["selection_reason"], "status": status,
        "content_id": content_id, "model": metadata["model"],
        "input_tokens": metadata["input_tokens"],
        "output_tokens": metadata["output_tokens"],
        "estimated_cost_usd": metadata["estimated_cost_usd"],
        "error_type": "" if passed else ",".join(metadata["quality_reasons"]),
        "dry_run": dry_run,
    }, path)
    return {
        "status": status, "content_id": content_id, "title": title,
        "article_type": selection["article_type"],
        "character_count": metadata["character_count"],
        "quality_score": metadata["quality_score"],
        "safety_score": metadata["safety_score"],
        "path": str(folder), "discord_sent": discord_sent,
        "cover_status": metadata.get("cover_status"),
        "cover_path": metadata.get("cover_path"),
        "published": False, "x_writes": 0,
        "estimated_cost_usd": metadata["estimated_cost_usd"],
    }


def load_note(content_id: str) -> tuple[Path, dict]:
    for path, metadata in _existing_metadata():
        if metadata.get("content_id") == content_id:
            return path.parent, metadata
    raise FileNotFoundError(f"note draft not found: {content_id}")


def generate_note_cover(content_id: str, *, path: Path | None = None) -> dict:
    """Generate or replace the fixed-size cover for a saved note draft."""
    folder, metadata = load_note(content_id)
    from note_cover import generate_cover
    generated_at = datetime.fromisoformat(
        str(metadata.get("generated_at") or datetime.now(JST).isoformat())
    )
    cover = generate_cover(
        str(metadata.get("title") or "政治と制度を読み解く"),
        str(metadata.get("article_type") or ""),
        folder / "cover.png",
        generated_at=generated_at,
    )
    metadata.update(cover)
    metadata["updated_at"] = datetime.now(JST).isoformat()
    _atomic_json(folder / "metadata.json", metadata)
    if not _save_db(metadata, path):
        _save_fallback_state(metadata)
    return {
        "content_id": content_id,
        **cover,
        "automatic_note_publish": False,
        "x_writes": 0,
    }


def list_notes() -> list[dict]:
    rows = []
    for path, row in _existing_metadata():
        rows.append({
            "content_id": row.get("content_id"), "title": row.get("title"),
            "article_type": row.get("article_type"), "status": row.get("status"),
            "generated_at": row.get("generated_at"),
            "target_publish_date": row.get("target_publish_date"),
            "character_count": row.get("character_count"),
            "quality_score": row.get("quality_score"),
            "discord_status": row.get("discord_notification_status"),
            "cover_status": row.get("cover_status"),
            "cover_path": row.get("cover_path"),
            "local_path": str(path.parent),
        })
    return sorted(rows, key=lambda row: str(row.get("generated_at") or ""), reverse=True)


def _destination_for(status: str, metadata: dict) -> Path | None:
    if status == "approved":
        return note_root() / "approved" / Path(metadata["draft_path"]).name
    if status == "published":
        return note_root() / "published" / Path(metadata["draft_path"]).name
    if status == "rejected":
        return note_root() / "failed" / Path(metadata["draft_path"]).name
    return None


def update_status(content_id: str, status: str, *, note_url: str | None = None,
                  path: Path | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError("unsupported note status")
    if status == "published" and (
        not note_url or not str(note_url).startswith("https://note.com/")
    ):
        raise ValueError("published status requires an https://note.com/ URL")
    folder, metadata = load_note(content_id)
    now = datetime.now(JST)
    destination = _destination_for(status, metadata)
    if destination and folder.resolve() != destination.resolve():
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"destination exists: {destination}")
        shutil.move(str(folder), str(destination))
        folder = destination
        if (folder / "cover.png").exists():
            metadata["cover_path"] = str(folder / "cover.png")
    metadata["status"] = status
    metadata["updated_at"] = now.isoformat()
    metadata["draft_path"] = str(folder)
    metadata.setdefault("status_history", []).append({
        "status": status, "at": now.isoformat(),
    })
    if status == "published":
        metadata["note_url"] = note_url
        metadata["published_at"] = now.isoformat()
        metadata["published"] = True
    _atomic_json(folder / "metadata.json", metadata)
    if not _save_db(metadata, path):
        _save_fallback_state(metadata)
    return metadata


def mark_published(content_id: str, url: str, path: Path | None = None) -> dict:
    return update_status(content_id, "published", note_url=url, path=path)


def send_note_discord(content_id: str, *, force: bool = False,
                      path: Path | None = None) -> bool:
    folder, metadata = load_note(content_id)
    if metadata.get("status") == "failed":
        return False
    if metadata.get("discord_notification_status") == "sent" and not force:
        return False
    from discord_notify import notify_note_draft_files
    sent, message_id = notify_note_draft_files(
        {
            "content_id": content_id,
            "title": metadata.get("title"),
            "article_type": metadata.get("article_type"),
            "character_count": metadata.get("character_count"),
            "reading_minutes": metadata.get("estimated_reading_minutes"),
            "target_publish_date": metadata.get("target_publish_date"),
            "status": metadata.get("status"),
            "path": str(folder),
        },
        [
            folder / "cover.png",
            folder / "article.md",
            folder / "sources.md",
            folder / "review.md",
        ] if (folder / "cover.png").exists() else [
            folder / "article.md",
            folder / "sources.md",
            folder / "review.md",
        ],
    )
    metadata["discord_notification_status"] = "sent" if sent else "failed"
    metadata["discord_message_id"] = message_id
    metadata["updated_at"] = datetime.now(JST).isoformat()
    _atomic_json(folder / "metadata.json", metadata)
    if not _save_db(metadata, path):
        _save_fallback_state(metadata)
    return sent


def _parse_clock(raw: str) -> tuple[int, int]:
    hour, minute = (int(value) for value in raw.split(":", 1))
    return hour, minute


def schedule_slots(now: datetime | None = None,
                   path: Path | None = None) -> list[dict]:
    now = now or datetime.now(JST)
    week_start = now.date() - timedelta(days=now.weekday())
    slots = []
    posts = max(1, min(2, _int("FREE_NOTE_POSTS_PER_WEEK", 2)))
    remaining = (
        _float("FREE_NOTE_MONTHLY_BUDGET_USD", 1.5)
        - _free_note_monthly_cost(now, path)
    )
    luna_cost = estimate_openai(
        os.environ.get("OPENAI_MODEL_FREE_NOTE_FALLBACK", "gpt-5.6-luna"),
        7000,
        _int("OPENAI_MAX_OUTPUT_TOKENS_FREE_NOTE", 6000),
    )
    if posts == 2 and luna_cost is not None and remaining < luna_cost * 2:
        posts = 1
    specs = []
    if posts == 2:
        specs.append((2, "wed", os.environ.get(
            "FREE_NOTE_SCHEDULE_WED", "20:30"), os.environ.get(
            "FREE_NOTE_WED_TYPE", "evergreen_institutional_explainer")))
    specs.append((6, "sun", os.environ.get(
        "FREE_NOTE_SCHEDULE_SUN", "20:30"), os.environ.get(
        "FREE_NOTE_SUN_TYPE", "weekly_top5")))
    for weekday, name, clock, article_type in specs:
        hour, minute = _parse_clock(clock)
        scheduled = datetime.combine(
            week_start + timedelta(days=weekday),
            datetime.min.time(), tzinfo=JST).replace(hour=hour, minute=minute)
        slots.append({
            "schedule_type": name, "scheduled_at": scheduled,
            "article_type": article_type,
            "week_key": f"{week_start.isoformat()}:{name}",
        })
    return slots


def _free_note_monthly_cost(now: datetime | None = None,
                            path: Path | None = None) -> float:
    now = now or datetime.now(JST)
    try:
        with closing(connect(path)) as conn:
            return float(conn.execute("""SELECT COALESCE(SUM(estimated_cost_usd),0)
                FROM note_generation_runs WHERE run_at LIKE ?""",
                (now.strftime("%Y-%m") + "%",)).fetchone()[0] or 0)
    except sqlite3.Error:
        return sum(
            float(row.get("estimated_cost_usd", 0) or 0)
            for _, row in _existing_metadata()
            if str(row.get("generated_at", "")).startswith(now.strftime("%Y-%m"))
        )


def due_slots(now: datetime | None = None,
              path: Path | None = None) -> list[dict]:
    now = now or datetime.now(JST)
    generated = {
        (row.get("schedule_type"), row.get("target_article_type"))
        for row in _generation_runs(path)
        if row.get("status") in {"draft", "published", "approved"}
        and str(row.get("run_at", ""))[:10]
        >= (now.date() - timedelta(days=now.weekday())).isoformat()
    }
    return [
        slot for slot in schedule_slots(now, path)
        if slot["scheduled_at"] <= now
        and (slot["schedule_type"], slot["article_type"])
        not in generated
    ]


def _generation_runs(path: Path | None = None) -> list[dict]:
    try:
        with closing(connect(path)) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM note_generation_runs ORDER BY id DESC")]
    except sqlite3.Error:
        return []


def generate_due(now: datetime | None = None, *, path: Path | None = None) -> list[dict]:
    now = now or datetime.now(JST)
    results = []
    for slot in due_slots(now, path):
        results.append(generate_free_note(
            slot["article_type"], dry_run=False,
            schedule_type=slot["schedule_type"], path=path, now=now))
    return results


def pipeline_status(now: datetime | None = None, path: Path | None = None) -> dict:
    now = now or datetime.now(JST)
    notes = list_notes()
    week_start = now.date() - timedelta(days=now.weekday())
    this_week = [
        row for row in notes
        if str(row.get("generated_at", ""))[:10] >= week_start.isoformat()
    ]
    future = [
        slot for slot in schedule_slots(now, path)
        if slot["scheduled_at"] > now
    ]
    try:
        with closing(connect(path)) as conn:
            cost = float(conn.execute("""SELECT COALESCE(SUM(estimated_cost_usd),0)
                FROM note_generation_runs WHERE run_at LIKE ?""",
                (now.strftime("%Y-%m") + "%",)).fetchone()[0] or 0)
    except sqlite3.Error:
        cost = sum(float(row.get("estimated_cost_usd", 0) or 0)
                   for _, row in _existing_metadata()
                   if str(row.get("generated_at", "")).startswith(now.strftime("%Y-%m")))
    sent = sum(row.get("discord_status") == "sent" for row in notes)
    attempted = sum(row.get("discord_status") in {"sent", "failed"} for row in notes)
    return {
        "generated_this_week": len(this_week),
        "next_schedule": min(
            [slot["scheduled_at"].isoformat() for slot in future], default=None),
        "drafts": sum(row.get("status") == "draft" for row in notes),
        "awaiting_review": sum(row.get("status") == "reviewing" for row in notes),
        "revision_required": sum(
            row.get("status") == "revision_required" for row in notes),
        "published": sum(row.get("status") == "published" for row in notes),
        "openai_cost_this_month_usd": round(cost, 8),
        "discord_success_rate": round(sent / attempted, 4) if attempted else 0.0,
        "monthly_budget_usd": _float("FREE_NOTE_MONTHLY_BUDGET_USD", 1.5),
        "automatic_note_publish": False,
        "x_writes": 0,
    }
