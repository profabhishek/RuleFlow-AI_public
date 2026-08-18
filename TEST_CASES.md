# RuleFlow-AI Test Cases

## Scope

This catalog covers the complete automated suite: HTTP routing, SQLite
persistence, rule evaluation, audit logging, AI-output validation, seeded
business rules, frontend/API integration contracts, and malformed-input edge
cases. Parametrized variants count as separate collected test cases.

Run the full suite from the repository root:

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

## Test inventory

- `tests/test_ai_validator.py` — 129 cases
- `tests/test_api_decisions.py` — 6 cases
- `tests/test_api_end_to_end.py` — 61 cases
- `tests/test_api_rules.py` — 6 cases
- `tests/test_audit.py` — 4 cases
- `tests/test_edge_cases.py` — 149 cases
- `tests/test_engine.py` — 21 cases
- `tests/test_evaluators.py` — 25 cases
- `tests/test_seed_rules.py` — 27 cases
- **Total — 428 cases**

## Final execution result

- Automated suite: **428 passed, 0 failed** in 7.54 seconds.
- Live backend smoke: `GET http://127.0.0.1:8000/health` returned HTTP 200
  with `{"status":"ok"}`.
- Live frontend smoke: `GET http://127.0.0.1:3000/` returned HTTP 200 and
  the RuleFlow-AI page.
- Frontend JavaScript syntax, Python formatting, Ruff checks, and IDE
  diagnostics passed.

## End-to-end API and frontend contract cases

These tests use the real FastAPI app, router functions, SQLite rule store,
rule engine, serialization, and audit repository. LLM calls are replaced at
the dependency-injection boundary so no network or API key is required.

### Application and frontend contract — 3 cases

1. `GET /health` returns HTTP 200 and `{"status": "ok"}`.
2. A CORS preflight from `http://localhost:3000` is accepted and permits GET.
3. `frontend/index.html` loads `styles.css` and `app.js`, and every static API
   route called by `frontend/app.js` exists in FastAPI's OpenAPI document.

### Single-decision edge cases — 6 cases

1. An array-valued request is rejected with HTTP 422.
2. A `null` request is rejected with HTTP 422.
3. A string-valued request is rejected with HTTP 422.
4. A numeric request is rejected with HTTP 422.
5. An audit-storage write failure is logged but does not block a valid
   decision response.
6. With no rules, `/decide` uses the configured `DEFAULT_OUTCOME`.

### Bulk decisions — 7 cases

1. A mixed approval, rejection, and empty request returns decisions in input
   order: `APPROVE`, `REJECT`, `REVIEW`.
2. An empty request list returns an empty response list.
3. An object instead of a list is rejected with HTTP 422.
4. A string instead of a list is rejected with HTTP 422.
5. A list containing an integer is rejected with HTTP 422.
6. A list containing `null` is rejected with HTTP 422.
7. Every item in a successful bulk request creates its own decision audit
   record in the same order.

### Gated decisions — 5 cases

1. A clean applicant clears all four gates and returns `APPROVE`.
2. A watchlist hit makes the fraud gate and final result `REJECT`, while other
   valid gates can still approve.
3. Missing fields fail safely to `REVIEW`.
4. An empty rule store returns the configured `REVIEW` default and no gates.
5. A non-object gated request is rejected with HTTP 422.

### Natural-language decisions — 12 cases

1. Valid extracted JSON runs through the real engine and returns the extracted
   request, an `APPROVE` decision, and the generated explanation.
2. Malformed AI JSON is rejected with HTTP 422.
3. AI output `[]` is rejected because it is not an object.
4. AI output `null` is rejected because it is not an object.
5. AI output containing a JSON string is rejected.
6. AI output containing a JSON number is rejected.
7. An extraction-provider exception maps to HTTP 502.
8. An explanation-provider exception maps to HTTP 502.
9. A missing `text` property is rejected with HTTP 422.
10. A `null` `text` property is rejected with HTTP 422.
11. A numeric `text` property is rejected with HTTP 422.
12. The configured offline stub extracts fields, evaluates real seed rules,
    and generates an explanation without a network call.

### Natural-language rule creation — 13 cases

1. A valid AI-generated rule is normalized (including `>=` to canonical
   `gte`), persisted, returned, and readable through `GET /rules/{id}`.
