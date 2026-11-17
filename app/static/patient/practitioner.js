// Practitioner page (/app/practitioner, R4): open handoffs of assigned patients, the
// conversation, a reply box, a resolve button and "Enable alerts" (Web Push for new
// handoffs). New handoffs and patient messages arrive on the private Realtime topic
// handoffs:<auth uid>; the API re-checks the assignment on every call.
// The Review tab (R5) lists the advisor's answers to assigned patients and records a
// verdict on each (append-only: a new review becomes the current verdict).
// The Knowledge base tab (R5) lists drafts awaiting approval (proposed by the weekly
// job or opened with Edit) and the approved entries. Only drafts are edited; approving
// needs the entry text and a source citation, and retires the entry it supersedes.
// The Audit tab (R5) shows answer counts with their rates (overall and per KB version),
// the KB status and the risk model card. Plain tables, no chart library.
// The dashboard tabs (R6) are read-only: Overview (KPI tiles), Patients (a sortable,
// filterable list and a patient detail with an inline-SVG glucose chart, the "why is
// this the limit" breakdown, the survey risk tier and recent advisories) and Alerts.
// All maths and labels live in common.js (tested with Node); this file only builds DOM.
"use strict";

const { $, say, guarded, clock, VERDICTS } = window.DA;
const POLL_MS = 20_000;
const TRIGGERS = { red_flag: "Red flag", practitioner_request: "Asked for help", breach_escalation: "Breach" };
const WHO = { patient: "Patient", assistant: "Advisor (automatic)", practitioner: "You" };

let selected = null;
let myName = null;
const STATUS = { open: "Waiting to be claimed", assigned: "Claimed", resolved: "Resolved" };
let channel = null;
let pollTimer = null;
let activeTab = "overview";
let reviewFilter = "unreviewed";
let reviewCursor = null;
let reviewLoaded = false;
let reviewRequest = 0; // ignore a slow page that arrives after the filter changed
let kbLoaded = false;
let auditLoaded = false;
let overviewLoaded = false;
let patientsLoaded = false;
let alertsLoaded = false;
let patientRows = [];
let patientSort = { key: "state", descending: false };
let openPatient = null;
let alertCursor = null;
let alertRequest = 0;
const TABS = [
  ["tab-overview", "overview"], ["tab-patients", "patients"], ["tab-alerts", "alerts"],
  ["tab-inbox", "inbox"], ["tab-review", "review"], ["tab-kb", "kb"], ["tab-audit", "audit"],
];

function bubble(message) {
  const el = document.createElement("div");
  if (message.role === "system") {
    el.className = "notice";
    el.textContent = `${message.text} · ${clock(message.created_at)}`;
    return el;
  }
  el.className = `bubble ${message.role}`;
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = WHO[message.role] || message.role;
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = `${message.spoken ? "voice · " : ""}${clock(message.created_at)}`;
  el.append(who, document.createTextNode(message.text), meta);
  return el;
}

function label(h) {
  return h.patient_name || `Patient ${h.patient_id.slice(0, 8)}`;
}

function renderList(handoffs) {
  const list = $("list");
  list.replaceChildren();
  $("empty").hidden = handoffs.some((h) => h.status !== "resolved");
  for (const h of handoffs) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.setAttribute("aria-current", String(h.id === selected));
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = label(h);
    const tags = document.createElement("div");
    const trig = document.createElement("span");
    trig.className = `tag${h.trigger === "red_flag" ? " urgent" : ""}`;
    trig.textContent = TRIGGERS[h.trigger] || h.trigger;
    const st = document.createElement("span");
    st.className = "tag";
    st.textContent = STATUS[h.status] || h.status;
    const when = document.createElement("span");
    when.className = "muted";
    when.textContent = ` ${clock(h.last_message_at || h.opened_at)}`;
    tags.append(trig, st, when);
    btn.append(name, tags);
    btn.addEventListener("click", () => { select(h.id).catch((err) => say(err.message, true)); });
    li.append(btn);
    list.append(li);
  }
}

async function loadList() {
  const handoffs = await window.DA.api("/api/practitioner/handoffs");
  renderList(handoffs);
  if (!selected) {
    const first = handoffs.find((h) => h.status !== "resolved");
    if (first) await select(first.id);
  }
}

async function select(id) {
  selected = id;
  const detail = await window.DA.api(`/api/practitioner/handoffs/${id}`);
  const h = detail.handoff;
  $("thread").hidden = false;
  $("thread-title").textContent = label(h);
  $("thread-meta").textContent = `${TRIGGERS[h.trigger] || h.trigger} · ${h.status} · opened ${new Date(h.opened_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}${h.reason ? ` · ${h.reason}` : ""}`;
  const box = $("messages");
  box.replaceChildren(...window.DA.renderWithDays(detail.messages, bubble));
  box.scrollTop = box.scrollHeight;
  const resolved = h.status === "resolved";
  const mine = h.practitioner_name && h.practitioner_name === myName;
  $("claim").hidden = h.status !== "open";
  $("claim-status").textContent = h.status === "open"
    ? "Not claimed yet: the Advisor is still answering. Claim it (or reply) to take over the chat."
    : h.status === "assigned"
      ? `Claimed by ${mine ? "you" : h.practitioner_name || "a practitioner"}: the patient's messages come to ${mine ? "you" : "them"}.`
      : "Resolved.";
  $("reply-form").hidden = resolved;
  $("resolved-note").hidden = !resolved;
  await loadListOnly();
}

async function loadListOnly() {
  renderList(await window.DA.api("/api/practitioner/handoffs"));
}

async function refresh() {
  await loadList();
  if (selected) await select(selected);
}

function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (document.visibilityState === "visible") await refresh().catch(() => {});
    schedulePoll();
  }, POLL_MS);
}

async function sendReply() {
  const text = $("reply").value.trim();
  if (!text || !selected) return;
  $("send").disabled = true;
  try {
    await window.DA.api(`/api/practitioner/handoffs/${selected}/messages`, "POST", { text });
    $("reply").value = "";
    await select(selected);
  } finally {
    $("send").disabled = false;
  }
}

