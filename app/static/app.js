"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const TYPE_LABEL = { local: "Landline (080, 022…)", mobile: "Mobile", tollfree: "Toll-free" };
const USER_TYPE = "business"; // India: only India-registered businesses can rent numbers
const money = (n) => `$${Number(n || 0).toFixed(2)}`;

const state = { me: null, apps: [], kycMode: null, resubmitApp: null, requirements: null, searchOffset: 0, lastSearch: null, pollTimer: null };

// ------------------------------------------------------------------ token handling
function storage(fn) { try { return fn(window.sessionStorage); } catch { return null; } }
function takeTokenFromUrl() {
  const params = new URLSearchParams(location.search);
  const t = params.get("token");
  if (t) {
    storage((s) => s.setItem("token", t));
    params.delete("token");
    history.replaceState(null, "", location.pathname + (params.toString() ? `?${params}` : ""));
  }
}
const getToken = () => storage((s) => s.getItem("token"));

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const detail = data && data.detail;
    const msg = Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : detail || `Request failed (${res.status})`;
    const err = new Error(msg); err.status = res.status; throw err;
  }
  return data;
}

function toast(msg) {
  const el = $("#toast"); el.textContent = msg; el.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.add("hidden"), 4000);
}
function showError(el, msg) { el.textContent = msg; el.classList.toggle("hidden", !msg); }

// ------------------------------------------------------------------ boot
async function boot() {
  takeTokenFromUrl();
  if (!getToken()) return showLogin();
  try {
    state.me = await api("/api/me");
  } catch (e) {
    if (e.status === 401) { storage((s) => s.removeItem("token")); return showLogin("Session expired. Please sign in again."); }
    throw e;
  }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#whoami").textContent = `${state.me.name} · ${state.me.client_id}`;
  $("#mockBanner").classList.toggle("hidden", !state.me.mock);
  const opts = state.me.number_types.map((t) => `<option value="${esc(t)}">${esc(TYPE_LABEL[t] || t)}</option>`).join("");
  $("#kycNumberType").innerHTML = opts;
  $("#searchType").innerHTML = opts;
  $("#citySelect").innerHTML = `<option value="">Any city</option>` +
    (state.me.cities || []).map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  await loadApps();
  switchTab(state.apps.some((a) => a.status === "accepted") ? "buy" : "kyc");
}

function showLogin(msg) {
  $("#login").classList.remove("hidden");
  if (msg) toast(msg);
}

$("#loginForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  try {
    const { token } = await api(`/api/dev/token?client_id=${encodeURIComponent(f.get("client_id"))}&name=${encodeURIComponent(f.get("name") || "")}`);
    storage((s) => s.setItem("token", token));
    boot();
  } catch (e) { toast(e.status === 404 ? "Dev sign-in is disabled. Open this page from your dashboard." : e.message); }
});

