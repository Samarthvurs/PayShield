// PayShield dashboard. Plain JavaScript, no build step.
// The API key is typed by the user and kept in localStorage; it is never in this file.

const CATEGORIES = [
  "entertainment", "food_dining", "gas_transport", "grocery_net", "grocery_pos",
  "health_fitness", "home", "kids_pets", "misc_net", "misc_pos", "personal_care",
  "shopping_net", "shopping_pos", "travel",
];
const REFRESH_MS = 5000;
const $ = (id) => document.getElementById(id);

let filter = "";
let chart = null;
let testCards = [];

// ---------- helpers ----------

function getKey() {
  try { return localStorage.getItem("payshield_api_key") || ""; } catch { return ""; }
}
function setKey(value) {
  try { localStorage.setItem("payshield_api_key", value); } catch { /* private mode */ }
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  let body = null;
  try { body = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const msg = body?.error || `Request failed (${res.status})`;
    const details = body?.details?.map((d) => `${d.field}: ${d.message}`).join("; ");
    throw new Error(details ? `${msg} — ${details}` : msg);
  }
  return body;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c !== null && c !== undefined) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const money = (n) => n.toLocaleString(undefined, { style: "currency", currency: "USD" });
const pct = (n) => `${(n * 100).toFixed(1)}%`;
const time = (iso) => new Date(iso).toLocaleTimeString();

function showResult(box, text, kind) {
  box.hidden = false;
  box.className = `result ${kind || ""}`;
  box.textContent = text;
}

// ---------- health + stats + chart ----------

async function loadHealth() {
  try {
    const h = await api("/health");
    $("dot-postgres").className = `dot ${h.postgres === "ok" ? "ok" : "bad"}`;
    $("dot-mongo").className = `dot ${h.mongo === "ok" ? "ok" : "bad"}`;
  } catch {
    $("dot-postgres").className = $("dot-mongo").className = "dot bad";
  }
}

async function loadStats() {
  const s = await api("/api/stats");
  $("stat-total").textContent = s.total_payments.toLocaleString();
  $("stat-approval").textContent = s.total_payments ? pct(s.approval_rate) : "–";
  $("stat-fraud").textContent = s.total_payments ? pct(s.fraud_decline_rate) : "–";
  $("stat-risk").textContent = s.avg_risk_score ?? "–";
  $("bucket-label").textContent = s.over_time.length ? `per ${s.bucket_size}` : "";
  $("chart-empty").hidden = s.over_time.length > 0;
  drawChart(s.over_time, s.bucket_size);
}

function drawChart(points, bucketSize) {
  const fmt = (iso) => {
    const d = new Date(iso);
    return bucketSize === "day" ? d.toLocaleDateString() : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  };
  const data = {
    labels: points.map((p) => fmt(p.bucket)),
    datasets: [
      { label: "Approved", data: points.map((p) => p.approved), backgroundColor: "#138a4b" },
      { label: "Declined", data: points.map((p) => p.declined), backgroundColor: "#c62828" },
    ],
  };
  if (typeof Chart === "undefined") return; // CDN blocked: stats and table still work
  if (chart) {
    chart.data = data;
    chart.update("none");
    return;
  }
  chart = new Chart($("chart"), {
    type: "bar",
    data,
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } },
      plugins: { legend: { position: "bottom" } },
    },
  });
}

// ---------- payments table ----------

async function loadPayments() {
  const params = new URLSearchParams({ page_size: "50" });
  if (filter) params.set("status", filter);
  const tbody = $("payments");
  try {
    const { items } = await api(`/api/payments?${params}`);
    $("table-error").hidden = true;
    tbody.replaceChildren(
      ...(items.length
        ? items.map(paymentRow)
        : [el("tr", {}, el("td", { colspan: "8", class: "empty" },
            filter ? "No declined payments yet." : "No payments yet. Send a test or demo payment."))])
    );
  } catch (err) {
    $("table-error").hidden = false;
    $("table-error").textContent = `Couldn't load payments: ${err.message}`;
  }
}

function paymentRow(p) {
  return el("tr", {},
    el("td", {}, time(p.created_at)),
    el("td", {}, el("span", { class: `pill ${p.status}` }, p.status), el("span", { class: "code" }, p.response_code)),
    el("td", {}, p.card_last4 ? `•••• ${p.card_last4}` : "unknown"),
    el("td", { class: "merchant", title: `${p.merchant_name} (${p.merchant_category})` }, p.merchant_name),
    el("td", { class: "num" }, money(p.amount)),
    el("td", { class: "num" }, p.risk_score ?? "–"),
    el("td", {}, p.rules_triggered.length ? p.rules_triggered.map((r) => el("span", { class: "rule" }, r)) : "–"),
    el("td", {}, p.status === "DECLINED"
      ? el("button", { type: "button", class: "why", onclick: () => explain(p.payment_id) }, "Why?")
      : ""),
  );
}

