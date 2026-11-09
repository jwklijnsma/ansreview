"use strict";
// ansreview web UI. No framework, no external assets: every string from the server
// (file contents, model output) goes into the DOM through textContent, never innerHTML.

const SEVS = ["critical", "high", "medium", "low", "info"];
const SOURCES = [["all", "All"], ["static", "AR checks"], ["ansible-lint", "ansible-lint"], ["llm", "Model"]];

const state = {
  status: null,
  repos: [],
  repoId: null,
  tab: "findings",
  detail: null,
  report: null,
  reportTime: null,
  fix: null,
  job: null,            // job we are following: {id, since, lines, data}
  files: new Map(),     // "repo|path" -> Promise<content>
  expanded: new Set(),
  logOpen: true,
  filters: { sev: new Set(SEVS), source: "all", q: "", group: "file" },
};

// ---------------------------------------------------------------- helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = (id) => document.getElementById(id);

function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem("ansreview:" + key) || "null");
    localStorage.setItem("ansreview:" + key, JSON.stringify(value));
  } catch (_) { return null; }
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return Math.floor(s / 86400) + " d ago";
}
function exitText(d) {
  if (d.status === "cancelled") return "cancelled";
  if (d.action === "scan" && d.exit_code === 1) return "findings at or above the fail-on level";
  if (d.action === "scan" && d.exit_code === 0) return "nothing at or above the fail-on level";
  return d.exit_code === 0 ? "completed" : `exit code ${d.exit_code}`;
}
function shortPath(p) {
  const parts = p.split("/").filter(Boolean);
  return parts.length > 3 ? "…/" + parts.slice(-3).join("/") : p;
}
function dur(s) {
  if (s == null) return "";
  s = Math.round(s);
  return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
}

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
}

class AuthError extends Error {}

async function api(path, opts = {}) {
  const init = { credentials: "same-origin", headers: {} };
  if (opts.method === "POST") {
    init.method = "POST";
    init.headers["X-Ansreview"] = "1";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body || {});
  }
  const res = await fetch("/api/" + path, init);
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (res.status === 401) { showLogin(); throw new AuthError("unauthorised"); }
  if (!res.ok) throw new Error((data && data.error) || res.statusText);
  return data;
}

const enc = encodeURIComponent;

// ---------------------------------------------------------------- auth
function showLogin() {
  $("app").hidden = true;
  $("login").hidden = false;
  $("login-token").focus();
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    await api("login", { method: "POST", body: { token: $("login-token").value } });
    $("login-token").value = "";
    $("login").hidden = true;
    boot();
  } catch (err) {
    if (!(err instanceof AuthError)) $("login-error").textContent = err.message;
    else $("login-error").textContent = "Wrong token.";
  }
});

// ---------------------------------------------------------------- top bar
function renderStatus() {
  const st = state.status;
  const box = $("status");
  box.replaceChildren();
  if (!st) return;
  $("version").textContent = "v" + st.version;
  const job = state.job && state.job.data;
  if (job && (job.status === "running" || job.status === "queued")) {
    box.append(h("span", { class: "pill keep", title: "A job is running" },
      h("span", { class: "dot run" }), `${job.action} · ${job.repo_name}`));
  }
  const ollamaOk = !!st.ollama;
  box.append(h("span", { class: "pill", title: st.ollama_error || st.ollama_host },
    h("span", { class: "dot " + (ollamaOk ? "ok" : "bad") }),
    ollamaOk ? `Ollama ${st.ollama}` : "Ollama unreachable"));
  if (ollamaOk) {
    box.append(h("span", { class: "pill", title: st.model_available ? "model is pulled" : `run: ollama pull ${st.model}` },
      h("span", { class: "dot " + (st.model_available ? "ok" : "warn") }), st.model));
  }
  box.append(h("span", { class: "pill", title: "ansible-lint" },
    h("span", { class: "dot " + (st.ansible_lint ? "ok" : "warn") }),
    st.ansible_lint ? "ansible-lint " + st.ansible_lint.split(" ")[0] : "no ansible-lint"));
}

// ---------------------------------------------------------------- repo list
function sevBar(counts) {
  if (!counts) return null;
  const total = SEVS.reduce((a, s) => a + (counts[s] || 0), 0);
  if (!total) return h("div", { class: "sevbar" }, h("span", { style: { width: "100%", background: "var(--ok)" } }));
  return h("div", { class: "sevbar", title: SEVS.map((s) => `${s}: ${counts[s] || 0}`).join(" · ") },
    SEVS.filter((s) => counts[s]).map((s) =>
      h("span", { style: { width: (100 * counts[s] / total) + "%", background: `var(--sev-${s})` } })));
}

