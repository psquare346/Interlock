# Agent: Price Tier Shape Detection

**Job:** Look at a supplier's pricing file and decide which of the known
quantity-break shapes it uses, and which columns play which role. Parsing and
arithmetic then happen in code (`backend/app/services/pricing.py`) — the agent
routes, it never computes a price.

**Phase:** 1 · **Model tier:** cheap (shape vote), smart only on conflict · **Runs:** inline, one call per file

---

## Input

- Header row + 20 sample rows of a pricing file
- The five known shapes and their parser signatures

## Output

```json
{
  "shape": "wide_columns | long_rows | encoded_string | discount_pct | base_plus_breaks",
  "confidence": 0.93,
  "column_roles": {"sku": "Part #", "tiers": "Qty Pricing", "base_price": null},
  "evidence": "Columns price_1_9, price_10_49, price_50_plus follow the wide pattern",
  "unrecognized": false
}
```

`unrecognized: true` routes the file to a human with the sample attached.
Guessing a shape is worse than asking: a mis-shaped parse produces ladders
that fail validation at best and plausible-but-wrong prices at worst.

## Pipeline

1. **Deterministic first** (code, free): run the existing detectors —
   `detect_wide_columns` on headers, `_ENCODED` regex on sampled cells. A
   single unambiguous hit skips the model entirely (~80% of files).
2. **Model vote** (cheap): only when detectors disagree or find nothing.
   Prompt = headers + samples + one example of each shape.
3. **Verification parse** (code): parse 5 rows with the chosen parser and run
   `validate_ladder`. Validation errors on all 5 → treat as `unrecognized`,
   never fall through to the second-choice shape silently.

## Tools

```
detect_wide_columns(headers) -> [(col, lo, hi)]
scan_encoded_cells(rows) -> hit_ratio
trial_parse(shape, rows) -> LadderParseResult
```

## Guardrails

- Never compute or transform a price. Role assignment only.
- Never map a column to `base_price` and `tier_price` simultaneously.
- Discount-percentage shapes: the agent identifies the shape; the absolute
  prices come from `apply_discount_ladder`, in code, always.
- A file mixing shapes across rows is `unrecognized`, not "mostly shape X".

## Evaluation

Golden set: 150 pricing files, 30 per shape, labelled.

| Metric | Target |
|---|---|
| Shape accuracy | > 98% |
| False `unrecognized` rate | < 10% |
| Wrong-shape-but-confident (> 0.8) | 0 — this is the catastrophic case |
