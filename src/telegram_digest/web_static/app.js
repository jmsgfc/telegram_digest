const state = {
  configured: false,
  chats: [],
  selected: new Set(),
  chatGroups: [],
  digests: [],
  selectedDigests: new Set(),
  collapsedDigestGroups: new Set(),
  digestGroupsInitialized: false,
  pollTimer: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const viewTitles = {
  home: "今日概览",
  account: "账号连接",
  workspace: "关注与设置",
  digest: "导出数据",
};

function activateView(view, updateHash = true) {
  const target = document.querySelector(`[data-view-panel="${view}"]`);
  if (!target) return;
  $$('[data-view-panel]').forEach((panel) => panel.classList.toggle("active", panel === target));
  $$('.nav-item').forEach((item) => {
    const active = item.dataset.view === view;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  document.title = `${viewTitles[view] || "清简"} · 清简`;
  $("#viewHeading").textContent = viewTitles[view] || "清简";
  if (updateHash) history.replaceState(null, "", `#${view}`);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Digest": "local-web-ui",
      ...(options.headers || {}),
    },
    ...options,
  });
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (body.detail?.message) detail = body.detail.message;
    } catch (_) {}
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}

function toast(message, type = "info") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  $("#toastRegion").appendChild(item);
  setTimeout(() => item.remove(), 3800);
}