2. A Markdown-fenced/non-JSON response is rejected with HTTP 422.
3. AI output `[]` is rejected with HTTP 422.
4. AI output `null` is rejected with HTTP 422.
5. AI output containing a JSON string is rejected with HTTP 422.
6. AI output containing a JSON number is rejected with HTTP 422.
7. An incomplete generated rule is rejected and is not persisted.
8. A generated duplicate ID returns HTTP 409.
9. An LLM provider failure returns HTTP 502.
10. A request missing `text` returns HTTP 422.
11. A request with `text: null` returns HTTP 422.
12. A request with a list-valued `text` returns HTTP 422.
13. The configured offline stub generates, normalizes, persists, and retrieves
    a rule without an API key.

### Rule CRUD edge cases — 11 cases

1. Getting a missing rule returns HTTP 404.
2. Updating a missing rule returns HTTP 404.
3. Deleting a missing rule returns HTTP 404.
4. During update, the path ID overrides a conflicting body ID.
5. Operator aliases are canonicalized before persistence.
6. A list-valued rule body is rejected with HTTP 422.
7. A `null` rule body is rejected with HTTP 422.
8. A string-valued rule body is rejected with HTTP 422.
9. A numeric rule body is rejected with HTTP 422.
10. Malformed request JSON is rejected with HTTP 422.
11. The SQLite store returns only seed rules matching a requested category.

### Audit endpoint edge cases — 4 cases

1. Filtering by `decision_evaluation` returns only matching records.
2. An unknown event filter is rejected with HTTP 422.
3. An unfiltered storage failure returns a sanitized HTTP 500 without private
   exception details.
4. A filtered storage failure returns a sanitized HTTP 500 without private
   exception details.

## Existing API integration cases

### `POST /decide` — 6 cases

- Clean applicant approval and response schema.
- Underage applicant rejection and matching rule ID.
- Missing decision fields fail safely to `REVIEW`.
- Invalid field datatypes do not crash evaluation.
- Empty objects fail safely to `REVIEW`.
- Unknown request fields are ignored without changing the result.

### Rule CRUD — 6 cases

- Valid create returns HTTP 201 and the complete rule schema.
- Update returns HTTP 200 and persists changed values.
- Delete returns HTTP 204 and the rule subsequently returns HTTP 404.
- Listing returns every created rule with the public schema.
- Invalid creation returns HTTP 422.
- Duplicate creation returns HTTP 409.

### Audit history — 4 cases

- Empty history returns an empty list.
- One decision record is serialized correctly.
- Multiple decision/schema/rule events retain insertion order and schema.
- A corrupt JSONL line is skipped without hiding valid records.

## AI validator cases — 129 cases

### Input boundary and response contract

- Truncated JSON, trailing commas, missing values, and invalid UTF-8.
- Python/JSON null, arrays, strings, integers, booleans, and floats where an
  object is required.
- Missing, null, non-string, empty, and unknown operations.
- Case-sensitive operation names.
- Raw JSON strings and bytes.
- Input/context immutability and structured validation responses.

### Add-field operations

- Every supported datatype: array, boolean, date, datetime, integer, number,
  object, and string.
- Missing field name/datatype, nulls, wrong types, and empty values.
- Duplicate and protected fields.
- Unsupported, case-mismatched, empty, null, numeric, object, and list
  datatype declarations.
- Unconventional-name warnings and injected datatype/protected-field policies.

### Update/delete/rename-field operations

- Valid existing-field updates, deletes, and renames.
- Unknown and protected source fields.
- Unsupported datatype changes and no-op updates.
- Fields referenced by rules cannot be changed, deleted, or renamed.
- Rename target requirements, target collisions, protected targets, and
  missing schema context.

### Rule operations

- Complete add/update/delete rule operations.
- Rule values that are null, strings, lists, or integers.
- Missing/empty IDs, outcomes, and conditions; invalid logic/enabled/weight/
  priority values.
- Duplicate, missing, and inferred rule IDs.
- Supported operator families for numeric, string, boolean, membership,
  emptiness, regex, and date conditions.
- Unsupported operators/types, unknown schema fields, dotted fields, and
  injected operator/type policies.
