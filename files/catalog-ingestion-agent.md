# Agent: Catalog Ingestion

**Job:** Turn an arbitrary supplier file into validated, SAP-ready catalog rows with
the smallest possible amount of human attention.

**Phase:** 1 · **Model tier:** smart (column mapping) + cheap (row work) · **Runs:** async job

---

## Input

- A file: CSV, XLSX, Ariba CIF 3.0, or BMEcat 1.2/2005
- `supplier_id`, `tenant_id`
- The tenant's canonical schema and existing mapping rules
- Any previously confirmed mapping for this supplier

## Output

A `CatalogVersion` with every row assigned one of three states:

| State | Gate | What happens |
|---|---|---|
| `auto_approved` | confidence ≥ 0.95 | Published on release |
| `needs_review` | 0.70 – 0.95 | Queued with suggestion pre-filled, one click to accept |
| `manual` | < 0.70 | Queued with top 3 candidates and the reason each was rejected |

---

## Pipeline — this is a pipeline, not a prompt

### Step 1 — Parse (code, no model)
Detect encoding, delimiter, header row. Handle merged cells, multi-row headers,
and trailing junk rows. Emit a normalized row iterator plus a sample of 20 rows.

### Step 2 — Column mapping (smart model, exactly one call per file)

If a confirmed mapping exists for this supplier, **skip this step entirely.**
That is the whole point — load #2 onward is zero-touch.

Prompt inputs: header row, 20 sampled data rows, canonical target schema,
and the tenant's glossary of prior mappings.

Return strict JSON:

```json
{
  "mappings": [
    {"source_column": "Art.-Nr.", "target_field": "supplier_part_id",
     "confidence": 0.98, "rationale": "German abbreviation for article number"},
    {"source_column": "VPE", "target_field": "price_unit",
     "confidence": 0.81, "rationale": "Verpackungseinheit — packaging unit"}
  ],
  "unmapped_columns": ["Lagerort"],
  "missing_required": []
}
```

Human confirms once in the UI. **Persist the mapping against the supplier forever.**

### Step 3 — Row normalization (cascade — do not skip layers)

Run the cheapest layer first and only escalate what it cannot resolve.

| Field | Layer 1 — free | Layer 2 — cheap | Layer 3 — model |
|---|---|---|---|
| Unit of measure | Lookup table → SAP T006. ~95% hit | Embedding nearest-neighbour on the tail | Cheap model on the ~1% remainder |
| UNSPSC | Use supplied code if valid | Embed description → kNN over pre-embedded UNSPSC corpus → top 10 | Cheap model reranks the 10, returns code + confidence |
| Material group | Tenant's UNSPSC→MatGroup map | Few-shot from tenant's historical `EKPO` lines | Cheap model proposes → always queued, never auto |
| Description | Truncate to 40, spill to `LONGTEXT` | — | Batch cleanup on marketing-copy rows |
| Duplicates | Exact SKU match | Cosine > 0.93 across suppliers | Model adjudicates ambiguous pairs only |

Escalating everything to a model costs roughly 50x more and scores *worse*.
Classification here is a retrieval problem wearing a generation costume.

### Step 4 — Validation (code, no model)

Hard-fail any row that violates these. No confidence score overrides them.

- `unit_price` parses as a positive decimal
- `currency` is ISO 4217 and matches the contract currency
- `price_unit` is a positive integer (missing this creates 100x price errors on "per 100" items)
- `uom_sap` exists in the tenant's T006 mirror
- `material_group` exists in the tenant's material group master
- `supplier_part_id` is non-empty and unique within the version
- effective dates fall inside the linked contract's validity window

### Step 5 — Price change surveillance (statistical, not a model)

Against the previous published version, compute per-item price delta.
Flag: any increase > 10%, any change on a contracted item, any move to or from zero.
The model only writes the *explanation* shown to the reviewer.

### Step 6 — Learning loop

Every human correction writes back:
1. a tenant-scoped mapping rule (deterministic, applied at Layer 1 next time)
2. a few-shot example for that supplier's file style

Track auto-approve coverage per supplier over successive loads. That rising
curve is both your engineering signal and a sales asset.

---

## Tools

```
parse_file(file_ref) -> RowIterator
get_supplier_mapping(supplier_id) -> Mapping | None
save_supplier_mapping(supplier_id, mapping)
lookup_uom(raw) -> SapUom | None
embed(texts) -> vectors
knn_unspsc(vector, k=10) -> candidates
get_tenant_matgroup_map(tenant_id)
get_price_history(item_key) -> series
queue_for_review(row, suggestion, rationale, confidence)
```

## Guardrails

- Never write a price. Prices are parsed from the file by code and validated, full stop.
- Never invent a `supplier_part_id`. A row without one is `manual`, always.
- Never auto-approve a material group. It drives GL account determination.
- Abort the job if projected model spend exceeds `LLM_MAX_SPEND_USD_PER_JOB`.
- Strip prices, cost centers, and commercial terms from anything sent to a model.
  Description and UoM strings are sufficient for classification.

## Evaluation

Golden set: 2,000 hand-labelled rows across five supplier file styles
(US industrial, EU/BMEcat, lab/scientific, IT/office, MRO).

| Metric | Target | Note |
|---|---|---|
| UNSPSC accuracy (level 3) | > 92% | |
| **Auto-approve precision** | **> 99.5%** | A false positive puts bad data in SAP. Weight far above recall |
| Auto-approve coverage | > 70%, rising per load | Drives onboarding economics |
| Cost per 50k-SKU load | < $2 | With batch mode on |

CI gate: no prompt or model change merges on a >1% regression in any of these.
