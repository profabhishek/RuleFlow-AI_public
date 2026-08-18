/* ============================================================
   FILE   : frontend/app.js
   OWNER  : Frontend (Abhishek)
   PURPOSE: All UI logic — API client, router, rules CRUD,
            AI rule authoring, decision playground with trace.
   No frameworks, no build step. Backend: FastAPI on :8000.
   ============================================================ */

"use strict";

/* ----------------------------------------------------------------
   API client — every network call goes through here.
----------------------------------------------------------------- */
// Resolved at runtime, in precedence order:
//   window.RULEFLOW_API (config.js, set per deployment) — "" is respected so a
//     same-origin/reverse-proxy deploy can use relative URLs
//   -> localStorage "ruleflow_api" (per-browser override)
//   -> local dev default
const API_BASE =
  (typeof window.RULEFLOW_API === "string" ? window.RULEFLOW_API : null) ??
  localStorage.getItem("ruleflow_api") ??
  "http://localhost:8000";

async function api(method, path, body) {
  let res;
  try {
    res = await fetch(API_BASE + path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    throw new ApiError(0, "Cannot reach the API. Is the backend running on " + API_BASE + "?");
  }
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON body */ }
  if (!res.ok) {
    const detail = data && data.detail ? formatDetail(data.detail) : res.status + " " + res.statusText;
    // Carry the parsed detail too: structured errors (e.g. the duplicate-rule
    // 409) need their fields, not just a flattened message string.
    throw new ApiError(res.status, detail, data && data.detail);
  }
  return data;
}

class ApiError extends Error {
  constructor(status, message, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

function formatDetail(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (d.loc ? d.loc.join(".") + ": " : "") + d.msg).join("; ");
  }
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return JSON.stringify(detail);
}

/* ----------------------------------------------------------------
   Constants & small helpers
----------------------------------------------------------------- */
const OPERATOR_GROUPS = [
  { label: "Comparison", ops: ["gt", "gte", "lt", "lte", "between"] },
  { label: "Equality", ops: ["eq", "ne", "eq_ci"] },
  { label: "Boolean", ops: ["is_true", "is_false"] },
  { label: "String", ops: ["contains", "not_contains", "starts_with", "ends_with", "regex"] },
  { label: "Membership", ops: ["in", "not_in"] },
  { label: "Emptiness", ops: ["is_empty", "is_not_empty"] },
  { label: "Date", ops: ["before", "after", "on_or_before", "on_or_after"] },
];
const OPERATORS = OPERATOR_GROUPS.flatMap((g) => g.ops);
const UNARY_OPS = new Set(["is_true", "is_false", "is_empty", "is_not_empty"]);

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value === undefined || value === null ? "" : String(value);
  return div.innerHTML;
}

function outcomeClass(outcome) {
  const o = String(outcome || "").toUpperCase();
  if (o === "APPROVE") return "approve";
  if (o === "REJECT") return "reject";
  if (o === "REVIEW") return "review";
  return "neutral";
}