function renderRepos() {
  const q = $("repo-filter").value.trim().toLowerCase();
  const list = $("repos");
  list.replaceChildren();
  const repos = state.repos.filter((r) => !q || r.name.toLowerCase().includes(q));
  if (!state.repos.length) {
    list.append(h("li", { class: "empty-roots" },
      "No Ansible repositories found under ", (state.status?.roots || []).join(", "),
      uploadsOn() ? ". Upload a ZIP above, or restart the server with --root pointing at a repo or a folder of repos."
        : ". Start the server with --root pointing at a repo or a folder of repos."));
    return;
  }
  const item = (r) => {
    const total = r.counts ? SEVS.reduce((a, s) => a + (r.counts[s] || 0), 0) : null;
    return h("li", {},
      h("button", { "aria-current": r.id === state.repoId ? "true" : "false", onclick: () => go(r.id, state.tab) },
        h("div", { class: "repo-name" }, r.name, r.upload ? h("span", { class: "zip-badge" }, "ZIP") : null),
        h("div", { class: "repo-meta" },
          r.upload ? "uploaded " + ago(r.upload.uploaded) + " · " : r.branch ? r.branch + " · " : "",
          r.report_time ? `${total} finding${total === 1 ? "" : "s"}` + (r.upload ? "" : " · " + ago(r.report_time)) : "not scanned yet"),
        sevBar(r.counts)));
  };
  const local = repos.filter((r) => !r.upload);
  const uploaded = repos.filter((r) => r.upload);
  local.forEach((r) => list.append(item(r)));
  if (uploaded.length) {
    list.append(h("li", { class: "list-head" }, "Uploads"));
    uploaded.forEach((r) => list.append(item(r)));
  }
}
$("repo-filter").addEventListener("input", renderRepos);

// ---------------------------------------------------------------- routing
function go(repoId, tab) {
  location.hash = "#/r/" + enc(repoId) + "/" + (tab || "findings");
}