// ---------- explanation panel ----------

async function explain(paymentId) {
  const body = $("explain-body");
  $("explain-overlay").hidden = false;
  body.replaceChildren(el("p", { class: "muted" }, "Asking the AI explainer…"));
  try {
    const e = await api(`/api/payments/${paymentId}/explain`);
    body.replaceChildren(
      el("p", { class: "summary" }, e.summary),
      el("h3", {}, "Reasons"),
      el("ul", {}, e.reasons.map((r) => el("li", {}, r))),
      el("h3", {}, "Sources from the fraud-rules guide"),
      el("div", {}, e.sources.length ? e.sources.map((s) => el("span", { class: "source" }, s)) : "–"),
      el("p", { class: "hint" }, `Generated by: ${e.model}${e.cached ? " (cached)" : ""}`),
    );
  } catch (err) {
    body.replaceChildren(el("div", { class: "error" }, `Couldn't explain this payment: ${err.message}`));
  }
}

function closePanel() { $("explain-overlay").hidden = true; }

// ---------- test payment form + demo ----------

async function loadTestCards() {
  const select = $("card-select");
  try {
    testCards = await api("/api/dev/test-cards");
    select.replaceChildren(...testCards.map((c, i) =>
      el("option", { value: String(i) }, `${c.label} — •••• ${c.card_number.slice(-4)}`)));
    if (!testCards.length) throw new Error("none found");
  } catch (err) {
    select.replaceChildren(el("option", { value: "" }, "No test cards — run `make seed`"));
  }
}

async function sendPayment(event) {
  event.preventDefault();
  const box = $("pay-result");
  const card = testCards[Number($("card-select").value)];
  if (!card) return showResult(box, "Pick a test card first (run `make seed` if the list is empty).", "error");
  if (!getKey()) return showResult(box, "Enter the API key at the top of the page first.", "error");
  $("pay-btn").disabled = true;
  try {
    const r = await api("/api/payments", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": getKey(), "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({
        card_number: card.card_number,
        expiry_month: card.expiry_month,
        expiry_year: card.expiry_year,
        amount: Number($("amount").value),
        merchant_name: $("merchant").value,
        merchant_category: $("category").value,
        merchant_lat: Number($("lat").value),
        merchant_lon: Number($("lon").value),
      }),
    });
    showResult(box, `${r.status} — ${r.message} (code ${r.response_code})${r.risk_score !== null ? `, risk score ${r.risk_score}` : ""}`, r.status);
    refresh();
  } catch (err) {
    showResult(box, err.message, "error");
  } finally {
    $("pay-btn").disabled = false;
  }
}

async function sendDemo() {
  const box = $("demo-result");
  if (!getKey()) return showResult(box, "Enter the API key at the top of the page first.", "error");
  const btn = $("demo-btn");
  btn.disabled = true;
  showResult(box, "Sending demo payments… this takes a few seconds.");
  try {
    const r = await api("/api/demo?count=200", { method: "POST", headers: { "X-API-Key": getKey() } });
    const codes = Object.entries(r.by_code).map(([c, n]) => `${c}: ${n}`).join(", ");
    showResult(box, `Sent ${r.sent} payments (${codes}). ${r.dataset_fraud} were fraud in the dataset.`);
    refresh();
  } catch (err) {
    showResult(box, err.message, "error");
  } finally {
    btn.disabled = false;
  }
}

// ---------- start ----------

async function refresh() {
  await Promise.allSettled([loadStats(), loadPayments(), loadHealth()]);
}

function init() {
  $("category").replaceChildren(...CATEGORIES.map((c) => el("option", { value: c }, c)));
  $("category").value = "grocery_pos";
  $("api-key").value = getKey();
  $("api-key").addEventListener("input", (e) => setKey(e.target.value.trim()));
  $("pay-form").addEventListener("submit", sendPayment);
  $("demo-btn").addEventListener("click", sendDemo);
  $("explain-close").addEventListener("click", closePanel);
  $("explain-overlay").addEventListener("click", (e) => { if (e.target.id === "explain-overlay") closePanel(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });
  document.querySelectorAll(".chip").forEach((chip) => chip.addEventListener("click", () => {
    document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    filter = chip.dataset.filter;
    loadPayments();
  }));
  loadTestCards();
  refresh();
  setInterval(refresh, REFRESH_MS);
}

init();
