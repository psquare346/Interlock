# Agent: Review Queue Assistant

**Job:** Make every queued catalog row a ten-second decision instead of a
two-minute investigation. It explains, ranks, and pre-fills; the click is
always human, and the data is never touched.

**Phase:** 1 · **Model tier:** cheap, batch mode · **Runs:** async, after ingestion

---

## Input

- The `needs_review` and `manual` rows of a `CatalogVersion`, each with its
  `review_reasons`, `price_flags`, candidate suggestions, and raw source row
  **with prices, cost centers, and commercial terms stripped** — the standing
  ingestion guardrail applies here too. Price *flags* (computed deltas) are
  in scope; price *values* are not needed to explain them and are not sent.
- The supplier's correction history (what reviewers fixed last time)

## Output

Per row, attached to the queue entry:

```json
{
  "explanation": "One sentence: why this row is here, in reviewer language",
  "recommendation": "accept_suggestion | pick_candidate_2 | needs_source_file | reject",
  "recommendation_confidence": 0.87,
  "batch_group": "uom-KRT"
}
```

`batch_group` clusters rows with the same root cause so the UI can offer
"37 rows failed on unit KRT — review once, apply to all". The apply-to-all
click is one human decision covering many rows; the agent only groups.

## Pipeline

1. **Group** (code): cluster queue rows by identical `review_reasons` shape.
2. **Explain** (cheap model, one batch call per group): write the explanation
   once per group, not once per row. A group of 37 identical UoM failures is
   one model call.
3. **Rank candidates** (code + cheap model): order suggestions by prior
   acceptance rate for this supplier; the model breaks ties on description
   similarity only.
4. **Learn** (code): every human decision writes back the mapping rule /
   few-shot example, per the ingestion agent's learning loop. The assistant
   reads that history; it never writes it directly.

## Guardrails

- Read-only on catalog data. The only writes are annotation fields.
- Never recommend `accept` for a hard-fail row (the validation layer's
  verdict is not negotiable) — for those, only explain and group.
- Never generate a value for a missing field as if it came from the file.
  A suggested value is always labelled as a suggestion with its source.
- Explanations name the evidence ("unit 'KRT' is not in the T006 mirror"),
  never the model's confidence theater ("I believe this is likely...").

## Evaluation

| Metric | Target | Note |
|---|---|---|
| Median seconds per queue decision | < 10 | Measured in the UI |
| Recommendation acceptance rate | > 85% | Below that, the ranking is noise |
| Batch-group precision | > 99% | A wrong row in an apply-to-all batch is a silent error |
| Explanation flagged unhelpful | < 5% | Thumbs-down in the queue UI |