async function route() {
  const m = location.hash.match(/^#\/r\/([^/]+)(?:\/(\w+))?/);
  const repoId = m ? decodeURIComponent(m[1]) : null;
  const tab = (m && m[2]) || "findings";
  const changed = repoId !== state.repoId;
  state.repoId = repoId;
  state.tab = tab;
  renderRepos();
  if (!repoId) return renderWelcome();
  if (changed) {
    state.detail = state.report = state.fix = null;
    state.expanded.clear();
    state.filters.q = "";
    renderMain();
    await loadRepo();
  } else {
    renderTabs();
  }
}
window.addEventListener("hashchange", route);

function renderWelcome() {
  const n = state.repos.length;
  $("main").replaceChildren(h("div", { class: "empty" },
    h("h3", {}, n ? "Pick a repository" : "No repositories yet"),
    h("p", {}, n ? `${n} Ansible ${n === 1 ? "repository" : "repositories"} found. Choose one on the left to scan it or browse its last report.`
      : "Restart ansreview serve with --root pointing at an Ansible repo or a folder that contains them.")));
}

async function loadRepo() {
  const id = state.repoId;
  try {
    const [detail, report, fix] = await Promise.all([
      api("repos/" + enc(id)), api("repos/" + enc(id) + "/report"), api("repos/" + enc(id) + "/fix")]);
    if (id !== state.repoId) return;
    state.detail = detail;
    state.report = report.report;
    state.reportTime = report.time;
    state.fix = fix;
    const r = state.repos.find((x) => x.id === id);
    if (r) { r.counts = detail.counts; r.report_time = detail.report_time; r.branch = detail.branch; }
    renderRepos();
    renderMain();
    if (state.autoScan === id) {
      state.autoScan = null;
      if (!busy()) startJob("scan");
    }
  } catch (err) {
    if (err instanceof AuthError) return;
    $("main").replaceChildren(h("div", { class: "empty" }, h("h3", {}, "Could not open this repository"), h("p", {}, err.message)));
  }
}

// ---------------------------------------------------------------- main view
function busy() {
  const d = state.job && state.job.data;
  return !!d && (d.status === "running" || d.status === "queued");
}

function renderMain() {
  const main = $("main");
  const d = state.detail;
  if (!d) {
    main.replaceChildren(h("div", { class: "empty" }, h("p", {}, "Loading…")));
    return;
  }
  main.replaceChildren(
    renderHead(d),
    renderOptions(d),
    h("div", { id: "jobcard" }),
    h("div", { class: "tabs", role: "tablist", id: "tabs" }),
    h("div", { id: "tabbody" }));
  renderJob();
  renderTabs();
}

function renderHead(d) {
  const running = busy();
  const up = d.upload;
  const sub = up ? h("div", { class: "repo-sub" },
    h("span", { class: "zip-badge" }, "ZIP"),
    h("span", { title: up.filename }, up.filename),
    h("span", {}, `${up.files} files · ${size(up.bytes)}`),
    h("span", {}, "uploaded " + ago(up.uploaded)),
    d.dirty ? h("span", { class: "badge dirty", title: (d.changed_files || []).join("\n") }, `${d.changed_files.length} fixed file${d.changed_files.length === 1 ? "" : "s"}`) : null,
    h("span", {}, "model ", h("code", {}, d.config.model)))
    : h("div", { class: "repo-sub" },
      d.branch ? h("span", {}, "⎇ ", h("code", {}, d.branch)) : null,
      d.last_commit ? h("span", { class: "mono" }, d.last_commit) : null,
      d.dirty === null ? h("span", {}, "not a git repo") :
        h("span", { class: "badge " + (d.dirty ? "dirty" : "clean"), title: (d.changed_files || []).join("\n") },
          d.dirty ? `${d.changed_files.length} uncommitted` : "clean"),
      h("span", {}, "model ", h("code", {}, d.config.model)),
      h("span", { class: "path", title: d.path }, shortPath(d.path)));
  const scanBtn = h("button", { class: "btn primary", disabled: running, onclick: () => startJob("scan") }, "Scan");
  const dryBtn = h("button", { class: "btn", disabled: running || !d.report_time, title: d.report_time ? "Write fix.patch only, change nothing" : "Scan first", onclick: () => startJob("fix", { dry_run: true }) }, "Preview fixes");
  const fixBtn = h("button", { class: "btn", disabled: running || !d.report_time, title: d.report_time ? "Write fixes to the working tree (never committed)" : "Scan first", onclick: () => startJob("fix", {}) }, "Apply fixes");
  return h("div", {},
    h("div", { class: "repo-head" },
      h("div", {}, h("h1", {}, d.name), sub),
      h("div", { class: "actions" }, scanBtn, dryBtn, fixBtn,
        up ? h("a", { class: "btn", href: `/api/repos/${enc(d.id)}/download`, download: "",
          title: d.dirty ? "The project with the applied fixes, as a ZIP" : "The uploaded project as a ZIP" },
        d.dirty ? "Download fixed ZIP" : "Download ZIP") : null,
        up ? h("button", { class: "btn danger", disabled: running && state.job?.data.repo === d.id, onclick: deleteUpload }, "Delete") : null)),
    d.config_warnings && d.config_warnings.length
      ? h("div", { class: "callout warn" }, d.config_warnings.map((w) => h("div", {}, w))) : null);
}

function optsFor(id) {
  return Object.assign({ scan: { diff_scope: "lines" }, fix: {} }, store("opts:" + id) || {});
}

function renderOptions(d) {
  const o = optsFor(state.repoId);
  const save = () => store("opts:" + state.repoId, o);
  const text = (group, key, label, attrs = {}) => h("div", { class: "field" },
    h("label", { for: `o-${group}-${key}` }, label),
    h(attrs.textarea ? "textarea" : "input", Object.assign({
      id: `o-${group}-${key}`, value: attrs.textarea ? null : (o[group][key] || ""),
      oninput: (e) => { o[group][key] = e.target.value; save(); }
    }, attrs.textarea ? {} : { type: "text" }, attrs.placeholder ? { placeholder: attrs.placeholder } : {}),
    attrs.textarea ? (o[group][key] || "") : null));
  const select = (group, key, label, choices) => h("div", { class: "field" },
    h("label", { for: `o-${group}-${key}` }, label),
    h("select", { id: `o-${group}-${key}`, onchange: (e) => { o[group][key] = e.target.value; save(); } },
      choices.map(([v, l]) => h("option", { value: v, selected: (o[group][key] || "") === v }, l))));
  const check = (group, key, label) => h("label", { class: "check" },
    h("input", { type: "checkbox", checked: !!o[group][key], onchange: (e) => { o[group][key] = e.target.checked; save(); } }), label);
  const sevChoices = [["", "default"], ...SEVS.slice().reverse().map((s) => [s, s])];

  const active = [];
  if (o.scan.diff) active.push("diff vs " + o.scan.diff);
  if (o.scan.only) active.push("paths limited");
  if (o.scan.no_llm) active.push("no model");
  if (o.scan.model) active.push(o.scan.model);

  return h("details", { class: "card options", open: store("opts-open") ? true : null,
    ontoggle: (e) => store("opts-open", e.target.open) },
  h("summary", {}, "Options", h("span", { class: "hint" }, active.length ? "· " + active.join(" · ") : "· defaults from .ansreview.yml")),
  h("div", { class: "opt-grid" },
    h("fieldset", {}, h("legend", {}, "Scope"),
      text("scan", "diff", "Only changes vs git ref", { placeholder: "e.g. origin/main" }),
      select("scan", "diff_scope", "Report on", [["lines", "changed lines"], ["files", "whole changed files"]]),
      text("scan", "only", "Only these roles / paths (one per line)", { textarea: true })),
    h("fieldset", {}, h("legend", {}, "Scan"),
      text("scan", "model", "Model override", { placeholder: d.config.model }),
      select("scan", "lint_profile", "ansible-lint profile", [["", "default"], ["min", "min"], ["basic", "basic"], ["moderate", "moderate"], ["safety", "safety"], ["shared", "shared"], ["production", "production"]]),
      select("scan", "min_severity", "Hide findings below", sevChoices),
      check("scan", "no_llm", "Skip the model (fast: lint + AR only)"),
      check("scan", "no_lint", "Skip ansible-lint"),
      check("scan", "no_static", "Skip AR checks"),
      check("scan", "no_cache", "Ignore the cache, ask the model again")),
    h("fieldset", {}, h("legend", {}, "Fix"),
      select("fix", "min_severity", "Model fixes from severity", sevChoices),
      text("fix", "min_confidence", "Model fixes from confidence", { placeholder: "0.8" }),
      check("fix", "allow_dirty", "Allow uncommitted changes"),
      check("fix", "no_llm", "Skip model fixes"),
      check("fix", "no_static_fix", "Skip AR fixes"),
      check("fix", "no_lint_fix", "Skip ansible-lint --fix"))));
}

// ---------------------------------------------------------------- jobs
async function startJob(action, extra) {
  const d = state.detail;
  const o = optsFor(state.repoId);
  const body = Object.assign({}, o[action], extra || {});
  if (action === "scan") { body.only = (o.scan.only || "").split("\n").map((s) => s.trim()).filter(Boolean); }
  else {
    body.only = (o.scan.only || "").split("\n").map((s) => s.trim()).filter(Boolean);
    if (o.scan.model && !body.model) body.model = o.scan.model;
    if (!body.dry_run) {
      if (d.dirty && !body.allow_dirty && !d.upload) {
        toast("This repo has uncommitted changes. Commit or stash them, or tick “Allow uncommitted changes” in Options.");
        return;
      }
      if (d.upload && d.dirty) body.allow_dirty = true;   // fixes on top of earlier fixes
      const msg = d.upload
        ? `Apply fixes to the uploaded ${d.name}?\n\nOnly this server's copy changes. Download the fixed ZIP afterwards.`
        : `Apply fixes to ${d.name}?\n\nChanges are written to the working tree and never committed. Review them with git diff; undo with git checkout -- .`;
      if (!confirm(msg)) return;
    }
  }
  try {
    const job = await api(`repos/${enc(state.repoId)}/${action}`, { method: "POST", body });
    follow(job);
  } catch (err) {
    if (!(err instanceof AuthError)) toast(err.message);
  }
}

function follow(summary) {
  state.job = { id: summary.id, since: 0, lines: [], data: summary };
  state.logOpen = true;
  renderStatus();
  if (state.detail) { renderMain(); }
  poll();
}

let pollTimer;
async function poll() {
  clearTimeout(pollTimer);
  const j = state.job;
  if (!j) return;
  try {
    const data = await api(`jobs/${j.id}?since=${j.since}`);
    if (state.job !== j) return;
    j.lines.push(...data.lines);
    if (j.lines.length > 5000) j.lines.splice(0, j.lines.length - 5000);
    j.since = data.next;
    const wasBusy = busy();
    j.data = data;
    renderJob();
    if (wasBusy && !busy()) {
      renderStatus();
      finished(data);
      return;
    }
  } catch (err) {
    if (err instanceof AuthError) return;
  }
  pollTimer = setTimeout(poll, 1000);
}

async function finished(data) {
  const msg = data.status === "done" ? `${data.action} of ${data.repo_name} finished in ${dur(data.seconds)}`
    : data.status === "cancelled" ? `${data.action} of ${data.repo_name} cancelled`
      : `${data.action} of ${data.repo_name} failed (exit ${data.exit_code}) — see the log`;
  toast(msg);
  // A finished run leaves the result in the tabs below; fold the log away (a failure keeps it open).
  if (data.status === "done") state.logOpen = false;
  await refreshRepos();
  if (data.repo === state.repoId) {
    await loadRepo();
    if (data.status === "done") go(state.repoId, data.action === "fix" ? "fixes" : "findings");
  }
}

function logLine(text) {
  let cls = "";
  if (text.startsWith("$ ")) cls = "l-cmd";
  else if (/^\s*\[\d+\/\d+\] ✗|^error|✗/.test(text)) cls = "l-bad";
  else if (/^warning|^\s*!/.test(text)) cls = "l-warn";
  else if (/✓/.test(text)) cls = "l-ok";
  return h("div", { class: cls }, text);
}

function renderJob() {
  const box = $("jobcard");
  if (!box) return;
  const j = state.job;
  if (!j || (j.data.repo !== state.repoId)) {
    if (busy()) {
      box.replaceChildren(h("div", { class: "callout" },
        `A ${j.data.action} of ${j.data.repo_name} is running; one job runs at a time (it shares the GPU). `,
        h("a", { href: "#/r/" + enc(j.data.repo) + "/findings" }, "Watch it")));
    } else box.replaceChildren();
    return;
  }
  const d = j.data;
  const q = d.queue && d.queue.stage === (d.action === "fix" ? "fix" : "scan") ? d.queue : null;
  const total = q ? q.total : 0;
  const counts = q ? q.counts || {} : {};
  const doneN = (counts.done || 0) + (counts.cached || 0) + (counts.failed || 0);
  const running = d.status === "running" || d.status === "queued";
  const pct = total ? Math.round(100 * doneN / total) : (running ? null : 100);
  const label = { queued: "Starting", running: d.action === "scan" ? "Scanning" : "Fixing",
    done: "Finished", failed: "Failed", cancelled: "Cancelled" }[d.status];
  const dotCls = running ? "run" : d.status === "done" ? "ok" : "bad";

  let log = box.querySelector(".log");
  const stick = !log || log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  const openLog = state.logOpen;

  const card = h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("div", { class: "job-status" },
        h("span", { class: "dot " + dotCls }),
        h("strong", {}, `${label}${d.options && d.options.dry_run ? " (preview)" : ""}`),
        h("span", { class: "muted" }, dur(d.seconds) + (q && running ? ` · model pass ${doneN}/${total} files` : ""))),
      h("div", { class: "actions" },
        h("button", { class: "btn small", onclick: () => { state.logOpen = !openLog; renderJob(); } }, openLog ? "Hide log" : "Show log"),
        running ? h("button", { class: "btn small danger", onclick: cancelJob }, "Cancel") : null,
        !running ? h("button", { class: "btn small", onclick: () => { state.job = null; renderJob(); } }, "Dismiss") : null)),
    h("div", { class: "card-body" },
      h("div", { class: "progress" + (pct === null ? " indeterminate" : "") }, h("div", { style: { width: (pct ?? 0) + "%" } })),
      h("div", { class: "progress-meta" },
        h("span", {}, !running ? exitText(d) : counts.running ? "working on " + ((q.files || []).find((f) => f.status === "running") || {}).file : "lint and AR checks"),
        h("span", {}, pct === null ? "" : pct + "%")),
      openLog ? h("div", { class: "log", role: "log" }, j.lines.slice(-1500).map(logLine)) : null));
  box.replaceChildren(card);
  log = box.querySelector(".log");
  if (log && stick) log.scrollTop = log.scrollHeight;
}

