"use strict";
// Adapted decision-center app.js: navigation, hero, quiet rows, dialogs and theme.
// All source integrations have been removed; endpoints below are local only.
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
})[character]);
const text = (selector, value) => { const node = $(selector); if (node) node.textContent = value; };
const count = (value) => Number.isInteger(value) && value >= 0 ? String(value) : "—";
const ui = {view: "needs-user", core: null, loading: false};
const pageMeta = {
  overview: ["概览", "待处理事项与最近进展"],
  "needs-user": ["问题", "这里只放需要你决定或亲自操作的事项"],
  records: ["处理记录", "AI 负责核查和处理；受阻、暂停与已结束的记录都保留"],
  automatic: ["AI 负责", "实际核查结果、下一步与当前卡点"],
  activity: ["运行记录", "本机记录与来源状态，逐条保留处理依据"],
  processes: ["进程", "PSI 提供的系统运行状态"],
  locks: ["锁与维护", "PSI 提供的维护与恢复信息"],
  history: ["历史", "历史子项与已结束问题，保留证据供查阅"],
};
function formatTime(value, withDate = false) {
  if (!value || !/(?:Z|[+-]\d\d:\d\d)$/.test(value)) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", hour12: false,
    ...(withDate ? {month: "numeric", day: "numeric"} : {}), hour: "2-digit", minute: "2-digit"}).format(date);
}
async function api(path, options = {}) {
  const response = await fetch(path, {method: options.method || "GET", credentials: "same-origin",
    cache: "no-store", signal: options.signal, headers: {"X-PSI-Session": csrf, ...(options.body ? {"Content-Type": "application/json"} : {})},
    ...(options.body ? {body: JSON.stringify(options.body)} : {})});
  const value = await response.json();
  if (!response.ok) throw new Error(value.message || "本机请求未完成");
  return value;
}
function announce(message) {
  text("#live-status", message);
  text("#maintenance-banner", message);
  $("#maintenance-banner").hidden = !message;
}
function showPage(view, targetId = null) {
  if (!pageMeta[view]) return;
  ui.view = view;
  $$('[data-view]').forEach(node => {node.hidden = node.dataset.view !== (view === "records" ? "needs-user" : view);});
  $$('[data-page]').forEach(node => {if (node.dataset.page === view) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");});
  text("#page-title", pageMeta[view][0]); text("#page-subtitle", pageMeta[view][1]);
  if (["processes", "locks", "history"].includes(view)) $("#system-nav").open = true;
  if (["needs-user", "records"].includes(view)) {
    $("#workbench-filters").hidden = view === "needs-user";
    return workbench.showScope(view === "needs-user" ? "attention" : "records", targetId).catch(error => announce(error.message));
  }
}
function quietRows(rows) {
  return rows.length ? rows.map(row => `<details><summary class="quiet-row"><div><strong>${esc(row.title)}</strong><p>${esc(row.explanation || row.summary || row.reason)}</p></div><span>${esc(row.local_answer ? "已在本机记录，待同步" : row.state_label)}</span></summary><div class="record-detail">${[row.background, row.evidence_summary, row.completion_rule].filter(Boolean).map(s => `<p>${esc(s)}</p>`).join("")}${row.local_answer ? `<p>${esc(row.local_answer.content || row.local_answer.message)}</p>` : ""}<button type="button" class="text-button" data-open-record="${esc(row.id)}">查看完整详情与对话</button></div></details>`).join("") : '<p class="empty">暂无可展示记录</p>';
}
function bindRecords(root = document) {
  $$('[data-open-record]', root).forEach(button => button.onclick = async () => {
    await showPage("records", button.dataset.openRecord);
  });
}
function renderHistory() {
  const needle = $("#history-search").value.trim().toLocaleLowerCase();
  $("#history-list").innerHTML = (ui.core?.history || []).map(group => {
    const rows = group.items.filter(row => [row.title, row.summary, row.evidence_summary].join(" ").toLocaleLowerCase().includes(needle));
    return rows.length ? `<section class="record-group"><h3>${esc(group.title)} · ${rows.length}</h3>${quietRows(rows)}</section>` : "";
  }).join("") || '<p class="empty">暂无符合条件的历史记录</p>';
  bindRecords($("#history-list"));
}
function renderState(value) {
  ui.core = value;
  const healthy = value.status === "ok";
  text("#service-label", healthy ? "本机服务正常" : value.status === "partial" ? "已读取部分状态" : "未能读取");
  $("#service-dot").className = `service-dot ${healthy ? "success" : "error"}`;
  text("#updated-at", [value.synthetic ? "虚构示例" : "", `更新于 ${formatTime(value.generated_at)}`].filter(Boolean).join(" · "));
  if (healthy) announce(value.review_notice || (value.systems.length ? `系统有 ${value.systems.length} 项状态需要核查，可在更多中查看；不要求你猜测决定。` : ""));
  if (!healthy) announce(value.status === "partial" ? "部分事项仍待核查。你可以查看已读取的记录和每条的核查情况。" : "未能读取 PSI 状态，请点击刷新重试。");
  const needs = count(value.metrics.needs);
  text("#hero-eyebrow", "待你处理");
  text("#hero-title", needs === "—" ? "当前责任归属尚未核实" : Number(needs) ? `${needs} 个问题待处理` : "当前没有需要你处理的问题");
  text("#hero-detail", "AI 的处理进度和历史保留在处理记录中。");
  text("#hero-next", "查看问题"); $("#hero-next").hidden = false;
  for (const key of ["needs", "automatic", "resolved", "history"]) text(`#metric-${key}`, count(value.metrics[key]));
  text("#needs-badge", needs); $("#needs-badge").hidden = needs === "0";
  text("#overall-state", value.synthetic ? "虚构示例" : healthy ? "PSI 只读来源" : "来源状态待核实");
  $("#source-list").innerHTML = value.sources.map(source => `<div class="source-row"><span class="state-dot neutral"></span><div><strong>${esc(source.title)}</strong><span>${esc(source.summary || source.reason)}</span></div><time>—</time></div>`).join("") || '<p class="empty">暂无来源信息</p>';
  $("#recent-resolved").innerHTML = quietRows(value.recent_resolved);
  text("#automatic-count", count(value.metrics.automatic));
  $("#automatic-list").innerHTML = quietRows(value.automatic);
  $("#process-list").innerHTML = value.systems.map(row => `<article class="diagnostic-card"><h3>${esc(row.title)}</h3><p>${esc(row.summary)}</p>${row.runtime ? `<p>${esc(row.runtime)}</p>` : ""}<small>${esc(row.reason)}</small></article>`).join("") || '<p class="empty">暂无进程状态</p>';
  $("#lock-list").innerHTML = value.systems.map(row => `<article class="diagnostic-card"><h3>${esc(row.title)}</h3><p>${esc(row.recovery || row.reason || "来源未提供锁与维护信息")}</p></article>`).join("") || '<p class="empty">来源未提供锁与维护信息</p>';
  const actionNames = {answer_choice: "记录问题选择", sync: "同步请求", resolve_plan: "确认方案", defer: "延期处理", continue_automatic: "继续自动观察", write_memory: "补充新业务记忆"};
  $("#activity-list").innerHTML = value.events.slice().reverse().map(row => `<li><span class="dot"></span><div><strong>${esc(actionNames[row.action] || "本机记录")}</strong><p>${esc(row.message)}${row.content ? " · " + esc(row.content) : ""}</p></div><time>${esc(formatTime(row.created_at, true))}</time></li>`).join("") || '<li class="empty">暂无本机操作记录</li>';
  renderHistory(); bindRecords();
}
async function loadAll(refresh = false) {
  if (ui.loading) return;
  ui.loading = true;
  $("#sync-all").disabled = true;
  text("#sync-all", "正在刷新…");
  $("#workspace").setAttribute("aria-busy", "true");
  if (refresh) workbench.invalidate(); $("#refresh").classList.add("loading");
  try {
    const value = await api(refresh ? "/api/refresh" : "/api/state", refresh ? {method: "POST", body: {}} : {});
    renderState(value);
    if (["needs-user", "records"].includes(ui.view)) await workbench.load(true);
  } catch (error) {text("#service-label", "未能读取"); $("#service-dot").className = "service-dot error"; announce(error.message);}
  finally {
    ui.loading = false; $("#refresh").classList.remove("loading");
    $("#sync-all").disabled = false; text("#sync-all", "刷新事项");
    $("#workspace").removeAttribute("aria-busy");
  }
}
function setAppearance(value) {
  if (!["system", "light", "dark"].includes(value)) value = "system";
  $("#appearance").value = value;
  if (value === "system") document.documentElement.removeAttribute("data-theme"); else document.documentElement.dataset.theme = value;
  try {localStorage.setItem("psi-sync-center:appearance", value);} catch (_) { /* theme still works */ }
}
document.addEventListener("DOMContentLoaded", () => {
  let theme = "system"; try {theme = localStorage.getItem("psi-sync-center:appearance") || theme;} catch (_) {}
  setAppearance(theme);
  $("#appearance").addEventListener("change", event => setAppearance(event.target.value));
  $$('[data-page]').forEach(button => button.addEventListener("click", () => showPage(button.dataset.page)));
  $$('[data-jump]').forEach(button => button.addEventListener("click", () => showPage(button.dataset.jump)));
  $("#history-search").addEventListener("input", renderHistory);
  $("#refresh").addEventListener("click", () => loadAll(true));
  $("#hero-next").addEventListener("click", () => showPage("needs-user"));
  $("#sync-all").addEventListener("click", () => loadAll(true));
  $("#sync-confirm").addEventListener("click", async event => {
    event.preventDefault(); const button = event.currentTarget; button.disabled = true;
    try {const result = await api("/api/sync", {method: "POST", body: {request_id: $("#sync-dialog").dataset.requestId}}); $("#sync-dialog").close(); announce(result.message); await loadAll();}
    catch (error) {announce(error.message);} finally {button.disabled = false;}
  });
  workbench.bind(); showPage("needs-user"); loadAll();
});