function openPromptDialog() {
  const dialog = $("#promptDialog");
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closePromptDialog() {
  const dialog = $("#promptDialog");
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

async function copySummaryPrompt() {
  const prompt = $("#summaryPrompt").value.trim();
  try {
    await navigator.clipboard.writeText(prompt);
  } catch (_) {
    const textarea = $("#summaryPrompt");
    textarea.focus();
    textarea.select();
    if (!document.execCommand("copy")) throw new Error("复制失败，请手动选择提示词复制。");
  }
  toast("提示词已复制，可以和 JSONL 文件一起发送给 AI。", "success");
}

function setBusy(button, busy, busyText = "处理中…") {
  if (!button) return;
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = busyText;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.label || button.textContent;
    button.disabled = false;
  }
}

function setLoginStatus(text, kind = "idle") {
  $("#loginStatus").textContent = text;
  $("#loginDot").className = `status-dot ${kind === "good" ? "good" : kind === "bad" ? "bad" : ""}`;
  $("#accountState").textContent = kind === "good" ? "已连接" : kind === "bad" ? "未连接" : "待检查";
}

function setStep(step) {
  $$(".step").forEach((item) => item.classList.toggle("active", Number(item.dataset.step) <= step));
}

async function loadStatus() {
  const data = await api("/api/status");
  state.configured = data.configured;
  state.selected = new Set((data.selected_chats || []).map((item) => Number(item.peer_id)));
  $("#selectedCount").textContent = state.selected.size;
  $("#selectionCount").textContent = state.selected.size;
  $("#lookbackHours").value = data.settings.initial_lookback_hours;
  $("#maxMessages").value = data.settings.max_messages_per_chat;

  if (data.configured) {
    setStep(data.session_exists ? 3 : 2);
    setLoginStatus(data.session_exists ? "发现本地登录会话，等待检查" : "API 已配置，尚未登录");
    $("#heroStatusText").textContent = state.selected.size
      ? `已关注 ${state.selected.size} 个会话，可以导出新增消息。`
      : "账号配置已保存，下一步选择你关心的群聊与频道。";
  } else {
    setStep(1);
    setLoginStatus("请先填写 API ID 与 API Hash");
    $("#heroStatusText").textContent = "从 API 配置开始，只需几步即可拥有安静的消息简报。";
  }
}

async function checkAccount({ quiet = false } = {}) {
  const button = $("#checkAccount");
  if (!quiet) setBusy(button, true, "检查中…");
  try {
    const data = await api("/api/account");
    if (data.authorized) {
      const name = data.account.name || `用户 ${data.account.id}`;
      setLoginStatus(`已登录：${name}`, "good");
      setStep(3);
      $("#heroStatusText").textContent = state.selected.size
        ? `欢迎回来，${name}。今天的消息可以开始整理了。`
        : `欢迎回来，${name}。请选择需要关注的会话。`;
      return true;
    }
    setLoginStatus("账号尚未完成登录", "bad");
    return false;
  } catch (error) {
    setLoginStatus(error.message, "bad");
    if (!quiet) toast(error.message, "error");
    return false;
  } finally {
    if (!quiet) setBusy(button, false);
  }
}

async function saveCredentialsIfNeeded() {
  const apiId = $("#apiId").value.trim();
  const apiHash = $("#apiHash").value.trim();
  if (!apiId && !apiHash && state.configured) return;
  if (!apiId || !apiHash) throw new Error("请同时填写 API ID 和 API Hash。");
  await api("/api/credentials", {
    method: "PUT",
    body: JSON.stringify({ api_id: apiId, api_hash: apiHash }),
  });
  state.configured = true;
  $("#apiHash").value = "";
}

async function sendCode() {
  const button = $("#sendCode");
  setBusy(button, true, "正在发送…");
  try {
    await saveCredentialsIfNeeded();
    const data = await api("/api/auth/code", {
      method: "POST",
      body: JSON.stringify({
        country_code: $("#countryCode").value.trim(),
        phone_number: $("#phoneNumber").value.trim(),
      }),
    });
    if (data.already_authorized) {
      setLoginStatus(`已经登录：${data.name}`, "good");
      setStep(3);
      toast("当前账号已经登录。", "success");
    } else {
      setLoginStatus("验证码已发送，请查看 Telegram 客户端");
      setStep(2);
      toast("验证码已发送，有效期约 10 分钟。", "success");
      $("#loginCode").focus();
    }
  } catch (error) {
    setLoginStatus(error.message, "bad");
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function completeLogin() {
  const button = $("#completeLogin");
  setBusy(button, true, "正在登录…");
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        code: $("#loginCode").value.trim(),
        password: $("#twoFactorPassword").value,
      }),
    });
    const name = data.account.name || `用户 ${data.account.id}`;
    setLoginStatus(`已登录：${name}`, "good");
    setStep(3);
    $("#loginCode").value = "";
    $("#twoFactorPassword").value = "";
    toast("登录成功，可以加载会话了。", "success");
  } catch (error) {
    setLoginStatus(error.message, "bad");
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function loadChats() {
  const button = $("#loadChats");
  setBusy(button, true, "加载中…");
  try {
    const data = await api("/api/chats");
    state.chats = data.chats || [];
    state.selected = new Set(state.chats.filter((item) => item.selected).map((item) => Number(item.peer_id)));
    renderChats();
    toast(`已加载 ${state.chats.length} 个群聊和频道。`, "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

function renderChats() {
  const list = $("#chatList");
  const keyword = $("#chatSearch").value.trim().toLowerCase();
  const visible = state.chats.filter((chat) => `${chat.title} ${chat.kind}`.toLowerCase().includes(keyword));
  list.replaceChildren();
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    const text = document.createElement("p");
    text.textContent = state.chats.length ? "没有匹配的会话" : "登录后加载你的群聊与频道";
    empty.appendChild(text);
    list.appendChild(empty);
    updateSelectionCount();
    return;
  }
  visible.forEach((chat) => {
    const label = document.createElement("label");
    label.className = `chat-item ${state.selected.has(Number(chat.peer_id)) ? "selected" : ""}`;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.className = "chat-checkbox";
    input.setAttribute("aria-label", `选择 ${chat.title}`);
    input.checked = state.selected.has(Number(chat.peer_id));
    input.addEventListener("change", () => {
      if (input.checked) state.selected.add(Number(chat.peer_id));
      else state.selected.delete(Number(chat.peer_id));
      label.classList.toggle("selected", input.checked);
      updateSelectionCount();
    });
    const avatar = document.createElement("span");
    avatar.className = "chat-avatar";
    avatar.textContent = (chat.title || "会").slice(0, 1).toUpperCase();
    const copy = document.createElement("span");
    copy.className = "chat-copy";
    const title = document.createElement("strong");
    title.textContent = chat.title;
    const kind = document.createElement("small");
    kind.textContent = `${chat.kind} · ${chat.peer_id}`;
    copy.append(title, kind);
    label.append(input, avatar, copy);
    list.appendChild(label);
  });
  updateSelectionCount();
}

async function loadGroups() {
  try {
    const data = await api("/api/chat-groups");
    state.chatGroups = data.groups || [];
    renderGroups();
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderGroups() {
  const list = $("#groupList");
  list.replaceChildren();
  state.chatGroups.forEach((group) => {
    const item = document.createElement("span");
    item.className = "group-chip";

    const use = document.createElement("button");
    use.type = "button";
    use.className = "group-use";
    use.textContent = `${group.name} · ${group.peer_ids.length}`;
    use.addEventListener("click", () => applyGroup(group));

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "group-delete";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `删除分组 ${group.name}`);
    remove.addEventListener("click", () => deleteGroup(group));

    item.append(use, remove);
    list.appendChild(item);
  });
}

async function saveGroup() {
  const button = $("#saveGroup");
  const name = $("#groupName").value.trim();
  if (!name) {
    toast("请输入分组名称。", "error");
    return;
  }
  if (!state.selected.size) {
    toast("请先选择群聊。", "error");
    return;
  }
  setBusy(button, true, "保存中…");
  try {
    const data = await api("/api/chat-groups", {
      method: "PUT",
      body: JSON.stringify({ name, peer_ids: [...state.selected] }),
    });
    state.chatGroups = data.groups || [];
    $("#groupName").value = "";
    renderGroups();
    toast("分组已保存。", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function applyGroup(group) {
  try {
    if (!state.chats.length) {
      const data = await api("/api/chats");
      state.chats = data.chats || [];
    }
    const available = new Set(state.chats.map((chat) => Number(chat.peer_id)));
    const selected = group.peer_ids.map(Number).filter((peerId) => available.has(peerId));
    const skipped = group.peer_ids.length - selected.length;
    if (!selected.length) throw new Error("该分组中没有可用会话。");

    await api("/api/chats/selection", {
      method: "PUT",
      body: JSON.stringify({ peer_ids: selected }),
    });
    state.selected = new Set(selected);
    renderChats();
    toast(skipped ? `已载入，跳过 ${skipped} 个失效会话。` : `已载入“${group.name}”。`, skipped ? "error" : "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function deleteGroup(group) {
  try {
    const data = await api(`/api/chat-groups/${encodeURIComponent(group.id)}`, { method: "DELETE" });
    state.chatGroups = data.groups || [];
    renderGroups();
    toast("分组已删除。", "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

function updateSelectionCount() {
  $("#selectionCount").textContent = state.selected.size;
  $("#selectedCount").textContent = state.selected.size;
}

async function saveSelection() {
  const button = $("#saveSelection");
  setBusy(button, true, "保存中…");
  try {
    const data = await api("/api/chats/selection", {
      method: "PUT",
      body: JSON.stringify({ peer_ids: [...state.selected] }),
    });
    updateSelectionCount();
    toast(`已保存 ${data.selected_count} 个关注会话。`, "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function saveSettings() {
  const button = $("#saveSettings");
  setBusy(button, true, "保存中…");
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        initial_lookback_hours: Number($("#lookbackHours").value),
        max_messages_per_chat: Number($("#maxMessages").value),
      }),
    });
    toast("导出设置已保存。", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function startDigest() {
  const button = $("#generateDigest");
  setBusy(button, true, "正在导出…");
  try {
    const data = await api("/api/digests", { method: "POST", body: "{}" });
    $("#jobProgress").hidden = false;
    $("#jobStatus").textContent = "正在读取并导出新增消息…";
    pollJob(data.job_id);
  } catch (error) {
    setBusy(button, false);
    toast(error.message, "error");
  }
}

function pollJob(jobId) {
  clearTimeout(state.pollTimer);
  const tick = async () => {
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      const latest = job.messages?.at(-1);
      if (latest) $("#jobStatus").textContent = latest;
      if (job.status === "succeeded" || job.status === "partial") {
        finishJobButtons();
        $("#jobProgress").hidden = true;
        if (job.results?.length) {
          const message = job.status === "partial"
            ? `已导出 ${job.results.length} 份 JSONL，部分会话处理失败。`
            : `已按会话导出 ${job.results.length} 份 JSONL。`;
          toast(message, job.status === "partial" ? "error" : "success");
          await loadHistory();
          await previewDigest(job.results[0]);
        } else {
          toast("当前没有需要处理的新消息。", "success");
        }
        return;
      }
      if (job.status === "failed") {
        finishJobButtons();
        $("#jobProgress").hidden = true;
        toast(job.error || "消息导出失败。", "error");
        return;
      }
      state.pollTimer = setTimeout(tick, 1000);
    } catch (error) {
      finishJobButtons();
      $("#jobProgress").hidden = true;
      toast(error.message, "error");
    }
  };
  tick();
}

function finishJobButtons() {
  setBusy($("#generateDigest"), false);
}

async function loadHistory() {
  try {
    const data = await api("/api/digests");
    state.digests = data.digests || [];
    const available = new Set(state.digests.map((digest) => digest.name));
    state.selectedDigests = new Set([...state.selectedDigests].filter((name) => available.has(name)));
    $("#digestCount").textContent = state.digests.length;
    renderHistory();
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderHistory() {
  const list = $("#historyList");
  list.replaceChildren();
  if (!state.digests.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state small";
    const text = document.createElement("p");
    text.textContent = "暂无数据文件";
    empty.appendChild(text);
    list.appendChild(empty);
    updateDigestSelection();
    return;
  }

  const visible = filteredDigests();
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state small";
    const text = document.createElement("p");
    text.textContent = "没有匹配的数据文件";
    empty.appendChild(text);
    list.appendChild(empty);
    updateDigestSelection();
    return;
  }

  const order = ["今天", "昨天", "本周", "更早"];
  const groups = new Map(order.map((label) => [label, []]));
  visible.forEach((digest) => groups.get(digestPeriod(digest.modified)).push(digest));

  if (!state.digestGroupsInitialized) {
    state.collapsedDigestGroups = new Set(order);
    const newestGroup = order.find((label) => groups.get(label).length);
    if (newestGroup) state.collapsedDigestGroups.delete(newestGroup);
    state.digestGroupsInitialized = true;
  }

  const searching = $("#digestSearch").value.trim().length > 0;
  order.forEach((label) => {
    const digests = groups.get(label);
    if (!digests.length) return;

    const section = document.createElement("section");
    section.className = "digest-group";
    const head = document.createElement("div");
    head.className = "digest-group-head";

    const groupCheckbox = document.createElement("input");
    groupCheckbox.type = "checkbox";
    groupCheckbox.className = "digest-group-checkbox";
    groupCheckbox.setAttribute("aria-label", `选择${label}的全部数据文件`);
    const selectedCount = digests.filter((digest) => state.selectedDigests.has(digest.name)).length;
    groupCheckbox.checked = selectedCount === digests.length;
    groupCheckbox.indeterminate = selectedCount > 0 && selectedCount < digests.length;
    groupCheckbox.addEventListener("change", () => {
      digests.forEach((digest) => {
        if (groupCheckbox.checked) state.selectedDigests.add(digest.name);
        else state.selectedDigests.delete(digest.name);
      });
      renderHistory();
    });

    const collapsed = !searching && state.collapsedDigestGroups.has(label);
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "digest-group-toggle";
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.disabled = searching;
    if (searching) toggle.title = "搜索时自动展开";
    toggle.addEventListener("click", () => {
      if (searching) return;
      if (state.collapsedDigestGroups.has(label)) state.collapsedDigestGroups.delete(label);
      else state.collapsedDigestGroups.add(label);
      renderHistory();
    });
    const groupName = document.createElement("strong");
    groupName.textContent = label;
    const groupCount = document.createElement("small");
    groupCount.textContent = `${digests.length} 份`;
    const chevron = document.createElement("span");
    chevron.textContent = collapsed ? "⌄" : "⌃";
    toggle.append(groupName, groupCount, chevron);
    head.append(groupCheckbox, toggle);

    const body = document.createElement("div");
    body.className = "digest-group-body";
    body.hidden = collapsed;
    digests.forEach((digest) => body.appendChild(createHistoryItem(digest)));
    section.append(head, body);
    list.appendChild(section);
  });
  updateDigestSelection();
}

function createHistoryItem(digest) {
  const item = document.createElement("div");
  item.className = `history-item ${state.selectedDigests.has(digest.name) ? "selected" : ""}`;

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "history-checkbox";
  checkbox.checked = state.selectedDigests.has(digest.name);
  checkbox.setAttribute("aria-label", `选择 ${digest.name}`);
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) state.selectedDigests.add(digest.name);
    else state.selectedDigests.delete(digest.name);
    renderHistory();
  });

  const button = document.createElement("button");
  button.type = "button";
  button.className = "history-open";
  button.setAttribute("aria-label", `预览 ${digest.name}`);
  button.addEventListener("click", () => previewDigest(digest.name));
  const icon = document.createElement("span");
  icon.className = "history-icon";
  icon.textContent = "笺";
  const copy = document.createElement("span");
  copy.className = "history-copy";
  copy.title = digest.name;
  const name = document.createElement("strong");
  name.textContent = digestChatTitle(digest.name);
  const meta = document.createElement("small");
  meta.textContent = `${new Date(digest.modified * 1000).toLocaleString("zh-CN")} · ${formatBytes(digest.size)}`;
  copy.append(name, meta);
  const arrow = document.createElement("span");
  arrow.className = "history-arrow";
  arrow.textContent = "›";
  button.append(icon, copy, arrow);
  item.append(checkbox, button);
  return item;
}

function filteredDigests() {
  const keyword = $("#digestSearch").value.trim().toLowerCase();
  if (!keyword) return state.digests;
  return state.digests.filter((digest) => `${digestChatTitle(digest.name)} ${digest.name}`.toLowerCase().includes(keyword));
}

function digestChatTitle(filename) {
  const matched = filename.match(/^messages-\d{8}-\d{6}-\d{6}--?\d+-(.+)\.jsonl$/i);
  return matched?.[1] || filename.replace(/\.jsonl$/i, "");
}

function digestPeriod(modified) {
  const value = new Date(modified * 1000);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const weekStart = new Date(today);
  weekStart.setDate(today.getDate() - ((today.getDay() + 6) % 7));
  if (value >= today) return "今天";
  if (value >= yesterday) return "昨天";
  if (value >= weekStart) return "本周";
  return "更早";
}

function updateDigestSelection() {
  const count = state.selectedDigests.size;
  const button = $("#downloadSelected");
  button.textContent = `下载所选 (${count})`;
  button.disabled = count === 0;
  const visible = filteredDigests();
  const allSelected = visible.length > 0 && visible.every((digest) => state.selectedDigests.has(digest.name));
  $("#selectAllDigests").textContent = allSelected ? "取消全选" : "全选";
}

function toggleAllDigests() {
  const visible = filteredDigests();
  const allSelected = visible.length > 0 && visible.every((digest) => state.selectedDigests.has(digest.name));
  visible.forEach((digest) => {
    if (allSelected) state.selectedDigests.delete(digest.name);
    else state.selectedDigests.add(digest.name);
  });
  renderHistory();
}

async function downloadSelectedDigests() {
  const button = $("#downloadSelected");
  if (!state.selectedDigests.size) return;
  setBusy(button, true, "打包中…");
  try {
    const response = await fetch("/api/digests/archive", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Digest": "local-web-ui",
      },
      body: JSON.stringify({ filenames: [...state.selectedDigests] }),
    });
    if (!response.ok) {
      let message = `下载失败（${response.status}）`;
      try {
        const body = await response.json();
        if (typeof body.detail === "string") message = body.detail;
      } catch (_) {}
      throw new Error(message);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const matched = disposition.match(/filename="?([^";]+)"?/i);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = matched?.[1] || "telegram-messages.zip";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
    toast(`已打包 ${state.selectedDigests.size} 份数据文件。`, "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
    updateDigestSelection();
  }
}

async function previewDigest(filename) {
  try {
    const content = await api(`/api/digests/${encodeURIComponent(filename)}`);
    $("#previewTitle").textContent = filename;
    $("#digestPreview").textContent = content;
    $("#downloadDigest").href = `/api/digests/${encodeURIComponent(filename)}?download=true`;
    $("#previewCard").hidden = false;
    $("#previewCard").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(error.message, "error");
  }
}

function formatBytes(value) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

async function refreshAll() {
  try {
    await Promise.all([loadStatus(), loadHistory(), loadGroups()]);
    if (state.configured) await checkAccount({ quiet: true });
    toast("状态已刷新。", "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

function bindEvents() {
  $$('.nav-item').forEach((item) => item.addEventListener("click", () => activateView(item.dataset.view)));
  $("#toggleHash").addEventListener("click", () => {
    const input = $("#apiHash");
    input.type = input.type === "password" ? "text" : "password";
    $("#toggleHash").textContent = input.type === "password" ? "显示" : "隐藏";
  });
  $("#sendCode").addEventListener("click", sendCode);
  $("#completeLogin").addEventListener("click", completeLogin);
  $("#checkAccount").addEventListener("click", () => checkAccount());
  $("#loadChats").addEventListener("click", loadChats);
  $("#chatSearch").addEventListener("input", renderChats);
  $("#selectNone").addEventListener("click", () => { state.selected.clear(); renderChats(); });
  $("#saveGroup").addEventListener("click", saveGroup);
  $("#groupName").addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveGroup();
  });
  $("#saveSelection").addEventListener("click", saveSelection);
  $("#saveSettings").addEventListener("click", saveSettings);
  $("#generateDigest").addEventListener("click", startDigest);
  $("#digestSearch").addEventListener("input", renderHistory);
  $("#selectAllDigests").addEventListener("click", toggleAllDigests);
  $("#downloadSelected").addEventListener("click", downloadSelectedDigests);
  $("#openPrompt").addEventListener("click", openPromptDialog);
  $("#closePrompt").addEventListener("click", closePromptDialog);
  $("#copyPrompt").addEventListener("click", copySummaryPrompt);
  $("#promptDialog").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closePromptDialog();
  });
  $("#refreshAll").addEventListener("click", refreshAll);
}

async function init() {
  $("#todayLabel").textContent = new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "short" }).format(new Date());
  bindEvents();
  const initialView = location.hash.slice(1);
  activateView(viewTitles[initialView] ? initialView : "home", false);
  try {
    await Promise.all([loadStatus(), loadHistory(), loadGroups()]);
    if (state.configured) await checkAccount({ quiet: true });
  } catch (error) {
    toast(error.message, "error");
    $("#heroStatusText").textContent = "服务状态读取失败，请稍后刷新。";
  }
}

init();
