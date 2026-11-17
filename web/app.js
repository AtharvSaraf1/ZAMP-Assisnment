/* Ledgerline UI: plain JS, no build step. Routes: #/process, #/run/:id, #/runs, #/reference */

const view = document.getElementById("view");
let META = null;
let liveSource = null;

const DECISION = {
  approved: { word: "Approved", stamp: "APPROVED" },
  review: { word: "Needs review", stamp: "ON HOLD" },
  rejected: { word: "Rejected", stamp: "REJECTED" },
};
const ROUTE = { vendor: "Ask the vendor", po_owner: "PO owner", ap_lead: "AP lead", onboarding: "Vendor onboarding" };

// ---------- helpers ----------
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => (v == null ? "-" : new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(v));
const when = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "-");
const plain = (text) => esc(text).replace(/ Rs /g, " ₹").replace(/^Rs /g, "₹").replace(/\(Rs /g, "(₹");

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 2600);
}

function finalState(run) {
  if (run.status === "running") return { key: "running", word: "Running" };
  if (run.status === "error") return { key: "error", word: "Process error" };
  if (run.reviewer_action === "approve") return { key: "approved", word: "Approved", by: "by reviewer" };
  if (run.reviewer_action === "reject") return { key: "rejected", word: "Rejected", by: "by reviewer" };
  return { key: run.decision, word: DECISION[run.decision]?.word || run.decision };
}

async function refreshQueueCount() {
  try {
    const runs = await api("/api/runs");
    const waiting = runs.filter((r) => r.decision === "review" && !r.reviewer_action).length;
    const badge = document.getElementById("queue-count");
    badge.textContent = waiting;
    badge.hidden = waiting === 0;
    badge.title = `${waiting} waiting for review`;
  } catch (_) { /* badge is optional */ }
}

