"""Balanced four-axis review scoring and anti-escalation learning filters."""

from __future__ import annotations


def _norm(value, ceiling: float) -> float:
    return max(0.0, min(1.0, float(value or 0) / max(ceiling, 1.0)))


def calculate_four_axes(metrics: dict) -> dict:
    definitions = {
        "spread": [
            ("impressions", 10000, .40, False),
            ("impressions_per_hour", 1000, .35, False),
            ("reposts", 100, .25, False),
        ],
        "trust": [
            ("bookmarks", 100, .25, False),
            ("quotes", 50, .20, False),
            ("profile_clicks", 200, .25, False),
            ("constructive_replies", 50, .20, False),
            ("corrections", 3, .10, True),
        ],
        "conversation": [
            ("replies", 100, .35, False),
            ("unique_repliers", 50, .30, False),
            ("manual_reply_candidates", 10, .20, False),
            ("conversation_depth", 10, .15, False),
        ],
        "business": [
            ("follow_gain_estimate", 50, .35, False),
            ("profile_clicks", 200, .30, False),
            ("url_clicks", 100, .20, False),
            ("external_conversions", 20, .15, False),
        ],
    }
    out = {}
    for axis, components in definitions.items():
        present = []
        for key, ceiling, weight, invert in components:
            if key not in metrics or metrics.get(key) is None:
                continue
            value = _norm(metrics.get(key), ceiling)
            present.append((1.0 - value if invert else value, weight))
        total_weight = sum(weight for _, weight in present)
        out[f"{axis}_score"] = (
            round(10 * sum(value * weight for value, weight in present) / total_weight, 3)
            if total_weight else None
        )
    available = [value for value in out.values() if value is not None]
    out["balanced_score"] = round(sum(available) / len(available), 3) if available else None
    return out


def safety_dimensions(post: dict) -> dict:
    text = str(post.get("tweet_text") or post.get("text") or "")
    anger_terms = ("許せない", "怒り", "ふざけるな", "売国", "狂って", "断固")
    attack_terms = ("無能", "馬鹿", "クズ", "人間失格", "消えろ")
    partisan_terms = ("左派は", "右派は", "支持者は", "全員")
    return {
        "anger_score": min(10, sum(term in text for term in anger_terms) * 2),
        "personal_attack_score": min(10, sum(term in text for term in attack_terms) * 4),
        "partisan_bias_score": min(10, sum(term in text for term in partisan_terms) * 3),
        "claim_risk": str(post.get("claim_risk", "low")),
        "correction_required": bool(post.get("correction_required")),
        "delete_or_hide_required": bool(
            post.get("delete_or_hide_required") or post.get("manual_delete")
            or post.get("manual_delete_required")
        ),
    }


def eligible_winning_example(post: dict, trust_minimum: float = 3.0) -> bool:
    safety = safety_dimensions(post)
    trust = float(post.get("trust_score", post.get("four_axes", {}).get("trust_score", 0)) or 0)
    follow_conversion = post.get("follow_conversion_estimate")
    inflammatory_low_conversion = (
        safety["anger_score"] >= 6 and follow_conversion is not None
        and float(follow_conversion or 0) <= 0
    )
    return bool(
        safety["personal_attack_score"] == 0
        and safety["partisan_bias_score"] < 6
        and safety["claim_risk"] != "high"
        and not safety["correction_required"]
        and not safety["delete_or_hide_required"]
        and not inflammatory_low_conversion
        and trust >= trust_minimum
    )


def winner_types(post: dict) -> list[str]:
    """Classify useful examples by purpose instead of a single viral score."""
    if not eligible_winning_example(post):
        return []
    axes = post.get("four_axes") or calculate_four_axes(post)
    out = []
    if float(axes.get("spread_score") or 0) >= 4:
        out.append("viral_winner")
    if float(axes.get("trust_score") or 0) >= 4:
        out.append("trust_winner")
    if float(axes.get("conversation_score") or 0) >= 4:
        out.append("conversation_winner")
    if float(axes.get("business_score") or 0) >= 3:
        out.append("conversion_winner")
    return out


def preferred_winner_types(post_type: str) -> tuple[str, ...]:
    return {
        "breaking_news": ("viral_winner", "trust_winner"),
        "strong_opinion": ("trust_winner", "conversation_winner"),
        "morning_evening_digest": ("conversion_winner", "trust_winner"),
        "digest": ("conversion_winner", "trust_winner"),
    }.get(post_type, ("trust_winner", "viral_winner"))
