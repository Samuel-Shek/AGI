"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  let token = "";
  let currentState = null;
  let activityTimer;
  let loadingState = false;
  let workPending = false;
  const pendingControls = new Set();

  const names = {
    paused: "已暂停", observing: "观察中", stopped: "已停止", healthy: "状态正常",
    recovered: "已有恢复证据", waiting: "等待核对", fault: "发现异常", unknown: "证据不足",
    revoked: "已撤销授权", takeover: "已人工接管", taken_over: "已人工接管", human_control: "已人工接管",
    enabled: "可用", available: "可用", active: "可用", allowed: "可用", ready: "已就绪",
    disabled: "未启用", blocked: "条件未满足", pending: "待确认", unconfigured: "尚未配置",
    not_authorized: "尚未授权", pending_confirmation: "身份待确认", unavailable: "暂不可用",
    verified: "有核验依据", partial: "部分证据", insufficient: "证据不足",
    observed: "已观察", candidate: "候选问题", triaged: "已判断优先级", planned: "计划已准备",
    building: "隔离改进中", verifying: "验证中", release_ready: "待发布核验", deploying: "发布中",
    measuring: "观察实际效果", retained: "保留改进", reverted: "已回退", no_change: "无需改动",
    cancelled: "已取消", completed: "已完成", done: "已完成", todo: "待处理", queued: "已排队",
    interrupted: "执行中断", effect_unknown: "操作结果待核对", investigate: "需要调查", observe: "继续观察",
    pass: "通过", passed: "通过", fail: "未通过", failed: "未通过", success: "成功",
    pending_owner_confirmation: "身份待确认", healthy_snapshot: "当前状态摘要正常",
    not_connected: "尚未接入", unconfirmed: "身份待确认",
    sources_unavailable: "约定的状态来源暂不可用，继续等待证据。",
    snapshot_incomplete: "状态摘要不完整，不能判断业务已经恢复。",
    invalid_schema: "状态记录格式不符合约定，需要核对来源。",
    invalid_timestamp: "记录时间缺失或顺序异常，需要核对。",
    future_timestamp: "记录包含超前时间，需要核对时钟。",
    stale_observation: "现有记录已超出有效时间，不能用来判断当前状态。",
    invalid_accounting: "尝试次数与等待记录不一致，需要核对。",
    request_pending: "新请求尚待核验，旧成功记录不能替代本次结果。",
    receipt_order_uncertain: "成功与失败回执顺序尚未确认。",
    receipt_binding_mismatch: "回执与当前任务的对应关系不足。",
    worker_receipt_conflict: "执行状态与回执不一致，需要进一步核对。",
    validation_running: "现有项目控制器正在核验，暂不重复操作。",
    resource_wait: "当前任务正在等待已有资源条件。",
    retry_scheduled: "现有项目控制器已安排重验，继续观察。",
    waiting_for_stability: "资料仍在变化，等待稳定后再判断。",
    latest_failure: "存在与当前任务匹配的新失败回执，需要调查。",
    state_unresolved: "状态尚未收敛，暂时无法确认结果。",
    receipt_missing: "缺少匹配的成功回执，不能宣称恢复。",
    receipt_context_mismatch: "回执与当前核验条件不一致。",
    receipt_generation_mismatch: "回执与当前资料版本不一致。",
    historical_failure_recovered: "旧失败之后已有匹配的恢复证据；保留现状并观察防复发。",
    matching_validation_receipt: "存在与本次资料和任务匹配的成功回执；仅确认该回执覆盖的范围。",
  };
  const eventNames = {pause:"已暂停",stop:"已停止",resume:"恢复已授权观察",revoke:"已撤销项目授权",takeover:"已人工接管",observation:"读取一次状态",replay:"运行固定回放",startup:"本地控制器启动",controller_started:"本地控制器启动",state_changed:"本地状态已记录",operator_read_session:"已登记限次只读授权",observation_rejected:"本次观察未完成",observation_recorded:"观察结果已记录",replay_completed:"固定回放结果已记录",operator_control:"已执行本地控制操作",task_enqueued:"问题进入工作队列",task_claimed:"任务已领取",completion_rejected:"过期或无效回执已拒绝",execution_completed:"任务回执已记录",task_deferred:"任务已暂缓",effect_prepared:"已准备动作回执",effect_transition:"动作状态已记录",execution_retired:"已结束失效执行",controller_recovered:"已核对上次运行状态"};
  const good = new Set(["healthy", "recovered", "enabled", "available", "active", "allowed", "verified", "passed", "pass", "ready", "retained", "success", "completed", "done"]);
  const bad = new Set(["fault", "failed", "fail", "effect_unknown"]);
  const warning = new Set(["unknown", "waiting", "partial", "insufficient", "blocked", "pending", "not_authorized", "pending_owner_confirmation", "pending_confirmation"]);
  const isAvailable = (cap) => Boolean(cap && ["enabled", "available", "active", "allowed", "ready"].includes(cap.status));
  const text = (value, fallback = "暂无记录") => value === null || value === undefined || value === "" ? fallback : String(value);
  const label = (value) => names[value] || text(value, "尚未确认");

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = String(content);
    return node;
  }

  function badge(status, customLabel) {
    const color = good.has(status) ? "good" : bad.has(status) ? "bad" : warning.has(status) ? "warn" : "neutral";
    return element("span", `badge ${color}`, customLabel || label(status));
  }

  function formatTime(value) {
    if (value === null || value === undefined || value === "") return "暂无时间证据";
    let candidate = value;
    if (typeof value === "number" && value < 10_000_000_000) candidate = value * 1000;
    const date = new Date(candidate);
    if (Number.isNaN(date.getTime())) return "时间格式待核对";
    return new Intl.DateTimeFormat("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(date);
  }

  function detail(value, title = "查看依据与记录") {
    const container = element("details", "detail-block");
    container.append(element("summary", "", title));
    // All controller and external content is text. No HTML interpretation.
    container.append(element("pre", "", JSON.stringify(value, null, 2)));
    return container;
  }

  function empty(title, description) {
    const container = element("div", "empty-state");
    const mark = element("span", "empty-symbol", "—");
    mark.setAttribute("aria-hidden", "true");
    const copy = element("div");
    copy.append(element("strong", "", title), element("p", "", description));
    container.append(mark, copy);
    return container;
  }

  function notify(message, error = false) {
    clearTimeout(activityTimer);
    const node = $("activity-message");
    node.textContent = message;
    node.classList.toggle("is-error", error);
    node.hidden = false;
    // Errors remain visible until another action supplies evidence.
    if (!error) activityTimer = setTimeout(() => { node.hidden = true; }, 8500);
  }

  function authRequired(message) {
    token = "";
    currentState = null;
    $("workspace").hidden = true;
    $("auth-panel").hidden = false;
    $("connection-state").textContent = "访问凭证待确认";
    if (message) { $("connection-error").textContent = message; $("connection-error").hidden = false; }
  }

  async function api(path, payload) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(path, {
        method: payload === undefined ? "GET" : "POST",
        credentials: "omit", cache: "no-store", redirect: "error",
        headers: {"X-AI-CEO-Token":token, ...(payload === undefined ? {} : {"Content-Type":"application/json"})},
        body: payload === undefined ? undefined : JSON.stringify(payload), signal: controller.signal,
      });
      const data = await response.json();
      if (!response.ok) {
        if (response.status === 401) authRequired(data.error?.message);
        throw new Error(data.error?.message || "本地请求未完成。请核对状态。");
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error(payload === undefined ? "读取状态超时，保留上次记录；当前状态尚未确认。" : "等待操作结果超时；是否完成尚未确认，请先刷新状态，不要直接重复操作。");
      if (error instanceof TypeError) throw new Error(payload === undefined ? "无法连接本地服务，以下记录可能已过期。请检查启动窗口。" : "与本地服务的连接中断；操作结果尚未确认，请先核对状态。");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function renderControls() {
    const state = currentState;
    if (!state) return;
    const observation = (state.capabilities || []).find((item) => item.id === "live_observation");
    const replay = (state.capabilities || []).find((item) => item.id === "replay");
    $("observe-button").disabled = workPending || state.mode === "stopped" || !isAvailable(observation);
    $("replay-button").disabled = workPending || state.mode === "stopped" || !isAvailable(replay);
    $("observe-reason").textContent = text(observation?.reason, "只读观察尚未授权，当前不会读取项目状态。");
    $("replay-reason").textContent = state.mode === "stopped" ? "系统已停止，本地回放也已阻止。恢复须满足既有授权条件。" : text(replay?.reason, "固定场景回放尚未就绪。");
    $("pause-button").disabled = ["paused", "stopped"].includes(state.mode) || pendingControls.has("pause");
    $("stop-button").disabled = state.mode === "stopped" || pendingControls.has("stop");
    const canResume = state.controls?.can_resume === true || (!state.controls && isAvailable(observation));
    $("resume-button").disabled = state.mode === "observing" || !canResume || pendingControls.has("resume");
    $("resume-button").title = canResume ? "只恢复当前有效的只读观察授权" : "尚无有效观察授权，面板不能自行扩大权限";
  }

  function renderAssets(assets) {
    const parent = $("asset-list");
    parent.replaceChildren();
    if (!assets.length) { parent.append(empty("还没有接入对象", "项目身份和只读入口确认后，才会开始观察。")); return; }
    for (const asset of assets) {
      const row = element("article", "asset-row");
      const identity = element("div");
      identity.append(element("h3", "asset-name", text(asset.name, "未命名项目")), element("p", "asset-id", text(asset.id)));
      const status = element("div");
      const effectiveStatus = asset.revoked === true ? "revoked" : asset.takeover === true ? "takeover" : asset.status;
      const reason = asset.revoked === true ? "此项目的观察授权已撤销；历史记录仍可查看，后续观察已阻止。" : asset.takeover === true ? "此项目已由你接管；AI 后续观察已冻结。" : (names[asset.reason] || text(asset.reason, "尚无足够证据判断当前状态。"));
      status.append(badge(effectiveStatus), element("p", "asset-description", reason));
      status.append(element("p", "asset-meta", `${label(asset.evidence_quality || "insufficient")} · ${formatTime(asset.observed_at)}`));
      if (asset.evidence || asset.latest_observation || asset.observation) status.append(detail(asset.evidence || asset.latest_observation || asset.observation));
      const controls = element("div", "asset-controls");
      for (const [action, title] of [["takeover", "人工接管"], ["revoke", "撤销此项目授权"]]) {
        const button = element("button", "text-button", title);
        button.type = "button";
        button.setAttribute("aria-label", `${title}：${text(asset.name, asset.id)}`);
        const alreadyRevoked = asset.status === "revoked" || asset.revoked === true;
        const alreadyTakenOver = ["takeover", "taken_over", "human_control"].includes(asset.status) || asset.takeover === true || asset.taken_over === true;
        const enrolled = asset.id === "sync-center";
        button.disabled = alreadyRevoked || (action === "takeover" && alreadyTakenOver) || !enrolled;
        if (!enrolled) button.title = "此项目尚未接入，没有可变更的授权";
        button.addEventListener("click", () => performControl(action, asset.id, button));
        controls.append(button);
      }
      row.append(identity, status, controls);
      parent.append(row);
    }
  }

  function renderTasks(tasks) {
    const parent = $("task-list");
    parent.replaceChildren();
    $("task-count").textContent = `${tasks.length} 项记录`;
    if (!tasks.length) { parent.append(empty("队列里暂时没有任务", "不会为了显得忙碌而制造需求。新任务必须对应可追溯的问题。")); return; }
    for (const task of tasks) {
      const row = element("article", "task-row");
      const summary = element("div", "task-summary");
      const copy = element("div");
      copy.append(element("h3", "task-title", text(task.title, "未命名任务")), element("p", "task-id", `${text(task.asset_id, "资产待确认")} · ${text(task.id, "任务编号待确认")}`));
      summary.append(copy, badge(task.phase || task.status || "unknown"));
      row.append(summary, detail(task, "查看问题、证据与处理结果"));
      parent.append(row);
    }
  }

  function renderReplay(result) {
    const parent = $("replay-result");
    parent.replaceChildren();
    if (!result) { parent.append(element("p", "empty-inline", "还没有回放结果。运行固定场景后，这里展示实际回执。")); return; }
    const passed = result.passed === true || ["passed", "pass", "success"].includes(result.verdict || result.status);
    const failed = result.passed === false || ["failed", "fail"].includes(result.verdict || result.status);
    const line = element("div", "result-heading");
    line.append(badge(passed ? "passed" : failed ? "failed" : "unknown"));
    if (Number.isFinite(result.total)) line.append(element("span", "quiet-label", `${Number.isFinite(result.passed) ? `${result.passed} / ` : ""}${result.total} 个固定场景`));
    parent.append(line, element("p", "result-description", "这是固定输入下的本地验收结果，不能替代真实使用效果。"), detail(result, "查看回放场景和验收回执"));
  }

  function renderBaseline(baseline, metrics) {
    const parent = $("baseline-result");
    parent.replaceChildren();
    const known = [
      ["manual_interventions", "人工介入次数"], ["repeated_faults", "重复故障"],
      ["verified_minutes_saved", "已验证节省时间（分钟）"], ["verified_improvements", "已验证改善"],
    ];
    const rows = known.filter(([key]) => metrics && typeof metrics[key] === "number" && Number.isFinite(metrics[key]));
    if (rows.length) {
      const list = element("dl", "metric-list");
      for (const [key, name] of rows) { const row = element("div", "metric-row"); row.append(element("dt", "", name), element("dd", "", metrics[key])); list.append(row); }
      parent.append(list);
    } else parent.append(element("p", "empty-inline", "尚无可比较的真实使用基线。没有数据时，不展示节省比例或改善趋势。"));
    if (baseline && Object.keys(baseline).length) parent.append(detail(baseline, "查看基线采集状态"));
  }

  function renderAudit(audit) {
    const parent = $("audit-list");
    parent.replaceChildren();
    $("audit-count").textContent = `（${audit.length} 条）`;
    if (!audit.length) { parent.append(element("p", "empty-inline", "暂无操作记录。")); return; }
    for (const event of audit) {
      const row = element("div", "audit-entry");
      const name = event.event === "operator_control" && event.details?.action ? eventNames[event.details.action] : eventNames[event.event];
      row.append(element("time", "", formatTime(event.occurred_at || event.at || event.timestamp || event.created_at || event.ts)), element("p", "", name || text(event.event || event.action, "控制记录")));
      row.append(detail(event, "查看操作回执"));
      parent.append(row);
    }
  }

  function render(state) {
    currentState = state;
    $("workspace").hidden = false;
    $("auth-panel").hidden = true;
    $("version").textContent = `本地验收版 · ${text(state.version, "版本待核对")}`;
    $("updated-at").textContent = formatTime(state.updated_at);
    $("mode-badge").replaceWith(Object.assign(badge(state.mode), {id: "mode-badge"}));
    const continuous = state.scheduler?.runtime_started === true;
    const modeCopy = {
      paused: ["自动行动已暂停", continuous ? "持续观察已暂停。仍可手动运行本地回放；观察需要恢复现有授权。" : "已暂停自动调度。手动的一次观察和回放仍按授权执行。"],
      observing: ["按现有授权观察", "观察范围以当前有效授权为准；模型、代码执行和发布仍分别受控。"],
      stopped: ["系统已停止", "观察和回放动作均已阻止。停止状态持续保留，不会因重启自行解除。"],
    };
    const copy = modeCopy[state.mode] || ["状态需要核对", "没有有效状态证据，暂不继续操作。"];
    $("mode-title").textContent = copy[0];
    $("mode-reason").textContent = copy[1];
    $("controls-explanation").textContent = continuous ? "暂停持续观察，手动回放仍可用；恢复后才能继续观察。停止阻止所有新动作。恢复不会扩大授权。" : "暂停自动调度；手动的一次观察和回放仍按授权执行。停止阻止所有新动作。恢复不会扩大授权。";
    $("stage-badge").textContent = `阶段 ${text(state.stage, "待确认")} · ${state.stage === 0 ? "接入与固定回放" : "按已验收范围运行"}`;
    $("deployment-state").textContent = state.scheduler?.installed === true ? (state.scheduler.enabled === true ? "已安装常驻调度 · 以授权为限" : "调度已安装，尚未启用") : "未安装常驻任务 · 关闭本地服务后停止运行";
    renderAssets(Array.isArray(state.assets) ? state.assets : []);
    renderTasks(Array.isArray(state.tasks) ? state.tasks : []);
    renderReplay(state.last_replay);
    renderBaseline(state.baseline, state.metrics);
    renderAudit(Array.isArray(state.audit) ? state.audit : []);
    const caps = $("capability-list");
    caps.replaceChildren();
    for (const cap of state.capabilities || []) {
      const item = element("article", "capability-item");
      const title = element("div", "capability-title");
      title.append(element("h3", "", text(cap.name, "能力待确认")), badge(cap.status));
      item.append(title, element("p", "", text(cap.reason, "当前条件尚未说明。")));
      caps.append(item);
    }
    if (!caps.childElementCount) caps.append(empty("能力状态尚未确认", "没有有效条件时，不开放动作。"));
    const boundary = $("boundary-list");
    boundary.replaceChildren();
    for (const item of state.boundaries || []) boundary.append(element("li", "", item));
    renderControls();
  }

  async function refresh(silent = false) {
    if (!token || loadingState) return;
    loadingState = true;
    try {
      const state = await api("/api/state");
      render(state);
      $("connection-error").hidden = true;
      $("connection-state").textContent = "本地连接正常";
      if (!silent) notify("已读取最新本地状态。");
    } catch (error) {
      $("connection-error").textContent = error.message;
      $("connection-error").hidden = false;
      $("connection-state").textContent = "当前连接未确认";
    } finally { loadingState = false; }
  }

  async function performControl(action, assetId, button) {
    if (pendingControls.has(action)) return;
    pendingControls.add(action);
    if (button) button.disabled = true;
    renderControls();
    try {
      const result = await api("/api/control", {action, ...(assetId ? {asset_id:assetId} : {})});
      if (result.ok === false || result.allowed === false || result.denied === true) throw new Error(result.message || result.reason || "当前授权不允许这项操作。");
      notify(eventNames[action] || "控制操作已提交。");
      await refresh(true);
    } catch (error) { notify(error.message, true); await refresh(true); }
    finally { pendingControls.delete(action); if (button?.isConnected) button.disabled = false; renderControls(); }
  }

  async function performWork(kind) {
    if (workPending) return;
    workPending = true;
    renderControls();
    notify(kind === "replay" ? "正在运行固定场景；不会触碰生产业务数据。" : "正在读取获准的状态字段。");
    try {
      const result = await api(`/api/${kind}`, {});
      if (result.ok === false || result.denied === true) throw new Error(result.message || result.reason || "当前条件不允许完成这项操作。");
      notify(kind === "replay" ? "回放已返回，请查看实际验收结果。" : "本次状态读取已返回，请查看观察依据。");
      await refresh(true);
    } catch (error) { notify(error.message, true); await refresh(true); }
    finally { workPending = false; renderControls(); }
  }

  $("connect-button").addEventListener("click", async () => {
    const candidate = $("access-token").value.trim();
    $("access-token").value = "";
    if (!/^[A-Za-z0-9_-]{32,128}$/.test(candidate)) { notify("请输入本地启动输出中的完整访问凭证。", true); return; }
    token = candidate;
    await refresh(true);
  });
  $("access-token").addEventListener("keydown", (event) => { if (event.key === "Enter") $("connect-button").click(); });
  document.querySelectorAll("[data-control]").forEach((button) => button.addEventListener("click", () => performControl(button.dataset.control, undefined, button)));
  $("observe-button").addEventListener("click", () => performWork("observe"));
  $("replay-button").addEventListener("click", () => performWork("replay"));
  $("refresh-button").addEventListener("click", () => refresh());
  const fragment = location.hash;
  const bootstrap = fragment.match(/^#(?:token=)?([A-Za-z0-9_-]{32,128})$/);
  if (fragment.startsWith("#token=") || bootstrap) history.replaceState(null, "", location.pathname);
  if (bootstrap) { token = bootstrap[1]; refresh(true); } else authRequired();
  // Refresh persisted state only. This never schedules an observation or model call.
  setInterval(() => { if (token && document.visibilityState === "visible" && !workPending) refresh(true); }, 20000);
})();