function toast(message, kind) {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function setBusy(button, busy, labelBusy) {
  if (busy) {
    button.dataset.label = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> ' + (labelBusy || "Working…");
  } else {
    button.disabled = false;
    if (button.dataset.label) button.innerHTML = button.dataset.label;
  }
}

/* Parse an input string into a sensible JSON value:
   numbers stay numbers, true/false stay booleans, [a,b] becomes a list. */
function smartValue(raw) {
  const s = String(raw).trim();
  if (s === "") return "";
  if (s === "true") return true;
  if (s === "false") return false;
  if (!isNaN(Number(s)) && s !== "") return Number(s);
  if (s.startsWith("[") && s.endsWith("]")) {
    try { return JSON.parse(s); } catch (_) { /* fall through */ }
    return s.slice(1, -1).split(",").map((p) => smartValue(p));
  }
  return s;
}

/* ----------------------------------------------------------------
   Router — hash-based, three views.
----------------------------------------------------------------- */
const VIEWS = ["dashboard", "rules", "playground"];

function navigate() {
  const view = (location.hash || "#dashboard").slice(1);
  const target = VIEWS.includes(view) ? view : "dashboard";
  VIEWS.forEach((v) => {
    $("#view-" + v).classList.toggle("hidden", v !== target);
  });
  $$(".nav-link").forEach((a) => a.classList.toggle("active", a.dataset.view === target));
  if (target === "dashboard") loadDashboard();
  if (target === "rules") loadRules();
}
window.addEventListener("hashchange", navigate);

/* ----------------------------------------------------------------
   Health pill
----------------------------------------------------------------- */
async function checkHealth() {
  const pill = $("#health-pill");
  try {
    await api("GET", "/health");
    pill.className = "health-pill ok";
    $("#health-text").textContent = "API healthy";
  } catch (_) {
    pill.className = "health-pill down";
    $("#health-text").textContent = "API unreachable";
  }
}

/* ----------------------------------------------------------------
   Dashboard
----------------------------------------------------------------- */
async function loadDashboard() {
  try {
    const rules = await api("GET", "/rules");
    $("#stat-rule-count").textContent = rules.length;
    $("#stat-enabled-count").textContent = rules.filter((r) => r.enabled).length;
    const recent = rules.slice(0, 5);
    $("#dash-rules").innerHTML = recent.length
      ? recent.map((r) =>
          '<div class="mini-row">' +
            '<span class="mini-id">' + esc(r.id) + "</span>" +
            '<span class="mini-desc">' + esc(r.description) + "</span>" +
            '<span class="badge ' + outcomeClass(r.outcome) + '">' + esc(r.outcome) + "</span>" +
          "</div>"
        ).join("")
      : '<div class="empty-state"><p>No rules yet.</p></div>';
  } catch (err) {
    $("#dash-rules").innerHTML = '<div class="empty-state"><p>' + esc(err.message) + "</p></div>";
  }
}

/* ----------------------------------------------------------------
   Rules list + delete
----------------------------------------------------------------- */
let rulesCache = [];

async function loadRules() {
  const tbody = $("#rules-tbody");
  tbody.innerHTML = '<tr><td colspan="7"><div class="skeleton-row"></div></td></tr>';
  try {
    rulesCache = await api("GET", "/rules");
    renderRulesTable();
  } catch (err) {
    tbody.innerHTML = "";
    $("#rules-empty").classList.remove("hidden");
    $("#rules-empty").querySelector("p").textContent = err.message;
  }
}

function renderRulesTable() {
  const tbody = $("#rules-tbody");
  if (!rulesCache.length) {
    tbody.innerHTML = "";
    $("#rules-empty").classList.remove("hidden");
    return;
  }
  $("#rules-empty").classList.add("hidden");
  tbody.innerHTML = rulesCache.map((r) =>
    "<tr>" +
      '<td class="td-id">' + esc(r.id) + "</td>" +
      '<td class="td-desc" title="' + esc(r.description) + '">' + esc(r.description) + "</td>" +
      "<td>" + esc(r.category || "general") + "</td>" +
      '<td><span class="badge ' + outcomeClass(r.outcome) + '">' + esc(r.outcome) + "</span></td>" +
      '<td class="num">' + esc(r.priority) + "</td>" +
      '<td class="num">' + esc(r.weight) + "</td>" +
      "<td>" + (r.enabled ? '<span class="pill-on">On</span>' : '<span class="pill-off">Off</span>') + "</td>" +
      '<td><div class="row-actions">' +
        '<button class="btn ghost small" data-edit="' + esc(r.id) + '">Edit</button>' +
        '<button class="btn ghost small" data-delete="' + esc(r.id) + '">Delete</button>' +
      "</div></td>" +
    "</tr>"
  ).join("");

  tbody.querySelectorAll("[data-edit]").forEach((b) =>
    b.addEventListener("click", () => openRuleForm("edit", b.dataset.edit)));
  tbody.querySelectorAll("[data-delete]").forEach((b) =>
    b.addEventListener("click", () => confirmDelete(b.dataset.delete)));
}

let pendingDeleteId = null;
function confirmDelete(id) {
  pendingDeleteId = id;
  $("#confirm-text").textContent = 'This permanently removes "' + id + '".';
  $("#confirm-overlay").classList.remove("hidden");
}

async function doDelete() {
  const btn = $("#btn-confirm-delete");
  setBusy(btn, true, "Deleting…");
  try {
    await api("DELETE", "/rules/" + encodeURIComponent(pendingDeleteId));
    toast("Rule deleted.", "success");
    $("#confirm-overlay").classList.add("hidden");
    loadRules();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(btn, false);
  }
}

/* ----------------------------------------------------------------
   Rule form (create / edit / from-text)
----------------------------------------------------------------- */
let formMode = "create"; // create | edit
let editingId = null;

/* Which authoring surface is showing inside the Add-rule panel.
   "ai"     — describe it in plain English (default; the friendlier path)
   "fields" — fill the structured form in directly
   Editing an existing rule always uses "fields" and hides the tabs. */
function setRuleMode(mode) {
  $$("#rule-mode-tabs .tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.ruleMode === mode));
  $("#ai-entry").classList.toggle("hidden", mode !== "ai");
  $("#rule-form").classList.toggle("hidden", mode !== "fields");
  // Saving only makes sense once the form is on screen; in AI mode the
  // primary action is "Generate rule" inside the panel.
  $("#btn-save-rule").classList.toggle("hidden", mode !== "fields");
}

/* After the AI produces a rule, show the populated form for review while
   keeping the prompt + raw-output disclosure visible above it. */
function showAiReview() {
  $$("#rule-mode-tabs .tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.ruleMode === "fields"));
  $("#ai-entry").classList.remove("hidden");
  $("#rule-form").classList.remove("hidden");
  $("#btn-save-rule").classList.remove("hidden");
  $("#ai-review-wrap").classList.remove("hidden");
}

function openRuleForm(mode, ruleId) {
  formMode = mode === "edit" ? "edit" : "create";
  editingId = ruleId || null;
  $("#form-error").classList.add("hidden");
  $("#json-preview").classList.add("hidden");
  $("#ai-raw-wrap").classList.add("hidden");
  $("#ai-review-wrap").classList.add("hidden");
  $("#conditions-rows").innerHTML = "";
  $("#rule-form").reset();
  $("#f-enabled").checked = true;
  $("#f-id").disabled = formMode === "edit";

  if (formMode === "edit") {
    $("#form-title").textContent = "Edit rule";
    $("#rule-mode-tabs").classList.add("hidden");
    setRuleMode("fields");
    const rule = rulesCache.find((r) => r.id === ruleId);
    if (rule) fillForm(rule);
  } else {
    $("#form-title").textContent = "Add rule";
    $("#rule-mode-tabs").classList.remove("hidden");
    $("#ai-rule-text").value = "";
    addConditionRow();
    setRuleMode("ai"); // plain English is the default entry point
  }
  $("#overlay").classList.remove("hidden");
}

function closeRuleForm() { $("#overlay").classList.add("hidden"); }

function fillForm(rule) {
  $("#f-id").value = rule.id;
  $("#f-description").value = rule.description || "";
  $("#f-category").value = rule.category || "general";
  $("#f-outcome").value = rule.outcome || "";
  $("#f-logic").value = rule.logic || "AND";
  $("#f-priority").value = rule.priority ?? 0;
  $("#f-weight").value = rule.weight ?? 0;
  $("#f-enabled").checked = rule.enabled !== false;
  $("#conditions-rows").innerHTML = "";
  (rule.conditions || []).forEach((c) => addConditionRow(c));
  if (!(rule.conditions || []).length) addConditionRow();
}

function addConditionRow(cond) {
  const row = document.createElement("div");
  row.className = "cond-row";
  const opts = OPERATOR_GROUPS.map((g) =>
    '<optgroup label="' + g.label + '">' +
    g.ops.map((op) =>
      '<option value="' + op + '"' + (cond && cond.operator === op ? " selected" : "") + ">" + op + "</option>"
    ).join("") +
    "</optgroup>"
  ).join("");
  const valAttr = cond && cond.value !== undefined && cond.value !== null
    ? esc(Array.isArray(cond.value) ? JSON.stringify(cond.value) : cond.value)
    : "";
  row.innerHTML =
    '<input type="text" class="c-field" placeholder="field e.g. credit_score" value="' + (cond ? esc(cond.field) : "") + '" />' +
    '<select class="c-op">' + opts + "</select>" +
    '<input type="text" class="c-value" placeholder="value" value="' + valAttr + '" />' +
    '<button type="button" class="btn icon c-del" title="Remove">✕</button>';
  row.querySelector(".c-del").addEventListener("click", () => { row.remove(); refreshJsonPreview(); });
  row.querySelector(".c-op").addEventListener("change", (e) => {
    row.querySelector(".c-value").disabled = UNARY_OPS.has(e.target.value);
    refreshJsonPreview();
  });
  row.querySelectorAll("input, select").forEach((el) => el.addEventListener("input", refreshJsonPreview));
  if (cond && UNARY_OPS.has(cond.operator)) row.querySelector(".c-value").disabled = true;
  $("#conditions-rows").appendChild(row);
  refreshJsonPreview();
}

function buildRuleFromForm() {
  const conditions = $$("#conditions-rows .cond-row").map((row) => {
    const field = row.querySelector(".c-field").value.trim();
    const operator = row.querySelector(".c-op").value;
    const cond = { field, operator };
    if (!UNARY_OPS.has(operator)) cond.value = smartValue(row.querySelector(".c-value").value);
    return cond;
  }).filter((c) => c.field !== "");

  return {
    id: $("#f-id").value.trim(),
    description: $("#f-description").value.trim(),
    type: "conditional",
    category: $("#f-category").value.trim() || "general",
    logic: $("#f-logic").value,
    outcome: $("#f-outcome").value.trim(),
    weight: Number($("#f-weight").value) || 0,
    priority: parseInt($("#f-priority").value, 10) || 0,
    enabled: $("#f-enabled").checked,
    conditions,
  };
}

function refreshJsonPreview() {
  const pre = $("#json-preview");
  if (!pre.classList.contains("hidden")) {
    pre.textContent = JSON.stringify(buildRuleFromForm(), null, 2);
  }
}

/* Show the "an equivalent rule already exists" dialog and resolve with the
   user's choice. Resolves true if they choose to proceed anyway. */
function confirmDuplicate(detail) {
  return new Promise((resolve) => {
    const isConflict = detail.kind === "conflict";
    $("#dup-title").textContent = isConflict
      ? "Conflicting rule already exists"
      : "Similar rule already exists";
    $("#dup-message").innerHTML =
      (isConflict ? '<span class="dup-kind-conflict">Conflict — </span>' : "") +
      esc(detail.message || "");
    $("#dup-existing").innerHTML = (detail.existing || []).map((r) =>
      '<div class="dup-rule">' +
        '<div class="dup-rule-top">' +
          '<span class="dup-rule-id">' + esc(r.id) + "</span>" +
          '<span class="badge ' + outcomeClass(r.outcome) + '">' + esc(r.outcome) + "</span>" +
          '<span class="rr-meta">' + esc(r.category) + " · prio " + esc(r.priority) + "</span>" +
        "</div>" +
        '<div class="dup-rule-desc">' + esc(r.description || "No description.") + "</div>" +
      "</div>"
    ).join("");
    $("#btn-dup-proceed").textContent = isConflict ? "Create anyway" : "Create duplicate";
    $("#dup-overlay").classList.remove("hidden");

    const cleanup = (choice) => {
      $("#dup-overlay").classList.add("hidden");
      $("#btn-dup-proceed").onclick = null;
      $("#btn-dup-cancel").onclick = null;
      resolve(choice);
    };
    $("#btn-dup-proceed").onclick = () => cleanup(true);
    $("#btn-dup-cancel").onclick = () => cleanup(false);
  });
}

async function saveRule() {
  const btn = $("#btn-save-rule");
  const errBox = $("#form-error");
  errBox.classList.add("hidden");

  const rule = buildRuleFromForm();
  if (!rule.id) return showFormError("Rule ID is required.");
  if (!rule.outcome) return showFormError("Outcome is required.");
  if (!rule.conditions.length) return showFormError("At least one condition (with a field name) is required.");

  const send = async (force) => {
    const query = force ? "?force=true" : "";
    if (formMode === "edit") {
      const body = { ...rule };
      delete body.id;
      await api("PUT", "/rules/" + encodeURIComponent(editingId) + query, body);
      toast("Rule updated.", "success");
    } else {
      await api("POST", "/rules" + query, rule);
      toast("Rule created.", "success");
    }
    closeRuleForm();
    loadRules();
    loadDashboard();
  };

  setBusy(btn, true, "Saving…");
  try {
    await send(false);
  } catch (err) {
    // A structured 409 means an equivalent rule exists — ask, don't just fail.
    if (err.status === 409 && err.detail && err.detail.kind) {
      setBusy(btn, false);
      if (await confirmDuplicate(err.detail)) {
        setBusy(btn, true, "Saving…");
        try {
          await send(true);
        } catch (retryErr) {
          showFormError(retryErr.message);
        }
      }
    } else {
      showFormError(err.status === 409 ? "A rule with this ID already exists." : err.message);
    }
  } finally {
    setBusy(btn, false);
  }
}

function showFormError(message) {
  const errBox = $("#form-error");
  errBox.textContent = message;
  errBox.classList.remove("hidden");
}

/* AI rule authoring: generate -> review in the same form -> save. */
async function generateRuleFromText() {
  const text = $("#ai-rule-text").value.trim();
  if (!text) return showFormError("Describe the rule first.");
  const btn = $("#btn-generate-rule");
  setBusy(btn, true, "Generating…");
  $("#form-error").classList.add("hidden");

  const generate = async (force) => {
    const res = await api("POST", "/rules/from-text" + (force ? "?force=true" : ""), { text });
    // The backend already SAVED the rule (validated via Rule.from_dict).
    // Show it in the form for transparency, switch to edit mode so any
    // tweaks the user makes update the same rule.
    fillForm(res.rule);
    formMode = "edit";
    editingId = res.rule.id;
    $("#f-id").disabled = true;
    $("#form-title").textContent = "Review AI-generated rule";
    $("#ai-raw").textContent = res.raw_ai_output;
    $("#ai-raw-wrap").classList.remove("hidden");
    showAiReview();
    if (res.ai_degraded) {
      showFormError("⚠ " + (res.ai_note || "AI unavailable — this rule came from offline extraction, review it carefully."));
    }
    toast(
      res.ai_degraded
        ? "Generated offline (AI unavailable) — review carefully."
        : "Rule generated and saved — review below, edit if needed.",
      res.ai_degraded ? "error" : "success"
    );
    loadRules();
    loadDashboard();
  };

  try {
    await generate(false);
  } catch (err) {
    // Describing an existing rule in different words is the most common way
    // to create an accidental duplicate — surface it and let the user decide.
    if (err.status === 409 && err.detail && err.detail.kind) {
      setBusy(btn, false);
      if (await confirmDuplicate(err.detail)) {
        setBusy(btn, true, "Generating…");
        try {
          await generate(true);
        } catch (retryErr) {
          showFormError(retryErr.message);
        }
      } else {
        showFormError("Cancelled — nothing was saved. The existing rule already covers this.");
      }
    } else {
      showFormError(err.status === 409
        ? "AI generated an ID that already exists — reword the description and retry."
        : err.message);
    }
  } finally {
    setBusy(btn, false);
  }
}

/* ----------------------------------------------------------------
   Playground — structured + NL, result rendering with trace.
----------------------------------------------------------------- */
function addKvRow(key, value) {
  const row = document.createElement("div");
  row.className = "kv-row";
  row.innerHTML =
    '<input type="text" class="kv-key" placeholder="field e.g. age" value="' + esc(key || "") + '" />' +
    '<input type="text" class="kv-value" placeholder="value e.g. 25" value="' + esc(value ?? "") + '" />' +
    '<button type="button" class="btn icon kv-del" title="Remove">✕</button>';
  row.querySelector(".kv-del").addEventListener("click", () => row.remove());
  $("#kv-rows").appendChild(row);
}

function buildRequestFromKv() {
  const request = {};
  $$("#kv-rows .kv-row").forEach((row) => {
    const key = row.querySelector(".kv-key").value.trim();
    if (key) request[key] = smartValue(row.querySelector(".kv-value").value);
  });
  return request;
}

async function runDecision() {
  const request = buildRequestFromKv();
  if (!Object.keys(request).length) return toast("Add at least one field.", "error");
  const btn = $("#btn-run-decision");
  setBusy(btn, true, "Evaluating…");
  try {
    const decision = await api("POST", "/decide", request);
    renderDecision(decision);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(btn, false);
  }
}

/* Gated decision: runs each rule category (kyc/fraud/underwriting/
   affordability, ...) as an independent gate via POST /decide/gated.
   Reuses the same structured request builder as runDecision — no separate
   input needed. Renders into #result-gated instead of #result-body. */
async function runGatedDecision() {
  const request = buildRequestFromKv();
  if (!Object.keys(request).length) return toast("Add at least one field.", "error");
  const btn = $("#btn-run-gated");
  setBusy(btn, true, "Checking gates…");
  try {
    const result = await api("POST", "/decide/gated", request);
    renderGatedDecision(result);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(btn, false);
  }
}

async function runNlQuery() {
  const text = $("#nl-query").value.trim();
  if (!text) return toast("Describe a scenario first.", "error");
  const btn = $("#btn-run-nl");
  setBusy(btn, true, "Asking…");
  try {
    const res = await api("POST", "/decide/query", { text });
    const extracted = $("#nl-extracted");
    extracted.innerHTML =
      (res.ai_degraded
        ? '<div class="degraded-note">⚠ ' + esc(res.ai_note || "AI unavailable — offline extraction used.") + "</div>"
        : "") +
      "AI understood: <code>" + esc(JSON.stringify(res.extracted_request)) + "</code>";
    extracted.classList.remove("hidden");
    renderDecision(res.decision, res.explanation);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(btn, false);
  }
}

function renderDecision(decision, nlExplanation) {
  $("#result-empty").classList.add("hidden");
  $("#result-gated").classList.add("hidden");
  $("#result-body").classList.remove("hidden");

  const badge = $("#verdict-badge");
  badge.textContent = decision.decision;
  badge.className = "verdict " + outcomeClass(decision.decision);

  const pct = Math.round((decision.confidence || 0) * 100);
  $("#confidence-num").textContent = pct + "%";
  $("#confidence-fill").style.width = pct + "%";

  $("#result-explanation").textContent = nlExplanation
    ? nlExplanation + " — " + decision.explanation
    : decision.explanation;

  const scoreLine = $("#result-score");
  if (decision.score !== undefined && decision.score !== 0) {
    scoreLine.textContent = "Aggregate score: " + decision.score;
    scoreLine.classList.remove("hidden");
  } else {
    scoreLine.classList.add("hidden");
  }

  const traceById = {};
  (decision.trace || []).forEach((t) => { traceById[t.rule_id] = t; });

  $("#rules-fired").innerHTML =
    (decision.rules_matched || []).map((id) => ruleResultHtml(traceById[id], true)).join("") ||
    '<p class="muted" style="font-size:13px">None.</p>';
  $("#rules-rejected").innerHTML =
    (decision.rules_rejected || []).map((id) => ruleResultHtml(traceById[id], false)).join("") ||
    '<p class="muted" style="font-size:13px">None.</p>';

  attachTraceToggles();
}

function ruleResultHtml(trace, matched) {
  if (!trace) return "";
  const conditions = (trace.conditions || []).map((c) =>
    "<tr>" +
      "<td>" + esc(c.field) + "</td>" +
      "<td>" + esc(c.operator) + "</td>" +
      "<td>" + esc(JSON.stringify(c.expected)) + "</td>" +
      "<td>" + esc(JSON.stringify(c.actual)) + "</td>" +
      "<td>" + (c.passed ? '<span class="trace-pass">✓</span>' : '<span class="trace-fail">✗</span>') + "</td>" +
      '<td class="note">' + esc(c.note || "") + "</td>" +
    "</tr>"
  ).join("");
  return (
    '<div class="rule-result">' +
      '<button type="button" class="rule-result-head">' +
        '<span class="rr-mark ' + (matched ? "pass" : "fail") + '">' + (matched ? "✓" : "✗") + "</span>" +
        '<span class="rr-id">' + esc(trace.rule_id) + "</span>" +
        '<span class="badge ' + outcomeClass(trace.outcome) + '">' + esc(trace.outcome) + "</span>" +
        '<span class="rr-meta">prio ' + esc(trace.priority) + " · wt " + esc(trace.weight) + "</span>" +
      "</button>" +
      '<div class="rule-result-body hidden">' +
        '<table class="trace-table">' +
          "<thead><tr><th>Field</th><th>Op</th><th>Expected</th><th>Actual</th><th></th><th>Note</th></tr></thead>" +
          "<tbody>" + conditions + "</tbody>" +
        "</table>" +
      "</div>" +
    "</div>"
  );
}

/* Renders the response of POST /decide/gated: a final worst-wins verdict
   plus one collapsible card per rule category, each with its own badge,
   confidence, and full condition trace (reusing ruleResultHtml). */
function renderGatedDecision(result) {
  $("#result-empty").classList.add("hidden");
  $("#result-body").classList.add("hidden");
  $("#result-gated").classList.remove("hidden");

  const finalBadge = $("#gated-verdict-badge");
  finalBadge.textContent = result.final_decision;
  finalBadge.className = "verdict " + outcomeClass(result.final_decision);

  $("#gated-gates").innerHTML = (result.gates || []).map((gate) => {
    const d = gate.decision || {};
    const traceById = {};
    (d.trace || []).forEach((t) => { traceById[t.rule_id] = t; });
    const pct = Math.round((d.confidence || 0) * 100);
    const fired = (d.rules_matched || []).map((id) => ruleResultHtml(traceById[id], true)).join("")
      || '<p class="muted" style="font-size:13px">None.</p>';
    const rejected = (d.rules_rejected || []).map((id) => ruleResultHtml(traceById[id], false)).join("")
      || '<p class="muted" style="font-size:13px">None.</p>';
    return (
      '<div class="gate-card">' +
        '<button type="button" class="gate-head">' +
          '<span class="gate-category">' + esc(gate.category) + "</span>" +
          '<span class="badge ' + outcomeClass(d.decision) + '">' + esc(d.decision) + "</span>" +
          '<span class="gate-conf">' + pct + "% confidence</span>" +
        "</button>" +
        '<div class="gate-body hidden">' +
          '<p class="explanation" style="margin-top:8px">' + esc(d.explanation || "") + "</p>" +
          '<h3 class="section-label">Rules fired</h3>' + fired +
          '<h3 class="section-label">Rules that didn\'t match</h3>' + rejected +
        "</div>" +
      "</div>"
    );
  }).join("") || '<p class="muted">No rule categories configured.</p>';

  $$("#gated-gates .gate-head").forEach((head) =>
    head.addEventListener("click", () => head.nextElementSibling.classList.toggle("hidden")));
  attachTraceToggles();
}

function attachTraceToggles() {
  $$(".rule-result-head").forEach((head) => {
    head.addEventListener("click", () => {
      head.nextElementSibling.classList.toggle("hidden");
    });
  });
}

/* ----------------------------------------------------------------
   Wiring & init
----------------------------------------------------------------- */
function init() {
  // Nav shortcut buttons on dashboard
  $$("[data-goto]").forEach((el) =>
    el.addEventListener("click", () => {
      location.hash = "#" + el.dataset.goto;
      if (el.dataset.action === "new-rule") setTimeout(() => openRuleForm("create"), 60);
    }));
  $$("[data-action='new-rule']").forEach((el) => {
    if (!el.dataset.goto) el.addEventListener("click", () => openRuleForm("create"));
  });

  // Rules page — a single "Add rule" entry point; the panel's own tabs
  // choose between describing it in English and filling the fields in.
  $("#btn-new-rule").addEventListener("click", () => openRuleForm("create"));
  $$("#rule-mode-tabs .tab").forEach((tab) =>
    tab.addEventListener("click", () => setRuleMode(tab.dataset.ruleMode)));

  // Form
  $("#btn-close-form").addEventListener("click", closeRuleForm);
  $("#btn-cancel-form").addEventListener("click", closeRuleForm);
  $("#btn-save-rule").addEventListener("click", (e) => { e.preventDefault(); saveRule(); });
  $("#btn-add-condition").addEventListener("click", () => addConditionRow());
  $("#btn-generate-rule").addEventListener("click", generateRuleFromText);
  $("#rule-form").addEventListener("submit", (e) => { e.preventDefault(); saveRule(); });

  // Disclosure toggles (JSON preview, raw AI output)
  $$(".disclosure").forEach((d) =>
    d.addEventListener("click", () => {
      const target = $("#" + d.dataset.target);
      const isHidden = target.classList.toggle("hidden");
      d.textContent = (isHidden ? "▸ " : "▾ ") + d.textContent.slice(2);
      if (d.dataset.target === "json-preview" && !isHidden) refreshJsonPreview();
    }));

  // Confirm delete
  $("#btn-confirm-cancel").addEventListener("click", () => $("#confirm-overlay").classList.add("hidden"));
  $("#btn-confirm-delete").addEventListener("click", doDelete);

  // Playground tabs
  $$(".tab").forEach((tab) =>
    tab.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      $("#tab-structured").classList.toggle("hidden", tab.dataset.tab !== "structured");
      $("#tab-ai").classList.toggle("hidden", tab.dataset.tab !== "ai");
    }));
  $("#btn-add-field").addEventListener("click", () => addKvRow());
  $("#btn-run-decision").addEventListener("click", runDecision);
  $("#btn-run-gated").addEventListener("click", runGatedDecision);
  $("#btn-run-nl").addEventListener("click", runNlQuery);

  // Seed the playground with a friendly example — a clean approval profile
  // for the fintech gates (kyc / fraud / underwriting / affordability).
  // Tweak one field to flip the verdict: age 16 → REJECT (kyc),
  // fraud_score 85 → REVIEW (fraud), credit_score 550 → REJECT (underwriting),
  // dti 45 → REJECT (affordability).
  addKvRow("age", "34");
  addKvRow("identity_verified", "true");
  addKvRow("credit_score", "720");
  addKvRow("income", "80000");
  addKvRow("dti", "28");
  addKvRow("fraud_score", "12");
  addKvRow("on_watchlist", "false");

  // Escape closes overlays
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      // The duplicate dialog owns its own lifecycle (it resolves a promise),
      // so let its Cancel button handle dismissal rather than orphaning it.
      if (!$("#dup-overlay").classList.contains("hidden")) {
        $("#btn-dup-cancel").click();
        return;
      }
      closeRuleForm();
      $("#confirm-overlay").classList.add("hidden");
    }
  });

  checkHealth();
  setInterval(checkHealth, 30000);
  navigate();
}

document.addEventListener("DOMContentLoaded", init);
