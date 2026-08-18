# Team Development Guidelines

## Tech Stack
- FastAPI
- Python 3.12
- Pydantic v2
- SQLAlchemy + SQLite
- Pytest
- Ruff + Black
- Docker

---

## Architecture

```
API → Service → Engine → Repository → Storage
```

- Keep layers independent.
- No direct API → Repository calls.
- Engine must not depend on FastAPI or the database.

---

## Code Style

- Use type hints everywhere.
- Follow single responsibility principle.
- Prefer composition over long `if-else` chains.
- Use the plugin/registry pattern for rule evaluators.
- Use `logging` instead of `print()`.
- Raise custom exceptions where appropriate.

---

## Git Workflow

- Never commit directly to `main`.
- Create feature branches:
  - `feature/rule-engine`
  - `feature/api`
  - `feature/tests`
- Open a PR into `main`, 1 teammate review before merge.
- No `develop` branch — event is time-boxed, extra hop just adds overhead.
- Pull and sync frequently.

---

## AI Usage Rules

AI is a coding assistant—not the final reviewer.

Before accepting AI-generated code:
- ✅ Understand it
- ✅ Ensure it compiles
- ✅ Ensure it follows our architecture
- ✅ Run tests
- ✅ Review for maintainability

Use focused prompts instead of asking AI to build the entire project.

---

## Definition of Done

A feature is complete only if:

- [ ] Code works
- [ ] Type hints added
- [ ] Tests pass
- [ ] Logging included
- [ ] Error handling implemented
- [ ] API/docs updated if needed

---

## Configuration

- No hardcoded values.
- Use `.env` or `config.py`.
- Keep secrets out of the repository.

---

## Rule Engine Guidelines

Every evaluator must implement the same interface:

```python
apply(rule, request) -> RuleResult
```

`apply()` is per-evaluator, not to be confused with the top-level engine
`evaluate(request, rules, policy) -> Decision` (see PS4_Team_Project_Structure.md
"contract" section) — that name is reserved for the engine's public entrypoint.

Adding a new rule type should require:
1. Creating a new evaluator.
2. Registering it in the registry.

No engine changes.

---

## Testing

Write tests for:
- Rule evaluators
- Decision engine
- API endpoints
- Edge cases
- Invalid requests

---

## Logging

Log important events:

- Incoming requests
- Rule evaluation
- Final decision
- Errors
- Execution time

Avoid excessive logging.

---

## AI Engineering Log

Update continuously during development.

Record:
- AI tool used
- Prompt
- Accepted code
- Modified code
- Rejected code
- Validation performed
- Timestamp

---

## Mid-Challenge Changes

When new requirements arrive:

- Extend the architecture.
- Avoid quick hacks.
- Keep interfaces stable.
- Minimize changes to existing modules.

Design for extensibility from the beginning.
