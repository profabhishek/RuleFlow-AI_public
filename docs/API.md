# RuleFlow AI - API Documentation

## Base URL

```
http://localhost:8000
```

---

# Health Check

## GET /

### Description

Checks whether the API is running.

### Response

```json
{
    "status": "healthy"
}
```

---

# Decision APIs

## POST /decide

### Description

Evaluates an input against the available rules.

### Request

```json
{
    "sample": "To be updated after implementation"
}
```

### Response

```json
{
    "decision": "...",
    "matched_rules": [],
    "confidence": 0
}
```

---

## POST /decide/bulk

### Description

Evaluates multiple requests in one API call.

---

# Rule Management

## GET /rules

Returns all configured rules.

---

## POST /rules

Creates a new rule.

---

## PUT /rules/{id}

Updates an existing rule.

---

## DELETE /rules/{id}

Deletes a rule.

---

# Audit APIs

## GET /audit

Returns decision history.

---

# Error Responses

## 400 Bad Request

Invalid request.

## 404 Not Found

Requested resource not found.

## 500 Internal Server Error

Unexpected server error.