// ---------- router ----------
async function route() {
  if (liveSource) { liveSource.close(); liveSource = null; }
  const [, page, id] = (location.hash || "#/process").split("/");
  document.querySelectorAll(".tabs a").forEach((a) => {
    const active = a.dataset.tab === (page === "run" ? "runs" : page);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  window.scrollTo(0, 0);
  try {
    if (page === "run" && id) await showRun(Number(id));
    else if (page === "runs") await showRuns();
    else if (page === "reference") await showReference();
    else await showProcess();
  } catch (error) {
    view.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }
  refreshQueueCount();
}

// ---------- process page ----------
async function showProcess() {
  const aiOn = META.ai_configured;
  const modes = Object.entries(META.modes).map(([key, label]) =>
    `<option value="${key}" ${!aiOn && key !== "local" ? "disabled" : ""} ${(!aiOn && key === "local") ? "selected" : ""}>${esc(label)}</option>`).join("");
  view.innerHTML = `
    <h1>Process an invoice</h1>
    <p class="lede">Pick a prepared invoice or drop in your own PDF. Each stage shows up as it runs, and the decision lands on the document with every reason behind it.</p>
    <div class="process">
      <form class="picker" id="run-form">
        <div class="picker-head"><h2>Invoice</h2></div>
        <ul class="samples">
          ${META.samples.map((s, i) => `
            <li class="sample"><label>
              <input type="radio" name="input" value="${esc(s.file)}" ${i === 0 ? "checked" : ""}>
              <div><span class="tag">${esc(s.tag)}</span><div class="title">${esc(s.title)}</div></div>
              <span class="about">${esc(s.about)}</span>
            </label></li>`).join("")}
        </ul>
        <label class="drop">
          <input type="radio" name="input" value="__upload">
          <div class="dropzone" id="dropzone">
            <strong id="drop-label">Drop a PDF here</strong> or click to choose a file
          </div>
        </label>
        <input type="file" id="file" accept="application/pdf,.pdf" hidden>
        <div class="run-controls">
          <label>Reader <select id="mode">${modes}</select></label>
          <button class="primary" id="go" type="submit">Process invoice</button>
        </div>
        ${aiOn ? "" : `<p class="small muted" style="padding:0 22px 18px;margin:0">No AI key is configured, so only the local parser is available. It cannot read scanned invoices.</p>`}
      </form>
      <aside class="preview" aria-live="polite">
        <figure><img id="preview-img" alt="First page of the selected invoice"><figcaption id="preview-cap"></figcaption></figure>
      </aside>
    </div>
    <section class="rules">
      <h2>Rules the process applies</h2>
      <dl>${META.policy.map((p) => `<dt>${esc(p.rule)}</dt><dd>${esc(p.text)}</dd>`).join("")}</dl>
    </section>`;

  const form = document.getElementById("run-form");
  const fileInput = document.getElementById("file");
  const zone = document.getElementById("dropzone");
  const img = document.getElementById("preview-img");
  const cap = document.getElementById("preview-cap");
  let chosenFile = null;

  const updatePreview = () => {
    const value = form.input.value;
    if (value === "__upload") {
      img.hidden = true;
      cap.textContent = chosenFile ? `${chosenFile.name} is ready. The preview appears once the run starts.` : "Choose a PDF to upload.";
    } else {
      img.hidden = false;
      img.src = `/api/samples/${encodeURIComponent(value)}/page.png`;
      cap.textContent = value;
    }
  };
  const takeFile = (file) => {
    if (!file) return;
    chosenFile = file;
    document.getElementById("drop-label").textContent = file.name;
    form.querySelector('input[value="__upload"]').checked = true;
    updatePreview();
  };
  form.addEventListener("change", (e) => { if (e.target.name === "input") updatePreview(); });
  zone.addEventListener("click", (e) => { e.preventDefault(); fileInput.click(); });
  fileInput.addEventListener("change", () => takeFile(fileInput.files[0]));
  ["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
  zone.addEventListener("drop", (e) => takeFile(e.dataTransfer.files[0]));
  updatePreview();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = new FormData();
    body.append("mode", document.getElementById("mode").value);
    if (form.input.value === "__upload") {
      if (!chosenFile) { toast("Choose a PDF first."); return; }
      body.append("file", chosenFile);
    } else {
      body.append("sample", form.input.value);
    }
    const button = document.getElementById("go");
    button.disabled = true;
    button.textContent = "Starting...";
    try {
      const { id } = await api("/api/runs", { method: "POST", body });
      location.hash = `#/run/${id}`;
    } catch (error) {
      toast(error.message);
      button.disabled = false;
      button.textContent = "Process invoice";
    }
  });
}

// ---------- run page (live and past) ----------
async function showRun(id) {
  const run = await api(`/api/runs/${id}`);
  view.innerHTML = `
    <div class="run-head">
      <h1>Run ${run.id}</h1>
      <span class="file">${esc(run.file_name)}</span>
      <span class="muted small">Reader: ${esc(META.modes[run.extraction_mode] || run.extraction_mode)}. Received ${esc(when(run.created_at))}.</span>
    </div>
    <div class="run-grid">
      <div>
        <ol class="track" id="track">
          ${META.stages.map((s) => `
            <li class="stage waiting" data-stage="${s.key}">
              <span class="mark" aria-hidden="true"></span>
              <span class="name">${esc(s.label)}</span>
              <span class="msg">Waiting</span>
              <span class="ms"></span>
            </li>`).join("")}
        </ol>
        <div id="verdict"></div>
        <div id="after"></div>
      </div>
      <aside class="doc">
        <div class="frame" id="frame"><img src="/api/runs/${run.id}/page.png" alt="First page of the invoice"></div>
        <p class="scan-note" id="scan-note"></p>
      </aside>
    </div>`;

  if (run.status === "running") {
    document.getElementById("verdict").innerHTML = `<div class="verdict"><p class="headline">Working through the stages...</p></div>`;
    liveSource = new EventSource(`/api/runs/${id}/events`);
    liveSource.onmessage = (msg) => applyEvent(JSON.parse(msg.data));
    liveSource.addEventListener("done", async () => {
      liveSource.close(); liveSource = null;
      renderResult(await api(`/api/runs/${id}`), true);
      refreshQueueCount();
    });
    liveSource.onerror = () => {   // connection dropped: fall back to polling
      if (!liveSource) return;
      liveSource.close(); liveSource = null;
      pollUntilDone(id);
    };
  } else {
    run.events.forEach(applyEvent);
    renderResult(run, false);
  }
}

async function pollUntilDone(id) {
  const run = await api(`/api/runs/${id}`);
  run.events.forEach(applyEvent);
  if (run.status === "running") setTimeout(() => location.hash === `#/run/${id}` && pollUntilDone(id), 1000);
  else renderResult(run, true);
}

function applyEvent(event) {
  const row = document.querySelector(`.stage[data-stage="${event.stage}"]`);
  if (!row) return;
  row.className = `stage ${event.state}`;
  row.querySelector(".msg").innerHTML = plain(event.message || "");
  row.querySelector(".ms").textContent = event.duration_ms != null ? `${(event.duration_ms / 1000).toFixed(event.duration_ms < 1000 ? 2 : 1)} s` : "";
  if (event.stage === "intake" && event.data && event.data.scanned) {
    document.getElementById("scan-note").textContent = "Scanned image with no text layer. Only the AI reader can read it.";
  }
}

function renderResult(run, animate) {
  const verdict = document.getElementById("verdict");
  const after = document.getElementById("after");
  if (run.status === "error") {
    verdict.innerHTML = `<div class="verdict rejected"><p class="headline">The process stopped with an error</p><p class="next">${esc(run.error)}</p></div>`;
    return;
  }
  const result = run.result;
  const state = finalState(run);
  const [first, ...rest] = (run.headline || "").split(" - ");
  verdict.innerHTML = `
    <div class="verdict ${esc(result.decision)}">
      <p class="headline">${esc(DECISION[result.decision].word)}${rest.length ? `: ${plain(rest.join(" - "))}` : ""}</p>
      <p class="next">${plain(result.next_step)}</p>
    </div>`;

  // stamp on the document
  const frame = document.getElementById("frame");
  frame.querySelector(".stamp")?.remove();
  const stamp = document.createElement("div");
  const stampKey = state.by ? state.key : result.decision;
  stamp.className = `stamp ${stampKey}${animate ? " land" : ""}`;
  const day = new Date(run.reviewed_at || run.finished_at || Date.now()).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
  stamp.innerHTML = `${DECISION[stampKey].stamp}<small>${state.by ? "Reviewer" : "AP desk"}, ${esc(day)}</small>`;
  frame.appendChild(stamp);

  const checks = result.checks || [];
  const order = (c) => (c.severity === "reject" ? 0 : c.severity === "review" ? 1 : c.status === "warn" ? 2 : 3);
  const raised = checks.filter((c) => c.status !== "pass").sort((a, b) => order(a) - order(b));
  const passed = checks.filter((c) => c.status === "pass");
  const finding = (c) => `
    <li class="finding ${c.status} ${c.severity || ""}">
      <span class="what">${esc(c.label)}</span>
      <span class="sev">${c.severity === "reject" ? "Blocks payment" : c.severity === "review" ? "Needs a person" : c.status === "warn" ? "Note" : ""}</span>
      <div class="why">${plain(c.detail)}</div>
      ${c.status === "fail" && c.route ? `<div class="who">Next: ${esc(ROUTE[c.route] || c.route)}</div>` : ""}
    </li>`;

  const extractEvent = (run.events || []).find((e) => e.stage === "extract" && e.state !== "running");
  after.innerHTML = `
    ${reviewBlock(run)}
    <section class="section">
      <h2>What the process found</h2>
      ${raised.length ? `<ul class="findings">${raised.map(finding).join("")}</ul>` : `<p class="muted">Nothing needs attention.</p>`}
      <details class="passed"><summary>${passed.length} checks passed</summary><ul class="findings">${passed.map(finding).join("")}</ul></details>
    </section>
    ${linesBlock(result)}
    ${messageBlock(result)}
    ${fieldsBlock(run.extracted, extractEvent)}`;

  const copy = document.getElementById("copy-mail");
  if (copy) copy.addEventListener("click", async () => {
    const m = result.vendor_message;
    try { await navigator.clipboard.writeText(`To: ${m.to}\nSubject: ${m.subject}\n\n${m.body}`); toast("Email copied"); }
    catch (_) { toast("Copy is blocked in this browser. Select the text instead."); }
  });
  const form = document.getElementById("review-form");
  if (form) form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const action = e.submitter.value;
    try {
      await api(`/api/runs/${run.id}/review`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, note: document.getElementById("review-note").value }) });
      toast(action === "approve" ? "Invoice approved and released" : "Invoice rejected");
      const fresh = await api(`/api/runs/${run.id}`);
      renderResult(fresh, true);
      refreshQueueCount();
    } catch (error) { toast(error.message); }
  });
}

