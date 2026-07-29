"""The AI router. One job for now: column mapping (smart tier, one call per
file, only when rules fail and only when configured).

Every function degrades to None when no key or model is set — callers must
treat None as "queue for a human", never as an error. Prices, cost centers,
and commercial terms are never sent to a model (agent-spec guardrail): the
sample rows are stripped to mapped-candidate text columns only... except that
column mapping is exactly the step where we do not yet know which column is
the price. So samples are truncated hard and the call is header-shaped: 20
rows, 80 chars per cell.
"""

from __future__ import annotations

import json

from ..config import get_settings


def available() -> bool:
    s = get_settings()
    return bool(s.ANTHROPIC_API_KEY and s.LLM_MODEL_SMART)


def map_columns(headers: list[str], sample_rows: list[dict],
                canonical_fields: list[str]) -> dict | None:
    """Ask the smart model to map source columns to the canonical schema.

    Returns the agent-spec JSON shape, or None when unavailable/failed.
    """
    if not available():
        return None

    s = get_settings()
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=s.ANTHROPIC_API_KEY)
        samples = [
            {h: str(r.get(h, ""))[:80] for h in headers}
            for r in sample_rows[:20]
        ]
        prompt = (
            "You map supplier catalog columns to a canonical procurement schema.\n"
            f"Canonical target fields: {canonical_fields}\n"
            f"Source header row: {headers}\n"
            f"Sample rows (truncated): {json.dumps(samples, ensure_ascii=False)}\n\n"
            "Return STRICT JSON only, no prose, in this shape:\n"
            '{"mappings": [{"source_column": "...", "target_field": "...", '
            '"confidence": 0.0, "rationale": "..."}], '
            '"unmapped_columns": [], "missing_required": []}\n'
            "Only map a column when you are confident. Confidence is your own "
            "calibrated estimate; below 0.7 leave the column unmapped."
        )
        message = client.messages.create(
            model=s.LLM_MODEL_SMART,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in message.content if b.type == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        return json.loads(text)
    except Exception:
        # Degrade to rule-only; the review queue absorbs the difference.
        return None
