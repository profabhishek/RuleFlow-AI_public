# RuleFlow-AI Frontend

Zero-dependency static frontend (HTML/CSS/JS — no build step, no CDN, works offline).

## Run

1. Start the backend (needs CORS enabled — already added to `app/main.py`):
   ```bash
   uvicorn app.main:app --reload
   ```
2. Serve this folder on any static server:
   ```bash
   cd frontend
   python -m http.server 3000
   ```
3. Open http://localhost:3000

If the API runs somewhere other than `http://localhost:8000`, set it once in the
browser console: `localStorage.setItem("ruleflow_api", "http://host:port")` and reload.

## API URL configuration

The API base URL is resolved at runtime (no build step), in precedence order:

1. `window.RULEFLOW_API` — set in `config.js`, one value per deployment
2. `localStorage["ruleflow_api"]` — per-browser override (console, above)
3. `http://localhost:8000` — local dev default

## Deploy

Serve this folder on any static host and point it at the deployed API by
**overwriting `config.js`** (the only file that changes per environment):

- **Container / CI** — regenerate `config.js` from an env var at start/publish:
  ```bash
  echo "window.RULEFLOW_API='${API_URL}';" > config.js
  ```
- **Static host** (Netlify/Vercel/GitHub Pages) — run the same one-liner in the
  build/publish step, with `API_URL` set in the host's env settings.
- **Same-origin** (frontend + API behind one domain via reverse proxy) — set it
  empty so calls use relative URLs:
  ```bash
  echo "window.RULEFLOW_API='';" > config.js
  ```

The committed `config.js` defaults to `http://localhost:8000` for local dev.
The backend must allow the frontend's origin (CORS is already wide-open in
`app/main.py` for the demo).

## Screens
- **Dashboard** — health pill, rule stats, quick actions, recent rules.
- **Rules** — full CRUD table; create via structured form (condition builder,
  JSON preview) or via **From text** (AI-authored, human-reviewed).
- **Playground** — structured request builder or plain-English query; renders
  the verdict, confidence, explanation, and the full per-condition trace
  (field / operator / expected / actual / pass / note) for every rule.