async function claimSelected() {
  if (!selected) return;
  await window.DA.api(`/api/practitioner/handoffs/${selected}/claim`, "POST");
  say("Handoff claimed: the patient's messages now come to you.");
  await select(selected);
}

async function resolveSelected() {
  if (!selected) return;
  const note = window.prompt("Resolution note (optional):", "");
  if (note === null) return;
  await window.DA.api(`/api/practitioner/handoffs/${selected}/resolve`, "POST", { note: note || null });
  say("Handoff resolved.");
  await select(selected);
}

// --- Review tab (R5) -------------------------------------------------------------

function el(tag, className = "", text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null) node.textContent = text;
  return node;
}

function showTab(name) {
  activeTab = name;
  for (const [tabId, panelId] of TABS) {
    const on = panelId === name;
    $(tabId).setAttribute("aria-selected", String(on));
    $(tabId).tabIndex = on ? 0 : -1;
    $(panelId).hidden = !on;
  }
  $(TABS.find(([, panelId]) => panelId === name)[0]).scrollIntoView({ block: "nearest", inline: "nearest" });
  if (name === "overview" && !overviewLoaded) loadOverview().catch((err) => say(err.message, true));
  if (name === "patients" && !patientsLoaded) loadPatients().catch((err) => say(err.message, true));
  if (name === "alerts" && !alertsLoaded) loadAlerts(true).catch((err) => say(err.message, true));
  if (name === "review" && !reviewLoaded) loadTurns(true).catch((err) => say(err.message, true));
  if (name === "kb" && !kbLoaded) loadKb().catch((err) => say(err.message, true));
  if (name === "audit" && !auditLoaded) loadAudit().catch((err) => say(err.message, true));
}

function sourceLine(s) {
  const version = s.kb_version ? `, KB v${s.kb_version}` : "";
  const citation = s.source_citation ? ` · ${s.source_citation}` : "";
  return `${s.title} (${s.entry_key}${version})${citation}`;
}

function currentVerdict(turn) {
  const r = turn.current_review;
  if (!r) return null;
  const when = new Date(r.created_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  const note = r.note ? `: "${r.note}"` : "";
  const history = turn.review_count > 1 ? ` (${turn.review_count} reviews in total)` : "";
  return `Current verdict: ${VERDICTS[r.verdict] || r.verdict} by ${r.reviewer_name || "a practitioner"}, ${when}${note}${history}`;
}

function turnCard(turn) {
  const card = el("li", "panel turn");
  const head = el("div", "head");
  head.append(
    el("span", "name", turn.patient_first_name || "Patient"),
    el("span", "muted", new Date(turn.created_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })),
  );
  for (const tag of window.DA.reviewTags(turn)) head.append(el("span", `tag${tag.urgent ? " urgent" : ""}`, tag.text));
  card.append(
    head,
    el("h3", "", turn.spoken ? "Patient said (voice)" : "Patient asked"),
    el("p", "text question", turn.question || "(no text: the Talk to a doctor button)"),
    el("h3", "", "Advisor answered"),
    el("p", "text answer", turn.answer),
    el("h3", "", "Source entry"),
  );
  if (turn.sources.length) {
    const list = el("ul", "sources");
    for (const s of turn.sources) list.append(el("li", "", sourceLine(s)));
    card.append(list);
  } else {
    card.append(el("p", "muted", turn.unanswered
      ? "None: no knowledge-base entry was close enough, so the advisor said it did not know."
      : "None: a fixed rule answered (reading, red flag, dosing guard or greeting)."));
  }
  const similarity = turn.retrieval_confidence !== null ? ` · similarity ${turn.retrieval_confidence.toFixed(2)}` : "";
  card.append(el("p", "muted", `${turn.intent || "other"} · ${turn.model || "unknown model"}${similarity}`));
  const verdictText = currentVerdict(turn);
  if (verdictText) card.append(el("p", "verdict-now", verdictText));

  const noteId = `note-${turn.turn_id}`;
  const label = el("label", "", "Note (required unless Correct: what is wrong, or what the knowledge base should say)");
  label.htmlFor = noteId;
  const note = el("textarea");
  note.id = noteId;
  note.rows = 2;
  note.maxLength = 1000;
  const status = el("p", "muted");
  status.setAttribute("role", "status");
  const buttons = el("div", "verdicts");
  for (const [verdict, text] of Object.entries(VERDICTS)) {
    const cls = verdict === "correct" ? "" : verdict === "unsafe" ? "unsafe" : "secondary";
    const btn = el("button", cls, text);
    btn.type = "button";
    btn.addEventListener("click", () => { submitReview(turn, verdict, card, note, status); });
    buttons.append(btn);
  }
  card.append(label, note, buttons, status);
  return card;
}

async function submitReview(turn, verdict, card, noteBox, status) {
  const note = noteBox.value.trim();
  status.classList.remove("error");
  if (window.DA.needsNote(verdict) && !note) {
    status.textContent = `Add a note before marking this "${VERDICTS[verdict]}".`;
    status.classList.add("error");
    noteBox.focus();
    return;
  }
  const buttons = card.querySelectorAll(".verdicts button");
  for (const b of buttons) b.disabled = true;
  try {
    const review = await window.DA.api(`/api/practitioner/turns/${turn.turn_id}/review`, "POST", { verdict, note: note || null });
    const updated = { ...turn, current_review: review, review_count: turn.review_count + 1 };
    const fresh = turnCard(updated);
    fresh.querySelector('[role="status"]').textContent = `Saved: ${VERDICTS[verdict]}.`;
    card.replaceWith(fresh);
  } catch (err) {
    status.textContent = err.message || String(err);
    status.classList.add("error");
    for (const b of buttons) b.disabled = false;
  }
}