function reviewBlock(run) {
  if (run.reviewer_action) {
    return `<div class="section reviewed ${run.reviewer_action}">
      <h3>${run.reviewer_action === "approve" ? "Approved by reviewer" : "Rejected by reviewer"} on ${esc(when(run.reviewed_at))}</h3>
      <div>${esc(run.reviewer_note || "No note left.")}</div></div>`;
  }
  if (run.decision !== "review") return "";
  const people = [...new Set((run.result.actions || []).map((a) => a.who))].join(", ");
  return `<form class="section review-box" id="review-form">
      <h3>Your decision</h3>
      <p class="small" style="margin:0 0 10px">Routed to ${esc(people)}. Approving releases the invoice for payment and counts it against the PO.</p>
      <label class="small" for="review-note">Note for the audit trail</label>
      <textarea id="review-note" placeholder="For example: confirmed with the PO owner by phone"></textarea>
      <div class="row">
        <button class="okay" type="submit" value="approve">Approve and release</button>
        <button class="danger" type="submit" value="reject">Reject invoice</button>
      </div>
    </form>`;
}

function linesBlock(result) {
  const rows = result.lines || [];
  if (!rows.length) return "";
  const after = result.po_after;
  let meter = "";
  if (after) {
    const base = Math.max(after.po_value, after.after);
    const pct = (v) => `${Math.max(0, (v / base) * 100).toFixed(2)}%`;
    const over = Math.max(0, after.after - after.po_value);
    meter = `
      <p class="small" style="margin:14px 0 6px">${esc(result.po_number)}${result.po_method === "inferred" ? " (suggested)" : ""}: ${money(after.approved_before)} approved before, ${money(after.this_invoice)} on this invoice, ${money(after.po_value)} ordered.${over > 0 ? ` <b style="color:var(--stop)">${money(over)} over.</b>` : ""}</p>
      <div class="meter" style="max-width:560px" role="img" aria-label="PO usage after this invoice: ${after.used_pct}%">
        <span class="a" style="width:${pct(after.approved_before)}"></span>
        <span class="p" style="width:${pct(after.this_invoice - over)}"></span>
        <span class="over" style="width:${pct(over)}"></span>
      </div>`;
  }
  return `<section class="section"><h2>Invoice lines against the PO</h2>
    <div class="scroll"><table>
      <thead><tr><th>Invoice line</th><th class="num">Qty</th><th class="num">Unit price</th><th>PO line</th><th class="num">PO price</th><th class="num">Billed before / ordered</th><th>Result</th></tr></thead>
      <tbody>${rows.map((r) => `
        <tr class="${r.status}">
          <td>${esc(r.description)}</td>
          <td class="num">${r.quantity ?? "-"}</td>
          <td class="num">${money(r.unit_price_ex_tax ?? r.unit_price)}${r.unit_price_ex_tax != null && r.unit_price_ex_tax !== r.unit_price ? `<div class="small muted">${money(r.unit_price)} incl. GST</div>` : ""}</td>
          <td>${r.po_line_no ? `${r.po_line_no}. ${esc(r.po_description)}` : "-"}</td>
          <td class="num">${money(r.po_unit_price)}</td>
          <td class="num">${r.po_line_no ? `${r.billed_before} / ${r.po_quantity}` : "-"}</td>
          <td class="note">${esc(r.note)}</td>
        </tr>`).join("")}</tbody>
    </table></div>${meter}</section>`;
}