async function cancelJob() {
  if (!state.job) return;
  try { await api(`jobs/${state.job.id}/cancel`, { method: "POST" }); } catch (err) { if (!(err instanceof AuthError)) toast(err.message); }
}

// ---------------------------------------------------------------- tabs
function renderTabs() {
  const tabs = $("tabs");
  if (!tabs) return;
  const r = state.report;
  const nF = r ? r.findings.length : null;
  const nNotes = r ? Object.keys(r.summaries || {}).length + (r.errors || []).length : null;
  const def = [["findings", "Findings", nF], ["fixes", "Fixes", null], ["notes", "Notes", nNotes]];
  tabs.replaceChildren(...def.map(([key, label, n]) => h("button", {
    role: "tab", "aria-selected": state.tab === key ? "true" : "false", onclick: () => go(state.repoId, key)
  }, label, n !== null ? h("span", { class: "count" }, n) : null)));
  const body = $("tabbody");
  if (state.tab === "fixes") body.replaceChildren(renderFixes());
  else if (state.tab === "notes") body.replaceChildren(renderNotes());
  else body.replaceChildren(renderFindings());
}

// ---- findings
function renderFindings() {
  const r = state.report;
  if (!r) {
    return h("div", { class: "empty" }, h("h3", {}, "No report yet"),
      h("p", {}, "Run a scan to review this repository. Tip: tick “Skip the model” in Options for a quick first pass."));
  }
  const f = state.filters;
  const wrap = h("div", {});
  const counts = r.counts || {};
  const chips = SEVS.map((s) => h("button", {
    class: "sev-chip", "aria-pressed": f.sev.has(s) ? "true" : "false",
    title: "Click to toggle, double-click to show only this severity",
    onclick: () => { f.sev.has(s) ? f.sev.delete(s) : f.sev.add(s); renderTabs(); },
    ondblclick: () => { f.sev = new Set([s]); renderTabs(); }
  }, h("span", { class: "n" }, counts[s] || 0), h("span", { class: "l" }, h("span", { class: "sw", style: { background: `var(--sev-${s})` } }), s)));
  const st = r.stats || {};
  wrap.append(h("div", { class: "summary-row" }, chips,
    h("div", { class: "meta-chip" },
      h("div", {}, `${(r.units || []).length} units · scanned ${ago(state.reportTime)}`),
      h("div", {}, [r.model ? "model " + r.model : "no model", st.seconds != null ? dur(st.seconds) : null].filter(Boolean).join(" · ")))));

  const search = h("input", { type: "search", placeholder: "Search rule, file or message…", value: f.q,
    oninput: (e) => { f.q = e.target.value; drawList(); } });
  wrap.append(h("div", { class: "filters" }, search,
    h("div", { class: "seg", role: "group", "aria-label": "Source" }, SOURCES.map(([k, l]) => h("button", {
      "aria-pressed": f.source === k ? "true" : "false", onclick: () => { f.source = k; renderTabs(); } }, l))),
    h("div", { class: "seg", role: "group", "aria-label": "Group by" }, [["file", "By file"], ["rule", "By rule"]].map(([k, l]) => h("button", {
      "aria-pressed": f.group === k ? "true" : "false", onclick: () => { f.group = k; renderTabs(); } }, l)))));

  const list = h("div", {});
  wrap.append(list);
  function drawList() {
    const q = f.q.trim().toLowerCase();
    const items = r.findings.filter((x) => f.sev.has(x.severity)
      && (f.source === "all" || x.source === f.source)
      && (!q || `${x.rule} ${x.file} ${x.message} ${x.category}`.toLowerCase().includes(q)));
    if (!items.length) {
      list.replaceChildren(h("div", { class: "empty" }, r.findings.length
        ? h("p", {}, "No findings match these filters.")
        : [h("h3", {}, "No findings"), h("p", {}, "Nothing to fix here.")]));
      return;
    }
    const groups = new Map();
    for (const x of items) {
      const key = f.group === "rule" ? x.rule : x.file;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(x);
    }
    const rank = (arr) => Math.min(...arr.map((x) => SEVS.indexOf(x.severity)));
    const sorted = [...groups.entries()].sort((a, b) => rank(a[1]) - rank(b[1]) || a[0].localeCompare(b[0]));
    list.replaceChildren(...sorted.slice(0, 400).map(([key, arr]) => h("div", { class: "file-group" },
      h("div", { class: "file-head" }, key, h("span", { class: "n" }, `${arr.length} finding${arr.length === 1 ? "" : "s"}`)),
      arr.map((x) => renderFinding(x, f.group === "rule")))));
  }
  drawList();
  return wrap;
}