async function loadTurns(reset) {
  const request = reset ? ++reviewRequest : reviewRequest;
  if (reset) reviewCursor = null;
  $("more").disabled = true;
  try {
    const page = await window.DA.api(window.DA.reviewQuery(reviewFilter, reviewCursor));
    if (request !== reviewRequest) return;
    if (reset) $("turns").replaceChildren();
    reviewLoaded = true;
    for (const turn of page.items) $("turns").append(turnCard(turn));
    reviewCursor = page.next_cursor;
    $("more").hidden = !reviewCursor;
    $("turns-empty").hidden = $("turns").children.length > 0;
  } finally {
    $("more").disabled = false;
  }
}

function setFilter(button) {
  reviewFilter = button.dataset.filter;
  for (const b of document.querySelectorAll(".filters button")) b.setAttribute("aria-pressed", String(b === button));
  return loadTurns(true);
}

// --- Knowledge base tab (R5) ------------------------------------------------------

function when(iso) {
  return new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function field(card, id, text, control) {
  const label = el("label", "", text);
  label.htmlFor = id;
  control.id = id;
  card.append(label, control);
  return control;
}

function draftCard(entry) {
  const card = el("li", `panel turn${entry.priority ? " priority" : ""}`);
  card.id = `kb-${entry.id}`;
  const head = el("div", "head");
  for (const tag of window.DA.kbDraftTags(entry)) head.append(el("span", `tag${tag.urgent ? " urgent" : ""}`, tag.text));
  head.append(el("span", "name", window.DA.kbDraftOrigin(entry)), el("span", "muted", when(entry.created_at)));
  card.append(head);
  if (entry.priority) {
    card.append(el("p", "error", `${window.DA.KB_PRIORITY_LABEL}: the advisor's answer may have put a patient at risk. Write the correct answer first.`));
  }

  if (entry.proposed_from_turn_id) {
    const turn = entry.source_turn;
    card.append(el("h3", "", "Patient asked"));
    if (turn) {
      card.append(
        el("p", "text question", turn.question || "(no text)"),
        el("h3", "", "Advisor answered"),
        el("p", "text answer", turn.answer || "(no reply)"),
        el("p", "muted", `Asked ${when(turn.asked_at)}`),
      );
    } else {
      card.append(el("p", "muted", "Asked by a patient who is not assigned to you; the draft title below is their question."));
    }
  }
  if (entry.review_hint) {
    card.append(el("h3", "", "Why it was proposed (staff only, never shown to patients)"), el("p", "text hint", entry.review_hint));
  }

  const title = el("input");
  title.maxLength = 2000;
  title.value = entry.title;
  field(card, `kb-title-${entry.id}`, "Title (patients see this)", title);
  const body = el("textarea");
  body.rows = 6;
  body.maxLength = 6000;
  body.value = entry.body;
  body.placeholder = "Write the answer in plain language, as you would say it to a patient.";
  field(card, `kb-body-${entry.id}`, "Entry text (patients see this)", body);
  const citation = el("input");
  citation.maxLength = 500;
  citation.value = entry.source_citation || "";
  citation.placeholder = "e.g. ADA Standards of Care in Diabetes-2026, section 6";
  field(card, `kb-cite-${entry.id}`, "Source citation (required to approve)", citation);

  const status = el("p", "muted");
  status.setAttribute("role", "status");
  const buttons = el("div", "verdicts");
  const values = () => ({ title: title.value.trim(), body: body.value.trim(), source_citation: citation.value.trim() || null });
  const act = async (work) => {
    status.classList.remove("error");
    for (const b of buttons.querySelectorAll("button")) b.disabled = true;
    try {
      await work();
    } catch (err) {
      status.textContent = err.message || String(err);
      status.classList.add("error");
    } finally {
      for (const b of buttons.querySelectorAll("button")) b.disabled = false;
    }
  };
  const save = el("button", "secondary", "Save draft");
  save.type = "button";
  save.addEventListener("click", () => act(async () => {
    await window.DA.api(`/api/practitioner/kb/${entry.id}`, "PUT", values());
    status.textContent = "Draft saved. It is not served until approved.";
  }));
  const approve = el("button", "", "Approve");
  approve.type = "button";
  approve.addEventListener("click", () => act(async () => {
    const draft = values();
    const problem = window.DA.kbApproveProblem(draft);
    if (problem) throw new Error(problem);
    if (!window.confirm("Approve this entry? Patients will get this text from their next question.")) return;
    // Save first, so what is approved is exactly what is on screen.
    await window.DA.api(`/api/practitioner/kb/${entry.id}`, "PUT", draft);
    const approved = await window.DA.api(`/api/practitioner/kb/${entry.id}/approve`, "POST");
    await loadKb();
    say(`Approved as knowledge base version ${approved.kb_version}.`);
  }));
  const reject = el("button", "unsafe", "Reject");
  reject.type = "button";
  reject.addEventListener("click", () => act(async () => {
    if (!window.confirm("Reject this draft? It is kept for the record but never served.")) return;
    await window.DA.api(`/api/practitioner/kb/${entry.id}/reject`, "POST");
    await loadKb();
    say("Draft rejected.");
  }));
  buttons.append(save, approve, reject);
  card.append(buttons, status);
  return card;
}

function approvedCard(entry) {
  const card = el("li", "panel turn");
  const head = el("div", "head");
  head.append(el("span", "name", entry.title), el("span", "tag", `v${entry.kb_version}`));
  if (entry.open_draft_id) head.append(el("span", "tag urgent", "Draft open"));
  const approvedBy = entry.approved_by_name ? ` by ${entry.approved_by_name}` : "";
  card.append(
    head,
    el("p", "muted", `${entry.entry_key} · ${entry.source_citation || "no citation"}`),
    el("p", "muted", `Approved${approvedBy}, ${when(entry.approved_at)}`),
  );
  const details = el("details");
  details.append(el("summary", "", "Entry text"), el("p", "text answer", entry.body));
  card.append(details);
  const status = el("p", "muted");
  status.setAttribute("role", "status");
  const edit = el("button", "secondary", entry.open_draft_id ? "Go to open draft" : "Edit");
  edit.type = "button";
  edit.addEventListener("click", async () => {
    edit.disabled = true;
    try {
      const draft = entry.open_draft_id
        ? { id: entry.open_draft_id }
        : await window.DA.api(`/api/practitioner/kb/${entry.id}/edit`, "POST");
      await loadKb();
      const target = $(`kb-${draft.id}`);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        target.querySelector("textarea")?.focus({ preventScroll: true });
      }
    } catch (err) {
      status.textContent = err.message || String(err);
      status.classList.add("error");
    } finally {
      edit.disabled = false;
    }
  });
  card.append(edit, status);
  return card;
}

