"""Bounded multi-stage OpenAI pipeline for factual, distinctive political posts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
X_LIMIT = 260
THREADS_LIMIT = 480

BANNED_PHRASES = (
    "今後の議論が注目されます", "今後の動向が注目されます",
    "慎重な議論が必要です", "動向を注視する必要があります",
    "私たちはどう考えるべきでしょうか", "重要な課題です",
    "賛否が分かれそうです", "一石を投じることになりそうです",
    "波紋を呼びそうです", "今後の行方が注目されます",
    "議論を呼びそうです", "考えさせられる問題です",
    "社会全体で考える必要があります",
)

CLICHE_PATTERNS = (
    re.compile(r"(?:今後|これから).{0,12}(?:注目|注視)(?:され|すべき|が必要|する必要)"),
    re.compile(r"(?:慎重|丁寧|十分)な.{0,8}(?:議論|検討)(?:が|を).{0,8}(?:必要|求め)"),
    re.compile(r"(?:私たち|社会全体|国民一人ひとり).{0,12}(?:考える|向き合う)"),
    re.compile(r"(?:賛否|意見).{0,8}(?:分かれ|割れ)"),
    re.compile(r"(?:重要|深刻|大きな).{0,6}(?:課題|問題)(?:です|だ)?[。．]?$"),
    re.compile(r"(?:どう考える|どう思う)(?:べき)?(?:でしょう)?[か？?]$"),
)

DEFAULT_ANGLES = (
    ("taxpayer", "税金・予算・費用負担"),
    ("accountability", "意思決定者と責任"),
    ("daily_life", "庶民生活や企業活動への影響"),
    ("official_gap", "公式説明と実態のズレ"),
    ("contradiction", "過去の発言や政策との矛盾"),
    ("feasibility", "制度設計と実現可能性"),
    ("government_strategy", "政権側の政治的狙い"),
    ("opposition_weakness", "野党側の弱点または代替案不足"),
)

QUALITY_WEIGHTS = {
    "scroll_stop": .18, "specificity": .14, "novelty": .14,
    "daily_life_relevance": .12, "accountability_clarity": .12,
    "quotability": .14, "argument_strength": .12,
    "emotional_resonance": .08, "cliche_penalty": -.10,
}


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _int(name: str, default: int, low: int = 0, high: int = 1000) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def contains_banned_phrase(text: str) -> list[str]:
    compact = re.sub(r"\s+", "", text or "")
    hits = [phrase for phrase in BANNED_PHRASES
            if re.sub(r"\s+", "", phrase) in compact]
    hits.extend(
        f"semantic_cliche:{pattern.pattern}"
        for pattern in CLICHE_PATTERNS if pattern.search(text or ""))
    return hits


def quality_score(scores: dict[str, Any]) -> float:
    return round(sum(
        float(scores.get(key, 0) or 0) * weight
        for key, weight in QUALITY_WEIGHTS.items()
    ), 4)


def safety_rejection_reasons(scores: dict[str, Any]) -> list[str]:
    reasons = []
    if float(scores.get("factual_accuracy", 0) or 0) < _int(
            "POLITICS_MIN_FACTUAL_SCORE", 8, 0, 10):
        reasons.append("factual_accuracy")
    if float(scores.get("source_support", 0) or 0) < 8:
        reasons.append("source_support")
    if float(scores.get("defamation_risk", 10) or 0) > _int(
            "POLITICS_MAX_DEFAMATION_RISK", 3, 0, 10):
        reasons.append("defamation_risk")
    if float(scores.get("unsupported_inference_risk", 10) or 0) > _int(
            "POLITICS_MAX_INFERENCE_RISK", 4, 0, 10):
        reasons.append("unsupported_inference_risk")
    if float(scores.get("misleading_risk", 10) or 0) >= 5:
        reasons.append("misleading_risk")
    if float(scores.get("policy_violation_risk", 10) or 0) >= 1:
        reasons.append("policy_violation_risk")
    return reasons


def select_winner(candidates: list[dict]) -> dict | None:
    eligible = [row for row in candidates if not row.get("rejected")]
    if not eligible:
        return None
    return max(eligible, key=lambda row: (
        float(row.get("final_score", 0) or 0),
        float((row.get("safety_scores") or {}).get("factual_accuracy", 0) or 0),
        float((row.get("quality_scores") or {}).get("scroll_stop", 0) or 0),
        float((row.get("quality_scores") or {}).get("quotability", 0) or 0),
        float((row.get("quality_scores") or {}).get("specificity", 0) or 0),
        -len(str(row.get("text") or "")),
    ))


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[。！？!?])\s*", text)
            if part.strip()]


def fit_platform_text(text: str, limit: int) -> str:
    """Remove low-value material without cutting a sentence midway."""
    text = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    text = re.sub(r"(?:\s*#[^\s#]+)+\s*$", "", text).strip()
    if len(text) <= limit:
        return text
    sentences = _sentences(text)
    while len("".join(sentences)) > limit and len(sentences) > 2:
        sentences.pop(-2)
    result = "".join(sentences).strip()
    if len(result) <= limit:
        return result
    last_stop = max(result.rfind(mark, 0, limit + 1) for mark in "。！？")
    return result[:last_stop + 1].strip() if last_stop >= 40 else ""


def independent_platform_versions(x_text: str, threads_text: str) -> dict:
    return {
        "x": fit_platform_text(x_text, X_LIMIT),
        "threads": fit_platform_text(threads_text, THREADS_LIMIT),
    }


def semantic_duplicate(candidate: dict, history: list[dict]) -> list[str]:
    reasons: list[str] = []
    text = re.sub(r"\s+", "", str(candidate.get("text") or ""))
    hook = re.sub(r"\s+", "", str(candidate.get("hook") or ""))
    recent10 = history[-10:]
    recent20 = history[-20:]
    template = candidate.get("template_id")
    if template and sum(
        (row.get("template_id") or row.get("template")) == template
        for row in recent10
    ) >= 3:
        reasons.append("template_overuse")
    if hook and any(
        hook == re.sub(r"\s+", "", str(row.get("hook") or ""))
        for row in recent20
    ):
        reasons.append("duplicate_hook")
    actor = str(candidate.get("target_actor") or "")
    if actor and all(
        actor in str(row.get("target_actor") or row.get("tweet_text") or "")
        for row in history[-2:]
    ) and len(history) >= 2:
        reasons.append("same_actor_streak")
    angle_id = str(candidate.get("angle_id") or "")
    if angle_id == "taxpayer" and sum(
        str(row.get("angle_id") or "") == "taxpayer"
        or any(term in str(
            row.get("claim_summary") or row.get("tweet_text") or "")
            for term in ("税金の無駄", "税金を無駄", "無駄遣い"))
        for row in history[-5:]
    ) >= 2:
        reasons.append("tax_waste_angle_streak")
    thesis = str(candidate.get("thesis") or "")
    for row in recent20:
        old = re.sub(r"\s+", "", str(
            row.get("thesis") or row.get("claim_summary")
            or row.get("tweet_text") or ""))
        if text and old and SequenceMatcher(None, text, old).ratio() >= .84:
            reasons.append("semantic_text_duplicate")
            break
        if thesis and old and SequenceMatcher(
                None, re.sub(r"\s+", "", thesis), old).ratio() >= .86:
            reasons.append("duplicate_thesis")
            break
    return sorted(set(reasons))


def _tokens(text: str) -> set[str]:
    normalized = re.sub(r"[^\w一-龥ぁ-んァ-ヶ]+", " ", str(text).lower())
    return {value for value in normalized.split() if len(value) >= 2}


def angle_distinctness_reasons(angles: list[dict]) -> list[str]:
    reasons = []
    ids = [str(row.get("id") or "") for row in angles]
    if len(ids) != len(set(ids)):
        reasons.append("duplicate_angle_id")
    theses = [str(row.get("thesis") or "") for row in angles]
    for left in range(len(theses)):
        for right in range(left + 1, len(theses)):
            a, b = _tokens(theses[left]), _tokens(theses[right])
            overlap = len(a & b) / max(1, len(a | b))
            ratio = SequenceMatcher(None, theses[left], theses[right]).ratio()
            if overlap >= .72 or ratio >= .86:
                reasons.append(
                    f"duplicate_angle_thesis:{ids[left]}:{ids[right]}")
    targets = {
        str(row.get("target_actor") or "") for row in angles
        if row.get("target_actor")
    }
    conclusions = {
        re.sub(r"\s+", "", str(row.get("thesis") or ""))[-24:]
        for row in angles
    }
    if len(angles) >= 6 and len(conclusions) < 4 and len(targets) < 2:
        reasons.append("angles_share_same_conclusion")
    return reasons


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    denom = math.sqrt(sum(a * a for a in left)) * math.sqrt(
        sum(b * b for b in right))
    return dot / denom if denom else 0.0


@dataclass
class PipelineConfig:
    angle_count: int
    candidate_count: int
    max_calls: int
    cache_ttl_hours: int
    mock: bool
    debug: bool

    @classmethod
    def from_env(cls) -> "PipelineConfig":
        return cls(
            angle_count=_int("POLITICS_ANGLE_COUNT", 6, 6, 8),
            candidate_count=_int("POLITICS_CANDIDATE_COUNT", 6, 1, 8),
            max_calls=_int("POLITICS_MAX_API_CALLS_PER_ARTICLE", 12, 1, 20),
            cache_ttl_hours=_int("POLITICS_CACHE_TTL_HOURS", 24, 1, 720),
            mock=_bool("POLITICS_MOCK_LLM"),
            debug=_bool("POLITICS_DEBUG_GENERATION"),
        )


class CallLimitExceeded(RuntimeError):
    pass


def call_structured_json_with_retry(
    create_once: Callable[[], Any], max_attempts: int = 2
) -> tuple[Any, dict, int]:
    """Retry only empty/incomplete JSON; transport errors remain caller-owned."""
    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        response = create_once()
        raw = (getattr(response, "output_text", "") or "").strip()
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("structured_output_not_object")
            return response, payload, attempt
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    raise ValueError(
        f"structured_output_invalid_after_{max_attempts}_attempts:"
        f"{type(last_error).__name__ if last_error else 'unknown'}")


class MultiStagePipeline:
    """Orchestrates six bounded JSON calls; never blends candidate arguments."""

    def __init__(
        self,
        call_json: Callable[[str, str, dict, str], dict],
        *,
        root: Path,
        history: list[dict] | None = None,
        config: PipelineConfig | None = None,
        embed_texts: Callable[[list[str]], list[list[float]]] | None = None,
    ):
        self.call_json = call_json
        self.root = Path(root)
        self.history = history or []
        self.config = config or PipelineConfig.from_env()
        self.embed_texts = embed_texts
        self.calls = 0
        self.usage: list[dict] = []

    def _call(self, stage: str, prompt: str, schema: dict, model_role: str) -> dict:
        if self.calls >= self.config.max_calls:
            raise CallLimitExceeded("politics_api_call_limit")
        self.calls += 1
        if self.config.mock:
            return self._mock(stage)
        result = self.call_json(stage, prompt, schema, model_role)
        if not isinstance(result, dict):
            raise ValueError(f"{stage}_invalid_payload")
        usage = result.pop("__usage", None)
        if isinstance(usage, dict):
            self.usage.append({"stage": stage, **usage})
        return result

    @staticmethod
    def _mock(stage: str) -> dict:
        facts = {
            "verified_facts": [{"fact": "政府が制度案を公表した",
                                "source_url": "https://example.invalid/source",
                                "confidence": 1.0}],
            "main_event": "制度案の公表", "key_actors": ["政府"],
            "decision_maker": "政府", "beneficiaries": ["対象者"],
            "cost_bearers": ["納税者"], "official_explanation": "制度改善",
            "practical_effect": "制度運用が変わる", "contradictions": [],
            "accountability_question": "検証責任を誰が負うか",
            "impact_on_daily_life": "家計に影響する", "missing_information": [],
            "usable_numbers": [],
        }
        if stage == "analysis":
            return {"analysis": facts}
        if stage == "angles":
            return {"angles": [{
                "id": key, "title": title, "thesis": f"{title}の説明が必要だ",
                "target_actor": "政府", "reader_relevance": "家計への影響",
                "supporting_facts": ["政府が制度案を公表した"],
                "risk_notes": ["詳細は未確定"],
            } for key, title in DEFAULT_ANGLES[:6]]}
        if stage == "candidates":
            return {"candidates": [{
                "candidate_id": f"c{i}", "angle_id": key,
                "text": f"制度案で見るべきは{title}だ。政府は制度案を公表した。"
                        "事実として確認できる範囲を示し、実施後の検証責任まで明確にすべきだ。",
                "hook": f"制度案で見るべきは{title}だ。",
                "thesis": f"{title}の説明が必要だ", "target_actor": "政府",
                "template_id": key, "supporting_fact_urls": [
                    "https://example.invalid/source"],
            } for i, (key, title) in enumerate(DEFAULT_ANGLES[:6], 1)]}
        if stage == "safety":
            return {"evaluations": [{
                "candidate_id": f"c{i}", "factual_accuracy": 9,
                "source_support": 9, "defamation_risk": 0,
                "unsupported_inference_risk": 1, "misleading_risk": 1,
                "policy_violation_risk": 0, "notes": [],
            } for i in range(1, 7)]}
        if stage == "quality":
            return {"evaluations": [{
                "candidate_id": f"c{i}",
                **{key: (min(10, i + 3) if key != "cliche_penalty" else 0)
                   for key in QUALITY_KEYS},
                "summary": "具体的",
            } for i in range(1, 7)]}
        if stage == "finalize":
            return {
                "candidate_id": "c6",
                "x_text": "制度案で見るべきは負担と責任だ。政府は制度案を公表した。実施後の検証責任まで明確にすべきだ。",
                "threads_text": "制度案で見るべきは負担と責任だ。政府は制度案を公表した。家計への影響と実施主体を分けて示し、実施後の検証責任まで明確にすべきだ。",
            }
        if stage == "final_verification":
            return {
                "passed": True, "factual_accuracy": 9, "source_support": 9,
                "names_unchanged": True, "numbers_unchanged": True,
                "claim_unchanged": True, "assertion_strength_safe": True,
                "semantic_cliche": False, "personal_attack": False,
                "reasons": [],
            }
        raise ValueError(f"unknown_mock_stage:{stage}")

    def _cache_path(self, article: dict) -> Path:
        identity = str(article.get("url") or article.get("title") or "")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self.root / "data" / "politics_analysis_cache" / f"{digest}.json"

    def _cached_analysis(self, article: dict) -> dict | None:
        path = self._cache_path(article)
        try:
            if datetime.now().timestamp() - path.stat().st_mtime > (
                    self.config.cache_ttl_hours * 3600):
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            return value.get("analysis") if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _save_analysis(self, article: dict, analysis: dict) -> None:
        path = self._cache_path(article)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "article_url": article.get("url", ""),
            "analysis": analysis,
            "created_at": datetime.now(JST).isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _analysis(self, article: dict) -> dict:
        cached = self._cached_analysis(article)
        if cached:
            return cached
        source_url = str(article.get("url") or "")
        prompt = (
            "投稿文は書かず、ニュースの確認済み事実と政治的構造だけを抽出する。"
            "推測はmissing_informationへ入れ、数字は入力にあるものだけ使う。\n"
            f"title={article.get('title','')}\n"
            f"summary={str(article.get('summary',''))[:5000]}\n"
            f"article_text={str(article.get('article_text',''))[:8000]}\n"
            "social_contextは需要・論点発見用であり、verified=trueかつ別の"
            "一次資料で裏付けられない主張を確認済み事実にしない。\n"
            f"social_context={json.dumps(article.get('social_context', []), ensure_ascii=False)}\n"
            f"source_url={source_url}\nsource_name={article.get('source_name','')}"
        )
        result = self._call("analysis", prompt, ANALYSIS_SCHEMA, "analysis")
        analysis = result.get("analysis") or result
        self._save_analysis(article, analysis)
        return analysis

    def _angles(self, analysis: dict) -> list[dict]:
        prompt = (
            f"次の構造から相互に異なる切り口を最低{self.config.angle_count}個作る。"
            "問題設定、利害関係者、結論を変え、動機を推測しない。"
            "適さない基本軸は除外してよいが、表現だけの言い換えは禁止。\n"
            + json.dumps(analysis, ensure_ascii=False)
        )
        result = self._call("angles", prompt, ANGLES_SCHEMA, "analysis")
        angles = [row for row in result.get("angles", []) if isinstance(row, dict)]
        unique = []
        seen = set()
        for row in angles:
            key = (str(row.get("id")), str(row.get("thesis")))
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        unique = unique[:self.config.angle_count]
        reasons = angle_distinctness_reasons(unique)
        if reasons:
            raise ValueError("angle_distinctness:" + ",".join(reasons))
        return unique

    def _candidates(self, analysis: dict, angles: list[dict]) -> list[dict]:
        prompt = (
            "各切り口から独立した投稿候補を1本ずつ作る。候補同士を融合しない。"
            "事実には忠実、論評には立場を持つ。冒頭で具体的に問題を示し、"
            "決定者・負担者・受益者・暫定結論のうち確認できるものを明示する。"
            "問いだけで終えず、禁止表現を使わない。URLは本文へ入れない。\n"
            f"analysis={json.dumps(analysis, ensure_ascii=False)}\n"
            f"angles={json.dumps(angles, ensure_ascii=False)}"
        )
        prompt += f"\n候補は先頭から最大{self.config.candidate_count}件生成する。"
        result = self._call("candidates", prompt, CANDIDATES_SCHEMA, "writer")
        return [row for row in result.get("candidates", [])
                if isinstance(row, dict)][:self.config.candidate_count]

    def _safety(self, analysis: dict, candidates: list[dict]) -> dict[str, dict]:
        prompt = (
            "各候補を事実性と安全性だけで0〜10評価する。面白さは評価しない。"
            "根拠URLのない重要断定、未確認の金額・人数・被害・動機を検出する。\n"
            f"analysis={json.dumps(analysis, ensure_ascii=False)}\n"
            f"candidates={json.dumps(candidates, ensure_ascii=False)}"
        )
        result = self._call("safety", prompt, SAFETY_SCHEMA, "judge")
        return {str(row.get("candidate_id")): row for row in result.get(
            "evaluations", []) if isinstance(row, dict)}

    def _judge(self, candidates: list[dict]) -> dict[str, dict]:
        prompt = (
            "安全足切り通過候補だけを、面白さ・具体性・引用性で採点する。"
            "中立性、礼儀正しさ、炎上回避は順位点へ混ぜない。\n"
            + json.dumps(candidates, ensure_ascii=False)
        )
        result = self._call("quality", prompt, QUALITY_SCHEMA, "judge")
        return {str(row.get("candidate_id")): row for row in result.get(
            "evaluations", []) if isinstance(row, dict)}

    def _embedding_reasons(self, candidates: list[dict]) -> dict[str, list[str]]:
        reasons = {str(row.get("candidate_id")): [] for row in candidates}
        if not (_bool("POLITICS_ENABLE_SEMANTIC_DEDUP", "true")
                and self.embed_texts and self.history):
            return reasons
        recent = self.history[-20:]
        candidate_texts = [str(row.get("text") or "") for row in candidates]
        history_texts = [str(row.get("claim_summary")
                             or row.get("tweet_text") or "") for row in recent]
        vectors = self.embed_texts(candidate_texts + history_texts)
        if len(vectors) != len(candidate_texts) + len(history_texts):
            return reasons
        threshold = float(os.environ.get(
            "POLITICS_SEMANTIC_DEDUP_THRESHOLD", ".88"))
        for index, row in enumerate(candidates):
            if any(cosine_similarity(vectors[index], old) >= threshold
                   for old in vectors[len(candidate_texts):]):
                reasons[str(row.get("candidate_id"))].append(
                    "embedding_semantic_duplicate")
        return reasons

    def _finalize(self, analysis: dict, winner: dict) -> dict:
        prompt = (
            "この1案だけを磨く。他候補の主張・論点を混ぜない。冒頭、重複、"
            "語尾、抽象語、改行、文字数だけを改善し、数字・固有名詞・主張を"
            "変えない。X版とThreads版を別々に自然に書き、途中切断しない。\n"
            f"analysis={json.dumps(analysis, ensure_ascii=False)}\n"
            f"winner={json.dumps(winner, ensure_ascii=False)}"
        )
        return self._call("finalize", prompt, FINAL_SCHEMA, "finalizer")

    def _verify_final(
        self, analysis: dict, winner: dict, versions: dict[str, str]
    ) -> dict:
        prompt = (
            "最終文を公開直前に再審査する。元候補との論点一致、確認済み事実、"
            "数字、人名・政党名・組織名、断定強度、人格攻撃、意味的な禁止表現を"
            "検査する。問題が一つでもあればpassed=false。面白さは採点しない。\n"
            f"analysis={json.dumps(analysis, ensure_ascii=False)}\n"
            f"winner={json.dumps(winner, ensure_ascii=False)}\n"
            f"versions={json.dumps(versions, ensure_ascii=False)}"
        )
        return self._call("final_verification", prompt, FINAL_VERIFY_SCHEMA, "judge")

    def run(self, article: dict, *, minimum_mode: bool = False) -> dict:
        analysis = self._analysis(article)
        if minimum_mode:
            angles = [{
                "id": "minimum_accountability",
                "title": "意思決定と検証責任",
                "thesis": str(analysis.get("accountability_question")
                              or analysis.get("main_event") or ""),
                "target_actor": str(analysis.get("decision_maker") or ""),
                "reader_relevance": str(
                    analysis.get("impact_on_daily_life") or ""),
                "supporting_facts": [
                    str(row.get("fact") or "")
                    for row in analysis.get("verified_facts", [])
                    if isinstance(row, dict)
                ][:3],
                "risk_notes": list(analysis.get("missing_information") or [])[:3],
            }]
        else:
            angles = self._angles(analysis)
            if len(angles) < min(6, self.config.angle_count):
                raise ValueError("insufficient_distinct_angles")
        candidates = self._candidates(analysis, angles)
        safety = self._safety(analysis, candidates)
        embedding_reasons = self._embedding_reasons(candidates)
        evaluated = []
        for row in candidates:
            candidate_id = str(row.get("candidate_id") or "")
            row["candidate_id"] = candidate_id
            row["safety_scores"] = safety.get(candidate_id, {})
            reasons = safety_rejection_reasons(row["safety_scores"])
            fact_urls = {
                str(fact.get("source_url") or "")
                for fact in analysis.get("verified_facts", [])
                if isinstance(fact, dict) and fact.get("source_url")
            }
            support_urls = {
                str(value) for value in row.get("supporting_fact_urls", [])
                if value
            }
            if not support_urls or not support_urls.issubset(fact_urls):
                reasons.append("important_claim_without_verified_source_url")
            reasons.extend(f"banned_phrase:{x}" for x in contains_banned_phrase(
                str(row.get("text") or "")))
            if _bool("POLITICS_ENABLE_SEMANTIC_DEDUP", "true"):
                reasons.extend(semantic_duplicate(row, self.history))
                reasons.extend(embedding_reasons.get(candidate_id, []))
            row["rejection_reasons"] = sorted(set(reasons))
            row["rejected"] = bool(reasons)
            evaluated.append(row)
        eligible = [row for row in evaluated if not row["rejected"]]
        if not eligible:
            return self._result(article, analysis, angles, evaluated, None, "")
        quality = self._judge(eligible)
        for row in eligible:
            row["quality_scores"] = quality.get(row["candidate_id"], {})
            row["final_score"] = quality_score(row["quality_scores"])
        winner = select_winner(evaluated)
        if not winner:
            return self._result(article, analysis, angles, evaluated, None, "")
        final = self._finalize(analysis, winner)
        if str(final.get("candidate_id") or "") != winner["candidate_id"]:
            winner["rejected"] = True
            winner["rejection_reasons"].append("finalizer_candidate_mismatch")
            return self._result(article, analysis, angles, evaluated, None, "")
        versions = independent_platform_versions(
            str(final.get("x_text") or ""), str(final.get("threads_text") or ""))
        final_reasons = contains_banned_phrase(versions["x"])
        allowed_numbers = {
            value for value in re.findall(
                r"\d+(?:[.,]\d+)?%?",
                json.dumps(analysis, ensure_ascii=False))
        }
        final_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", versions["x"]))
        if final_numbers - allowed_numbers:
            final_reasons.append("finalizer_added_number")
        final_reasons.extend(semantic_duplicate({
            **winner, "text": versions["x"],
            "hook": next(iter(_sentences(versions["x"])), ""),
        }, self.history))
        verification = self._verify_final(analysis, winner, versions)
        if not verification.get("passed"):
            final_reasons.extend(
                f"final_verification:{value}"
                for value in verification.get("reasons", [])
            )
        if not versions["x"] or final_reasons:
            winner["rejected"] = True
            winner["rejection_reasons"].extend(
                ["final_length"] if not versions["x"] else [
                    f"banned_phrase:{x}" if x in BANNED_PHRASES else x
                    for x in final_reasons])
            return self._result(article, analysis, angles, evaluated, None, "")
        winner_id = winner["candidate_id"]
        return self._result(
            article, analysis, angles, evaluated, winner_id, versions["x"],
            threads_text=versions["threads"], winner=winner)

    def _result(
        self, article: dict, analysis: dict, angles: list[dict],
        candidates: list[dict], selected_id: str | None, final_text: str,
        *, threads_text: str = "", winner: dict | None = None,
    ) -> dict:
        result = {
            "article_url": article.get("url", ""), "analysis": analysis,
            "angles": angles, "candidates": candidates,
            "selected_candidate_id": selected_id or "",
            "final_text": final_text, "threads_text": threads_text,
            "winner": winner or {}, "model_usage": {
                "api_calls": self.calls, "max_api_calls": self.config.max_calls,
                "api_attempts": sum(int(row.get("api_attempts", 1) or 1)
                                    for row in self.usage),
                "input_tokens": sum(int(row.get("input_tokens", 0) or 0)
                                    for row in self.usage),
                "output_tokens": sum(int(row.get("output_tokens", 0) or 0)
                                     for row in self.usage),
                "estimated_cost_usd": round(sum(float(
                    row.get("estimated_cost_usd", 0) or 0)
                    for row in self.usage), 8),
                "stages": self.usage,
            }, "created_at": datetime.now(JST).isoformat(),
        }
        self._debug_log(result)
        return result

    def _debug_log(self, result: dict) -> None:
        path = self.root / "logs" / "politics_generation.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = result if self.config.debug else {
            "article_url_hash": hashlib.sha256(str(
                result.get("article_url", "")).encode()).hexdigest(),
            "selected_candidate_id": result.get("selected_candidate_id", ""),
            "candidate_decisions": [{
                "candidate_id": row.get("candidate_id", ""),
                "angle_id": row.get("angle_id", ""),
                "final_score": row.get("final_score", 0),
                "rejected": row.get("rejected", False),
                "rejection_reasons": row.get("rejection_reasons", []),
            } for row in result.get("candidates", [])],
            "model_usage": result.get("model_usage", {}),
            "created_at": result.get("created_at", ""),
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _obj_schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


FACT_SCHEMA = _obj_schema({
    "fact": {"type": "string"}, "source_url": {"type": "string"},
    "confidence": {"type": "number"},
}, ["fact", "source_url", "confidence"])
NUMBER_SCHEMA = _obj_schema({
    "label": {"type": "string"}, "value": {"type": "string"},
    "source_url": {"type": "string"},
}, ["label", "value", "source_url"])
ANALYSIS_BODY = {
    "verified_facts": {"type": "array", "items": FACT_SCHEMA},
    "main_event": {"type": "string"},
    "key_actors": {"type": "array", "items": {"type": "string"}},
    "decision_maker": {"type": "string"},
    "beneficiaries": {"type": "array", "items": {"type": "string"}},
    "cost_bearers": {"type": "array", "items": {"type": "string"}},
    "official_explanation": {"type": "string"},
    "practical_effect": {"type": "string"},
    "contradictions": {"type": "array", "items": {"type": "string"}},
    "accountability_question": {"type": "string"},
    "impact_on_daily_life": {"type": "string"},
    "missing_information": {"type": "array", "items": {"type": "string"}},
    "usable_numbers": {"type": "array", "items": NUMBER_SCHEMA},
}
ANALYSIS_SCHEMA = _obj_schema(
    {"analysis": _obj_schema(ANALYSIS_BODY, list(ANALYSIS_BODY))}, ["analysis"])
ANGLE_BODY = _obj_schema({
    "id": {"type": "string"}, "title": {"type": "string"},
    "thesis": {"type": "string"}, "target_actor": {"type": "string"},
    "reader_relevance": {"type": "string"},
    "supporting_facts": {"type": "array", "items": {"type": "string"}},
    "risk_notes": {"type": "array", "items": {"type": "string"}},
}, ["id", "title", "thesis", "target_actor", "reader_relevance",
    "supporting_facts", "risk_notes"])
ANGLES_SCHEMA = _obj_schema(
    {"angles": {"type": "array", "items": ANGLE_BODY}}, ["angles"])
CANDIDATE_BODY = _obj_schema({
    "candidate_id": {"type": "string"}, "angle_id": {"type": "string"},
    "text": {"type": "string"}, "hook": {"type": "string"},
    "thesis": {"type": "string"}, "target_actor": {"type": "string"},
    "template_id": {"type": "string"},
    "supporting_fact_urls": {"type": "array", "items": {"type": "string"}},
}, ["candidate_id", "angle_id", "text", "hook", "thesis", "target_actor",
    "template_id", "supporting_fact_urls"])
CANDIDATES_SCHEMA = _obj_schema(
    {"candidates": {"type": "array", "items": CANDIDATE_BODY}}, ["candidates"])
SAFETY_KEYS = ("factual_accuracy", "source_support", "defamation_risk",
               "unsupported_inference_risk", "misleading_risk",
               "policy_violation_risk")
SAFETY_BODY = _obj_schema({
    "candidate_id": {"type": "string"},
    **{key: {"type": "number"} for key in SAFETY_KEYS},
    "notes": {"type": "array", "items": {"type": "string"}},
}, ["candidate_id", *SAFETY_KEYS, "notes"])
SAFETY_SCHEMA = _obj_schema(
    {"evaluations": {"type": "array", "items": SAFETY_BODY}}, ["evaluations"])
QUALITY_KEYS = tuple(QUALITY_WEIGHTS)
QUALITY_BODY = _obj_schema({
    "candidate_id": {"type": "string"},
    **{key: {"type": "number"} for key in QUALITY_KEYS},
    "summary": {"type": "string"},
}, ["candidate_id", *QUALITY_KEYS, "summary"])
QUALITY_SCHEMA = _obj_schema(
    {"evaluations": {"type": "array", "items": QUALITY_BODY}}, ["evaluations"])
FINAL_SCHEMA = _obj_schema({
    "candidate_id": {"type": "string"}, "x_text": {"type": "string"},
    "threads_text": {"type": "string"},
}, ["candidate_id", "x_text", "threads_text"])
FINAL_VERIFY_SCHEMA = _obj_schema({
    "passed": {"type": "boolean"},
    "factual_accuracy": {"type": "number"},
    "source_support": {"type": "number"},
    "names_unchanged": {"type": "boolean"},
    "numbers_unchanged": {"type": "boolean"},
    "claim_unchanged": {"type": "boolean"},
    "assertion_strength_safe": {"type": "boolean"},
    "semantic_cliche": {"type": "boolean"},
    "personal_attack": {"type": "boolean"},
    "reasons": {"type": "array", "items": {"type": "string"}},
}, ["passed", "factual_accuracy", "source_support", "names_unchanged",
    "numbers_unchanged", "claim_unchanged", "assertion_strength_safe",
    "semantic_cliche", "personal_attack", "reasons"])