function renderFinding(x, showFile) {
  const open = state.expanded.has(x.fingerprint);
  const row = h("div", { class: "finding" },
    h("button", { "aria-expanded": open ? "true" : "false", onclick: () => {
      open ? state.expanded.delete(x.fingerprint) : state.expanded.add(x.fingerprint);
      row.replaceWith(renderFinding(x, showFile));
    } },
    h("span", { class: "sev sev-" + x.severity }, x.severity),
    h("span", { class: "lineno" }, x.line ? "L" + x.line : "—"),
    h("span", { class: "f-main" },
      h("span", { class: "f-rule" }, x.rule),
      showFile ? h("span", { class: "f-rule" }, x.file + (x.line ? ":" + x.line : "")) : null,
      h("span", { class: "f-src" }, x.source === "static" ? "AR" : x.source === "llm" ? "model" : x.source),
      h("span", { class: "f-msg" }, x.message))));
  if (open) {
    const detail = h("div", { class: "f-detail" },
      x.suggestion ? h("div", { class: "sugg" }, h("b", {}, "Suggested fix"), x.suggestion) : null,
      h("div", { class: "facts" },
        h("span", {}, "category: " + (x.category || "general")),
        x.confidence != null ? h("span", {}, "confidence: " + Math.round(x.confidence * 100) + "%") : null,
        h("span", { class: "mono" }, x.file + (x.line ? ":" + x.line : ""))));
    if (x.line) {
      const slot = h("div", { class: "code" }, h("div", { style: { padding: "8px 12px", color: "var(--muted)" } }, "Loading code…"));
      detail.append(slot);
      snippet(x).then((node) => slot.replaceChildren(node)).catch((err) => slot.replaceChildren(
        h("div", { style: { padding: "8px 12px", color: "var(--muted)" } }, "Could not load the file: " + err.message)));
    }
    row.append(detail);
  }
  return row;
}

