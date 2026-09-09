"""Opt-in OpenAI variant generation for offline post experiments.

The model sees only the immutable fact packet.  Published outcomes, historical
posts and experiment results are deliberately not accepted by this module.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable

from api_budget import estimate_openai, finalize, reserve


def _enabled() -> bool:
    return os.environ.get(
        "POST_EXPERIMENT_OPENAI_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _uses_only_packet_literals(text: str, fact_packet: dict) -> bool:
    """Reject invented numeric and URL literals before candidates are accepted."""
    packet_text = json.dumps(fact_packet, ensure_ascii=False)
    literals = re.findall(
        r"https?://[^\s、。]+|(?<![A-Za-z])\d[\d,.]*(?:兆|億|万|円|%|％|年|月|日|人|件)?",
        text,
    )
    return all(literal in packet_text for literal in literals)


def generate_fact_packet_variants(
    fact_packet: dict,
    count: int,
    *,
    client_factory: Callable | None = None,
    budget_path: Path | None = None,
) -> dict:
    """Return structured variants, or a safe fallback reason.

    This function never publishes.  A caller must explicitly opt in through
    ``POST_EXPERIMENT_OPENAI_ENABLED``.  Budget/pricing/key failures all fail
    closed and allow the caller to use deterministic local candidates.
    """
    if not _enabled():
        return {"variants": None, "status": "disabled"}
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return {"variants": None, "status": "missing_credentials"}

    model = os.environ.get(
        "POST_EXPERIMENT_OPENAI_MODEL",
        os.environ.get("OPENAI_MODEL_STANDARD", "gpt-5.6-luna"),
    ).strip()
    max_output = max(300, min(
        4000, int(os.environ.get(
            "POST_EXPERIMENT_OPENAI_MAX_OUTPUT_TOKENS", "1800"))))
    estimated_input = max(500, min(
        8000, len(json.dumps(fact_packet, ensure_ascii=False)) * 2))
    maximum_cost = estimate_openai(model, estimated_input, max_output)
    hard_cap = max(0.0, float(os.environ.get(
        "POST_EXPERIMENT_OPENAI_MAX_COST_USD", "0.08")))
    if maximum_cost is None:
        return {"variants": None, "status": "pricing_unavailable"}
    if maximum_cost > hard_cap:
        return {"variants": None, "status": "per_run_cost_cap",
                "estimated_max_cost_usd": maximum_cost}

    reservation_id, reason = reserve(
        "openai", "post_experiment_candidates", model, maximum_cost,
        metadata={
            "generation_reason": "ab_fact_packet_only",
            "variant_count": count,
            "publishing_allowed": False,
        },
        path=budget_path,
    )
    if not reservation_id:
        return {"variants": None, "status": reason or "budget_restricted",
                "estimated_max_cost_usd": maximum_cost}

    schema = {
        "type": "object",
        "properties": {
            "variants": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "properties": {
                        "angle_type": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["angle_type", "text"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["variants"],
        "additionalProperties": False,
    }
    # Do not add content, scores, prior posts, outcomes, or recommendations.
    model_input = {
        "fact_packet": fact_packet,
        "variant_count": count,
        "constraints": {
            "language": "Japanese",
            "use_only_supplied_facts": True,
            "do_not_invent_numbers_names_quotes_or_urls": True,
            "distinct_angles": True,
            "publication": "prohibited; candidate analysis only",
        },
    }
    try:
        if client_factory is None:
            from openai import OpenAI
            client_factory = OpenAI
        client = client_factory(
            api_key=os.environ["OPENAI_API_KEY"],
            timeout=float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "90")),
            max_retries=0,
        )
        response = client.responses.create(
            model=model,
            instructions=(
                "Generate Japanese A/B test drafts using only the supplied fact_packet. "
                "Never infer or add facts. Make every angle materially distinct. "
                "Return only the required JSON. These are offline candidates and must "
                "not contain instructions to publish."
            ),
            input=json.dumps(model_input, ensure_ascii=False),
            max_output_tokens=max_output,
            text={"format": {
                "type": "json_schema", "name": "post_experiment_variants",
                "strict": True, "schema": schema,
            }},
            store=False,
        )
        payload = json.loads(str(getattr(response, "output_text", "") or ""))
        variants = payload.get("variants")
        if not isinstance(variants, list) or len(variants) != count:
            raise ValueError("invalid_variant_count")
        cleaned = []
        for item in variants:
            angle = str(item.get("angle_type") or "").strip()
            text = str(item.get("text") or "").strip()
            if not angle or not text:
                raise ValueError("empty_variant")
            if not _uses_only_packet_literals(text, fact_packet):
                raise ValueError("unsupported_literal")
            cleaned.append({"angle_type": angle, "text": text})
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        details = getattr(usage, "input_tokens_details", None)
        cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
        actual = estimate_openai(
            model, input_tokens, output_tokens, cached_tokens) or 0
        finalize(
            reservation_id, actual, success=True,
            input_tokens=input_tokens, cached_tokens=cached_tokens,
            output_tokens=output_tokens, path=budget_path,
        )
        return {
            "variants": cleaned, "status": "generated", "model": model,
            "estimated_cost_usd": actual,
        }
    except Exception as exc:
        finalize(
            reservation_id, 0, success=False,
            error_type=type(exc).__name__, path=budget_path,
        )
        return {"variants": None, "status": "generation_failed",
                "error_type": type(exc).__name__, "model": model}
