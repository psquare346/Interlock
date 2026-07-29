# Agent: Supplier Onboarding

**Job:** Get a supplier from "here are our cXML credentials" to a passing test
punchout with the least back-and-forth. The agent validates, test-fires, and
diagnoses; a human flips the switch from `test` to `production`.

**Phase:** 2 · **Model tier:** cheap (diagnosis phrasing) · **Runs:** interactive session

---

## Input

- The credential set from START-HERE §4 (endpoint URL, From/To identities,
  shared secret, protocol, SAP vendor number)
- The supplier's contact email (for the drafted-but-never-sent fix requests)

## Output

An onboarding report per supplier:

| Check | Result | Detail |
|---|---|---|
| Field completeness | pass/fail | e.g. "SAP vendor number missing — this is the field everyone forgets" |
| Endpoint reachable | pass/fail | TLS version, cert chain, response time |
| PunchOutSetupRequest | pass/fail | Response code + parsed StartPage URL |
| Round trip | pass/fail | Test cart returned and parsed |
| Diagnosis | prose | Written by the model **from the structured failures only** |

## Pipeline

1. **Static validation** (code): all required fields for the protocol present;
   URL is https; vendor number matches SAP's 10-digit zero-padded format;
   deployment mode is `test`.
2. **Handshake** (code): send a real `PunchOutSetupRequest`; classify the
   failure — DNS, TLS, HTTP status, cXML `Status` code, credential rejection
   (`406`), malformed response. Each failure class maps to a known fix.
3. **Test round trip** (code): open the StartPage, return a one-line test cart,
   verify the `PunchOutOrderMessage` parses and the line survives field mapping.
4. **Diagnose** (cheap model): turn the structured failure into two outputs —
   a fix-it note for our admin, and a draft email to the supplier's e-commerce
   contact. Both land in the UI; neither is sent automatically.

## Tools

```
validate_credentials(supplier) -> [errors]
send_posr(supplier) -> HandshakeResult          # classified, never raw-text-only
fetch_start_page(url) -> PageResult
parse_poom(xml) -> CartLines | ParseError
draft_supplier_email(failure) -> text           # draft only, human sends
```

## Guardrails

- The shared secret is write-only: never echoed to the UI, the logs, or a
  model prompt. Diagnosis sees the failure *class*, not the credential.
- Never flip `deployment_mode` to `production`. Ever.
- Never retry a rejected credential more than twice — supplier gateways
  lock accounts, and unlocking one takes a week of email.
- Outbound test carts are marked as tests in the cXML (`deploymentMode="test"`).

## Evaluation

| Metric | Target |
|---|---|
| Failure classification accuracy | > 95% on the replay corpus of real failed handshakes |
| Onboardings needing zero human diagnosis | > 60%, rising |
| Median time credential-receipt → passing test punchout | < 1 day |