function fileContent(path) {
  const key = state.repoId + "|" + path;
  if (!state.files.has(key)) {
    const p = api(`repos/${enc(state.repoId)}/file?path=${enc(path)}`).then((d) => d.content);
    p.catch(() => state.files.delete(key));
    state.files.set(key, p);
  }
  return state.files.get(key);
}

async function snippet(x) {
  const text = await fileContent(x.file);
  const lines = text.split("\n");
  const end = Math.min(lines.length, Math.max(x.end_line || x.line, x.line) + 4);
  const start = Math.max(1, x.line - 4);
  const hiEnd = x.end_line && x.end_line >= x.line ? Math.min(x.end_line, x.line + 40) : x.line;
  const rows = [];
  for (let n = start; n <= Math.min(end, start + 80); n++) {
    rows.push(h("tr", { class: n >= x.line && n <= hiEnd ? "hl" : null },
      h("td", { class: "ln" }, n), h("td", {}, lines[n - 1] ?? "")));
  }
  return h("table", {}, h("tbody", {}, rows));
}

// ---- fixes
function renderFixes() {
  const fx = state.fix;
  if (!fx || (!fx.patch && !fx.report_md)) {
    return h("div", { class: "empty" }, h("h3", {}, "No fix run yet"),
      h("p", {}, state.report ? "Use “Preview fixes” to see what ansreview would change without touching your files."
        : "Scan the repository first; fixes are based on the last report."));
  }
  const wrap = h("div", {});
  wrap.append(h("div", { class: "summary-row" }, h("div", { class: "meta-chip", style: { marginLeft: 0, textAlign: "left" } }, "Last fix run " + ago(fx.time))));
  if (state.detail && state.detail.upload) {
    wrap.append(h("div", { class: "callout" },
      "Fixes on an upload change only this server's copy (ignore the git hints below). ",
      h("a", { href: `/api/repos/${enc(state.repoId)}/download`, download: "" }, "Download the fixed ZIP"),
      " to take them with you."));
  }
  if (fx.report_md) wrap.append(h("div", { class: "card" }, h("div", { class: "card-body md" }, markdown(fx.report_md))));
  const files = parseDiff(fx.patch || "");
  if (files.length) {
    wrap.append(h("h3", { style: { margin: "24px 0 4px", fontSize: "15px" } }, `Diff · ${files.length} file${files.length === 1 ? "" : "s"}`));
    for (const f of files) {
      wrap.append(h("div", { class: "diff-file" },
        h("h4", {}, f.name, h("span", { class: "stat" }, h("span", { class: "plus" }, "+" + f.add), " ", h("span", { class: "minus" }, "−" + f.del))),
        h("div", { class: "diff-body" }, f.lines.map((l) => h("div", {
          class: l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : null
        }, l || " ")))));
    }
  } else {
    wrap.append(h("p", { class: "empty" }, "The last fix run changed no files."));
  }
  return wrap;
}