// ------------------------------------------------------------------ tabs
function switchTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${name}`));
  if (name === "buy") renderBuyGate();
  if (name === "mine") loadMyNumbers();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

// ------------------------------------------------------------------ KYC list
async function loadApps() {
  state.apps = await api("/api/compliance/applications");
  renderApps();
  clearTimeout(state.pollTimer);
  if (state.apps.some((a) => a.status === "submitted")) state.pollTimer = setTimeout(loadApps, 10000);
}

function statusText(a) {
  switch (a.status) {
    case "submitted": return "Under review by the carrier. This page refreshes automatically.";
    case "accepted": return "Approved. You can buy numbers of this type.";
    case "rejected": return `Rejected: ${a.rejection_reason || "no reason given"}`;
    case "suspended": return "Suspended because of unresolved spam (UCC) complaints. Calls from your numbers are blocked until consent proof is accepted. Contact support.";
    case "expired": return "Expired. Please start a new KYC application.";
    default: return "";
  }
}

function renderApps() {
  const list = $("#appsList");
  if (!state.apps.length) { list.innerHTML = `<p class="notice">No KYC submitted yet. Click <b>Start new KYC</b> to begin.</p>`; return; }
  list.innerHTML = state.apps.map((a) => `
    <div class="app-item">
      <div class="row between">
        <div><b>${esc(TYPE_LABEL[a.number_type] || a.number_type)} numbers</b> · ${esc(a.user_type)} · ${esc(a.end_user?.name)}
          <span class="muted">#${a.id} · ${new Date(a.created_at).toLocaleString()}</span></div>
        <span class="badge ${esc(a.status)}">${esc(a.status)}</span>
      </div>
      <div class="muted">${esc(statusText(a))}</div>
      ${a.status === "rejected" ? `<button data-resubmit="${a.id}" style="margin-top:8px">Fix &amp; resubmit</button>` : ""}
      ${a.status === "accepted" ? `<button data-gobuy="${esc(a.number_type)}" class="primary" style="margin-top:8px">Buy ${esc(TYPE_LABEL[a.number_type] || a.number_type)} numbers</button>` : ""}
    </div>`).join("");
  list.querySelectorAll("[data-resubmit]").forEach((b) => b.addEventListener("click", () => openKycForm(state.apps.find((a) => a.id === +b.dataset.resubmit))));
  list.querySelectorAll("[data-gobuy]").forEach((b) => b.addEventListener("click", () => { $("#searchType").value = b.dataset.gobuy; switchTab("buy"); }));
}

// ------------------------------------------------------------------ KYC form
$("#newKycBtn").addEventListener("click", () => openKycForm(null));
$("#kycCancel").addEventListener("click", () => $("#kycFormCard").classList.add("hidden"));
$("#kycNumberType").addEventListener("change", loadRequirements);
$("#docTypeSelect").addEventListener("change", renderDocFields);

async function openKycForm(app) {
  state.resubmitApp = app;
  $("#kycFormTitle").textContent = app ? `Correct & resubmit application #${app.id}` : "New KYC application";
  $("#kycSubmit").textContent = app ? "Resubmit for verification" : "Submit for verification";
  $("#kycTypeRow").classList.toggle("hidden", !!app);
  if (app) $("#kycNumberType").value = app.number_type;
  $("#sealConfirm").checked = false;
  showError($("#kycError"), app ? `Rejection reason: ${app.rejection_reason || "not provided"}` : "");
  $("#kycFormCard").classList.remove("hidden");
  await loadRequirements();
  $("#kycFormCard").scrollIntoView({ behavior: "smooth" });
}

function endUserFieldDefs() {
  return [
    { k: "name", label: "Registered business name", hint: "Exactly as on the certificate", req: true },
    { k: "registration_number", label: "Registration number", hint: "GSTIN, CIN or Udyam number, matching the document you upload", req: true },
    { k: "email", label: "Email", type: "email", req: true },
    { k: "address_line1", label: "Address line 1", req: true },
    { k: "address_line2", label: "Address line 2" },
    { k: "city", label: "City", req: true },
    { k: "state", label: "State", req: true },
    { k: "postal_code", label: "PIN code", req: true, pattern: "\\d{6}" },
  ];
}

async function loadRequirements() {
  const numberType = $("#kycNumberType").value;
  const prev = state.resubmitApp?.end_user || {};
  $("#endUserFields").innerHTML = endUserFieldDefs().map((f) => `
    <label>${esc(f.label)} ${f.req ? '<span class="req">*</span>' : ""}${f.hint ? `<span class="hint">${esc(f.hint)}</span>` : ""}
      <input data-eu="${f.k}" type="${f.type || "text"}" ${f.req ? "required" : ""} ${f.pattern ? `pattern="${f.pattern}"` : ""} value="${esc(prev[f.k] || "")}">
    </label>`).join("");
  $("#docFields").innerHTML = `<p class="muted">Loading document requirements…</p>`;
  try {
    state.requirements = await api(`/api/compliance/requirements?number_type=${encodeURIComponent(numberType)}&user_type=${USER_TYPE}`);
  } catch (e) { $("#docFields").innerHTML = ""; return showError($("#kycError"), e.message); }
  const types = state.requirements.document_types;
  const one = state.me.kyc_documents_mode === "one";
  const names = types.map((d) => d.name);
  $("#docIntro").innerHTML = one
    ? `Upload <b>one</b> of: ${esc(names.join(", "))}.`
    : `Upload <b>all</b> of: ${esc(names.join(" and "))}.`;
  $("#docTypeWrap").classList.toggle("hidden", !one);
  $("#oneFileWrap").classList.toggle("hidden", one || types.length < 2);
  $("#oneFileToggle").checked = false;
  const prevType = state.resubmitApp?.documents?.[0]?.document_type_id;
  $("#docTypeSelect").innerHTML = types.map((dt) =>
    `<option value="${esc(dt.document_type_id)}" ${dt.document_type_id === prevType ? "selected" : ""}>${esc(dt.name)}</option>`).join("");
  renderDocFields();
}