function messageBlock(result) {
  const m = result.vendor_message;
  if (!m) return "";
  return `<section class="section"><h2>Draft to the vendor</h2>
    <div class="letter">
      <div class="meta">To ${esc(m.to)}<br>Subject: ${esc(m.subject)}</div>
      <pre>${plain(m.body)}</pre>
      <div class="row"><span class="small muted">${esc(m.note || "Draft only. Nothing is sent until a person sends it.")}</span>
      <button class="ghost" type="button" id="copy-mail">Copy email</button></div>
    </div></section>`;
}

function fieldsBlock(ext, extractEvent) {
  if (!ext) return "";
  const d = extractEvent?.data || {};
  const reader = d.method === "ai" ? `Read by ${esc(d.model)}${d.cached ? " (cached result for this exact file)" : ""}.`
    : d.method === "local_parser" ? "Read by the local parser." : "";
  const taxes = (ext.tax_lines || []).map((t) => `${t.tax_type}${t.rate_percent ? ` ${t.rate_percent}%` : ""}: ${money(t.amount)}`).join(", ");
  const items = [
    ["Vendor", ext.vendor_name], ["Vendor GSTIN", ext.vendor_gstin], ["Billed to GSTIN", ext.buyer_gstin],
    ["Invoice number", ext.invoice_number], ["Invoice date", ext.invoice_date], ["Due date", ext.due_date],
    ["PO reference", ext.po_reference], ["Prices include GST", ext.tax_inclusive ? "Yes" : "No"],
    ["Sub-total", ext.subtotal != null ? money(ext.subtotal) : null], ["Tax", taxes || null],
    ["Total", ext.total != null ? money(ext.total) : null], ["Bank account", ext.bank_account_number && `${ext.bank_account_number} (${ext.bank_ifsc || "no IFSC"})`],
  ];
  return `<section class="section"><h2>What was read from the document</h2>
    <p class="small muted" style="margin-top:-6px">${reader}</p>
    <dl class="fields">${items.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd class="${v == null || v === "" ? "none" : ""}">${v == null || v === "" ? "Not on invoice" : esc(v)}</dd></div>`).join("")}</dl>
    ${(ext.unreadable_notes || []).length ? `<p class="small" style="color:var(--hold)">Reader notes: ${esc(ext.unreadable_notes.join("; "))}</p>` : ""}
  </section>`;
}

// ---------- runs page ----------
async function showRuns(filter = "all") {
  const runs = await api("/api/runs");
  const done = runs.filter((r) => r.status === "completed");
  const by = (key) => done.filter((r) => finalState(r).key === key);
  const waiting = done.filter((r) => r.decision === "review" && !r.reviewer_action);
  const autoApproved = done.filter((r) => r.decision === "approved");
  const sum = (list) => list.reduce((s, r) => s + (r.total || 0), 0);
  const share = (n) => (done.length ? (n / done.length) * 100 : 0);

  const sentence = done.length
    ? `<b>${done.length}</b> invoice${done.length === 1 ? "" : "s"} processed. <b>${autoApproved.length}</b> approved with no one touching them, <b>${waiting.length}</b> waiting for review${waiting.length ? ` (${money(sum(waiting))} on hold)` : ""}, and <b>${by("rejected").length}</b> rejected${by("rejected").length ? ` (${money(sum(by("rejected")))} stopped)` : ""}.`
    : "No invoices processed yet.";

  const filters = { all: "All", waiting: "Waiting for review", approved: "Approved", rejected: "Rejected" };
  const shown = runs.filter((r) => filter === "all" ? true
    : filter === "waiting" ? r.decision === "review" && !r.reviewer_action
    : finalState(r).key === filter);

  view.innerHTML = `
    <h1>Runs</h1>
    <div class="summary">
      <p>${sentence}</p>
      ${done.length ? `<div class="split" role="img" aria-label="Approved ${by("approved").length}, waiting ${waiting.length}, rejected ${by("rejected").length}">
        <span class="a" style="width:${share(by("approved").length)}%"></span><span class="r" style="width:${share(waiting.length)}%"></span><span class="x" style="width:${share(by("rejected").length)}%"></span></div>` : ""}
    </div>
    <div class="filters" role="group" aria-label="Filter runs">
      ${Object.entries(filters).map(([k, label]) => `<button type="button" data-filter="${k}" aria-pressed="${k === filter}">${label}</button>`).join("")}
    </div>
    ${shown.length ? `<div class="scroll"><table class="runs-table">
      <thead><tr><th>Run</th><th>Received</th><th>Vendor</th><th>Invoice</th><th>PO</th><th class="num">Total</th><th>Decision</th><th>Reason</th><th class="num">Time</th></tr></thead>
      <tbody>${shown.map((r) => {
        const s = finalState(r);
        const raw = (r.headline || r.error || "").split(" - ").slice(1).join(" - ");
        const reason = raw.charAt(0).toUpperCase() + raw.slice(1);
        return `<tr data-id="${r.id}" tabindex="0">
          <td>${r.id}</td><td>${esc(when(r.created_at))}</td><td>${esc(r.vendor_name || "-")}</td>
          <td>${esc(r.invoice_number || r.file_name)}</td><td>${esc(r.po_number || "-")}</td>
          <td class="num">${money(r.total)}</td>
          <td><span class="dec ${s.key}">${esc(s.word)}${s.by ? `<span class="by">${esc(s.by)}</span>` : ""}</span></td>
          <td class="small">${plain(reason)}</td>
          <td class="num muted">${r.duration_ms != null ? `${(r.duration_ms / 1000).toFixed(1)} s` : ""}</td></tr>`;
      }).join("")}</tbody></table></div>`
      : `<div class="empty">${runs.length ? "No runs match this filter." : `Nothing here yet. <a href="#/process">Process an invoice</a> to start the history.`}</div>`}`;

  view.querySelectorAll("[data-filter]").forEach((b) => b.addEventListener("click", () => showRuns(b.dataset.filter)));
  view.querySelectorAll("tr[data-id]").forEach((tr) => {
    const open = () => { location.hash = `#/run/${tr.dataset.id}`; };
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  });
}