function parseDiff(text) {
  const files = [];
  let cur = null;
  for (const line of text.split("\n")) {
    if (line.startsWith("--- ")) {
      cur = { name: line.slice(4).replace(/^a\//, "").split("\t")[0], lines: [], add: 0, del: 0, header: true };
      files.push(cur);
      continue;
    }
    if (!cur) continue;
    if (cur.header && line.startsWith("+++ ")) {
      const n = line.slice(4).replace(/^b\//, "").split("\t")[0];
      if (n !== "/dev/null") cur.name = n;
      cur.header = false;
      continue;
    }
    if (line.startsWith("diff --git") || line.startsWith("index ")) continue;
    if (line.startsWith("+")) cur.add++;
    else if (line.startsWith("-")) cur.del++;
    cur.lines.push(line);
  }
  for (const f of files) while (f.lines.length && f.lines[f.lines.length - 1] === "") f.lines.pop();
  return files;
}

// tiny, safe markdown: headings, lists, tables, paragraphs, `code`, **bold**
// (no _emphasis_: rule names like ignore_errors and failed_when would turn italic)
function inline(text) {
  const out = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    if (t.startsWith("`")) out.push(h("code", {}, t.slice(1, -1)));
    else out.push(h("strong", {}, t.slice(2, -2)));
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
function markdown(src) {
  const frag = document.createDocumentFragment();
  const lines = src.split("\n");
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }
    const hm = l.match(/^(#{1,4})\s+(.*)$/);
    if (hm) { frag.append(h("h" + Math.min(hm[1].length, 4), {}, inline(hm[2]))); i++; continue; }
    if (l.startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      const cells = (r) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const align = rows[1] && /^\|?\s*:?-+/.test(rows[1]) ? cells(rows[1]).map((c) => c.endsWith(":")) : [];
      const body = rows.slice(align.length ? 2 : 1);
      frag.append(h("table", {},
        h("thead", {}, h("tr", {}, cells(rows[0]).map((c, k) => h("th", { class: align[k] ? "num" : null }, inline(c))))),
        h("tbody", {}, body.map((r) => h("tr", {}, cells(r).map((c, k) => h("td", { class: align[k] ? "num" : null }, inline(c))))))));
      continue;
    }
    if (/^\s*[-*] /.test(l)) {
      const items = [];
      while (i < lines.length && /^\s*[-*] /.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*] /, ""));
      frag.append(h("ul", {}, items.map((t) => h("li", {}, inline(t)))));
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#|\||\s*[-*] )/.test(lines[i])) para.push(lines[i++]);
    frag.append(h("p", {}, inline(para.join(" "))));
  }
  return frag;
}

// ---- notes
function renderNotes() {
  const r = state.report;
  if (!r) return h("div", { class: "empty" }, h("p", {}, "Run a scan first."));
  const sums = Object.entries(r.summaries || {}).sort((a, b) => a[0].localeCompare(b[0]));
  const kinds = {};
  for (const u of r.units || []) kinds[u.kind] = (kinds[u.kind] || 0) + 1;
  return h("div", {},
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Reviewer notes per unit")),
      h("div", { class: "card-body" }, sums.length
        ? h("ul", { class: "notes" }, sums.map(([k, v]) => h("li", {}, h("b", {}, k), h("div", {}, v))))
        : h("p", { style: { color: "var(--muted)", margin: 0 } }, r.model ? "The model left no notes." : "No model review in the last scan."))),
    (r.errors || []).length ? h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Problems during the scan")),
      h("div", { class: "card-body" }, h("ul", { class: "errors" }, r.errors.map((e) => h("li", {}, e))))) : null,
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Scanned")),
      h("div", { class: "card-body" },
        h("p", { style: { margin: "0 0 8px" } }, Object.entries(kinds).map(([k, n]) => `${n} ${k}${n === 1 ? "" : "s"}`).join(" · ") || "nothing"),
        h("ul", { class: "notes" }, (r.units || []).slice(0, 300).map((u) => h("li", {},
          h("b", {}, u.name), " ", h("span", { style: { color: "var(--muted)" } }, `${u.kind} · ${u.files.length} file${u.files.length === 1 ? "" : "s"}`)))))));
}