async function loadKb() {
  const listing = await window.DA.api("/api/practitioner/kb");
  kbLoaded = true;
  $("kb-drafts").replaceChildren(...window.DA.kbSortDrafts(listing.drafts).map(draftCard));
  $("kb-approved").replaceChildren(...listing.approved.map(approvedCard));
  $("kb-drafts-empty").hidden = listing.drafts.length > 0;
  $("kb-approved-empty").hidden = listing.approved.length > 0;
}

// --- Audit tab (R5) ------------------------------------------------------------------

function row(cells, header = false) {
  const tr = el("tr");
  cells.forEach(([text, num], i) => {
    const cell = el(header || i === 0 ? "th" : "td", num ? "num" : "", text);
    if (header) cell.scope = "col";
    else if (i === 0) cell.scope = "row";
    tr.append(cell);
  });
  return tr;
}

function renderSummary(summary) {
  const t = summary.totals;
  $("audit-since").value = window.DA.localDay(summary.since);
  // until is exclusive: the last day shown is the one before it.
  $("audit-until").value = window.DA.localDay(summary.until, 1);
  $("audit-window").textContent = `${when(summary.since)} to ${when(summary.until)}`;
  const body = $("audit-summary-body");
  body.replaceChildren(row([["Advisor answers"], [String(t.total), true], ["", true], ["", true]]));
  for (const [text, cr] of window.DA.auditRows(t)) {
    body.append(row([[text], [String(cr.count), true], [String(cr.denominator), true], [window.DA.pct(cr.rate), true]]));
  }
  $("audit-by-kb-head").replaceChildren(row(
    [["KB version"], ["Answers", true], ...window.DA.AUDIT_KB_COLUMNS.map(([h]) => [h, true])], true,
  ));
  $("audit-by-kb-body").replaceChildren(...summary.by_kb_version.map((g) => row([
    [g.kb_version === null ? "No KB used" : `v${g.kb_version}`],
    [String(g.counts.total), true],
    ...window.DA.AUDIT_KB_COLUMNS.map(([, pick]) => [window.DA.countOf(pick(g.counts)), true]),
  ])));
  $("audit-kb-empty").hidden = summary.by_kb_version.length > 0;
  $("audit-kb-status").textContent = window.DA.kbStatusLine(summary.kb_status);
}

function renderModelCard(card) {
  const m = card.test_metrics;
  $("model-meta").textContent = `Version ${card.version} (${card.model_name}), trained ${when(card.trained_at)} on ${card.training_rows.toLocaleString()} survey rows; ${window.DA.pct(card.prevalence)} of them had diabetes. Test set: ${card.test_rows.toLocaleString()} rows.`;
  $("model-metrics-body").replaceChildren(...window.DA.MODEL_METRICS.map(([key, text]) => row([[text], [m[key].toFixed(3), true]])));
  const c = card.confusion;
  for (const key of ["tn", "fp", "fn", "tp"]) $(`cm-${key}`).textContent = c[key].toLocaleString();
  const f = card.flagged_without_diabetes;
  $("model-flagged").textContent = `Of the ${f.denominator.toLocaleString()} people the model flagged, ${f.count.toLocaleString()} (${window.DA.pct(f.rate)}) do not have diabetes: FP / (FP + TP).`;
  $("model-threshold").textContent = `Flagged at probability ${card.decision_threshold.toFixed(3)} or above. Tiers: moderate from ${card.tier_cutoffs.moderate.toFixed(3)}, high from ${card.tier_cutoffs.high.toFixed(3)}.`;
  $("model-limitations").replaceChildren(...card.limitations.map((text) => el("li", "", text)));
  $("model-features-body").replaceChildren(...card.top_features.map((f2) => row([[f2.feature], [f2.importance.toFixed(4), true]])));
  const last = card.recent_retrains[0] || null;
  const at = last ? ` (${when(last.finished_at || last.started_at)})` : "";
  $("model-retrain").textContent = `${window.DA.retrainLine(last)}${at}`;
}

async function loadAudit(query = window.DA.auditQuery(null, null)) {
  const [summary, card] = await Promise.allSettled([
    window.DA.api(query),
    window.DA.api("/api/practitioner/model-card"),
  ]);
  auditLoaded = true;
  if (card.status === "fulfilled") {
    $("model-error").hidden = true;
    $("model-card").hidden = false;
    renderModelCard(card.value);
  } else {
    $("model-card").hidden = true;
    $("model-error").hidden = false;
    $("model-error").textContent = card.reason.message || String(card.reason);
  }
  if (summary.status === "rejected") throw summary.reason;
  renderSummary(summary.value);
}


// --- Dashboard (R6): Overview, Patients, Alerts -----------------------------------

const D = window.DA;
const SVG = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

/** A table cell with its label (shown above the value on a phone). */
function cell(text, label, className = "") {
  const td = el("td", className, text);
  td.dataset.label = label;
  return td;
}

/** A state tag: words and a direction glyph, colour only as a second signal. */
function stateTag(state) {
  const tone = state ? D.BAND_TONE[state] : "";
  return el("span", `tag${tone ? ` ${tone}` : ""}`, D.bandLabel(state));
}

function stateCell(state, label = "State") {
  const td = cell("", label);
  td.append(stateTag(state));
  return td;
}