// ---------- reference page ----------
async function showReference() {
  const data = await api("/api/reference");
  const vendorName = Object.fromEntries(data.vendors.map((v) => [v.vendor_id, v.legal_name]));
  view.innerHTML = `
    <h1>Purchase orders</h1>
    <p class="lede">What each PO allows and how much of it has been billed. Green is approved, amber is waiting for review. These numbers are what the split-billing and quantity checks read.</p>
    <ul class="po-list">${data.purchase_orders.map((po) => {
      const base = Math.max(po.value, po.approved + po.pending);
      const pct = (v) => `${(v / base) * 100}%`;
      return `<li class="po">
        <div><div class="num">${esc(po.po_number)}</div><div class="small ${po.status === "closed" ? "closed" : "muted"}">${po.status === "closed" ? "Closed" : "Open"}, ${esc(po.date)}</div></div>
        <div><div>${esc(po.description)}</div><div class="small muted">${esc(po.vendor)}. Owner: ${esc(po.owner)}</div></div>
        <div><div class="meter"><span class="a" style="width:${pct(po.approved)}"></span><span class="p" style="width:${pct(po.pending)}"></span></div>
          <div class="use">${money(po.approved)} of ${money(po.value)} billed${po.pending ? `, ${money(po.pending)} waiting` : ""}</div></div>
        <details><summary>${po.lines.length} line${po.lines.length === 1 ? "" : "s"}, ${po.invoices.length} invoice${po.invoices.length === 1 ? "" : "s"}</summary>
          <div class="scroll"><table><thead><tr><th>#</th><th>Item</th><th class="num">Ordered</th><th class="num">Billed</th><th class="num">Unit price</th><th class="num">GST</th></tr></thead>
          <tbody>${po.lines.map((l) => `<tr><td>${l.line_no}</td><td>${esc(l.description)}</td><td class="num">${l.quantity} ${esc(l.unit)}</td><td class="num">${l.billed}</td><td class="num">${money(l.unit_price)}</td><td class="num">${l.tax_rate}%</td></tr>`).join("")}</tbody></table></div>
          ${po.invoices.length ? `<p class="small">Invoices: ${po.invoices.map((i) => `${esc(i.invoice_number)} (${esc(i.status)}, ${money(i.total)}${i.source === "history" ? ", from before this system" : ""})`).join("; ")}</p>` : ""}
        </details></li>`;
    }).join("")}</ul>

    <h2>Approved vendors</h2>
    <div class="scroll section"><table>
      <thead><tr><th>ID</th><th>Vendor</th><th>GSTIN</th><th>Bank account</th><th>Terms</th><th>Status</th></tr></thead>
      <tbody>${data.vendors.map((v) => `<tr><td>${esc(v.vendor_id)}</td><td>${esc(v.legal_name)}</td><td>${esc(v.gstin)}</td>
        <td>${esc(v.bank_account)} <span class="muted small">${esc(v.bank_ifsc)}</span></td><td>${v.payment_terms_days} days</td>
        <td class="dec ${v.status === "approved" ? "approved" : "rejected"}">${v.status === "approved" ? "Active" : "Blocked"}</td></tr>`).join("")}</tbody>
    </table></div>

    <h2>Invoice ledger</h2>
    <div class="scroll"><table>
      <thead><tr><th>Vendor</th><th>Invoice</th><th>PO</th><th>Date</th><th class="num">Total</th><th>Status</th><th>Source</th></tr></thead>
      <tbody>${data.ledger.map((l) => `<tr><td>${esc(vendorName[l.vendor_id] || l.vendor_id)}</td><td>${esc(l.invoice_number)}</td><td>${esc(l.po_number || "-")}</td>
        <td>${esc(l.invoice_date || "-")}</td><td class="num">${money(l.total)}</td><td>${esc(l.status)}</td>
        <td>${l.run_id ? `<a href="#/run/${l.run_id}">Run ${l.run_id}</a>` : "Before this system"}</td></tr>`).join("")}</tbody>
    </table></div>`;
}

// ---------- boot ----------
document.getElementById("reset").addEventListener("click", async () => {
  if (!confirm("Clear all runs and put the ledger back to its starting history?")) return;
  try {
    await api("/api/reset", { method: "POST" });
    toast("Demo data reset");
    if (location.hash === "#/process" || !location.hash) route(); else location.hash = "#/process";
  } catch (error) { toast(error.message); }
});

(async () => {
  try {
    META = await api("/api/meta");
    document.getElementById("company").textContent = `Accounts payable, ${META.company.name}`;
  } catch (error) {
    view.innerHTML = `<div class="empty">The server is not responding: ${esc(error.message)}</div>`;
    return;
  }
  window.addEventListener("hashchange", route);
  route();
})();