// ---------------------------------------------------------------- uploads
function uploadsOn() { return !!(state.status && state.status.uploads && state.status.uploads.enabled); }
function size(n) {
  if (n == null) return "";
  return n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB";
}

function uploadZip(file) {
  if (!uploadsOn()) { toast("Uploads are switched off on this server."); return; }
  if (!/\.zip$/i.test(file.name)) { toast("Choose a .zip file of an Ansible repository."); return; }
  const max = state.status.uploads.max_mb * 1048576;
  if (file.size > max) { toast(`${file.name} is ${size(file.size)}; the limit is ${state.status.uploads.max_mb} MB.`); return; }
  const bar = $("upload-progress");
  const fill = bar.firstElementChild;
  bar.hidden = false;
  fill.style.width = "0%";
  $("upload-label").textContent = `Uploading ${file.name}…`;
  $("upload-btn").disabled = true;
  // XMLHttpRequest rather than fetch: it reports upload progress.
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/uploads");
  xhr.withCredentials = true;
  xhr.setRequestHeader("X-Ansreview", "1");
  xhr.setRequestHeader("Content-Type", "application/zip");
  xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.round(100 * e.loaded / e.total);
    fill.style.width = pct + "%";
    $("upload-label").textContent = pct < 100 ? `Uploading ${file.name}… ${pct}%` : "Unpacking and checking…";
  };
  const done = () => { bar.hidden = true; $("upload-btn").disabled = false; $("upload-label").textContent = ""; };
  xhr.onerror = () => { done(); toast("Upload failed: the connection was closed (file too large?)."); };
  xhr.onload = async () => {
    done();
    let data = null;
    try { data = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
    if (xhr.status === 401) { showLogin(); return; }
    if (xhr.status !== 201) { toast("Upload refused: " + ((data && data.error) || xhr.statusText)); return; }
    toast(`${data.filename}: ${data.files} files unpacked` + (data.skipped ? `, ${data.skipped} skipped` : "") + ".");
    // Uploading is asking for a review: scan as soon as its page has loaded (if the GPU is free).
    state.autoScan = data.id;
    await refreshRepos();
    go(data.id, "findings");
  };
  xhr.send(file);
}

async function deleteUpload() {
  const d = state.detail;
  if (!d || !d.upload) return;
  if (!confirm(`Delete the uploaded ${d.name} and its reports from this server?`)) return;
  try {
    await api(`repos/${enc(d.id)}/delete`, { method: "POST" });
    toast(`${d.name} deleted.`);
    state.repoId = null;
    await refreshRepos();
    location.hash = "";
    renderWelcome();
  } catch (err) {
    if (!(err instanceof AuthError)) toast(err.message);
  }
}

$("upload-btn").addEventListener("click", () => $("upload-input").click());
$("upload-input").addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  e.target.value = "";
  if (f) uploadZip(f);
});
// Drop a ZIP anywhere on the page.
let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  if (!uploadsOn() || $("app").hidden || ![...(e.dataTransfer?.types || [])].includes("Files")) return;
  e.preventDefault();
  dragDepth++;
  $("dropzone").hidden = false;
});
window.addEventListener("dragover", (e) => { if (!$("dropzone").hidden) e.preventDefault(); });
window.addEventListener("dragleave", () => {
  if (--dragDepth <= 0) { dragDepth = 0; $("dropzone").hidden = true; }
});
window.addEventListener("drop", (e) => {
  if ($("dropzone").hidden) return;
  e.preventDefault();
  dragDepth = 0;
  $("dropzone").hidden = true;
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) uploadZip(f);
});

function renderUploadBox() {
  $("upload-box").hidden = !uploadsOn();
  if (uploadsOn()) $("upload-hint").textContent = `or drop it on the page · max ${state.status.uploads.max_mb} MB`;
}

// ---------------------------------------------------------------- boot
async function refreshRepos() {
  const data = await api("repos");
  state.repos = data.repos;
  renderRepos();
}

async function refreshStatus() {
  try { state.status = await api("status"); renderStatus(); renderUploadBox(); } catch (_) { /* shown via login or pills */ }
}

async function boot() {
  try {
    await refreshRepos();
  } catch (err) {
    if (!(err instanceof AuthError)) toast(err.message);
    return;
  }
  $("app").hidden = false;
  await refreshStatus();
  setInterval(refreshStatus, 30000);
  try {
    const { jobs } = await api("jobs");
    const live = jobs.find((j) => j.status === "running" || j.status === "queued");
    if (live) follow(live);
  } catch (_) { /* ignore */ }
  if (!location.hash && state.repos.length === 1) go(state.repos[0].id);
  else route();
}

boot();
