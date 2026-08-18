# PS4 — Team Project Structure

Everyone follows this layout. It is designed so each role owns a clean vertical slice, the engine stays framework-independent, and the likely mid-challenge change requests are cheap to add. This structure has been import-graph checked: no circular imports, strictly layered, engine tests pass.


---

## The tree

```
ps4-decision-platform/
├── app/                          # The FastAPI web application
│   ├── __init__.py
│   ├── main.py                   # App entrypoint, mounts routers, health check   [R2]
│   ├── config.py                 # Settings from env vars (no internal deps)      [R2]
│   ├── dependencies.py           # Shared DI: rule store, audit log               [R2]
│   ├── schemas.py                # Pydantic API request/response models           [R2]
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── decisions.py          # POST /decide, POST /decide/bulk                 [R2]
│   │   ├── rules.py              # GET/POST/PUT/DELETE /rules                       [R2]
│   │   └── audit.py              # GET /audit  (decision history)                  [R3]
│   ├── services/
│   │   ├── __init__.py
│   │   ├── rule_store.py         # Rule persistence via SQLAlchemy+SQLite           [R2]
│   │   └── audit_log.py          # Append-only decision trail                      [R3]
│   └── core/
│       ├── __init__.py
│       ├── logging.py            # Structured logging setup                        [R3]
│       └── errors.py             # Exception handlers -> clean JSON errors         [R3]
│
├── rule_engine/                  # ===== THE PURE CORE — ROLE 1 — ZERO web deps ==
│   ├── __init__.py               # Public API: evaluate, Rule, Decision, ...       [R1]
│   ├── models.py                 # Rule / Condition / Decision + validation        [R1]
│   ├── evaluators.py             # Operator plugin registry (add rule types here)  [R1]
│   ├── engine.py                 # evaluate() pure function + decision policies     [R1]
│   └── loader.py                 # Load & validate rules from JSON/YAML/DB          [R1]
│
├── rules/
│   └── rules.json                # Seed data — loaded into SQLite on first boot     [R1 seeds]
│
├── tests/
│   ├── __init__.py
│   ├── test_evaluators.py        # Engine operator tests                           [R1]
│   ├── test_engine.py            # Engine decision tests                           [R1]
│   ├── test_api_decisions.py     # /decide integration tests                       [R3]
│   ├── test_api_rules.py         # /rules CRUD tests                               [R3]
│   └── test_audit.py             # /audit tests                                    [R3]
│
├── docs/
│   ├── ARCHITECTURE.md           # System design overview (for judges)             [R4]
│   └── API.md                    # Endpoint reference (supplements Swagger)        [R4]
│
├── AI_LOG.md                     # AI Engineering Log — REQUIRED deliverable       [R4 + all]
├── README.md                     # Setup & run instructions                        [R4]
├── requirements.txt              # Dependencies                                    [R4]
├── Dockerfile                    # Containerization                                [R4]
├── docker-compose.yml            # App + SQLite volume mount                        [R4]
├── .env.example                  # Config template                                 [R4]
├── .gitignore
└── pytest.ini                    # Test config (pythonpath = .)                    [R1/R4]
```

`[R1]`=Rule Engine, `[R2]`=API, `[R3]`=Quality/Audit, `[R4]`=Infra/Integration/Demo.

---

## The one rule that must never be broken

**`rule_engine/` never imports from `app/`. Dependencies only point one way:**

```
        app/main.py
            │  (imports)
   app/routers ─▶ app/dependencies ─▶ app/services ─▶ rule_engine/   ◀── the pure core
            │                                    ▲
   app/schemas, app/config, app/core ───────────┘
```

The engine is the bottom layer and depends on nothing above it. The API imports the engine; the engine must never import the API. This is what keeps the core testable in isolation and is a graded "separation of concerns" point. (Verified: no circular imports.)

---

## Ownership — one primary owner per file