// Overview ----------------------------------------------------------------------------

async function loadOverview() {
  const ov = await D.api("/api/dashboard/overview");
  overviewLoaded = true;
  $("ov-tiles").replaceChildren(...D.overviewTiles(ov).map((t) => {
    const box = el("div", `kpi${t.urgent ? " urgent" : ""}`);
    const dd = el("dd", "", t.value);
    if (t.detail) dd.append(el("span", "detail", t.detail));
    box.append(el("dt", "", t.label), dd);
    return box;
  }));
  $("ov-batch").textContent = D.batchLine(ov.last_batch_run);
  $("ov-delivery").replaceChildren(...D.deliveryRows(ov.delivery).map(([text, cr]) => row([
    [text], [String(cr.count), true], [String(cr.denominator), true], [D.pct(cr.rate), true],
  ])));
}

// Patients ----------------------------------------------------------------------------

function patientFilters() {
  return { band_state: $("pf-band").value, breach_7d: $("pf-breach").value, live_handoff: $("pf-handoff").value };
}

async function loadPatients() {
  // The list is short (assigned patients), so every page is fetched and sorted here;
  // sorting a partial list would put the wrong patient first.
  const rows = [];
  let cursor = null;
  for (let page = 0; page < 20; page += 1) {
    const body = await D.api(D.patientsQuery(patientFilters(), cursor));
    rows.push(...body.items);
    cursor = body.next_cursor;
    if (!cursor) break;
  }
  patientsLoaded = true;
  patientRows = rows;
  renderPatients();
}

function renderPatients() {
  const sorted = D.sortPatients(patientRows, patientSort.key, patientSort.descending);
  const LABELS = ["Patient", "Age", "Diabetes", "Last reading", "State", "Limits F / PM / Low", "Alerts 7 d", "Survey risk", "Handoff"];
  $("patient-rows").replaceChildren(...sorted.map((r) => {
    const c = D.patientCells(r);
    const tr = el("tr", "pick");
    const nameCell = cell("", LABELS[0], "wide");
    const open = el("button", "link", c.name);
    open.type = "button";
    open.setAttribute("aria-label", `Open ${c.name}`);
    nameCell.append(open);
    tr.append(
      nameCell,
      cell(c.age, LABELS[1]),
      cell(c.status, LABELS[2]),
      lastCell(c, LABELS[3]),
      stateCell(r.last_reading ? r.last_reading.band_state : null, LABELS[4]),
      cell(c.limits, LABELS[5], "num"),
      cell(c.alerts, LABELS[6], "num"),
      riskCell(c, LABELS[7]),
      cell(c.handoff, LABELS[8]),
    );
    if (!r.last_reading) tr.children[4].replaceChildren(document.createTextNode("-"));
    tr.addEventListener("click", () => { openDetail(r.user_id).catch((err) => say(err.message, true)); });
    return tr;
  }));
  $("patients-empty").hidden = sorted.length > 0;
  for (const th of document.querySelectorAll("#patient-table th[data-sort]")) {
    const on = th.dataset.sort === patientSort.key;
    th.setAttribute("aria-sort", on ? (patientSort.descending ? "descending" : "ascending") : "none");
  }
  $("pf-sort").value = patientSort.key;
}

function lastCell(c, label) {
  const td = cell(c.last, label, "wide");
  if (c.lastWhen) td.append(el("span", "sub", c.lastWhen));
  return td;
}

function riskCell(c, label) {
  const td = cell(c.risk, label);
  if (c.riskModel) td.title = `Model ${c.riskModel}`;
  return td;
}

function setSort(key) {
  patientSort = { key, descending: patientSort.key === key ? !patientSort.descending : false };
  renderPatients();
}

async function openDetail(userId) {
  openPatient = userId;
  const detail = await D.api(`/api/dashboard/patients/${userId}?days=${encodeURIComponent($("pd-days").value)}`);
  if (openPatient !== userId) return;
  $("patient-list").hidden = true;
  $("patient-detail").hidden = false;
  renderDetail(detail);
  $("pd-name").focus({ preventScroll: true });
  $("pd-name").scrollIntoView({ block: "start" });
}

function closeDetail() {
  openPatient = null;
  $("patient-detail").hidden = true;
  $("patient-list").hidden = false;
}

function renderDetail(d) {
  $("pd-name").textContent = d.first_name || "Patient";
  const status = d.diabetes_status === "diabetes" ? "Diabetes" : d.diabetes_status === "no_diabetes" ? "No diabetes" : "Diabetes status not set";
  $("pd-meta").textContent = `${d.age_band ? `Age ${d.age_band}, ` : ""}${status}. Pre-breach band: within ${Math.round(d.pre_breach_band * 100)} % of a limit.`;
  renderChart(d);
  $("pd-readings").replaceChildren(...[...d.readings].reverse().map((r) => {
    const tr = el("tr");
    tr.append(
      cell(D.shortWhen(r.measured_at), "Taken"),
      cell(D.CONTEXT_LABELS[r.context] || r.context, "Context"),
      cell(D.mgdl(r.value_mg_dl), "mg/dL", "num"),
      stateCell(r.band_state),
    );
    return tr;
  }));
  renderWhy(d.thresholds);
  $("pd-risk").textContent = d.risk
    ? `${d.risk.tier[0].toUpperCase()}${d.risk.tier.slice(1)} (score ${d.risk.score.toFixed(2)}${d.risk.flagged ? ", flagged" : ""}), model ${d.risk.model_version}, scored ${D.shortWhen(d.risk.assessed_at)}.`
    : "Not scored yet (the survey profile is incomplete, or the batch has not run).";
  $("pd-advisories").replaceChildren(...d.advisories.map((a) => {
    const c = D.alertCells(a);
    const tr = el("tr");
    tr.append(cell(c.when, "Reading taken"), cell(c.limit, "Limit"), stateCell(a.state), cell(c.reading, "Reading vs limit", "num"),
      cell(c.escalated, "Escalated"), cell(c.delivery, "Delivery"));
    return tr;
  }));
  $("pd-advisories-empty").hidden = d.advisories.length > 0;
}

