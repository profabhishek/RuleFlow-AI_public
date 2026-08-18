# RuleFlow AI

## 📌 Overview

RuleFlow AI is a configurable decision engine built using FastAPI. It evaluates incoming requests against business rules and returns explainable decisions based on configurable policies.

The platform is designed with a layered architecture to ensure modularity, extensibility, and maintainability.

---

## ✨ Features

- Numeric, Boolean, String and Date Rule Evaluation
- Priority-based Decision Engine
- Explainable Decision Responses
- Rule Management API (CRUD)
- Bulk Decision Evaluation
- Audit Logging
- JSON/YAML Rule Support
- Dockerized Deployment
- Unit & Integration Testing

---

## 📁 Project Structure

```
ps4-decision-platform/
│
├── app/                 # FastAPI application
├── rule_engine/         # Core decision engine
├── rules/               # Seed rule files
├── tests/               # Unit and integration tests
├── docs/                # Project documentation
├── Dockerfile
├── docker-compose.yml
├── README.md
```

---

## 🛠 Tech Stack

- Python
- FastAPI
- SQLAlchemy
- SQLite
- Pydantic
- Docker
- Pytest

---

## 🚀 Setup

```bash
pip install -r requirements.txt
```

Run the application

```bash
uvicorn app.main:app --reload
```

---

## 🐳 Docker

Build and start the application:

```bash
docker compose up --build
```

The application will be available at:

```
http://localhost:8000
```

Swagger API documentation:

```
http://localhost:8000/docs
```

Stop the application:

```bash
docker compose down
```

## ✅ Run Tests

```bash
pytest
```

---

## 📖 API Documentation

After starting the application, Swagger UI will be available at:

```
http://localhost:8000/docs
```

---

## 🏗 Architecture

The project follows a layered architecture.

```
Client
   │
FastAPI
   │
Routers
   │
Services
   │
Rule Engine
   │
SQLite
```

This separation ensures:

- Easy testing
- Clean architecture
- Extensible rule engine
- Independent business logic

---

## 👥 Team Roles

| Role | Responsibility |
|------|----------------|
| Role 1 | Rule Engine |
| Role 2 | API & Data |
| Role 3 | Quality & Audit |
| Role 4 | Infrastructure & Documentation |

---


## 📄 Documentation

Additional documentation is available in the `docs/` directory.

- API.md
- ARCHITECTURE.md

AI engineering logs are available in the repository root:

- AI_LOG R1.md
- AI_LOG R2.md
- AI_LOG R3.md
- AI_LOG_R4.md
