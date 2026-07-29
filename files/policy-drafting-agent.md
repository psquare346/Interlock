# Agent: Policy Drafting

**Job:** Turn a written procurement policy document into structured rules a human
can review and activate. It drafts; it never decides. Runtime evaluation is
deterministic code (`backend/app/services/policy.py`) — a model that evaluates
policy at runtime will eventually approve a $400k purchase because the request
was worded persuasively.

**Phase:** 1 · **Model tier:** smart (extraction) · **Runs:** async job

---

## Input

- A policy document: PDF, DOCX, or pasted text
- `tenant_id`, optional `contract_no` to bind the policy to
- The rule grammar (ops, actions, scope keys from `SCOPE_FIELD_MAP`)
- The tenant's known field vocabulary (company codes, plants, material groups)

## Output

A policy payload for `load_policy_from_dict` — which **always lands in DRAFT**.
There is no code path from this agent to ACTIVE. Every rule carries:

| Field | Why |
|---|---|
| `source_clause` | Section/paragraph reference in the source document |
| `source_quote` | The verbatim sentence the rule was derived from |
| `draft_confidence` | The agent's own calibrated estimate |

A rule without a quotable source sentence is not emitted; it is listed under
`unextracted_items` with the reason, so the reviewer knows what the document
said that the agent could not formalize.

## Pipeline

1. **Segment** (code): split the document into numbered clauses.
2. **Extract** (smart model, one call per clause batch): for each clause decide —
   is this a rule, a definition, or prose? Rules become `{scope, condition,
   action, route_to}` in the grammar. Definitions feed a glossary used by later
   clauses ("'major purchase' means > $25,000").
3. **Cross-check** (code): every emitted rule must round-trip — evaluate it
   against 3 synthetic contexts (clearly-inside, clearly-outside, boundary) and
   verify it fires exactly where the quote says it should. Rules that fail the
   round-trip drop to `draft_confidence: 0` and are flagged.
4. **Conflict scan** (code + cheap model): flag pairs of rules whose conditions
   overlap with different actions; the model writes the one-line explanation.

## Tools

```
segment_document(file_ref) -> [Clause]
get_tenant_vocabulary(tenant_id) -> {company_codes, plants, material_groups, ...}
validate_rule_grammar(rule) -> [errors]        # ops exist, scope keys known
dry_run_rule(rule, context) -> fired: bool     # the real evaluator, in-process
```

## Guardrails

- Never activate. Never call anything but draft-creation endpoints.
- Never emit a rule without `source_quote`. Paraphrase is not evidence.
- Never widen: if a threshold is ambiguous ("around $5,000"), pick the
  stricter reading and flag it.
- Monetary values: extract the number and currency verbatim; no unit conversion.
- The requested validity window comes from the document or the human — never
  defaulted to "forever".

## Evaluation

Golden set: 20 real policy documents (anonymized) with hand-labelled rule sets.

| Metric | Target | Note |
|---|---|---|
| Rule recall | > 90% | A missed rule is a silent policy hole |
| Rule precision | > 95% | An invented rule blocks legitimate purchases |
| Round-trip pass rate | 100% | Enforced in code, not a model metric |
| Human edits per drafted rule | < 0.3, falling | The product signal |
