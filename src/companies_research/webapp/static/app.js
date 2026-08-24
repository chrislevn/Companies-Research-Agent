/* Companies Research Agent — local UI.
   No build step and no dependencies: this file is served as-is. */

const TOKEN = document.querySelector('meta[name="cr-token"]').content;

let STATE = null;
let AUTHED = false;   // set by the auth gate before any app data loads
let PRESETS = [];
let VIEW = null;
let pollTimer = null;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// --- transport -------------------------------------------------------------

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-CR-Token": TOKEN,
        ...(options.headers || {}),
      },
    });
  } catch {
    throw new Error(t("err.network"));
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 409) throw new Error(t("err.busy"));
    throw new Error(payload.error || payload.detail || `Error ${response.status}`);
  }
  return payload;
}

const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

// --- helpers ---------------------------------------------------------------

function showError(el, message) {
  el.textContent = message;
  el.hidden = !message;
}

function busy(button, labelKey) {
  button.disabled = true;
  button.dataset.idle = button.textContent;
  button.textContent = t(labelKey);
  return () => {
    button.disabled = false;
    button.textContent = button.dataset.idle;
  };
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(LANG === "vi" ? "vi-VN" : undefined, {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// --- routing ---------------------------------------------------------------

const SETUP_STEPS = ["mailbox", "key", "seed"];

/* Whether a step is finished is a question about stored state, not about how
   far along the wizard you are standing — otherwise jumping back to step 2
   would award a tick to step 1. */
function stepDone(step) {
  if (step === "mailbox") return STATE.mailboxes.length > 0;
  if (step === "key") return STATE.anthropic.configured;
  if (step === "seed") return STATE.seeded;
  return false;
}

function nextSetupStep() {
  return SETUP_STEPS.find((step) => !stepDone(step)) || null;
}

/* VIEW is null for "wherever setup says I should be", or an explicit
   "settings" / "mailbox" once the user navigates deliberately. */
function render() {
  // The gate. Nothing else is reachable — not setup, not the dashboard —
  // until a session exists. Checked here as well as at boot so a 401 landing
  // mid-session (an expired cookie) drops straight back to the login form
  // rather than a half-rendered page.
  if (!AUTHED) {
    $("#view-auth").hidden = false;
    ["setup", "signin", "dashboard", "settings", "review"].forEach(
      (v) => { const n = $("#view-" + v); if (n) n.hidden = true; });
    $$(".topbar-actions [data-nav]").forEach((b) => { b.hidden = true; });
    $("#theme-toggle").hidden = false;   // theme + language stay usable pre-login
    return;
  }
  $("#view-auth").hidden = true;

  // Authenticated, but the first /api/state has not returned yet — right after
  // login, render() runs once before bootApp() populates STATE. Everything
  // below reads STATE, so hold on a blank frame; bootApp's refresh calls
  // render() again the moment the data lands.
  if (!STATE) {
    ["setup", "signin", "dashboard", "settings", "review"].forEach(
      (v) => { const n = $("#view-" + v); if (n) n.hidden = true; });
    return;
  }

  const pending = nextSetupStep();
  let view;
  let step = pending;

  if (VIEW === "settings") {
    view = "settings";
  } else if (VIEW === "review") {
    view = "review";
  } else if (SETUP_STEPS.includes(VIEW)) {
    view = "setup";
    step = VIEW;
  } else if (!STATE.mailboxes?.length) {
    // Nothing is connected yet, so there is no wizard worth showing — one
    // decision ("which mailbox?") does not need three numbered steps.
    view = "signin";
  } else {
    view = pending ? "setup" : "dashboard";
  }

  $("#view-signin").hidden = view !== "signin";
  $("#view-setup").hidden = view !== "setup";
  $("#view-dashboard").hidden = view !== "dashboard";
  $("#view-settings").hidden = view !== "settings";
  $("#view-review").hidden = view !== "review";
  // During first-run setup there is nowhere else to go yet.
  $$(".topbar-actions [data-nav]").forEach((b) => {
    // Sign out is available on every signed-in view, including the sign-in
    // wizard: someone who logged into the wrong account needs the way back.
    if (b.dataset.nav === "signout") { b.hidden = false; return; }
    b.hidden = view === "signin" || (!!pending && VIEW === null);
  });

  if (view === "setup") renderSetup(step);
  if (view === "dashboard") renderDashboard();
  if (view === "settings") renderSettings();
  if (view === "review") renderReview();
}

// --- setup -----------------------------------------------------------------

function renderSetup(step) {
  $$("#rail li").forEach((li) => {
    li.classList.toggle("active", li.dataset.step === step);
    li.classList.toggle("done", stepDone(li.dataset.step));
  });
  $("#card-mailbox").hidden = step !== "mailbox";
  $("#card-key").hidden = step !== "key";
  $("#card-seed").hidden = step !== "seed";
  renderMailboxList($("#mailbox-list"));
}

function renderPresets() {
  const select = $("#imap-preset");
  select.innerHTML = "";
  PRESETS.forEach((preset) => {
    select.appendChild(new Option(preset.name, preset.id));
  });
  onPresetChange();
}

function currentPreset() {
  return PRESETS.find((p) => p.id === $("#imap-preset").value) || PRESETS[PRESETS.length - 1];
}

function onPresetChange() {
  const preset = currentPreset();
  $("#imap-manual").hidden = Boolean(preset.host);
  $("#imap-port").value = preset.port || 993;

  const help = $("#apppass-help");
  const steps = $("#apppass-steps");
  steps.innerHTML = "";

  if (!preset.app_password_url) {
    steps.appendChild(el("li", null, t("mailbox.help.ask")));
    $("#apppass-link").hidden = true;
  } else {
    if (preset.needs_2fa) steps.appendChild(el("li", null, t("mailbox.help.2fa")));
    steps.appendChild(el("li", null, t("mailbox.help.generate")));
    steps.appendChild(el("li", null, t("mailbox.help.paste")));
    const link = $("#apppass-link");
    link.href = preset.app_password_url;
    link.hidden = false;
  }
  help.hidden = false;
}

function renderGoogleSteps() {
  const list = $("#google-steps");
  list.innerHTML = "";
  ["google.s1", "google.s2", "google.s3", "google.s4", "google.s5", "google.s6"].forEach((key) => {
    list.appendChild(el("li", null, t(key)));
  });
  const link = el("a", "btn small", t("google.open"));
  link.href = "https://console.cloud.google.com/projectcreate";
  link.target = "_blank";
  link.rel = "noopener";
  const item = el("li");
  item.appendChild(link);
  list.appendChild(item);
  $("#google-file-ok").hidden = !STATE.google_client_ready;
}

function renderMailboxList(container) {
  container.innerHTML = "";
  STATE.mailboxes.forEach((mailbox) => {
    const row = el("li", "mailbox");
    row.appendChild(el("span", "mailbox-icon", mailbox.provider === "gmail" ? "✉︎" : "✉"));

    const info = el("div", "mailbox-info");
    info.appendChild(el("strong", null, mailbox.email || mailbox.account_id));
    info.appendChild(el("small", "muted", mailbox.label || mailbox.provider));
    row.appendChild(info);

    const status = el("span", "mailbox-status");
    row.appendChild(status);

    const test = el("button", "btn tiny", t("mailbox.check"));
    test.onclick = async () => {
      const restore = busy(test, "mailbox.checking");
      try {
        await post(`/api/mailboxes/${encodeURIComponent(mailbox.account_id)}/check`);
        status.textContent = "✓ " + t("mailbox.working");
        status.className = "mailbox-status ok";
      } catch (err) {
        status.textContent = err.message;
        status.className = "mailbox-status bad";
      } finally {
        restore();
      }
    };
    row.appendChild(test);

    const remove = el("button", "btn tiny danger", t("mailbox.remove"));
    remove.onclick = async () => {
      if (!confirm(t("mailbox.removeConfirm"))) return;
      await api(`/api/mailboxes/${encodeURIComponent(mailbox.account_id)}`, { method: "DELETE" });
      await refresh();
    };
    row.appendChild(remove);

    container.appendChild(row);
  });
}

// --- dashboard -------------------------------------------------------------

function renderWatchStatus() {
  const line = $("#watch-status");
  if (!STATE.seeded) { line.textContent = t("watch.setup"); return; }
  if (!STATE.settings.watch_enabled) { line.textContent = t("watch.off"); return; }
  const when = formatDate(STATE.last_scan_at);
  line.textContent = when ? t("watch.on", { when }) : t("watch.onNever");
}

async function renderDashboard() {
  $("#dash-sub").textContent = t("dash.sub");
  renderWatchStatus();

  const stats = $("#stats");
  stats.innerHTML = "";
  const entries = [
    [STATE.known_senders, t("stats.known")],
    [STATE.processed, t("stats.checked")],
    [formatDate(STATE.last_scan_at) || t("stats.never"), t("stats.lastScan")],
  ];
  entries.forEach(([value, label]) => {
    const box = el("div", "stat");
    box.appendChild(el("strong", null, String(value)));
    box.appendChild(el("span", "muted", label));
    stats.appendChild(box);
  });

  const container = $("#leads");
  container.innerHTML = "";
  let leads = [];
  try {
    leads = (await api("/api/leads")).leads;
  } catch (err) {
    showError($("#dash-error"), err.message);
    return;
  }

  if (!leads.length) {
    const empty = el("div", "empty");
    empty.appendChild(el("p", "empty-title", t("leads.empty")));
    empty.appendChild(el("p", "muted", t("leads.emptyHint")));
    container.appendChild(empty);
    return;
  }
  leads.forEach((lead) => container.appendChild(leadCard(lead)));
}

function leadCard(lead) {
  const triage = lead.triage || {};
  const card = el("article", "lead");

  const head = el("header", "lead-head");
  const title = el("div");
  title.appendChild(el("h3", null, triage.company_name || lead.sender_email));
  const meta = el("p", "muted small");
  meta.textContent = [lead.sender_email, formatDate(lead.received_at)].filter(Boolean).join(" · ");
  title.appendChild(meta);
  head.appendChild(title);

  const tags = el("div", "tags");
  tags.appendChild(el("span", "tag rel-" + (triage.relationship || "unknown"),
    t("rel." + (triage.relationship || "unknown"))));
  if (triage.mentions_meeting) tags.appendChild(el("span", "tag meeting", t("lead.meeting")));
  if ((triage.confidence ?? 1) < 0.5) tags.appendChild(el("span", "tag unsure", t("lead.unsure")));
  head.appendChild(tags);
  card.appendChild(head);

  card.appendChild(el("p", "subject", lead.subject));

  if (triage.intent_summary) {
    const row = el("p", "kv");
    row.appendChild(el("span", "k", t("lead.intent")));
    row.appendChild(el("span", "v", triage.intent_summary));
    card.appendChild(row);
  }
  if (triage.contact_name || triage.contact_title) {
    const row = el("p", "kv");
    row.appendChild(el("span", "k", t("lead.contact")));
    row.appendChild(el("span", "v",
      [triage.contact_name, triage.contact_title].filter(Boolean).join(" — ")));
    card.appendChild(row);
  }

  if (lead.research) card.appendChild(researchBlock(lead));

  if (lead.provider === "gmail" && lead.thread_id) {
    const link = el("a", "btn tiny", t("lead.open"));
    link.href = `https://mail.google.com/mail/u/0/#all/${lead.thread_id}`;
    link.target = "_blank";
    link.rel = "noopener";
    card.appendChild(link);
  }
  return card;
}

// Step 2 output. Collapsed by default: the brief is long, and the point of the
// list is to scan many leads quickly and open the one that matters.
function researchBlock(lead) {
  const r = lead.research;
  const box = el("details", "research");
  const summary = el("summary");
  summary.appendChild(el("span", "tag research-tag", t("lead.research")));
  summary.appendChild(el("span", "muted small",
    [r.one_liner || r.industry, r.news?.length ? t("lead.newsCount", { n: r.news.length }) : ""]
      .filter(Boolean).join(" · ")));
  box.appendChild(summary);

  const body = el("div", "research-body");

  if (r.description) body.appendChild(el("p", null, r.description));

  const facts = [
    [t("lead.industry"), r.industry],
    [t("lead.hq"), r.hq_location],
    [t("lead.size"), r.size_estimate],
    [t("lead.founded"), r.founded],
    [t("lead.products"), (r.products || []).join(", ")],
  ].filter(([, v]) => v);
  facts.forEach(([k, v]) => {
    const row = el("p", "kv");
    row.appendChild(el("span", "k", k));
    row.appendChild(el("span", "v", v));
    body.appendChild(row);
  });

  if (r.news?.length) {
    body.appendChild(el("h4", null, t("lead.news")));
    const list = el("ul", "news");
    r.news.forEach((item) => {
      const li = el("li");
      if (item.url) {
        const a = el("a", null, item.title);
        a.href = item.url;
        a.target = "_blank";
        a.rel = "noopener";
        li.appendChild(a);
      } else {
        li.appendChild(el("span", null, item.title));
      }
      if (item.published) li.appendChild(el("span", "muted small", ` ${item.published}`));
      if (item.summary) li.appendChild(el("p", "muted small", item.summary));
      list.appendChild(li);
    });
    body.appendChild(list);
  }

  if (r.meeting_prep?.length) {
    body.appendChild(el("h4", null, t("lead.prep")));
    const list = el("ul", "prep");
    r.meeting_prep.forEach((p) => list.appendChild(el("li", null, p)));
    body.appendChild(list);
  }

  if (r.notes) {
    const note = el("p", "muted small note");
    note.textContent = r.notes;
    body.appendChild(note);
  }

  // Sources are what make the brief checkable rather than merely plausible.
  if (r.sources?.length) {
    body.appendChild(el("h4", null, t("lead.sources")));
    const list = el("ul", "sources");
    r.sources.forEach((url) => {
      const li = el("li");
      const a = el("a", "small", url.replace(/^https?:\/\//, "").slice(0, 70));
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener";
      li.appendChild(a);
      list.appendChild(li);
    });
    body.appendChild(list);
  }

  const foot = el("p", "muted small");
  foot.textContent = t("lead.researchedAt", {
    when: formatDate(lead.researched_at),
    conf: (r.confidence ?? 0).toFixed(2),
  });
  body.appendChild(foot);

  box.appendChild(body);
  return box;
}

// --- review (step 5) -------------------------------------------------------

/* Designed for someone tired at the end of the day. Three rules:
   the brief and its sources are visible together, everything doubtful is
   flagged rather than smoothed over, and the recipient is confirmed in its own
   block — because the recipient is exactly what an injected instruction is
   trying to change. */
/* Renders are serialised by generation, not by a lock.

   The list is emptied before the fetch and filled after it, so two overlapping
   calls — a tab click landing while the initial load is still in flight —
   both clear and then both append, and the panel shows every brief twice. It
   looks like a data bug and is not one. Each call takes a ticket; only the
   newest may write. */
let reviewGeneration = 0;

async function renderReview() {
  const generation = ++reviewGeneration;
  const list = $("#review-list");
  const banner = $("#delivery-banner");
  list.textContent = "";
  $("#review-error").hidden = true;

  let payload;
  try {
    payload = await api("/api/briefs");
  } catch (err) {
    if (generation !== reviewGeneration) return;
    $("#review-error").textContent = err.message;
    $("#review-error").hidden = false;
    return;
  }
  // A newer render started while this one was waiting; it owns the list now.
  if (generation !== reviewGeneration) return;

  const d = payload.delivery || {};
  banner.className = "delivery-banner " + (d.leaves_machine ? "warn" : "safe");
  banner.textContent = d.error
    ? t("review.deliveryBroken", { error: d.error })
    : d.leaves_machine
      ? t("review.deliveryLeaves", { provider: d.describes_as })
      : t("review.deliveryLocal", { provider: d.describes_as });
  if (!d.scope_granted) {
    banner.textContent += "  " + t("review.scopeOff");
  }

  const briefs = payload.briefs || [];
  const drafts = briefs.filter((b) => b.status === "draft");
  $("#review-sub").textContent = t("review.sub", {
    drafts: drafts.length, total: briefs.length,
  });

  if (!briefs.length) {
    const empty = el("div", "empty");
    empty.appendChild(el("p", "empty-title", t("review.empty")));
    empty.appendChild(el("p", "muted", t("review.emptyHint")));
    list.appendChild(empty);
    return;
  }
  briefs.forEach((b) => list.appendChild(briefCard(b, d)));
}

function briefCard(brief, delivery) {
  const card = el("article", "brief-card status-" + brief.status);

  const head = el("header", "brief-head");
  const title = el("div");
  title.appendChild(el("h3", null, brief.company || brief.domain));
  title.appendChild(el("p", "muted small",
    [brief.domain, formatDate(brief.generated_at)].filter(Boolean).join(" · ")));
  head.appendChild(title);

  const tags = el("div", "tags");
  tags.appendChild(el("span", "tag status-tag", t("review.status." + brief.status)));
  if (brief.unverified_count) {
    tags.appendChild(el("span", "tag unsure",
      t("review.unverified", { n: brief.unverified_count })));
  }
  if (brief.meeting) tags.appendChild(el("span", "tag meeting", t("lead.meeting")));
  head.appendChild(tags);
  card.appendChild(head);

  /* Brief on the left, sources on the right: verification is one glance and
     one click away, not a scroll to the bottom of the document. */
  const split = el("div", "brief-split");
  const doc = el("div", "brief-doc");
  doc.innerHTML = brief.html;          // server-rendered, every value escaped
  split.appendChild(doc);

  const side = el("aside", "brief-side");
  if (brief.unknowns?.length) {
    side.appendChild(el("h4", null, t("review.gaps")));
    const gaps = el("ul", "gaps");
    brief.unknowns.forEach((g) => gaps.appendChild(el("li", null, g)));
    side.appendChild(gaps);
  }
  side.appendChild(el("h4", null, t("review.sources", { n: brief.sources.length })));
  const sources = el("ul", "sources");
  (brief.sources || []).forEach((url) => {
    const li = el("li");
    const a = el("a", "small", url.replace(/^https?:\/\//, "").slice(0, 46));
    a.href = url; a.target = "_blank"; a.rel = "noopener";
    li.appendChild(a);
    sources.appendChild(li);
  });
  side.appendChild(sources);
  split.appendChild(side);
  card.appendChild(split);

  card.appendChild(pdfBlock(brief));

  if (brief.status !== "draft") {
    const done = el("p", "muted small");
    done.textContent = t("review.decided", {
      status: t("review.status." + brief.status),
      who: brief.approved_by || "—",
      when: formatDate(brief.approved_at),
    });
    card.appendChild(done);

    /* A decision can be withdrawn; a delivery cannot. The button is simply
       absent once the brief has been delivered, rather than present and
       failing — an action you can click and cannot perform teaches people to
       distrust the whole panel. */
    if (brief.status !== "delivered") {
      const undo = el("div", "approve-actions");
      const button = el("button", "btn ghost", t("review.unapprove"));
      const note = el("span", "muted small");
      button.type = "button";
      button.onclick = async () => {
        button.disabled = true;
        try {
          await api(`/api/briefs/${brief.id}/unapprove`, {
            method: "POST",
            // No name field, exactly as the approve call has none: the
            // server stamps "operator". One identity story, not two.
            body: JSON.stringify({}),
          });
          note.textContent = t("review.unapproveDone");
          await renderReview();
        } catch (error) {
          note.textContent = error.message;
          button.disabled = false;
        }
      };
      undo.appendChild(button);
      undo.appendChild(note);
      card.appendChild(undo);
    }
    return card;
  }

  card.appendChild(approvalBlock(brief, delivery));
  return card;
}

/* The document, shown before it is approved.

   Approving something you cannot see is not review, it is a rubber stamp — so
   the PDF is the *same bytes* the delivery path sends, fetched from the same
   endpoint, not a re-render that could drift from it.

   It is fetched as a blob rather than pointed at with `<iframe src>`, because
   a frame cannot send the run token as a header and the alternative is putting
   the token in a URL, where it lands in history and in every log that records
   paths. Loaded on demand: a page of briefs should not render a page of PDFs
   nobody asked for. */
function pdfBlock(brief) {
  const box = el("div", "pdf-block");
  const bar = el("div", "pdf-actions");
  const toggle = el("button", "btn ghost", t("review.preview"));
  const download = el("button", "btn ghost", t("review.download"));
  const word = el("button", "btn ghost", t("review.downloadWord"));
  toggle.type = download.type = word.type = "button";
  bar.appendChild(toggle);
  bar.appendChild(download);
  bar.appendChild(word);
  box.appendChild(bar);

  const status = el("span", "muted small");
  bar.appendChild(status);

  const frame = el("iframe", "pdf-frame");
  frame.hidden = true;
  frame.title = brief.company || "brief";
  box.appendChild(frame);

  let objectUrl = null;
  let pending = null;

  async function bytes() {
    if (objectUrl) return objectUrl;
    if (pending) return pending;
    pending = (async () => {
      const response = await fetch(`/api/briefs/${brief.id}/pdf`, {
        headers: { "X-CR-Token": TOKEN },
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(detail.detail || response.statusText);
      }
      objectUrl = URL.createObjectURL(await response.blob());
      return objectUrl;
    })();
    try {
      return await pending;
    } finally {
      pending = null;
    }
  }

  toggle.onclick = async () => {
    if (!frame.hidden) {
      frame.hidden = true;
      toggle.textContent = t("review.preview");
      return;
    }
    status.textContent = t("review.previewLoading");
    try {
      frame.src = await bytes();
      frame.hidden = false;
      toggle.textContent = t("review.previewHide");
      status.textContent = t("review.previewNote");
    } catch (error) {
      status.textContent = t("review.previewError", { error: error.message });
    }
  };

  /* Word is fetched fresh each time rather than cached like the PDF: it is the
     format people edit, so a stale copy is a copy of someone else's edits. */
  word.onclick = async () => {
    status.textContent = t("review.previewLoading");
    try {
      const response = await fetch(`/api/briefs/${brief.id}/docx`, {
        headers: { "X-CR-Token": TOKEN },
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(detail.detail || response.statusText);
      }
      const href = URL.createObjectURL(await response.blob());
      const link = el("a");
      link.href = href;
      link.download = `${(brief.company || "brief").replace(/[^\w.-]+/g, "-")}.docx`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(href);
      status.textContent = "";
    } catch (error) {
      status.textContent = t("review.previewError", { error: error.message });
    }
  };

  download.onclick = async () => {
    status.textContent = t("review.previewLoading");
    try {
      const href = await bytes();
      const link = el("a");
      link.href = href;
      link.download = `${(brief.company || "brief").replace(/[^\w.-]+/g, "-")}.pdf`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      status.textContent = "";
    } catch (error) {
      status.textContent = t("review.previewError", { error: error.message });
    }
  };

  return box;
}

/* The recipient gets its own confirmed block. It is the single field an
   injected instruction most wants to change, so it is never pre-filled with
   anything the model produced — only with addresses the operator allow-listed. */
function approvalBlock(brief, delivery) {
  const box = el("form", "approve-block");
  box.appendChild(el("h4", null, t("review.sendTo")));

  const allowed = delivery.allowed_recipients || [];
  const select = el("select");
  select.id = "to-" + brief.id;
  if (!allowed.length) {
    const opt = el("option", null, t("review.noRecipients"));
    opt.value = ""; select.appendChild(opt); select.disabled = true;
  }
  allowed.forEach((addr) => {
    const opt = el("option", null, addr);
    opt.value = addr;
    select.appendChild(opt);
  });
  box.appendChild(select);
  box.appendChild(el("p", "muted small", t("review.allowlistNote")));

  const note = el("textarea");
  note.placeholder = t("review.notePlaceholder");
  note.rows = 2;
  box.appendChild(note);

  const actions = el("div", "approve-actions");
  const approve = el("button", "btn primary", t("review.approve"));
  const reject = el("button", "btn ghost", t("review.reject"));
  approve.type = reject.type = "button";
  const result = el("p", "muted small");

  approve.onclick = async () => {
    approve.disabled = reject.disabled = true;
    result.textContent = t("review.working");
    try {
      const out = await api(`/api/briefs/${brief.id}/approve`, {
        method: "POST",
        body: JSON.stringify({ recipient: select.value, note: note.value }),
      });
      result.className = out.delivered ? "ok small" : "error small";
      result.textContent = out.delivered
        ? t("review.delivered", { where: out.destination })
        : t("review.approvedNotSent", { error: out.error });
      setTimeout(renderReview, 1200);
    } catch (err) {
      result.className = "error small";
      result.textContent = err.message;
      approve.disabled = reject.disabled = false;
    }
  };

  reject.onclick = async () => {
    reject.disabled = approve.disabled = true;
    try {
      await api(`/api/briefs/${brief.id}/reject`, {
        method: "POST", body: JSON.stringify({ reason: note.value }),
      });
      renderReview();
    } catch (err) {
      result.className = "error small";
      result.textContent = err.message;
      reject.disabled = approve.disabled = false;
    }
  };

  actions.appendChild(approve);
  actions.appendChild(reject);
  box.appendChild(actions);
  box.appendChild(result);
  return box;
}

// --- settings --------------------------------------------------------------

const asList = (v) => (v || "").split(",").map((x) => x.trim()).filter(Boolean);

async function renderOrgProfile() {
  let profile;
  try {
    profile = (await api("/api/profile")).profile;
  } catch {
    return;                       // the rest of Settings still works
  }
  $("#org-name").value = profile.name || "";
  $("#org-domain").value = profile.domain || "";
  $("#org-what").value = profile.what_we_do || "";
  $("#org-icp").value = profile.ideal_customer || "";
  $("#org-industries").value = (profile.target_industries || []).join(", ");
  $("#org-regions").value = (profile.target_regions || []).join(", ");
  $("#org-sizes").value = (profile.target_company_sizes || []).join(", ");
  $("#org-never").value = (profile.not_interested_in || []).join(", ");
  $("#org-criteria").value = profile.research_criteria || "";
}

async function saveOrgProfile(button) {
  const restore = busy(button, "org.saving");
  showError($("#org-error"), "");
  try {
    const out = await api("/api/profile", {
      method: "POST",
      body: JSON.stringify({
        profile: {
          name: $("#org-name").value.trim(),
          domain: $("#org-domain").value.trim(),
          what_we_do: $("#org-what").value.trim(),
          ideal_customer: $("#org-icp").value.trim(),
          target_industries: asList($("#org-industries").value),
          target_regions: asList($("#org-regions").value),
          target_company_sizes: asList($("#org-sizes").value),
          not_interested_in: asList($("#org-never").value),
          research_criteria: $("#org-criteria").value.trim(),
        },
      }),
    });
    // Say which behaviour just changed, not merely that a write happened.
    $("#org-status").textContent = out.configured
      ? t("org.saved") : t("org.savedEmpty");
  } catch (err) {
    showError($("#org-error"), err.message);
  } finally {
    restore();
  }
}

/* Consequences of a permission, in the operator's terms. The gate enforces
   scope names; a person deciding whether to grant one needs to know what it
   lets happen. */
const SCOPE_COPY = {
  "mail:read":     { key: "scope.mail",     danger: false },
  "research:read": { key: "scope.research", danger: false },
  "calendar:read": { key: "scope.calendar", danger: false },
  "memory:write":  { key: "scope.memory",   danger: false },
  "brief:deliver": { key: "scope.deliver",  danger: true  },
};

function renderScopes() {
  const box = $("#scope-list");
  box.textContent = "";
  const granted = new Set(STATE.settings.tool_scopes || []);
  /* Deliberate order, not the API's alphabetical one: the everyday read
     permissions first, then the single one that can send something out. A list
     that opens with the dangerous item reads as a warning about all of them. */
  const known = Object.keys(SCOPE_COPY);
  const all = STATE.settings.all_tool_scopes || [];
  const ordered = [...known.filter((k) => all.includes(k)),
                   ...all.filter((k) => !known.includes(k))];
  ordered.forEach((scope) => {
    const copy = SCOPE_COPY[scope] || { key: scope, danger: false };
    const row = el("label", "check" + (copy.danger ? " danger" : ""));
    const input = el("input");
    input.type = "checkbox";
    input.dataset.scope = scope;
    input.checked = granted.has(scope);
    input.onchange = () => {
      $("#deliver-fields").hidden = !$('#scope-list input[data-scope="brief:deliver"]').checked;
    };
    row.appendChild(input);
    const label = el("span");
    label.appendChild(el("strong", null, t(copy.key)));
    label.appendChild(el("span", "muted", " — " + t(copy.key + ".note")));
    row.appendChild(label);
    box.appendChild(row);
  });
  $("#deliver-fields").hidden = !granted.has("brief:deliver");
  $("#allowed-recipients").value = (STATE.settings.allowed_recipients || []).join(", ");
  $("#delivery-provider").value = STATE.settings.delivery_provider || "file";
  $("#delivery-account").value = STATE.settings.delivery_account || "";
  $("#delivery-account-field").hidden = $("#delivery-provider").value !== "gmail_send";
}

async function savePermissions(button) {
  const restore = busy(button, "org.saving");
  showError($("#perms-error"), "");
  try {
    const scopes = $$("#scope-list input[data-scope]")
      .filter((i) => i.checked).map((i) => i.dataset.scope);
    await post("/api/settings", {
      tool_scopes: scopes,
      allowed_recipients: $("#allowed-recipients").value,
      delivery_provider: $("#delivery-provider").value,
      delivery_account: $("#delivery-account").value.trim(),
    });
    $("#perms-status").textContent = scopes.includes("brief:deliver")
      ? t("cfg.perms.savedSending") : t("cfg.perms.savedLocal");
    await refresh();
  } catch (err) {
    showError($("#perms-error"), err.message);
  } finally {
    restore();
  }
}

let PROMPTS = null;
let PROMPT_TAB = "triage";

async function renderPrompts() {
  try {
    PROMPTS = (await api("/api/prompts")).prompts;
  } catch {
    return;
  }
  const current = PROMPTS[PROMPT_TAB];
  $("#prompt-text").value = current.text;
  $("#prompt-source").textContent = current.customised
    ? t("cfg.prompts.custom") : t("cfg.prompts.builtin");
  $$("#prompt-tabs .tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.prompt === PROMPT_TAB));
}

async function savePrompt(button, { reset = false } = {}) {
  const restore = busy(button, "org.saving");
  showError($("#prompt-error"), "");
  try {
    const out = await api(`/api/prompts/${PROMPT_TAB}`, {
      method: "POST",
      body: JSON.stringify({ text: reset ? "" : $("#prompt-text").value }),
    });
    $("#prompt-status").textContent = out.redacted
      ? t("cfg.prompts.redacted")
      : out.customised ? t("cfg.prompts.saved") : t("cfg.prompts.restored");
    await renderPrompts();
  } catch (err) {
    showError($("#prompt-error"), err.message);
  } finally {
    restore();
  }
}

function renderSystemConfig() {
  const s = STATE.settings;
  const local = s.triage_backend === "ollama";
  $("#backend-anthropic").checked = !local;
  $("#backend-ollama").checked = local;
  $("#ollama-fields").hidden = !local;
  $("#ollama-model").value = s.ollama_model || "";
  $("#ollama-host").value = s.ollama_host || "";
  $("#ollama-status").textContent = s.ollama_reachable
    ? t("cfg.ollama.found") : t("cfg.ollama.missing");
  $("#ollama-status").className = "muted small " + (s.ollama_reachable ? "ok" : "warn-text");

  $("#research-enabled").checked = s.research_enabled;
  $("#research-effort").value = s.research_effort || "medium";
  $("#research-searches").value = s.research_max_searches;
  $("#research-companies").value = s.research_max_companies;
  $("#research-ttl").value = s.research_ttl_days;
  $("#calendar-enabled").checked = s.calendar_enabled;
  $("#calendar-days").value = s.calendar_lookahead_days;
  renderScopes();
}

/* Settings is eleven cards. Shown at once it is a scroll nobody reads to the
   end of; grouped, each screen answers one question. The group lives in the
   URL after the view — #settings/permissions — so a specific screen can be
   linked to and survives a reload. */
let SETTINGS_GROUP = "connection";

function showSettingsGroup(group) {
  const groups = $$(".settings-group").map((g) => g.dataset.group);
  SETTINGS_GROUP = groups.includes(group) ? group : groups[0];
  $$(".settings-group").forEach((g) => {
    g.hidden = g.dataset.group !== SETTINGS_GROUP;
  });
  $$("#settings-nav .tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.group === SETTINGS_GROUP);
  });
}

function renderSettings() {
  showSettingsGroup(SETTINGS_GROUP);
  renderOrgProfile();
  renderSystemConfig();
  renderPrompts();
  renderMailboxList($("#settings-mailboxes"));
  $("#key-masked").textContent = STATE.anthropic.configured
    ? `${t("settings.keySet")} ${STATE.anthropic.masked}`
    : t("settings.keyNone");
  $("#ignored").value = STATE.settings.ignored_domains.join(", ");
  $("#scan-days").value = String(STATE.settings.scan_days);
  $("#model").value = STATE.settings.model;
  $("#watch-enabled").checked = STATE.settings.watch_enabled;
  $("#watch-interval").value = String(STATE.settings.watch_interval_minutes);
}

// --- jobs ------------------------------------------------------------------

/* A disclosure that opens onto nothing reads as a broken page, and a job that
   fails before it ever starts has no log to show — so the control only exists
   when there is something behind it. */
function setJobLog(lines) {
  const pane = $("#job-log");
  pane.textContent = (lines || []).join("\n");
  $("#job-details").hidden = !pane.textContent;
  pane.scrollTop = pane.scrollHeight;
}

function openOverlay(titleKey) {
  $("#job-title").textContent = t(titleKey);
  $("#job-phase").textContent = "";
  setJobLog([]);
  $("#job-result").innerHTML = "";
  $("#job-spinner").hidden = false;
  $("#job-close").hidden = true;
  $("#job-cancel").hidden = true;   // shown once the job says it can be stopped
  $("#overlay").hidden = false;
}

function closeOverlay() {
  $("#overlay").hidden = true;
  clearTimeout(pollTimer);
}

async function runJob(path, body, titleKey, describe) {
  openOverlay(titleKey);
  let job;
  try {
    job = await post(path, body);
  } catch (err) {
    await finishJob({ status: "error", error: err.message });
    return;
  }
  return poll(job.id, describe);
}

/* Resolves with the finished job when it leaves the running state, so callers
   can keep their button disabled for the whole run — and, where one step feeds
   the next, read what the first one produced. */
function poll(jobId, describe) {
  return new Promise((resolve) => {
    const tick = async () => {
      let job;
      try {
        job = await api(`/api/jobs/${jobId}`);
      } catch (err) {
        await finishJob({ status: "error", error: err.message });
        return resolve(null);
      }
      $("#job-phase").textContent = job.phase;
      setJobLog(job.lines);

      // Only offered while the job has told us how it can be stopped — a scan
      // halfway through a mailbox has not.
      const cancel = $("#job-cancel");
      cancel.hidden = !job.cancellable;
      cancel.onclick = async () => {
        cancel.disabled = true;
        cancel.textContent = t("job.cancelling");
        try {
          await post(`/api/jobs/${jobId}/cancel`);
        } catch (err) {
          showError($("#dash-error"), err.message);
        }
      };

      if (job.status === "running") {
        pollTimer = setTimeout(tick, 800);
        return;
      }
      await finishJob(job, describe);
      // The result, not just "it stopped": a failed job resolves to null so a
      // caller chaining a second step does not run it on a failure.
      resolve(job.status === "done" ? (job.result ?? {}) : null);
    };
    clearTimeout(pollTimer);
    pollTimer = setTimeout(tick, 600);
  });
}

async function finishJob(job, describe) {
  $("#job-spinner").hidden = true;
  $("#job-close").hidden = false;
  const cancel = $("#job-cancel");
  cancel.hidden = true;
  cancel.disabled = false;
  cancel.textContent = t("job.cancel");
  const result = $("#job-result");
  result.innerHTML = "";
  setJobLog(job.lines);

  if (job.status === "cancelled") {
    $("#job-title").textContent = t("job.cancelled");
    result.appendChild(el("p", "muted", t("job.cancelledNote")));
    await refresh();
    return;
  }

  if (job.status === "error") {
    $("#job-title").textContent = t("job.failed");
    result.appendChild(el("p", "error", job.error));
    // Only worth expanding if the job got far enough to record anything.
    if (!$("#job-details").hidden) $("#job-details").open = true;
    return;
  }
  $("#job-title").textContent = t("job.done");
  result.appendChild(el("p", "big-result", describe ? describe(job.result) : ""));
  await refresh();
}

// --- wiring ----------------------------------------------------------------

async function refresh() {
  STATE = await api("/api/state");
  render();
}

function wire() {
  $("#lang-toggle").onclick = () => {
    setLang(LANG === "en" ? "vi" : "en");
    renderPresets();
    renderGoogleSteps();
    render();
  };

  $("#org-save").onclick = (e) => saveOrgProfile(e.currentTarget);
  $("#perms-save").onclick = (e) => savePermissions(e.currentTarget);
  $("#prompt-save").onclick = (e) => savePrompt(e.currentTarget);
  $("#prompt-reset").onclick = (e) => savePrompt(e.currentTarget, { reset: true });
  $("#delivery-provider").onchange = (e) => {
    $("#delivery-account-field").hidden = e.target.value !== "gmail_send";
  };
  $$("#prompt-tabs .tab").forEach((tab) => {
    tab.onclick = () => { PROMPT_TAB = tab.dataset.prompt; renderPrompts(); };
  });
  $$('input[name="backend"]').forEach((radio) => {
    radio.onchange = async () => {
      $("#ollama-fields").hidden = radio.value !== "ollama";
      if (radio.value === STATE.settings.triage_backend) return;   // nothing chosen
      await post("/api/settings", { triage_backend: radio.value });
      await refresh();
    };
  });
  /* These save on change rather than behind a button: they are single settings
     with immediate meaning, and a Save button for one number is a step that
     exists only to be forgotten. */
  const auto = {
    "#ollama-model": (v) => ({ ollama_model: v }),
    "#ollama-host": (v) => ({ ollama_host: v }),
    "#research-enabled": (_, el) => ({ research_enabled: el.checked }),
    "#research-effort": (v) => ({ research_effort: v }),
    "#research-searches": (v) => ({ research_max_searches: Number(v) }),
    "#research-companies": (v) => ({ research_max_companies: Number(v) }),
    "#research-ttl": (v) => ({ research_ttl_days: Number(v) }),
    "#calendar-enabled": (_, el) => ({ calendar_enabled: el.checked }),
    "#calendar-days": (v) => ({ calendar_lookahead_days: Number(v) }),
  };
  Object.entries(auto).forEach(([sel, build]) => {
    const node = $(sel);
    if (!node) return;
    node.onchange = async () => {
      const body = build(node.value.trim(), node);
      const [key, value] = Object.entries(body)[0];
      // Only write when the value actually differs from what is stored. A
      // handler that fires on render rather than on intent is how a setting
      // changes without anybody having chosen it.
      if (STATE.settings[key] === value) return;
      await post("/api/settings", body);
      await refresh();
    };
  });

  /* The view lives in the URL so a screen can be linked to, reloaded without
     losing your place, and reached directly — which also makes it reachable
     from a script, so the layout can be checked in a real browser. */
  const NAMED_VIEWS = ["settings", "review"];

  function readHash() {
    const [name, group] = (location.hash || "").replace(/^#/, "").split("/");
    if (group) SETTINGS_GROUP = group;
    return NAMED_VIEWS.includes(name) || SETUP_STEPS.includes(name) ? name : null;
  }

  window.addEventListener("hashchange", () => { VIEW = readHash(); render(); });
  if (readHash()) VIEW = readHash();

  $$("#settings-nav .tab").forEach((tab) => {
    tab.onclick = () => {
      showSettingsGroup(tab.dataset.group);
      location.hash = `settings/${tab.dataset.group}`;
      // Long groups otherwise open part-scrolled, at wherever the last one ended.
      window.scrollTo({ top: 0, behavior: "instant" });
    };
  });

  $$(".topbar-actions [data-nav]").forEach((button) => {
    button.onclick = () => {
      const target = button.dataset.nav;
      if (target === "signout") { signOut(); return; }
      VIEW = NAMED_VIEWS.includes(target) ? target : null;
      location.hash = VIEW === "settings" ? `settings/${SETTINGS_GROUP}` : (VIEW || "");
      render();
    };
  });

  /* The three steps are a table of contents, not a one-way road. The API key
     does not depend on having a mailbox, so there is no reason to make people
     connect one before they can paste it — and any step can be revisited. */
  $$("#rail li").forEach((li) => {
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    const open = () => { VIEW = li.dataset.step; render(); };
    li.onclick = open;
    li.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
    };
  });

  /* Scoped to its own container. As a bare `.tab` selector this claimed every
     tab on the page — including the settings nav and the prompt switcher — and
     because it is wired last, `.onclick =` silently replaced their handlers.
     The underline still moved, so it looked like it worked while every section
     showed the same content. */
  const mailboxTabs = $$("#mailbox-tabs .tab");
  mailboxTabs.forEach((tab) => {
    tab.onclick = () => {
      mailboxTabs.forEach((other) => other.classList.toggle("active", other === tab));
      $$(".tabpanel").forEach((panel) => { panel.hidden = panel.dataset.panel !== tab.dataset.tab; });
    };
  });

  $("#imap-preset").onchange = onPresetChange;
  $("#imap-email").oninput = (event) => {
    // Pick the matching provider automatically — one less thing to answer.
    const domain = (event.target.value.split("@")[1] || "").toLowerCase();
    if (!domain) return;
    const match = PRESETS.find((p) => p.domains.includes(domain));
    if (match && match.id !== $("#imap-preset").value) {
      $("#imap-preset").value = match.id;
      onPresetChange();
    }
  };

  $("#imap-connect").onclick = async () => {
    showError($("#imap-error"), "");
    const restore = busy($("#imap-connect"), "mailbox.connecting");
    try {
      await post("/api/mailboxes/imap", {
        email: $("#imap-email").value,
        password: $("#imap-password").value,
        host: $("#imap-host").value,
        port: Number($("#imap-port").value) || 993,
      });
      $("#imap-password").value = "";
      await refresh();
    } catch (err) {
      showError($("#imap-error"), err.message);
    } finally {
      restore();
    }
  };

  // Google client-secret upload
  const drop = $("#drop");
  const readFile = async (file) => {
    showError($("#google-error"), "");
    try {
      const content = await file.text();
      await post("/api/mailboxes/google/client-secret", { content });
      $("#google-file-ok").hidden = false;
      await refresh();
    } catch (err) {
      showError($("#google-error"), err.message);
    }
  };
  drop.ondragover = (event) => { event.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (event) => {
    event.preventDefault();
    drop.classList.remove("over");
    if (event.dataTransfer.files[0]) readFile(event.dataTransfer.files[0]);
  };
  $("#pick-file").onclick = () => $("#file-input").click();
  $("#file-input").onchange = (event) => { if (event.target.files[0]) readFile(event.target.files[0]); };

  $("#google-connect").onclick = () =>
    runJob("/api/mailboxes/google/connect", {}, "job.connect",
      (result) => t("job.connected", { email: result.email }));

  /* Sign in: connect the mailbox, then immediately look in it.

     "Sign in with Google and it scans my Gmail" is one intention, so it is one
     click. The scan is fired only after the connect job actually returns an
     address — chaining it optimistically would start a scan against a mailbox
     that failed to connect and report the OAuth failure as an empty inbox. */
  const signinError = $("#signin-error");
  const signinStatus = $("#signin-status");

  $("#signin-google").onclick = async () => {
    showError(signinError, "");
    const button = $("#signin-google");
    button.disabled = true;
    try {
      if (!STATE?.google_client_ready) {
        // Google requires an OAuth client before any consent screen exists.
        // Send them to the step that collects it rather than failing at the
        // browser with a redirect_uri error nobody can act on.
        signinStatus.textContent = t("signin.needSecret");
        VIEW = "mailbox";
        render();
        return;
      }
      signinStatus.textContent = t("signin.opening");
      const result = await runJob("/api/mailboxes/google/connect", {}, "job.connect",
        (out) => t("job.connected", { email: out.email }));
      if (result && result.email) {
        signinStatus.textContent = t("signin.scanning", { email: result.email });
        await runJob("/api/jobs/scan", {}, "job.scan", (out) =>
          out.leads
            ? t("job.scanDone", { leads: out.leads, fetched: out.fetched })
            : t("job.scanNone", { days: STATE?.settings?.scan_days ?? 1 }));
      }
      VIEW = null;
      await refresh();
    } catch (error) {
      showError(signinError, error.message);
    } finally {
      button.disabled = false;
      signinStatus.textContent = "";
    }
  };

  const secondCta = $("#signin-google-2");
  if (secondCta) secondCta.onclick = () => $("#signin-google").click();

  $("#signin-other").onclick = () => { VIEW = "mailbox"; render(); };

  /* Theme: system → light → dark → system.

     Stored rather than inferred, because "system" is a real third choice and
     not the absence of one: with nothing stamped on the root, the page follows
     the OS, which is what most people want and what the stylesheet's bare
     :root block is written for. */
  const THEMES = ["system", "light", "dark"];
  const GLYPH = { system: "◐", light: "☀", dark: "☾" };

  function applyTheme(name) {
    if (name === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", name);
    const button = $("#theme-toggle");
    if (button) {
      button.textContent = GLYPH[name];
      button.title = `Theme: ${name}`;
    }
    try { localStorage.setItem("cra-theme", name); } catch { /* private mode */ }
  }

  let theme = "system";
  try { theme = localStorage.getItem("cra-theme") || "system"; } catch { /* ignore */ }
  if (!THEMES.includes(theme)) theme = "system";
  applyTheme(theme);

  $("#theme-toggle").onclick = () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    applyTheme(theme);
  };

  const saveKey = async (input, errorEl, button) => {
    showError(errorEl, "");
    const restore = busy(button, "key.checking");
    try {
      await post("/api/anthropic", { api_key: input.value });
      input.value = "";
      // Saved from the wizard: hand control back to it so it moves on to
      // whatever is still outstanding. From Settings, stay in Settings.
      if (SETUP_STEPS.includes(VIEW)) VIEW = null;
      await refresh();
    } catch (err) {
      showError(errorEl, err.message);
    } finally {
      restore();
    }
  };
  $("#key-save").onclick = () => saveKey($("#api-key"), $("#key-error"), $("#key-save"));
  $("#settings-key-save").onclick = () =>
    saveKey($("#settings-key"), $("#settings-key-error"), $("#settings-key-save"));

  $("#seed-start").onclick = () =>
    runJob("/api/jobs/seed", { months: Number($("#seed-months").value) }, "job.seed",
      (result) => t("job.seedDone", { n: result.known_senders }));

  $("#scan-now").onclick = async () => {
    const days = STATE.settings.scan_days;
    const restore = busy($("#scan-now"), "dash.scanning");
    try {
      await runJob("/api/jobs/scan", { days }, "job.scan", (result) =>
        result.leads
          ? t("job.scanDone", { leads: result.leads, fetched: result.fetched })
          : t("job.scanNone", { days }));
    } finally {
      restore();
    }
  };

  $("#add-mailbox").onclick = () => { VIEW = "mailbox"; render(); };

  $("#settings-save").onclick = async () => {
    await post("/api/settings", {
      ignored_domains: $("#ignored").value,
      scan_days: Number($("#scan-days").value),
      model: $("#model").value,
      watch_enabled: $("#watch-enabled").checked,
      watch_interval_minutes: Number($("#watch-interval").value),
    });
    $("#settings-saved").hidden = false;
    setTimeout(() => { $("#settings-saved").hidden = true; }, 2000);
    await refresh();
  };

  $("#purge").onclick = async () => {
    if (!confirm(t("settings.reset.confirm"))) return;
    await post("/api/purge");
    VIEW = null;
    await refresh();
  };

  $("#job-close").onclick = closeOverlay;
}

let AUTH_MODE = "login";

function setAuthMode(mode) {
  AUTH_MODE = mode === "signup" ? "signup" : "login";
  $$(".auth-tab").forEach((tab) =>
    tab.classList.toggle("active", tab.dataset.auth === AUTH_MODE));
  const signup = AUTH_MODE === "signup";
  $("#auth-name-field").hidden = !signup;
  $("#auth-submit").textContent = t(signup ? "auth.submit.signup" : "auth.submit.login");
  // The password manager should offer a new password on signup and the saved
  // one on login; the autocomplete token is how the browser is told which.
  $("#auth-password").setAttribute("autocomplete",
    signup ? "new-password" : "current-password");
  showError($("#auth-error"), "");
}

function wireAuth() {
  // Scoped to the container, per the rule test_ui enforces: a bare $$(".x")
  // that assigns .onclick silently replaces any earlier handler on the page.
  $$(".auth-tabs .auth-tab").forEach((tab) => {
    tab.onclick = () => setAuthMode(tab.dataset.auth);
  });

  /* Continue with Google. Same success tail as the password form
     (AUTHED=true; render(); bootApp()), reached a different way: kick off the
     server-side OAuth job, poll the auth-scoped public endpoint until it
     finishes, then trade the finished job for a session. The verified email is
     resolved server-side from the job result — the client never names an
     account. Wired by unique id, so it does not trip the unscoped-handler
     guard in test_ui. */
  const googleButton = $("#auth-google");
  if (googleButton) googleButton.onclick = async () => {
    showError($("#auth-error"), "");
    googleButton.disabled = true;
    try {
      const job = await post("/api/auth/google/start", {});
      // The start response hands us a one-time secret; poll and finish require
      // it, so only this browser can watch or claim the sign-in it began.
      const q = "job_id=" + encodeURIComponent(job.id) +
                "&secret=" + encodeURIComponent(job.secret);
      let state = job;
      while (state.status === "running") {
        await new Promise((resolve) => setTimeout(resolve, 800));
        state = await api("/api/auth/google/poll?" + q);
      }
      if (state.status !== "done") {
        throw new Error(state.error || t("auth.googleCancelled"));
      }
      await post("/api/auth/google/finish", { job_id: job.id, secret: job.secret });
      AUTHED = true;
      render();
      await bootApp();
    } catch (error) {
      showError($("#auth-error"), error.message);
    } finally {
      googleButton.disabled = false;
    }
  };

  $("#auth-form").onsubmit = async (event) => {
    event.preventDefault();
    const submit = $("#auth-submit");
    const email = $("#auth-email").value.trim();
    const password = $("#auth-password").value;
    const name = $("#auth-name").value.trim();
    showError($("#auth-error"), "");
    submit.disabled = true;
    try {
      const path = AUTH_MODE === "signup" ? "/api/auth/signup" : "/api/auth/login";
      const body = AUTH_MODE === "signup" ? { email, password, name } : { email, password };
      await post(path, body);
      // The session cookie is set by the response; from here the app boots
      // exactly as it would on a fresh authenticated load.
      AUTHED = true;
      $("#auth-password").value = "";
      render();
      await bootApp();
    } catch (error) {
      showError($("#auth-error"), error.message);
    } finally {
      submit.disabled = false;
    }
  };
}

async function signOut() {
  try { await post("/api/auth/logout", {}); } catch { /* already gone */ }
  AUTHED = false;
  STATE = null;
  VIEW = null;
  setAuthMode("login");
  render();
}

// --- boot ------------------------------------------------------------------

async function bootApp() {
  // Everything here needs a session, so it only runs once the gate is open.
  PRESETS = (await api("/api/presets")).presets;
  renderPresets();
  await refresh();
  renderGoogleSteps();
  startWatching();
  await rejoinRunningJob();
}

(async function start() {
  document.documentElement.lang = LANG;
  applyTranslations();
  wire();
  wireAuth();

  let status;
  try {
    status = await api("/api/auth/status");
  } catch {
    status = { authenticated: false, has_users: false };
  }
  AUTHED = !!status.authenticated;
  // Google sign-in only works on this machine with a client configured; the
  // server tells us, and the button (and its "or" divider) stay hidden
  // otherwise — e.g. on the public tunnel deploy, where loopback OAuth cannot
  // complete.
  const googleReady = !!status.google_login_available;
  const gb = $("#auth-google"), gw = $("#auth-google-wrap");
  if (gb) gb.hidden = !googleReady;
  if (gw) gw.hidden = !googleReady;
  // Open on "Create account" for a fresh install, "Log in" once someone has
  // registered — the common case each time, without a toggle to hunt for.
  setAuthMode(status.has_users ? "login" : "signup");
  render();
  if (AUTHED) await bootApp();
})();

// The interval boot that used to live at the end of start(); pulled into its
// own function so it is armed only after sign-in, not on the login screen.
function startWatching() {
  if (startWatching._armed) return;
  startWatching._armed = true;

  /* Mail now arrives without anyone pressing anything, so the page has to
     notice on its own. Only while the dashboard is on screen and nothing is
     mid-dialog. */
  setInterval(async () => {
    if (!AUTHED || !$("#overlay").hidden || $("#view-dashboard").hidden) return;
    try { await refresh(); } catch { /* transient — the next tick retries */ }
  }, 30000);
}

/* Rejoin a job that was running before a reload. Called from bootApp, after a
   session is known to exist. */
async function rejoinRunningJob() {
  if (STATE?.job && STATE.job.status === "running" && STATE.job.kind !== "watch") {
    openOverlay("job." + STATE.job.kind);
    poll(STATE.job.id);
  }
}