function renderWhy(steps) {
  const rows = D.whyLimits(steps);
  $("pd-why-empty").hidden = rows.length > 0;
  $("pd-why").hidden = rows.length === 0;
  $("pd-why-head").replaceChildren(row([["mg/dL"], ...rows.map((r) => [r.label, true])], true));
  const line = (label, pick, cls = "") => {
    const tr = row([[label], ...rows.map((r) => [pick(r), true])]);
    if (cls) tr.className = cls;
    return tr;
  };
  $("pd-why-body").replaceChildren(
    line("Base (cited)", (r) => D.mgdl(r.base)),
    line("+ Age / health", (r) => D.signed(r.demo)),
    line("+ Survey risk", (r) => D.signed(r.risk)),
    line("+ Trend", (r) => D.signed(r.trend)),
    line("= Sum", (r) => D.mgdl(r.sum)),
    line("Allowed range", (r) => `${D.mgdl(r.min)}-${D.mgdl(r.max)}`),
    line("Limit", (r) => `${D.mgdl(r.value)}${r.clamped ? " (held in range)" : ""}`, "total"),
  );
  $("pd-why-notes").replaceChildren(...rows.map((r) => {
    const li = el("li");
    li.append(el("strong", "", `${r.label}: `), document.createTextNode(`${r.citation}. Last change: ${r.reason}, ${D.shortWhen(r.since)}.`));
    return li;
  }));
}

// The glucose chart ----------------------------------------------------------------

const SERIES = Object.freeze({
  fasting: { color: "var(--s-fasting)", label: "Fasting reading" },
  post_meal: { color: "var(--s-post)", label: "Post-meal reading" },
  random: { color: "var(--s-random)", label: "Random reading" },
});
const LIMIT_STYLE = Object.freeze({
  fasting_high: { color: "var(--s-fasting)", label: "Fasting limit" },
  post_meal_high: { color: "var(--s-post)", label: "Post-meal limit" },
  low: { color: "var(--fg)", label: "Low limit" },
});

/** One reading marker: fasting circle, post-meal triangle, random diamond. */
function marker(context, x, y, r = 4.5) {
  const fill = SERIES[context] ? SERIES[context].color : "var(--fg)";
  const common = { fill, stroke: "var(--card)", "stroke-width": 1.5 };
  if (context === "post_meal") {
    const h = r * 1.25;
    return svg("path", { d: `M${x} ${y - h}L${x + h} ${y + h * 0.8}L${x - h} ${y + h * 0.8}Z`, ...common });
  }
  if (context === "random") {
    const h = r * 1.2;
    return svg("path", { d: `M${x} ${y - h}L${x + h} ${y}L${x} ${y + h}L${x - h} ${y}Z`, ...common });
  }
  return svg("circle", { cx: x, cy: y, r, ...common });
}

function legendItem(icon, text) {
  const li = el("li");
  li.append(icon, document.createTextNode(text));
  return li;
}

function legendSwatch(draw) {
  const s = svg("svg", { width: 22, height: 14, viewBox: "0 0 22 14", "aria-hidden": "true" });
  draw(s);
  return s;
}

function renderLegend(model, contexts) {
  const items = contexts.map((c) => legendItem(legendSwatch((s) => s.append(marker(c, 11, 7))), SERIES[c].label));
  for (const l of model.limits) {
    items.push(legendItem(legendSwatch((s) => s.append(svg("line", { x1: 1, y1: 7, x2: 21, y2: 7, stroke: LIMIT_STYLE[l.kind].color, "stroke-width": 2 }))), LIMIT_STYLE[l.kind].label));
  }
  if (model.limits.length) {
    items.push(legendItem(legendSwatch((s) => s.append(svg("rect", { x: 1, y: 2, width: 20, height: 10, fill: "url(#band-hatch-legend)" }))),
      `Pre-breach band (within ${Math.round(model.band * 100)} %)`));
    const defs = svg("defs");
    defs.append(hatch("band-hatch-legend"));
    items[items.length - 1].querySelector("svg").prepend(defs);
  }
  items.push(legendItem(legendSwatch((s) => s.append(svg("circle", { cx: 11, cy: 7, r: 5.5, fill: "none", stroke: "var(--critical)", "stroke-width": 2 }))), "Breach (ringed)"));
  if (model.beforeFirst) {
    items.push(legendItem(legendSwatch((s) => s.append(svg("line", { x1: 1, y1: 7, x2: 21, y2: 7, stroke: "var(--axis)", "stroke-width": 2, "stroke-dasharray": "4 3" }))),
      "Dashed: first limit, applied to readings taken before it was set"));
  }
  $("pd-legend").replaceChildren(...items);
}