- Required complete metadata and condition values.
- Unknown rule properties.
- Operator/datatype/value semantic compatibility.
- Missing state context fails closed.
- Internal handler exceptions are sanitized.
- Custom operation registration.

## Rule-engine edge cases — 149 cases

### Model validation and serialization

- Condition objects reject non-dicts, invalid fields, and invalid operators;
  omitted values default to null.
- Rule objects reject non-dicts, invalid IDs/outcomes/logic/conditions/enabled
  values, and invalid numeric fields.
- Logic normalization, numeric coercion, defaults, disabled rules, category,
  priority, version, and weight behavior.
- Condition, rule-result, and decision serialization including confidence
  rounding.

### Numeric, equality, boolean, string, membership, empty, and date operators

- Ordering boundaries, negatives, floats, numeric strings, and bool rejection.
- Inclusive `between` edges and malformed ranges.
- Cross-type numeric equality, fallback equality, and `ne`.
- Strict `is_true`/`is_false`.
- Contains, prefix, suffix, case-insensitive equality, regex search, and
  malformed regex.
- `in`/`not_in` with list, tuple, set, absent values, and invalid collections.
- Empty/non-empty values and inverse behavior.
- Date aliases, date/datetime values, boundaries, and invalid dates.
- Unknown operators, aliases, unary registration, and custom registration.

### Field lookup and conditional evaluation

- Direct keys, dotted paths, literal dotted-key precedence, missing keys, and
  broken nested paths.
- AND/OR behavior, condition ordering, missing-field notes, `is_empty` on a
  missing field, type/operator errors, and unknown operators.
- Evaluator lookup failures and custom evaluator registration.

### Decision policy and evaluation

- Invalid policies; priority/score defaults and explicit thresholds.
- Non-object requests, no rules, omitted policy, priority winner, stable tie
  breaking, weighted confidence, zero weights, disabled rules, and matched/
  rejected/evaluated partitions.
- Unknown rule types and score accumulation.
- Score bands, inclusive thresholds, default outcomes, bounded confidence,
  sentinel bounds, boundary distance, fallback spans, and clamping.
- Deterministic repeated priority and score evaluation.

### Rule loading

- Top-level lists, wrapped rule objects, and empty lists.
- Invalid JSON, wrong wrapper types, scalar/string documents, duplicate IDs,
  file loading, and lossless serialization round trips.

## Core engine cases — 21 cases

- Highest-priority winner, default on no match, winning-rule explanation, and
  stable input-order tie breaking.
- Score-band selection and bounded in-band confidence.
- Invalid decision mode.
- Determinism, missing fields, non-object requests, disabled rules, dotted
  paths, and decision serialization.
- Valid rule parsing and serialization round trips.
- Invalid JSON, non-list input, missing IDs, empty conditions, invalid logic,
  and duplicate IDs.

## Evaluator cases — 25 cases

- Numeric comparisons, aliases, numeric strings, inclusive ranges, malformed
  ranges, and non-numeric values.
- Equality and strict boolean identity.
- String contains/prefix/suffix/case-insensitive/regex behavior and bad regex.
- Membership validation and empty-value behavior.
- Date ordering and invalid dates.
- Unknown operators.
- Conditional result schema, AND/OR, safe missing-field/type mismatch handling,
  and unknown rule types.

## Seed-rule business cases — 27 cases

- All seed rules load, expected IDs exist, four business gates exist, every
  gate has an approval path, and serialization round trips.
- Clean applicant approval.
- Rejections for underage, unverified identity, watchlist, poor credit, and
  high debt-to-income.
- Reviews for high fraud score, fair credit, and borderline debt-to-income.
- Highest-priority explanation when two rejection rules match.
- Empty/missing/wrong-type inputs, non-object input, unknown fields, and
  deterministic repeated evaluation.
- Clean gated approval, one failing gate, and missing gate data.
- Score-mode approval, poor-credit reduction, and deep-negative watchlist
  scoring.

## Additional non-pytest checks

- `node --check frontend/app.js` validates JavaScript syntax.
- `python -m black --check ...` validates formatting of changed Python files.
- `python -m ruff check ...` validates changed test code; existing FastAPI
  dependency declarations trigger Ruff B008 unless that framework-specific
  rule is excluded.