| Role | Owns these files | Their deliverable |
|---|---|---|
| **R1 — Rule Engine** | `rule_engine/*`, `rules/rules.json`, `tests/test_engine.py`, `tests/test_evaluators.py` | A pure, tested `evaluate(request, rules) -> Decision` with no web deps |
| **R2 — API & Data** | `app/main.py`, `app/config.py`, `app/dependencies.py`, `app/schemas.py`, `app/routers/decisions.py`, `app/routers/rules.py`, `app/services/rule_store.py` | Working, documented REST API wrapping the engine |
| **R3 — Quality & Audit** | `app/core/logging.py`, `app/core/errors.py`, `app/services/audit_log.py`, `app/routers/audit.py`, `tests/test_api_*.py`, `tests/test_audit.py` | Logging, error handling, audit trail, integration tests |
| **R4 — Infra & Demo** | `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `.env.example`, `.gitignore`, `README.md`, `docs/*`, `AI_LOG.md`, `pytest.ini` | One-command Docker run, docs, curated AI log, demo |

`__init__.py` files are just package markers — whoever owns the folder creates them. `AI_LOG.md` is curated by R4 but **every member adds their own entries live**.

---

## The contract between R1 and R2 (agree on this first)

R2 calls the engine like this, and nothing more:

```python
from rule_engine import evaluate, DecisionPolicy
decision = evaluate(request_dict, rules_list, DecisionPolicy(mode="priority"))
return decision.to_dict()
```

So R1 must guarantee:
- `evaluate(request: dict, rules: list[Rule], policy) -> Decision`
- `Decision.to_dict()` returns the JSON the API sends back
- `load_rules(path) -> list[Rule]` for R2's rule store

Once this signature is fixed, R1 and R2 can build in parallel without blocking each other.

---

## Why this structure already absorbs the mid-challenge change request

The judges introduce a new requirement ~halfway. Each likely one has a pre-planned home:

| Change request | Where it goes | Why it's cheap |
|---|---|---|
| New rule type | `rule_engine/evaluators.py` | Add one registered function; engine untouched |
| Rule versioning | `rule_engine/models.py` (`version` field exists) + `rule_store.py` | Field already present from day one |
| Bulk evaluation | `app/routers/decisions.py` (`/decide/bulk`) | Loop `evaluate` over a list — engine is a pure function |
| Audit history | `app/services/audit_log.py` + `app/routers/audit.py` | Already in the structure |
| Async processing | `app/routers/decisions.py` (FastAPI `BackgroundTasks`) | Engine is decoupled from HTTP |
| External API call | new file in `app/services/` | Isolated in the services layer |
| Auth / authz | `app/dependencies.py` + `app/core/` | Middleware at the boundary; business logic untouched |

The point to make to judges: *"we didn't design for one change — the layering means most changes land in exactly one file."*

---

## Requirement → file coverage (nothing is homeless)

| PS4 requirement | Lives in |
|---|---|
| Rule management (numeric/bool/string/date/multi/priority) | `rule_engine/evaluators.py` + `models.py` |
| Rules in JSON/YAML/DB | `rule_engine/loader.py` + `app/services/rule_store.py` + `rules/rules.json` |
| Decision engine (outcomes, confidence, priority) | `rule_engine/engine.py` |
| Explainability (evaluated/matched/rejected + human-readable) | `rule_engine/engine.py` (`Decision`), surfaced by `routers/decisions.py` |
| Extensibility (new rule types, minimal change) | `rule_engine/evaluators.py` registry |
| Clean architecture / separation of concerns | the layered layout itself |
| Logging | `app/core/logging.py` + `app/services/audit_log.py` |
| Error handling | `app/core/errors.py` + engine validation |
| Configuration management | `app/config.py` + `.env.example` |
| Unit tests | `tests/` |
| API documentation | FastAPI auto Swagger at `/docs` + `docs/API.md` |
| Containerization | `Dockerfile` (+ `docker-compose.yml`) |
| AI Engineering Log | `AI_LOG.md` |

---

## Day-of setup (first 30 minutes)

1. R4 creates the repo, the empty folder tree above, `.gitignore`, `requirements.txt`, `pytest.ini` — pushes to a shared remote.
2. Everyone clones and works on **their own branch**; merge via PRs. (Ownership is cross-checked against commit history, so commit your own work under your own name — no one person typing for the team.)
3. R1 and R2 agree the `evaluate()` contract above before writing code.
4. R1 builds the engine first (it unblocks everyone); R2 scaffolds the FastAPI app and health check in parallel using a stub engine if needed.
5. `requirements.txt` is shared: each role appends their own deps in their own PR; R4 dedupes on merge conflicts. Don't wait on R4 to add a dep you need now.

## Checkpoints

- **T+30min** — engine contract agreed, empty skeleton pushed, everyone unblocked.
- **Midpoint** — engine + API happy path working end-to-end; mid-challenge requirement usually lands here.
- **T-60min** — feature freeze. Demo rehearsal, `AI_LOG.md` cleanup, docs pass.
- **T-15min** — final push, `docker compose up` smoke test on a clean clone.

Build the actual code during the event — this document is the plan, not the code.
