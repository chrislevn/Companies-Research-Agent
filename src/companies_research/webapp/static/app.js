/* Companies Research Agent — local UI.
   No build step and no dependencies: this file is served as-is. */

const TOKEN = document.querySelector('meta[name="cr-token"]').content;

let STATE = null;
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
  const pending = nextSetupStep();
  let view;
  let step = pending;

  if (VIEW === "settings") {
    view = "settings";
  } else if (SETUP_STEPS.includes(VIEW)) {
    view = "setup";
    step = VIEW;
  } else {
    view = pending ? "setup" : "dashboard";
  }

  $("#view-setup").hidden = view !== "setup";
  $("#view-dashboard").hidden = view !== "dashboard";
  $("#view-settings").hidden = view !== "settings";
  // During first-run setup there is nowhere else to go yet.
  $$(".topbar-actions [data-nav]").forEach((b) => { b.hidden = !!pending && VIEW === null; });

  if (view === "setup") renderSetup(step);
  if (view === "dashboard") renderDashboard();
  if (view === "settings") renderSettings();
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

// --- settings --------------------------------------------------------------

function renderSettings() {
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
  await poll(job.id, describe);
}

/* Resolves when the job leaves the running state, so callers can keep their
   button disabled for the whole run. */
function poll(jobId, describe) {
  return new Promise((resolve) => {
    const tick = async () => {
      let job;
      try {
        job = await api(`/api/jobs/${jobId}`);
      } catch (err) {
        await finishJob({ status: "error", error: err.message });
        return resolve();
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
      resolve();
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

  $$(".topbar-actions [data-nav]").forEach((button) => {
    button.onclick = () => { VIEW = button.dataset.nav === "settings" ? "settings" : null; render(); };
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

  $$(".tab").forEach((tab) => {
    tab.onclick = () => {
      $$(".tab").forEach((other) => other.classList.toggle("active", other === tab));
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

// --- boot ------------------------------------------------------------------

(async function start() {
  document.documentElement.lang = LANG;
  applyTranslations();
  wire();
  PRESETS = (await api("/api/presets")).presets;
  renderPresets();
  await refresh();
  renderGoogleSteps();

  // A job started before a reload keeps running on the server; rejoin it —
  // except a background check, which nobody asked for and should not be
  // interrupted by a dialog appearing over the page.
  if (STATE.job && STATE.job.status === "running" && STATE.job.kind !== "watch") {
    openOverlay("job." + STATE.job.kind);
    poll(STATE.job.id);
  }

  /* Mail now arrives without anyone pressing anything, so the page has to
     notice on its own. Only while the dashboard is on screen and nothing is
     mid-dialog. */
  setInterval(async () => {
    if (!$("#overlay").hidden || $("#view-dashboard").hidden) return;
    try { await refresh(); } catch { /* transient — the next tick retries */ }
  }, 30000);
})();