function hatch(id) {
  const pattern = svg("pattern", { id, width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
  pattern.append(
    svg("rect", { width: 6, height: 6, fill: "var(--band)", "fill-opacity": 0.22 }),
    svg("line", { x1: 0, y1: 0, x2: 0, y2: 6, stroke: "var(--band)", "stroke-width": 1.5, "stroke-opacity": 0.75 }),
  );
  return pattern;
}

let chartDetail = null;
let chartObserver = null;

function renderChart(detail) {
  chartDetail = detail;
  const box = $("pd-chart");
  const empty = detail.readings.length === 0 && detail.thresholds.length === 0;
  $("pd-chart-empty").hidden = detail.readings.length > 0;
  box.hidden = empty;
  if (empty) { $("pd-legend").replaceChildren(); return; }
  drawChart();
  if (!chartObserver && "ResizeObserver" in window) {
    let lastWidth = 0;
    chartObserver = new ResizeObserver(() => {
      const w = Math.round(box.clientWidth);
      if (w && w !== lastWidth && chartDetail && !box.hidden) { lastWidth = w; drawChart(); }
    });
    chartObserver.observe(box);
  }
}

function drawChart() {
  const detail = chartDetail;
  const box = $("pd-chart");
  const width = Math.max(300, Math.round(box.clientWidth || 600));
  const wide = width >= 520;
  const height = wide ? 320 : 260;
  // Limit labels sit in a right gutter, clear of the readings: name and value on wide
  // screens, the value alone on a phone (the legend names every line).
  const m = D.chartModel(detail, width, height, { top: 12, right: wide ? 104 : 34, bottom: 28, left: 40 });
  const contexts = ["fasting", "post_meal", "random"].filter((c) => detail.readings.some((r) => r.context === c));
  renderLegend(m, contexts);

  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img",
    "aria-label": `Glucose readings and personal limits, ${detail.readings.length} readings. The table below lists them.` });
  const defs = svg("defs");
  defs.append(hatch("band-hatch"));
  root.append(defs);

  const { plot } = m;
  // Recessive grid and y axis (mg/dL).
  for (const t of m.yTicks) {
    root.append(svg("line", { x1: plot.x0, x2: plot.x1, y1: t.y, y2: t.y, stroke: "var(--grid)", "stroke-width": 1 }));
    const label = svg("text", { x: plot.x0 - 6, y: t.y + 4, "text-anchor": "end" });
    label.textContent = String(t.v);
    root.append(label);
  }
  const unit = svg("text", { x: 4, y: plot.y0 + 2, "dominant-baseline": "hanging" });
  unit.textContent = "mg/dL";
  root.append(unit);
  // x axis: dates.
  root.append(svg("line", { x1: plot.x0, x2: plot.x1, y1: plot.y1, y2: plot.y1, stroke: "var(--axis)", "stroke-width": 1 }));
  for (const t of m.xTicks) {
    root.append(svg("line", { x1: t.x, x2: t.x, y1: plot.y1, y2: plot.y1 + 4, stroke: "var(--axis)" }));
    const label = svg("text", { x: t.x, y: plot.y1 + 17, "text-anchor": "middle" });
    label.textContent = new Date(t.t).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
    root.append(label);
  }

  // Pre-breach bands, then the limit step lines, then direct labels.
  for (const l of m.limits) {
    for (const s of l.segments) {
      root.append(svg("rect", { x: s.x0, y: s.bandTop, width: Math.max(0, s.x1 - s.x0), height: Math.max(0, s.bandBottom - s.bandTop), fill: "url(#band-hatch)" }));
    }
  }
  for (const l of m.limits) {
    const style = LIMIT_STYLE[l.kind];
    l.segments.forEach((s, i) => {
      root.append(svg("line", { x1: s.x0, x2: s.x1, y1: s.y, y2: s.y, stroke: style.color, "stroke-width": 2,
        ...(s.beforeFirst ? { "stroke-dasharray": "5 4" } : {}) }));
      const next = l.segments[i + 1];
      if (next) root.append(svg("line", { x1: s.x1, x2: s.x1, y1: s.y, y2: next.y, stroke: style.color, "stroke-width": 2 }));
    });
    const last = l.segments[l.segments.length - 1];
    const label = svg("text", { x: plot.x1 + 4, y: last.y + 4, "text-anchor": "start", class: "limit-label" });
    label.textContent = wide ? `${LIMIT_STYLE[l.kind].label.replace(" limit", "")} ${D.mgdl(last.value)}` : D.mgdl(last.value);
    root.append(label);
  }

  // Readings; breaches get a ring in the critical colour on top.
  for (const p of m.points) {
    root.append(marker(p.context, p.x, p.y));
    if (p.breach) root.append(svg("circle", { cx: p.x, cy: p.y, r: 8, fill: "none", stroke: "var(--critical)", "stroke-width": 2 }));
  }

  // Hover / tap: the nearest reading within reach.
  const tip = el("div", "tip");
  tip.hidden = true;
  tip.setAttribute("role", "status");
  const focusRing = svg("circle", { r: 10, fill: "none", stroke: "var(--fg)", "stroke-width": 1.5, visibility: "hidden" });
  root.append(focusRing);
  const show = (event) => {
    const rect = root.getBoundingClientRect();
    const sx = width / rect.width;
    const px = (event.clientX - rect.left) * sx;
    const py = (event.clientY - rect.top) * sx;
    const i = D.nearestPoint(m.points, px, py, 28);
    if (i < 0) { tip.hidden = true; focusRing.setAttribute("visibility", "hidden"); return; }
    const p = m.points[i];
    focusRing.setAttribute("cx", p.x);
    focusRing.setAttribute("cy", p.y);
    focusRing.setAttribute("visibility", "visible");
    tip.textContent = `${D.shortWhen(p.measured_at)}: ${D.mgdl(p.value)} mg/dL ${D.CONTEXT_LABELS[p.context].toLowerCase()}, ${D.bandLabel(p.state).toLowerCase()}`;
    tip.hidden = false;
    const left = (p.x / sx);
    const top = (p.y / sx);
    tip.style.left = `${Math.min(Math.max(0, left - tip.offsetWidth / 2), rect.width - tip.offsetWidth)}px`;
    tip.style.top = `${top > 60 ? top - tip.offsetHeight - 12 : top + 14}px`;
  };
  root.addEventListener("pointermove", show);
  root.addEventListener("pointerdown", show);
  root.addEventListener("pointerleave", () => { tip.hidden = true; focusRing.setAttribute("visibility", "hidden"); });
  box.replaceChildren(root, tip);
}

// Alerts ------------------------------------------------------------------------------

function alertFilters() {
  return {
    state: $("af-state").value, escalated: $("af-escalated").value, delivery_status: $("af-delivery").value,
    since: $("af-since").value, until: $("af-until").value,
  };
}

