/* ============================================================
   FILE   : frontend/config.js
   OWNER  : Frontend (shared R1/R2)
   PURPOSE: Runtime API base URL. Loaded BEFORE app.js.

   This static frontend has no build step, so the API URL can't be baked
   in at build time. Instead it's read at runtime from window.RULEFLOW_API,
   which this file sets. Precedence in app.js:
       window.RULEFLOW_API  (this file — set per deployment)
         -> localStorage "ruleflow_api"  (per-browser override)
           -> "http://localhost:8000"    (local dev default)

   DEPLOY: overwrite this file with the target API URL. Examples —
     Docker entrypoint:  echo "window.RULEFLOW_API='${API_URL}';" > config.js
     Static host (CI):    same one-liner in the build/publish step
     Same-origin (proxy): set it to "" so calls use relative URLs.
   ============================================================ */
window.RULEFLOW_API = "http://localhost:8000";
