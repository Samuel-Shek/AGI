"use strict";
// Adapted decision-center workbench.js / app.js detail and conversation markup.
const workbench = (() => {
  const state = {scope: "attention", selected: null, detail: null, chat: null, request: 0, listRequest: 0, pending: null, drafts: new Map(), timers: new Map(), draftWrites: new Map(), dirty: new Set(), handoffs: new Set()};
  const endpoint = (id, suffix = "") => `/api/workbench/${encodeURIComponent(id)}${suffix}`;
  const labels = {open: "问题与进展", records: "处理记录", attention: "问题", automatic: "AI 负责", waiting: "等待条件", closed: "已结束", all: "全部"};
  function showSummary(visible) {
    $("#problem-summary").hidden = !visible;
    $("#case-workbench").hidden = visible;
  }
  function renderProblemSummary(attention, records) {
    const rows = records.items;
    const pending = rows.filter(row => row.review_valid === false);
    const known = attention.status === "ok" && attention.counts.attention === 0 && records.status === "ok";
    const title = known ? "当前没有需要你处理的问题" : "已读取的事项中，暂未确认有需要你处理的问题";
    const summary = pending.length ? `已读取 ${rows.length} 条处理记录，其中 ${pending.length} 条需要重新核查。` : `已读取 ${rows.length} 条处理记录。`;
    $("#problem-summary").innerHTML = `<header class="section-header"><div><h2>${title}</h2><p>${summary}</p></div></header>
      <button type="button" id="all-problem-records" class="text-button">查看全部处理记录</button>`;
    $("#all-problem-records").addEventListener("click", () => showPage("records"));
    showSummary(true);
  }
  async function load(revalidate = false) {
    const ticket = ++state.listRequest;
    if (revalidate) showSummary(false);
    const value = await api(`/api/workbench?scope=${state.scope}`);
    if (ticket !== state.listRequest) return;
    if (state.scope === "attention" && !value.items.length && value.status !== "unavailable") {
      const records = await api("/api/workbench?scope=records");
      if (ticket !== state.listRequest) return;
      if (records.status !== "unavailable") {
        clearSelection();
        renderProblemSummary(value, records);
        return;
      }
    }
    showSummary(false);
    $$('[data-issue-scope]').forEach(button => {button.textContent = `${labels[button.dataset.issueScope]} ${count(value.counts[button.dataset.issueScope])}`; button.setAttribute("aria-pressed", String(button.dataset.issueScope === state.scope));});
    const total = count(value.counts[state.scope]);
    const visibleCount = total === "—" && value.status === "partial" ? `已读取 ${value.items.length} 条` : total;
    text("#needs-count-label", `${labels[state.scope]} · ${visibleCount}`);
    text("#needs-item-count", state.scope === "attention" ? "只列需要你决定或亲自操作的事项。" : "保留实际结果、下一步和卡点；记录不代表已经解决。");
    if (state.selected && !value.items.some(row => row.id === state.selected) && !state.recordTarget) clearSelection();
    $("#needs-list").innerHTML = value.items.map(row => `<button type="button" class="case-row" role="option" aria-selected="${row.id === state.selected}" data-case-id="${esc(row.id)}"><small>${esc(row.responsibility_label || row.source_label)} · ${esc(row.state_label)}${row.baseline_number ? ` · 原第 ${row.baseline_number} 项` : row.is_new ? " · 本次以外" : ""}</small><strong>${esc(row.title)}</strong><span>${esc(row.local_answer ? "有本机笔记；原事项仍保留" : row.list_summary ?? (row.reason || row.summary))}</span></button>`).join("") || (value.status === 'ok' ? '<p class="empty">当前没有此类问题。</p>' : '<p class="empty">当前分类尚未核清，请刷新核查。</p>');
    $$('[data-case-id]').forEach(button => button.addEventListener("click", () => open(button.dataset.caseId)));
    if (revalidate && state.selected) await open(state.selected);
    else if (!state.selected && value.items.length) await open(value.items[0].id);
    if (value.status === "unavailable") {state.selected = null; state.detail = null; state.chat = null; $("#needs-detail").innerHTML = '<p class="empty">未能读取，请刷新重试。</p>'; $("#case-conversation").innerHTML = '';}
  }
  function invalidate() {
    state.request++; state.detail = null; state.chat = null;
    $("#needs-detail").innerHTML = '<p class="empty">正在重新核对当前显示范围…</p>';
    $("#case-conversation").innerHTML = '';
  }
  function navigate(id) {
    state.recordTarget = id; state.selected = id; invalidate(); return load(true);
  }
  function clearSelection() {
    state.selected = null; state.request++; state.detail = null; state.chat = null;
    $("#needs-detail").innerHTML = '<p class="empty">选择一条记录，查看依据与下一步。</p>';
    $("#case-conversation").innerHTML = "";
  }
  function showScope(scope, id = null) {
    state.scope = scope; state.recordTarget = id; showSummary(false); clearSelection();
    return id ? navigate(id) : load();
  }
  async function open(id) {
    const ticket = ++state.request;
    state.selected = id; state.detail = null; state.chat = null;
    $("#needs-detail").className = "detail-pane";
    $("#needs-detail").innerHTML = '<div class="workbench-loading"><span class="loading-ring"></span><strong>正在读取证据</strong></div>';
    $("#case-conversation").innerHTML = '';
    $$('[data-case-id]').forEach(button => button.setAttribute("aria-selected", String(button.dataset.caseId === id)));
    try {
      const [detail, chat] = await Promise.all([api(endpoint(id)), api(endpoint(id, "/conversation"))]);
      if (ticket !== state.request) return;
      if (state.scope === "attention" && detail.status !== "attention") {
        clearSelection(); await load(); return;
      }
      state.detail = detail; state.chat = chat; renderDetail(); renderChat();
    } catch (error) {
      if (ticket !== state.request) return;
      $("#needs-detail").innerHTML = `<div class="workbench-error"><strong>${esc(error.message)}</strong><button id="detail-retry" type="button">重新读取</button></div>`;
      $("#detail-retry").addEventListener("click", () => open(id));
      $("#case-conversation").innerHTML = '<p class="empty">详情未通过读取校验，暂不能记录。</p>';
    }
  }
  function section(number, title, body) {
    return body ? `<section class="issue-section"><span class="issue-number">${number}</span><div><h3>${title}</h3><p>${esc(body)}</p></div></section>` : "";
  }
  function renderDetail() {
    if (state.detail && !state.detail.responsibility) return renderLegacyDetail();
    const item = state.detail;
    if (!item) return;
    const plan = item.synthetic_plan || item.recommendation;
    const ended = item.status === "closed";
    $("#needs-detail").innerHTML = `<div class="detail-heading"><span class="eyebrow">问题与进展</span><h2>${esc(item.title)}</h2><div class="detail-tags"><span>${esc(item.responsibility_label || item.source_label)}</span><span>${esc(item.state_label)}</span></div></div>
      ${item.local_answer ? `<section class="detail-callout"><p>本机笔记已保留，不能据此判定事项结束。</p></section>` : ""}
      ${item.question ? `<section class="decision-brief"><span class="eyebrow">需要你决定</span><h3>${esc(item.question)}</h3></section>` : ""}
      ${item.user_action ? section("01", "需要你做的最少操作", item.user_action) : ""}
      ${section("02", "核查结论", item.basis || item.reason)}
      ${section("03", "下一步", item.next_step || item.recommendation)}
      ${item.outcome ? section("04", "实际结果", item.outcome) : ""}
      <details class="issue-evidence"><summary>查看依据与完成条件</summary>
      ${section("", "问题背景", item.background)}${section("", "依据", item.evidence_summary)}${section("", "完成条件", item.completion_rule)}
      <p>核查时间：${esc(formatTime(item.reviewed_at, true))} · 依据版本：${esc(item.evidence_version || "待核清")}</p></details>`;
  }
  function renderLegacyDetail() {
    const item = state.detail;
    if (!item) return;
    const plan = item.synthetic_plan || item.recommendation;
    const ended = item.status === "closed";
    $("#needs-detail").innerHTML = `<div class="detail-heading"><span class="eyebrow">证据与判断</span><h2>${esc(item.title)}</h2><div class="detail-tags"><span>${esc(item.source_label)}</span><span>${esc(item.state_label)}</span></div></div>
      ${item.local_answer ? `<section class="plan-card"><span class="eyebrow">本机记录</span><p>已在本机记录，待同步</p><small>仅退出本网页待处理列表；Core 状态未改变。</small></section>` : ""}
      ${item.question ? `<section class="decision-brief"><span class="eyebrow">${esc(item.question_subject || "只有你知道的事")}</span><h3>${esc(item.question)}</h3></section><div class="decision-options">${(item.answer_options || []).map((option, index) => `<button type="button" class="secondary-button" data-answer-index="${index}" ${ended ? "disabled" : ""}>${esc(option.label)}</button>`).join("")}</div>` : ""}
      ${section("01", "具体是什么", item.background)}${section("02", item.status === "attention" ? "为什么需要你" : "当前处理情况", item.reason)}${section("03", "证据", item.evidence_summary)}${section("04", "做到什么才算结束", item.completion_rule)}
      ${item.reason && item.status === "attention" ? `<section class="detail-callout"><span class="eyebrow">为什么需要和你讨论</span><p>${esc(item.reason)}</p></section>` : ""}
      ${plan ? `<section class="plan-card"><span class="eyebrow">${item.synthetic_plan ? "AI 形成的待确认方案 · 合成示例" : "来源提供的待确认方案"}</span><p>${esc(plan)}</p><small>仅供查看和本机记录，不代表事实或操作已经完成。</small></section>` : ""}
      <section class="action-dock"><div><h3>下一步怎么处理</h3><p>${ended ? "已结束；对话仍可保留，本机记录不执行操作。" : "点击后直接进入 Codex；已有负责会话会直接打开。"}</p></div><div class="action-buttons"><button class="primary-button" type="button" data-case-action="resolve_plan" ${!plan || ended ? "disabled" : ""} title="${!plan ? "尚无可确认方案，可在 Codex 中讨论" : "直接在 Codex 中继续"}">确认方案</button><button class="secondary-button" type="button" data-case-action="defer" ${ended ? "disabled" : ""}>延期处理</button><button class="secondary-button" type="button" data-case-action="continue_automatic" ${ended ? "disabled" : ""}>继续自动观察</button><button class="memory-button" type="button" data-case-action="write_memory" ${ended ? "disabled" : ""}>补充新业务记忆</button></div></section>`;
    $$('[data-case-action]').forEach(button => button.addEventListener("click", () => choose(button.dataset.caseAction)));
    $$('[data-answer-index]').forEach(button => button.addEventListener("click", () => choose("answer_choice", item.answer_options[Number(button.dataset.answerIndex)])));
  }
  function messageMarkup(message) {
    const isUser = message.author === "user";
    const label = message.synthetic ? `${isUser ? "你" : "AI"} · 合成示例` : isUser ? "你" : "系统提示";
    return `<article class="conversation-message ${isUser ? "from-user" : "from-ai"}"><header><strong>${label}</strong>${message.synthetic ? '<span class="message-state">仅用于界面测试</span>' : ""}<time>${esc(formatTime(message.created_at, true))}</time></header><p>${esc(message.content)}</p>${message.focused_questions?.length ? `<div class="focused-questions"><strong>接下来需要你确认 · 合成</strong><ol>${message.focused_questions.map(q => `<li>${esc(q)}</li>`).join("")}</ol></div>` : ""}</article>`;
  }
  function renderChat() {
    const chat = state.chat, id = state.selected;
    if (!chat) return;
    const pane = $("#case-conversation"); pane.className = "conversation-pane";
    pane.innerHTML = `<header class="conversation-header"><div class="conversation-title"><span class="eyebrow chat-wide-title">问题对话</span><span class="eyebrow chat-narrow-title">本问题的对话</span><h2 class="chat-wide-title">和 AI 一起处理</h2><h2 class="chat-narrow-title">一起判断下一步</h2></div><span class="conversation-status archived">在 Codex 继续</span><p class="chat-boundary">事项和你的回复会一起交给 Codex</p></header>
      <div class="conversation-messages" aria-live="polite">${chat.messages.length ? chat.messages.map(messageMarkup).join("") : '<article class="conversation-onboarding"><strong>把你的想法直接告诉 AI</strong><p>在下面写下你对这件事的回复。</p></article>'}${chat.synthetic_plan ? `<article class="memory-proposal-card"><header><span class="eyebrow">可补充到业务记忆 · 合成示例</span><span class="fact-status">待确认</span></header><strong>个人偏好记录示例</strong><p>${esc(chat.synthetic_plan)}</p><dl><dt>内容归属</dt><dd>合成夹具，不来自真实用户</dd><dt>证据边界</dt><dd>仅供界面测试，本版不会写入记忆。</dd></dl></article>` : ""}</div>
      <div class="composer-wrap"><p id="chat-error" class="error-text" role="status"></p><label class="sr-only" for="case-input">给当前问题补充想法</label><textarea id="case-input" maxlength="4000" placeholder="写下你对这件事的回复…"></textarea><div class="composer-footer"><span id="draft-status">输入时自动保存草稿。</span><button class="send-button" type="button" id="case-codex">在 Codex 中继续</button></div></div>`;
    $("#case-codex").addEventListener("click", () => presentCodex($("#case-input").value));
    $("#case-codex").disabled = state.handoffs.has(id) || state.detail?.source_missing;
    if (state.detail?.kind === "closure" && !state.detail.codex_reply_available && !state.detail.codex_handoff_available) {
      text(".chat-boundary", state.detail.codex_thread_id
        ? "可以打开现有 Codex 会话，输入会保留在本机。"
        : "这项回复暂不能交给 Codex，输入会保留在本机。");
    }
    const input = $("#case-input");
    input.disabled = Boolean(chat.read_only || state.handoffs.has(id));
    input.value = state.drafts.has(id) ? state.drafts.get(id) : chat.draft;
    input.addEventListener("input", () => {
      state.drafts.set(id, input.value);
      state.dirty.add(id);
      reportPending();
      clearTimeout(state.timers.get(id));
      state.timers.set(id, setTimeout(() => saveDraft(id), 350));
    });
  }
  function reportPending() {
    if (typeof window !== "undefined") {
      window.webkit?.messageHandlers?.psiDraftPending?.postMessage(Boolean(state.dirty.size || state.handoffs.size));
      if (!state.dirty.size) $("#draft-recovery").hidden = true;
    }
  }
  async function saveDraft(id) {
    clearTimeout(state.timers.get(id));
    state.timers.delete(id);
    if (!state.dirty.has(id)) return true;
    const content = state.drafts.get(id);
    const previous = state.draftWrites.get(id);
    if (previous?.content === content) return previous.promise;
    const promise = (previous?.promise || Promise.resolve()).then(async () => {
      if (state.drafts.get(id) !== content) return true;
      try {
        await api(endpoint(id, "/draft"), {method: "POST", body: {content}, signal: globalThis.AbortSignal?.timeout?.(4000)});
        if (state.drafts.get(id) === content) {
          state.dirty.delete(id);
          reportPending();
          if (state.selected === id) text("#draft-status", "草稿已保存在本机。");
        }
        return true;
      } catch (error) {
        if (state.selected === id) text("#chat-error", "草稿尚未保存：" + error.message);
        return false;
      }
    });
    const write = {content, promise};
    state.draftWrites.set(id, write);
    try { return await promise; }
    finally { if (state.draftWrites.get(id) === write) state.draftWrites.delete(id); }
  }
  async function prepareToClose() {
    if (state.handoffs.size) {
      announce("正在核对交接结果，请稍后再关闭工作台。");
      return false;
    }
    document.body.inert = true;
    await Promise.all([...state.dirty].map(saveDraft));
    if (state.dirty.size || state.handoffs.size) {
      resumeEditing();
      return false;
    }
    return true;
  }
  function resumeEditing() {
    document.body.inert = false;
    if (state.dirty.size) {
      announce("工作台草稿尚未确认保存。输入已保留，请稍后再关闭。");
      $("#draft-recovery").hidden = false;
    }
  }
  async function presentCodex(content) {
    const id = state.selected, revision = state.detail?.revision;
    if (!id || !revision || state.handoffs.has(id)) return;
    const editor = $("#case-input"), draft = editor?.value || "";
    if (editor) editor.disabled = true;
    state.handoffs.add(id);
    reportPending();
    const show = message => {if (state.selected === id) text("#chat-error", message);};
    const button = $("#case-codex"); if (button) button.disabled = true;
    show("正在打开 Codex 并交接当前事项…");
    try {
      clearTimeout(state.timers.get(id));
      await state.draftWrites.get(id)?.promise;
      let result = await api(endpoint(id, "/codex-continue"), {method: "POST", body: {content, revision, draft}});
      show(result.message);
      for (let attempt = 0; result.phase === "pending" && attempt < 60; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        result = await api(endpoint(id, "/codex-status"), {method: "POST", body: {}});
        show(result.message);
      }
      if (["delivered", "opened"].includes(result.phase) && state.selected === id) {await open(id); show(result.message);}
      if (result.phase === "pending") show("仍在核对接收回执。稍后点击此按钮只会核对原交接，不会重复提交。");
    } catch (error) {show("交接结果待核对；输入保留。再次点击只核对原交接，请勿另行重复发送。");}
    finally {
      state.handoffs.delete(id);
      reportPending();
      if (state.selected === id && $("#case-input")) $("#case-input").disabled = Boolean(state.chat?.read_only);
      if (state.selected === id && $("#case-codex")) $("#case-codex").disabled = Boolean(state.detail?.source_missing);
    }
  }
  function choose(action, option = null) {
    const names = {resolve_plan: "我希望按当前事项的建议方案继续，请先核对原权限和证据。", defer: "我想讨论这件事的延期安排。", continue_automatic: "请核对这件事是否可在现有授权内继续处理。", write_memory: "我想讨论这件事是否需要补充记忆，请先核对归属和授权。"};
    presentCodex(option ? "我的选择：" + option.full : names[action] || "请核对当前事项。");
  }
  function preview() {
    const p = state.pending;
    const names = {answer_choice: "记录这个选择？", resolve_plan: "确认这份方案？", defer: "记录延期处理？", continue_automatic: "记录继续自动观察的意愿？", write_memory: "记录新业务记忆候选？"};
    text("#choice-title", names[p.action]);
    text("#choice-effect", "确认后只在本机记录，待同步。本版不会执行操作、安排提醒或写入 Core。");
    const plan = p.action === "answer_choice" ? p.content : p.action === "resolve_plan" ? state.detail.synthetic_plan || state.detail.recommendation : "";
    p.content = plan;
    const node = $("#choice-preview-detail"); node.hidden = false;
    node.innerHTML = `<p>${esc(p.action === "defer" ? `期望日期：${formatTime(p.until, true)}` : plan || "仅记录当前问题的选择。")}</p>${p.action === "write_memory" ? '<label for="choice-note">候选内容（只存本机）</label><textarea id="choice-note" maxlength="4000"></textarea>' : ""}`;
    text("#choice-error", ""); $("#choice-dialog").showModal();
  }
  function bind() {
    $("#draft-recovery").addEventListener("click", () => {
      if (!state.dirty.size) return;
      text("#draft-recovery-note", `工作台中有 ${state.dirty.size} 项编辑尚未确认保存。已写入的版本仍会保留，你可以继续编辑并重试。`);
      $("#draft-recovery-dialog").showModal();
    });
    $("#discard-draft-edits").addEventListener("click", () => {
      if (state.draftWrites.size || state.handoffs.size) {
        text("#draft-recovery-note", "正在核对一次保存或交接。请等结果返回后再处理。");
        return;
      }
      for (const id of state.dirty) {
        clearTimeout(state.timers.get(id));
        state.timers.delete(id);
        state.drafts.delete(id);
      }
      state.dirty.clear();
      reportPending();
      $("#draft-recovery").hidden = true;
      $("#draft-recovery-dialog").close();
      announce("已放弃本次未保存的输入。可以关闭工作台。");
      if (state.selected) open(state.selected);
    });
    $$('[data-issue-scope]').forEach(button => button.addEventListener("click", () => {showScope(button.dataset.issueScope).catch(error => announce(error.message));}));
    $$('[name="defer-choice"]').forEach(input => input.addEventListener("change", () => {$("#defer-date").disabled = input.value !== "custom";}));
    $("#defer-confirm").addEventListener("click", event => {
      event.preventDefault(); const choice = $('[name="defer-choice"]:checked');
      if (!choice) {text("#defer-error", "请选择日期；没有默认选项。"); return;}
      const date = choice.value === "custom" ? new Date($("#defer-date").value + "T09:00:00+08:00") : new Date(Date.now() + Number(choice.value) * 86400000);
      if (!Number.isFinite(date.getTime()) || date <= new Date()) {text("#defer-error", "请选择未来日期。"); return;}
      state.pending.until = date.toISOString(); $("#defer-dialog").close(); preview();
    });
    $("#choice-confirm").addEventListener("click", async event => {
      event.preventDefault(); const pending = state.pending; if (!pending) return;
      if (pending.action === "write_memory") {pending.content = $("#choice-note").value.trim(); if (!pending.content) {text("#choice-error", "请填写候选内容。"); return;}}
      event.currentTarget.disabled = true;
      try {const result = await api(endpoint(pending.id, "/action"), {method: "POST", body: pending}); $("#choice-dialog").close(); state.pending = null; announce(result.message); state.selected = null; state.request++; state.detail = null; state.chat = null; $("#needs-detail").innerHTML = '<p class="empty">已在本机记录，原事项仍保留，等待实际结果。</p>'; $("#case-conversation").innerHTML = ''; await loadAll();}
      catch (error) {text("#choice-error", error.message);} finally {$("#choice-confirm").disabled = false;}
    });
  }
  return {load, open, bind, invalidate, navigate, showScope, prepareToClose, resumeEditing};
})();