async function loadAlerts(reset) {
  const request = reset ? ++alertRequest : alertRequest;
  if (reset) alertCursor = null;
  $("alerts-more").disabled = true;
  try {
    const page = await D.api(D.alertsQuery(alertFilters(), alertCursor));
    if (request !== alertRequest) return;
    alertsLoaded = true;
    if (reset) $("alert-rows").replaceChildren();
    for (const a of page.items) {
      const c = D.alertCells(a);
      const tr = el("tr", "pick");
      const name = cell("", "Patient");
      const open = el("button", "link", c.name);
      open.type = "button";
      open.addEventListener("click", (event) => {
        event.stopPropagation();
        showTab("patients");
        openDetail(a.user_id).catch((err) => say(err.message, true));
      });
      name.append(open);
      tr.append(cell(c.when, "Reading taken"), name, cell(c.limit, "Limit"), stateCell(a.state), cell(c.reading, "Reading vs limit", "num"),
        cell(c.escalated, "Escalated"), cell(c.delivery, "Delivery"));
      $("alert-rows").append(tr);
    }
    alertCursor = page.next_cursor;
    $("alerts-more").hidden = !alertCursor;
    $("alerts-empty").hidden = $("alert-rows").children.length > 0;
  } finally {
    $("alerts-more").disabled = false;
  }
}

function showModelCard() {
  showTab("audit");
  const target = $("model-card-panel");
  target.scrollIntoView({ block: "start" });
  target.focus({ preventScroll: true });
}

// --- Page --------------------------------------------------------------------------

async function render() {
  const session = await window.DA.session();
  if (channel) {
    await window.DA.sb.removeChannel(channel);
    channel = null;
  }
  $("signed-out").hidden = Boolean(session);
  $("signed-in").hidden = !session;
  $("tabs").hidden = true;
  for (const [, panelId] of TABS) $(panelId).hidden = true;
  $("ov-tiles").replaceChildren();
  $("patient-rows").replaceChildren();
  $("alert-rows").replaceChildren();
  closeDetail();
  overviewLoaded = false;
  patientsLoaded = false;
  alertsLoaded = false;
  $("inbox").hidden = true;
  $("review").hidden = true;
  $("kb").hidden = true;
  $("audit").hidden = true;
  $("turns").replaceChildren();
  $("kb-drafts").replaceChildren();
  $("kb-approved").replaceChildren();
  reviewLoaded = false;
  kbLoaded = false;
  auditLoaded = false;
  if (!session) return;
  $("who").textContent = session.user.email;
  const me = await window.DA.api("/api/me");
  if (me.role !== "practitioner") {
    location.replace("/app/");
    return;
  }
  myName = me.name;
  $("tabs").hidden = false;
  showTab(activeTab);
  channel = await window.DA.listen(`handoffs:${session.user.id}`, "handoff", () => { refresh().catch(() => {}); });
  await loadList();
  schedulePoll();
}

async function init() {
  const { sb } = await window.DA.init();
  sb.auth.onAuthStateChange(() => { setTimeout(() => { render().catch((err) => say(err.message, true)); }, 0); });
  $("login-form").addEventListener("submit", guarded(async () => {
    const { error } = await sb.auth.signInWithPassword({ email: $("email").value, password: $("password").value });
    if (error) throw error;
  }));
  $("signout").addEventListener("click", guarded(async () => { await sb.auth.signOut(); }));
  $("enable").addEventListener("click", guarded(async () => {
    await window.DA.enableAlerts();
    say("Alerts are on: you will be notified about new handoffs on this device.");
  }));
  $("refresh").addEventListener("click", guarded(async () => {
    overviewLoaded = false;
    patientsLoaded = false;
    alertsLoaded = false;
    showTab(activeTab);
    await refresh();
  }));
  $("reply-form").addEventListener("submit", guarded(sendReply));
  $("resolve").addEventListener("click", guarded(resolveSelected));
  $("claim").addEventListener("click", guarded(claimSelected));
  for (const [tabId, panelId] of TABS) $(tabId).addEventListener("click", () => showTab(panelId));
  $("tabs").addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const at = TABS.findIndex(([, panelId]) => panelId === activeTab);
    const step = event.key === "ArrowRight" ? 1 : TABS.length - 1;
    const next = TABS[(at + step) % TABS.length];
    showTab(next[1]);
    $(next[0]).focus();
  });
  for (const b of document.querySelectorAll(".filters button")) b.addEventListener("click", guarded(() => setFilter(b)));
  $("more").addEventListener("click", guarded(() => loadTurns(false)));
  for (const node of document.querySelectorAll("[data-risk-label]")) node.textContent = window.DA.RISK_LABEL;
  for (const b of document.querySelectorAll("[data-model-card]")) b.addEventListener("click", showModelCard);
  for (const id of ["pf-band", "pf-breach", "pf-handoff"]) $(id).addEventListener("change", guarded(loadPatients));
  $("patient-filters").addEventListener("submit", (event) => event.preventDefault());
  $("pf-sort").addEventListener("change", () => { patientSort = { key: $("pf-sort").value, descending: false }; renderPatients(); });
  for (const th of document.querySelectorAll("#patient-table th[data-sort]")) {
    th.querySelector("button").addEventListener("click", () => setSort(th.dataset.sort));
  }
  $("patient-back").addEventListener("click", closeDetail);
  $("pd-days").addEventListener("change", guarded(() => (openPatient ? openDetail(openPatient) : null)));
  $("alert-filters").addEventListener("submit", guarded(() => {
    const { since, until } = alertFilters();
    if (since && until && since > until) throw new Error("The From date must be on or before the To date.");
    return loadAlerts(true);
  }));
  $("alerts-more").addEventListener("click", guarded(() => loadAlerts(false)));
  $("audit-range").addEventListener("submit", (event) => {
    event.preventDefault();
    const since = $("audit-since").value;
    const until = $("audit-until").value;
    if (since && until && since > until) {
      say("The From date must be on or before the To date.", true);
      return;
    }
    loadAudit(window.DA.auditQuery(since, until)).catch((err) => say(err.message, true));
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !$("inbox").hidden) refresh().catch(() => {});
  });
  await render();
}

init().catch((err) => say(err.message || String(err), true));