// Document types the client is submitting: all of them, or the one picked in "one" mode.
function activeDocTypes() {
  const types = state.requirements?.document_types || [];
  if (state.me.kyc_documents_mode !== "one") return types;
  return types.filter((dt) => dt.document_type_id === $("#docTypeSelect").value);
}

// Plivo requires business_name on every document to match the registered business name exactly.
function syncBusinessName() {
  const name = $('[data-eu="name"]')?.value.trim() || "";
  document.querySelectorAll('[data-df$=":business_name"]').forEach((i) => { i.value = name; });
}
$("#endUserFields").addEventListener("input", (e) => { if (e.target.dataset.eu === "name") syncBusinessName(); });

function renderDocFields() {
  const prevDocs = Object.fromEntries((state.resubmitApp?.documents || []).map((d) => [d.document_type_id, d.data_fields || {}]));
  $("#docFields").innerHTML = activeDocTypes().map((dt) => {
    const pv = prevDocs[dt.document_type_id] || {};
    const fields = (dt.required_fields || []).map((f) => {
      const id = `${dt.document_type_id}:${f.field_name}`;
      const req = f.required ? "required" : "";
      let input;
      if (f.field_type === "enum") {
        const opts = (Array.isArray(f.enums) ? f.enums : String(f.enums || "").split(",")).map((o) => o.trim()).filter(Boolean);
        input = `<select data-df="${esc(id)}" ${req}><option value="">Select…</option>${opts.map((o) => `<option ${pv[f.field_name] === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
      } else if (f.field_name === "business_name") {
        input = `<input data-df="${esc(id)}" type="text" readonly title="Filled from the registered business name">`;
      } else {
        input = `<input data-df="${esc(id)}" type="${f.field_type === "date" ? "date" : "text"}" ${req}
          ${f.max_length ? `maxlength="${+f.max_length}"` : ""} ${f.min_length ? `minlength="${+f.min_length}"` : ""} value="${esc(pv[f.field_name] || "")}">`;
      }
      return `<label>${esc(f.friendly_name || f.field_name)} ${f.required ? '<span class="req">*</span>' : ""}${f.help_text ? `<span class="hint">${esc(f.help_text)}</span>` : ""}${input}</label>`;
    }).join("");
    return `<h3 style="margin-top:14px">${esc(dt.name)}</h3><p class="muted" style="margin-top:0">${esc(dt.description || "")}</p>
      <div class="grid2">${fields}
        ${dt.proof_required ? `<label>Upload ${esc(dt.name)} <span class="req">*</span><span class="hint">PDF, JPG or PNG · max 5 MB · clear, complete, not cropped</span>
          <input type="file" data-file="${esc(dt.document_type_id)}" data-name="${esc(dt.name)}" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png" required></label>` : ""}
      </div>`;
  }).join("");
  syncBusinessName();
  applyOneFileMode();
}

// Plivo needs every document slot filled, but accepts the same file in each (Plivo support, Nov 2026).
const oneFileMode = () => !$("#oneFileWrap").classList.contains("hidden") && $("#oneFileToggle").checked;
function applyOneFileMode() {
  document.querySelectorAll("[data-file]").forEach((input, i) => {
    const label = input.closest("label");
    const extra = oneFileMode() && i > 0;
    label.classList.toggle("hidden", extra);
    input.required = !extra;
    if (i === 0) label.firstChild.textContent = oneFileMode() ? "Upload your document (GST, COI or Udyam) " : `Upload ${input.dataset.name} `;
  });
}
$("#oneFileToggle").addEventListener("change", applyOneFileMode);

$("#kycForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const errEl = $("#kycError"); showError(errEl, "");
  const endUser = {};
  document.querySelectorAll("[data-eu]").forEach((i) => { if (i.value.trim()) endUser[i.dataset.eu] = i.value.trim(); });
  const fd = new FormData();
  syncBusinessName();
  const documents = activeDocTypes().map((dt) => {
    const data_fields = {};
    document.querySelectorAll("[data-df]").forEach((i) => {
      const [dtId, field] = i.dataset.df.split(/:(.+)/);
      if (dtId === dt.document_type_id && i.value) data_fields[field] = i.value;
    });
    return { document_type_id: dt.document_type_id, data_fields };
  });
  const sameFile = oneFileMode();
  for (const input of document.querySelectorAll("[data-file]")) {
    const file = input.files[0];
    if (!file || (sameFile && input.closest("label").classList.contains("hidden"))) continue;
    if (file.size > 5 * 1024 * 1024) return showError(errEl, `${file.name} is larger than 5 MB`);
    fd.append(`file_${input.dataset.file}`, file);
  }
  fd.append("payload", JSON.stringify({ number_type: $("#kycNumberType").value, user_type: USER_TYPE, end_user: endUser,
                                       documents, same_file_for_all: sameFile }));
  const btn = $("#kycSubmit"); btn.disabled = true;
  try {
    const url = state.resubmitApp ? `/api/compliance/applications/${state.resubmitApp.id}/resubmit` : "/api/compliance/applications";
    await api(url, { method: "POST", body: fd });
    $("#kycFormCard").classList.add("hidden");
    ev.target.reset();
    toast("KYC submitted for verification");
    await loadApps();
  } catch (e) { showError(errEl, e.message); }
  finally { btn.disabled = false; }
});

// ------------------------------------------------------------------ Buy
function renderBuyGate() {
  const type = $("#searchType").value;
  const ok = state.apps.some((a) => a.status === "accepted" && a.number_type === type);
  const pending = state.apps.find((a) => a.status === "submitted" && a.number_type === type);
  const gate = $("#buyGate");
  gate.classList.toggle("hidden", ok);
  gate.innerHTML = ok ? "" : pending
    ? `Your KYC for ${esc(TYPE_LABEL[type] || type)} numbers is under review. You can buy numbers once it is approved.`
    : `You need an approved KYC for ${esc(TYPE_LABEL[type] || type)} numbers first. <a href="#" id="gotoKyc">Start KYC</a>`;
  $("#gotoKyc")?.addEventListener("click", (e) => { e.preventDefault(); switchTab("kyc"); openKycForm(null); });
  $("#searchForm button").disabled = !ok;
  $("#cityWrap").classList.toggle("hidden", type !== "local");
}
$("#searchType").addEventListener("change", () => { renderBuyGate(); $("#searchResults").innerHTML = ""; });

$("#searchForm").addEventListener("submit", (ev) => { ev.preventDefault(); state.searchOffset = 0; runSearch(false); });

async function runSearch(append) {
  const f = new FormData($("#searchForm"));
  const q = new URLSearchParams({ number_type: f.get("number_type"), offset: state.searchOffset });
  if (f.get("pattern")) q.set("pattern", f.get("pattern"));
  if (f.get("city") && f.get("number_type") === "local") q.set("city", f.get("city"));
  showError($("#searchError"), "");
  const box = $("#searchResults");
  if (!append) box.innerHTML = `<p class="muted">Searching…</p>`;
  try {
    const res = await api(`/api/numbers/search?${q}`);
    const rows = res.numbers.map((n) => `<tr>
        <td class="mono">+${esc(n.number)}</td><td>${esc(n.city || n.region || "—")}</td>
        <td>${money(n.setup_price)}</td><td>${money(n.monthly_price)}/mo</td>
        <td><button class="primary" data-buy="${esc(n.number)}" data-price="${n.monthly_price}" data-setup="${n.setup_price}">Buy</button></td></tr>`).join("");
    if (!append) {
      box.innerHTML = res.numbers.length ? `<div class="table-wrap"><table><thead><tr><th>Number</th><th>Location</th><th>Setup</th><th>Monthly</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
        <button id="moreBtn" class="${res.has_more ? "" : "hidden"}" style="margin-top:10px">Load more</button>`
        : `<p class="notice">No numbers found. Try another city or fewer starting digits.</p>`;
    } else {
      box.querySelector("tbody").insertAdjacentHTML("beforeend", rows);
      $("#moreBtn").classList.toggle("hidden", !res.has_more);
    }
    $("#moreBtn")?.addEventListener("click", () => { state.searchOffset += 20; runSearch(true); }, { once: true });
    box.querySelectorAll("[data-buy]:not([data-bound])").forEach((b) => { b.dataset.bound = 1; b.addEventListener("click", () => buyNumber(b)); });
  } catch (e) { box.innerHTML = ""; showError($("#searchError"), e.message); }
}

async function buyNumber(btn) {
  const n = btn.dataset.buy;
  if (!confirm(`Buy +${n}?\n\nSetup: ${money(btn.dataset.setup)}\nMonthly rental: ${money(btn.dataset.price)}\n\nThe number will be registered under your KYC.`)) return;
  btn.disabled = true; btn.textContent = "Buying…";
  try {
    await api("/api/numbers", { method: "POST", json: { number: n } });
    btn.closest("tr").remove();
    toast(`+${n} is now yours`);
  } catch (e) { btn.disabled = false; btn.textContent = "Buy"; showError($("#searchError"), e.message); }
}

// ------------------------------------------------------------------ My numbers
$("#showReleased").addEventListener("change", loadMyNumbers);

async function loadMyNumbers() {
  const box = $("#myNumbers");
  try {
    const list = await api(`/api/numbers?include_released=${$("#showReleased").checked}`);
    if (!list.length) { box.innerHTML = `<p class="notice">You don't have any numbers yet. Go to <b>Buy numbers</b>.</p>`; return; }
    box.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Number</th><th>Type</th><th>Location</th><th>Label</th><th>Monthly</th><th>Status</th><th></th></tr></thead><tbody>
      ${list.map((n) => `<tr>
        <td class="mono">${esc(n.e164)}</td><td>${esc(TYPE_LABEL[n.number_type] || n.number_type)}</td><td>${esc(n.city || n.region || "—")}</td>
        <td>${n.status !== "released" ? `<input data-alias="${n.id}" value="${esc(n.alias || "")}" placeholder="Add a label" maxlength="99">` : esc(n.alias || "")}</td>
        <td>${money(n.monthly_price)}</td><td><span class="badge ${esc(n.status)}" title="${n.status === "pending_activation" ? "Bought; the carrier is still activating it. It can't be used for calls yet." : ""}">${esc(n.status === "pending_activation" ? "activating" : n.status)}</span></td>
        <td>${n.status !== "released" ? `<button class="danger" data-release="${n.id}" data-num="${esc(n.e164)}">Release</button>` : ""}</td></tr>`).join("")}
      </tbody></table></div>`;
    box.querySelectorAll("[data-alias]").forEach((i) => i.addEventListener("change", async () => {
      try { await api(`/api/numbers/${i.dataset.alias}`, { method: "PATCH", json: { alias: i.value } }); toast("Label saved"); }
      catch (e) { toast(e.message); }
    }));
    box.querySelectorAll("[data-release]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm(`Release ${b.dataset.num}?\n\nThe number goes back to the carrier and cannot be recovered. Calls can no longer be placed from it.`)) return;
      b.disabled = true;
      try { await api(`/api/numbers/${b.dataset.release}`, { method: "DELETE" }); toast(`${b.dataset.num} released`); loadMyNumbers(); }
      catch (e) { b.disabled = false; toast(e.message); }
    }));
  } catch (e) { box.innerHTML = ""; toast(e.message); }
}

boot().catch((e) => toast(e.message));
