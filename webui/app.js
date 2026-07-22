const state = {
  overview: null,
  mailboxes: [],
  mailboxPath: "",
  accounts: [],
  failures: [],
  configFields: [],
  configDraft: {},
  configActiveGroup: "",
  proxyConfig: null,
  proxyPool: null,
  proxyMode: "clash_subscription",
  proxySubscriptions: [],
  proxyStaticProxies: [],
  proxyNodeTests: {},
  modelActions: {},
  cpaPushRunning: false,
  registrationCountTouched: false,
  registerCapacity: 0,
  accountPushGroup: "all",
  importText: "",
  taskSignature: "",
};

const pages = {
  overview: ["运行总览", "账户、邮箱和凭证的实时状态"],
  mailboxes: ["邮箱凭证池", "导入并管理 Hotmail / Outlook 四段凭证"],
  accounts: ["成功账户", "集中保留账号、SSO 和 CPA 凭证"],
  failures: ["失败记录", "定位失败阶段并将邮箱重新加入可用池"],
  proxy: ["代理网络", "多订阅分组与静态认证 IP 的选取、随机和测活"],
  tasks: ["任务中心", "注册、补全 CPA 与查看实时日志"],
  settings: ["配置中心", "按模块切换并管理 config.json 配置"],
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function isProxyConfigKey(key) {
  return key === "proxy" || key === "cpa_proxy" || key === "proxy_enabled" || key === "proxy_mode" || key === "proxy_fallback_direct" || key === "proxy_subscription_url" || key === "proxy_subscriptions" || key === "proxy_static_proxies" || key === "proxy_selection_mode" || key === "proxy_selected_node" || key.startsWith("proxy_pool_");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[ch]);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    ...options,
  });
  const data = await response.json().catch(() => ({error: `HTTP ${response.status}`}));
  if (!response.ok) {
    const error = new Error(data.error || `请求失败：${response.status}`);
    error.code = data.code || "";
    error.status = response.status;
    throw error;
  }
  return data;
}

function toast(message, type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  $("#toastStack").append(item);
  setTimeout(() => item.remove(), 3600);
}

function openModal(id) {
  const modal = $(id);
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
}

function closeModal(id) {
  const modal = $(id);
  modal.classList.remove("open");
  modal.setAttribute("aria-hidden", "true");
}

function navigate(name) {
  $$(".nav-item").forEach(button => button.classList.toggle("active", button.dataset.page === name));
  $$(".page").forEach(page => page.classList.toggle("active", page.id === `page-${name}`));
  $("#pageTitle").textContent = pages[name][0];
  $("#pageSubtitle").textContent = pages[name][1];
  history.replaceState(null, "", `#${name}`);
}

function percent(value, total) {
  return total ? Math.round(value / total * 100) : 0;
}

function taskLabel(status) {
  return ({idle:"空闲", running:"运行中", stopping:"正在停止", stopped:"已停止", completed:"已完成", failed:"运行失败"})[status] || status || "空闲";
}

function renderOverview() {
  const item = state.overview;
  if (!item) return;
  $("#metricMailboxes").textContent = item.mailboxes;
  const registerCapacity = Math.max(0, Number(item.register_capacity || 0));
  state.registerCapacity = registerCapacity;
  $("#metricMailboxesHint").textContent = `${registerCapacity} 个可注册名额`;
  [$("#quickExtra"), $("#registerExtra")].forEach(input => {
    input.max = String(Math.max(100, registerCapacity));
    input.title = `当前可注册 ${registerCapacity} 个账户`;
    if (!state.registrationCountTouched) input.value = String(registerCapacity);
  });
  updateRegistrationControls();
  $("#metricAccounts").textContent = item.accounts;
  $("#metricAccountsHint").textContent = `${item.accounts_with_sso} 个保留 SSO`;
  $("#metricCpa").textContent = item.accounts_with_cpa;
  $("#metricCpaHint").textContent = `${percent(item.accounts_with_cpa, item.accounts)}% 账户覆盖`;
  $("#metricFailures").textContent = item.failures;
  $("#providerBadge").textContent = String(item.provider || "未配置").toUpperCase();
  const proxyModeText = ({static_pool:"静态 IP 池", clash_subscription:"Clash 订阅池"})[item.proxy_mode] || item.proxy_mode;
  $("#proxyState").textContent = item.proxy_enabled ? `代理已启用 · ${proxyModeText}` : "代理已关闭 · 直接连接";
  $("#cpaState").textContent = item.cpa_enabled ? "CPA 自动导出已开启" : "CPA 自动导出已关闭";
  $("#updatedAt").textContent = `更新于 ${new Date(item.time).toLocaleTimeString()}`;

  const sso = percent(item.accounts_with_sso, item.accounts);
  const cpa = percent(item.accounts_with_cpa, item.accounts);
  $("#ssoProgress").style.width = `${sso}%`;
  $("#cpaProgress").style.width = `${cpa}%`;
  $("#ssoPercent").textContent = `${sso}%`;
  $("#cpaPercent").textContent = `${cpa}%`;

  const task = item.task || {};
  const activity = $("#activityState");
  if (task.running) {
    activity.innerHTML = `<div class="activity-orb"><span>▶</span></div><strong>${escapeHtml(task.kind === "register" ? "注册任务正在运行" : "CPA 补全正在运行")}</strong><p>开始于 ${escapeHtml(task.started_at || "刚刚")}</p>`;
  } else if (task.status && task.status !== "idle") {
    activity.innerHTML = `<div class="activity-orb"><span>✓</span></div><strong>最近任务${escapeHtml(taskLabel(task.status))}</strong><p>${escapeHtml(task.ended_at || "")}</p>`;
  } else {
    activity.innerHTML = `<div class="activity-orb"><span>✓</span></div><strong>当前没有运行中的任务</strong><p>从快速启动或任务中心开始。</p>`;
  }
}

function updateRegistrationControls() {
  $("#quickStart").disabled = state.registerCapacity === 0 || Number($("#quickExtra").value || 0) <= 0;
  $("#startRegister").disabled = state.registerCapacity === 0 || Number($("#registerExtra").value || 0) <= 0;
}

function renderMailboxes() {
  const query = $("#mailboxSearch").value.trim().toLowerCase();
  const items = state.mailboxes.filter(item => item.email.toLowerCase().includes(query));
  const invalidCount = state.mailboxes.filter(item => item.status === "oauth_expired").length;
  $("#mailboxCount").textContent = `${state.mailboxes.length} 条记录${state.mailboxPath ? ` · ${state.mailboxPath}` : ""}`;
  $("#mailboxCount").title = state.mailboxPath;
  $("#deleteInvalidMailboxes").textContent = invalidCount ? `删除授权失效 (${invalidCount})` : "删除授权失效";
  $("#deleteInvalidMailboxes").disabled = invalidCount === 0;
  $("#mailboxRows").innerHTML = items.map(item => {
    const badge = item.status === "oauth_expired" ? ["danger", "授权失效"] : item.status === "ready" ? ["success", "可使用"] : item.status === "active" ? ["neutral", "使用中"] : ["warning", "需关注"];
    return `<tr>
      <td class="check-cell"><input type="checkbox" class="mail-check" value="${escapeHtml(item.email)}"></td>
      <td><div class="email-cell"><span class="avatar">${escapeHtml(item.email.slice(0,1).toUpperCase())}</span><div><strong>${escapeHtml(item.email)}</strong><small>${escapeHtml(item.password_masked)}</small></div></div></td>
      <td class="mono">${escapeHtml(item.client_id_masked)}</td><td class="mono">${escapeHtml(item.token_masked)}</td>
      <td>${item.used_count}</td><td>${item.failed_count}</td><td><span class="badge ${badge[0]}" title="${escapeHtml(item.invalid_reason || "")}">● ${badge[1]}</span></td>
    </tr>`;
  }).join("");
  $("#mailboxEmpty").classList.toggle("visible", items.length === 0);
}

function renderAccounts() {
  const query = $("#accountSearch").value.trim().toLowerCase();
  const pushedCount = state.accounts.filter(item => item.has_cpa && item.cpa_pushed).length;
  const pendingCount = state.accounts.filter(item => item.has_cpa && !item.cpa_pushed).length;
  const items = state.accounts.filter(item => {
    if (!item.email.toLowerCase().includes(query)) return false;
    if (state.accountPushGroup === "pushed") return item.has_cpa && item.cpa_pushed;
    if (state.accountPushGroup === "pending") return item.has_cpa && !item.cpa_pushed;
    return true;
  });
  $("#accountCount").textContent = `${state.accounts.length} 个账户 · 未推送 ${pendingCount} · 已推送 ${pushedCount}`;
  $("#accountGroupAll").textContent = state.accounts.length;
  $("#accountGroupPending").textContent = pendingCount;
  $("#accountGroupPushed").textContent = pushedCount;
  $$("[data-account-push-group]").forEach(button => button.classList.toggle("active", button.dataset.accountPushGroup === state.accountPushGroup));
  $("#accountRows").innerHTML = items.map(item => `<tr>
    <td><div class="email-cell"><span class="avatar">${escapeHtml(item.email.slice(0,1).toUpperCase())}</span><div><strong>${escapeHtml(item.email)}</strong><small>${escapeHtml(item.updated_at ? new Date(item.updated_at).toLocaleString() : "")}</small></div></div></td>
    <td class="mono">${escapeHtml(item.password_masked)}</td>
    <td><span class="badge ${item.has_sso ? "success" : "warning"}">${item.has_sso ? "✓ 已保留" : "! 缺失"}</span></td>
    <td><div class="cpa-state"><span class="badge ${item.has_cpa ? "violet" : "neutral"}">${item.has_cpa ? "◆ " + escapeHtml(item.cpa_file) : "未生成"}</span>${item.has_cpa ? `<span class="badge ${item.cpa_pushed ? "success" : "warning"}" title="${item.cpa_pushed ? `已推送到 ${escapeHtml(item.cpa_push_target || "CPA")} · ${escapeHtml(item.cpa_pushed_at ? new Date(item.cpa_pushed_at).toLocaleString() : "")}` : "尚未推送，或凭证更新后需要重新推送"}">${item.cpa_pushed ? "✓ 已推送" : "! 待推送"}</span>` : ""}</div></td>
    <td class="model-capability-cell">${renderAccountModels(item)}</td>
    <td>${escapeHtml(item.source)}</td><td><div class="table-row-actions"><button class="table-action credential-button" data-email="${escapeHtml(item.email)}">查看凭证</button>${item.has_cpa ? `<button class="table-action cpa-push-button" data-email="${escapeHtml(item.email)}" ${state.cpaPushRunning ? "disabled" : ""}>${item.cpa_pushed ? "重新推送" : "推送 CPA"}</button><button class="table-action refresh-credential-button" data-email="${escapeHtml(item.email)}">刷新凭证</button><button class="table-action refresh-models-button" data-email="${escapeHtml(item.email)}">${item.models?.length ? "刷新模型" : "获取模型"}</button><button class="table-action refresh-quota-button" data-email="${escapeHtml(item.email)}">刷新额度状态</button>` : `<button class="table-action cpa-backfill-button" data-email="${escapeHtml(item.email)}">获取 CPA</button>`}<button class="table-action account-delete-button" data-email="${escapeHtml(item.email)}">删除账户</button></div></td>
  </tr>`).join("");
  const missing = state.accounts.filter(item => !item.has_cpa).length;
  const pendingCpaCount = pendingCount;
  $("#backfillMissingCpa").textContent = missing ? `一键补全缺失 CPA (${missing})` : "CPA 已全部生成";
  $("#backfillMissingCpa").disabled = missing === 0;
  $("#pushCpa").textContent = state.cpaPushRunning ? "正在推送…" : pendingCpaCount ? `一键推送 CPA (${pendingCpaCount})` : "CPA 已全部推送";
  $("#pushCpa").disabled = state.cpaPushRunning || pendingCpaCount === 0;
  $("#accountEmpty").classList.toggle("visible", items.length === 0);
  if (items.length === 0) {
    const labels = {pending:"当前没有未推送账户", pushed:"当前没有已推送账户", all:"还没有成功账户"};
    $("#accountEmpty strong").textContent = labels[state.accountPushGroup] || labels.all;
  }
}

function modelRateLimitText(test) {
  if (test?.reason === "permission_denied") return "无聊天权限";
  if (test?.reason === "credential_invalid") return "凭证失效";
  if (test?.reason === "quota_limited") return "额度受限";
  const limits = test?.rate_limits || {};
  const remaining = limits["x-ratelimit-remaining-requests"] ?? limits["x-ratelimit-remaining-tokens"];
  const limit = limits["x-ratelimit-limit-requests"] ?? limits["x-ratelimit-limit-tokens"];
  if (remaining != null && limit != null) return `剩余 ${remaining}/${limit}`;
  if (remaining != null) return `剩余 ${remaining}`;
  if (limits["retry-after"]) return `${limits["retry-after"]} 秒后重试`;
  return "";
}

function renderAccountModels(item) {
  if (!item.has_cpa) return '<span class="badge neutral">需先获取 CPA</span>';
  const action = state.modelActions[item.email];
  if (action?.startsWith("models")) return '<span class="badge neutral model-loading">正在获取模型…</span>';
  const models = Array.isArray(item.models) ? item.models : [];
  const tags = models.map(model => {
    const test = item.model_tests?.[model];
    const testing = action === `test:${model}` || action === "quota";
    const statusClass = test ? (test.ok ? "available" : "limited") : "unknown";
    const rateText = modelRateLimitText(test);
    const title = test ? `${test.ok ? "测试通过" : "测试失败"} · HTTP ${test.status || 0}${rateText ? ` · ${rateText}` : ""} · 点击重新测试` : "点击测试此模型";
    return `<button class="model-tag ${statusClass}${testing ? " testing" : ""}" type="button" data-email="${escapeHtml(item.email)}" data-model="${escapeHtml(model)}" title="${escapeHtml(title)}"><i></i>${escapeHtml(model)}${rateText ? `<small>${escapeHtml(rateText)}</small>` : ""}</button>`;
  }).join("");
  const checkedAt = item.models_checked_at ? new Date(item.models_checked_at).toLocaleString() : "";
  if (tags) return `<div class="model-capabilities"><div class="model-tags">${tags}</div><small>${checkedAt ? `列表更新：${escapeHtml(checkedAt)}` : "点击标签可测试模型"}</small></div>`;
  if (item.models_error) return `<span class="badge warning" title="${escapeHtml(item.models_error)}">模型获取失败</span>`;
  return '<span class="badge neutral">尚未获取</span>';
}

function renderFailures() {
  const query = $("#failureSearch").value.trim().toLowerCase();
  const items = state.failures.filter(item => `${item.email} ${item.reason} ${item.stage}`.toLowerCase().includes(query));
  $("#failureCount").textContent = `${state.failures.length} 条记录`;
  $("#failureRows").innerHTML = items.map(item => `<tr>
    <td class="check-cell"><input type="checkbox" class="failure-check" value="${escapeHtml(item.email)}"></td>
    <td><strong>${escapeHtml(item.email)}</strong></td><td><span class="badge warning">${escapeHtml(item.stage)}</span></td>
    <td title="${escapeHtml(item.reason)}">${escapeHtml(item.reason.slice(0,100))}</td><td class="mono">${escapeHtml(item.source)}</td><td>${escapeHtml(item.time || "—")}</td>
  </tr>`).join("");
  $("#failureEmpty").classList.toggle("visible", items.length === 0);
}

function configInput(field) {
  const key = escapeHtml(field.key);
  if (field.type === "boolean") {
    return `<label class="config-switch"><span>${field.value ? "已开启" : "已关闭"}</span><input type="checkbox" data-config-key="${key}" ${field.value ? "checked" : ""}><i></i></label>`;
  }
  if (field.key === "proxy_mode") {
    const options = [["static_pool","静态 IP 池"],["clash_subscription","Clash 订阅池"]];
    return `<div class="config-control"><select data-config-key="${key}" data-config-type="string">${options.map(([value,label]) => `<option value="${value}" ${field.value === value ? "selected" : ""}>${label}</option>`).join("")}</select></div>`;
  }
  const type = field.secret ? "password" : field.type === "number" ? "number" : "text";
  return `<div class="config-control"><input type="${type}" data-config-key="${key}" data-config-type="${field.type}" value="${escapeHtml(field.value ?? "")}">${field.secret ? '<button class="reveal-config" type="button" title="显示或隐藏">◉</button>' : ""}</div>`;
}

const configGroupMeta = {
  "邮箱服务": {icon:"✉", description:"邮箱来源、Hotmail、CloudMail 与验证码服务"},
  "浏览器与网络": {icon:"⌁", description:"浏览器标识、页面等待与验证码超时"},
  "注册任务": {icon:"▶", description:"注册数量、并发、重试与账户超时"},
  "CPA / OIDC": {icon:"◆", description:"CPA 凭证生成、OIDC 与探测配置"},
  "Grok2API": {icon:"G", description:"成功账户同步到 Grok2API 的配置"},
  "界面": {icon:"▦", description:"本地界面和启动显示选项"},
  "其他": {icon:"•••", description:"尚未归入其他模块的高级配置"},
};

function groupedConfigFields() {
  const grouped = new Map();
  for (const field of state.configFields) {
    if (isProxyConfigKey(field.key)) continue;
    if (!grouped.has(field.group)) grouped.set(field.group, []);
    grouped.get(field.group).push(field);
  }
  const order = Object.keys(configGroupMeta);
  return new Map([...grouped.entries()].sort(([a], [b]) => {
    const ai = order.indexOf(a), bi = order.indexOf(b);
    return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi);
  }));
}

function captureConfigDraft() {
  $$("#configGroups [data-config-key]").forEach(input => {
    const key = input.dataset.configKey;
    if (input.type === "checkbox") state.configDraft[key] = input.checked;
    else if (input.dataset.configType === "number") state.configDraft[key] = input.value === "" ? 0 : Number(input.value);
    else state.configDraft[key] = input.value;
  });
}

function renderConfig() {
  const grouped = groupedConfigFields();
  const groups = [...grouped.keys()];
  if (!groups.includes(state.configActiveGroup)) state.configActiveGroup = groups[0] || "";
  $("#configModuleTabs").innerHTML = groups.map(group => {
    const meta = configGroupMeta[group] || {icon:"•", description:"模块配置"};
    return `<button type="button" class="config-module-tab ${group === state.configActiveGroup ? "active" : ""}" data-config-group="${escapeHtml(group)}"><span>${escapeHtml(meta.icon)}</span><div><strong>${escapeHtml(group)}</strong><small>${grouped.get(group).length} 项</small></div></button>`;
  }).join("");
  const fields = grouped.get(state.configActiveGroup) || [];
  const meta = configGroupMeta[state.configActiveGroup] || {description:"模块配置"};
  $("#configGroups").innerHTML = `<section class="config-section config-module-page">
    <div class="config-section-head"><div><p class="eyebrow">MODULE SETTINGS</p><h2>${escapeHtml(state.configActiveGroup)}</h2><small>${escapeHtml(meta.description)}</small></div><span>${fields.length} 项配置</span></div>
    <div class="config-fields">${fields.map(field => {
      const draftField = {...field, value:Object.prototype.hasOwnProperty.call(state.configDraft, field.key) ? state.configDraft[field.key] : field.value};
      return `<div class="config-field"><div class="config-copy"><strong>${escapeHtml(field.label)}</strong><code>${escapeHtml(field.key)}</code><p title="${escapeHtml(field.description)}">${escapeHtml(field.description || "项目运行配置")}</p></div>${configInput(draftField)}</div>`;
    }).join("")}</div>
  </section>`;
}

function updateProxyModeUI(mode) {
  state.proxyMode = mode === "static_pool" ? "static_pool" : "clash_subscription";
  $$("[data-proxy-mode]").forEach(button => button.classList.toggle("active", button.dataset.proxyMode === state.proxyMode));
  $("#staticProxyPanel").classList.toggle("hidden", state.proxyMode !== "static_pool");
  $("#subscriptionProxyPanel").classList.toggle("hidden", state.proxyMode !== "clash_subscription");
  $("#proxyEnabledLabel").textContent = $("#proxyEnabledInput").checked ? "代理已启用" : "代理已关闭";
}

function populateProxyForm() {
  const item = state.proxyConfig;
  if (!item) return;
  state.proxySubscriptions = (item.subscriptions || []).map(row => ({...row}));
  state.proxyStaticProxies = (item.static_proxies || []).map(row => ({...row}));
  $("#proxyEnabledInput").checked = Boolean(item.enabled);
  $("#proxyRandomInput").checked = item.selection_mode !== "manual";
  $("#proxyHealthUrlInput").value = item.health_url || "https://accounts.x.ai/";
  $("#proxyMaxNodesInput").value = item.max_test_nodes || 12;
  $("#proxyTimeoutInput").value = item.test_timeout_sec || 8;
  $("#proxyRefreshInput").value = item.refresh_seconds || 3600;
  $("#proxyMixedPortInput").value = item.mixed_port || 17890;
  $("#proxyControllerPortInput").value = item.controller_port || 19090;
  $("#proxyMihomoPathInput").value = item.mihomo_path || "";
  $("#proxyRuntimeDirInput").value = item.runtime_dir || "./proxy_pool_runtime";
  updateProxyModeUI(item.mode);
  renderProxySources();
}

function proxyFormValues() {
  return {
    enabled: $("#proxyEnabledInput").checked,
    mode: state.proxyMode,
    subscriptions: state.proxySubscriptions,
    static_proxies: state.proxyStaticProxies,
    selection_mode: $("#proxyRandomInput").checked ? "random" : "manual",
    selected_node: $("#proxyRandomInput").checked ? "" : (state.proxyConfig?.selected_node || state.proxyPool?.current || ""),
    health_url: $("#proxyHealthUrlInput").value.trim(),
    max_test_nodes: Number($("#proxyMaxNodesInput").value || 12),
    test_timeout_sec: Number($("#proxyTimeoutInput").value || 8),
    refresh_seconds: Number($("#proxyRefreshInput").value || 3600),
    mixed_port: Number($("#proxyMixedPortInput").value || 17890),
    controller_port: Number($("#proxyControllerPortInput").value || 19090),
    mihomo_path: $("#proxyMihomoPathInput").value.trim(),
    runtime_dir: $("#proxyRuntimeDirInput").value.trim() || "./proxy_pool_runtime",
  };
}

function renderProxySources() {
  $("#subscriptionCount").textContent = `${state.proxySubscriptions.length} 组`;
  $("#subscriptionList").innerHTML = state.proxySubscriptions.map(item => `<div class="proxy-source-item" data-source-id="${escapeHtml(item.id)}"><input class="proxy-source-toggle" data-source-type="subscription" type="checkbox" ${item.enabled ? "checked" : ""}><div class="proxy-source-item-copy"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.url_masked || "新订阅链接已填写")}</small></div><button class="icon-button proxy-source-delete" data-source-type="subscription" title="删除" type="button">×</button></div>`).join("") || '<div class="mini-empty">还没有订阅分组</div>';
  $("#staticProxyCount").textContent = `${state.proxyStaticProxies.length} 条`;
  $("#staticProxyList").innerHTML = state.proxyStaticProxies.map(item => `<div class="proxy-source-item" data-source-id="${escapeHtml(item.id)}"><input class="proxy-source-toggle" data-source-type="static" type="checkbox" ${item.enabled ? "checked" : ""}><div class="proxy-source-item-copy"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.host || "待保存")}:${escapeHtml(item.port || "")} · ${escapeHtml(item.username_masked || "凭证待保存")}</small></div><button class="icon-button proxy-source-delete" data-source-type="static" title="删除" type="button">×</button></div>`).join("") || '<div class="mini-empty">还没有静态代理</div>';
}

function nodeDelayView(node) {
  const tested = state.proxyNodeTests[node.id];
  if (tested?.testing) return ["测试中…", ""];
  if (tested?.error) return ["Error", "error"];
  const delay = Number(tested?.delay || node.delay || 0);
  if (!delay) return [node.alive === false ? "Error" : "未测试", node.alive === false ? "error" : ""];
  return [`${delay} ms`, delay < 500 ? "good" : "medium"];
}

function renderProxyPool() {
  const item = state.proxyPool;
  if (!item) return;
  $("#proxyPoolNode").textContent = item.current || "—";
  $("#proxyPoolNode").title = item.current || "";
  $("#proxyPoolAlive").textContent = `${item.alive || 0} / ${item.node_count || 0}`;
  const badge = $("#proxyRuntimeBadge");
  badge.textContent = item.running ? "运行中" : (item.enabled ? "待应用" : "已关闭");
  badge.className = `badge ${item.running ? "success" : item.enabled ? "violet" : "neutral"}`;
  if (!item.enabled) $("#proxyPoolSummary").textContent = "代理总开关已关闭，浏览器、邮箱和 CPA 直接连接。";
  else if (item.error) $("#proxyPoolSummary").textContent = item.error;
  else if (item.selection_mode === "random") $("#proxyPoolSummary").textContent = "跨组随机已开启：每次任务前会随机测试节点，不可用时自动更换。";
  else $("#proxyPoolSummary").textContent = "手动固定模式：点击任意节点卡片即可测试并设为默认出口。";
  const nodes = item.nodes || [];
  const groups = new Map();
  nodes.forEach(node => { if (!groups.has(node.group)) groups.set(node.group, []); groups.get(node.group).push(node); });
  $("#proxyNodeGroups").innerHTML = [...groups.entries()].map(([group, rows]) => `<section class="proxy-node-group"><div class="proxy-node-group-head"><h3>${escapeHtml(group)}</h3><span>${rows.length} 个节点</span></div><div class="proxy-node-grid">${rows.map(node => {
    const [delayText, delayClass] = nodeDelayView(node);
    const selected = (item.selected_node || item.current) === node.id && item.selection_mode === "manual";
    return `<article class="proxy-node-card ${selected ? "selected" : ""} ${state.proxyNodeTests[node.id]?.testing ? "testing" : ""}" data-node-id="${escapeHtml(node.id)}"><div class="proxy-node-card-head"><i class="${node.alive ? "alive" : ""}"></i><strong title="${escapeHtml(node.name)}">${escapeHtml(node.name)}</strong></div><div class="proxy-node-meta"><span>${escapeHtml(node.type || "Proxy")}</span>${node.endpoint ? `<span>${escapeHtml(node.endpoint)}</span>` : ""}</div><div class="proxy-node-card-foot"><b class="node-delay ${delayClass}">${delayText}</b><button class="node-test-button" type="button">测试</button></div></article>`;
  }).join("")}</div></section>`).join("");
  $("#proxyNodeEmpty").classList.toggle("visible", nodes.length === 0);
}

function renderTask(task) {
  const pill = $("#taskPill");
  pill.classList.toggle("running", task.running);
  $("span", pill).textContent = taskLabel(task.status);
  $("#taskStatusText").textContent = `${task.kind ? task.kind.toUpperCase() + " · " : ""}${taskLabel(task.status)}`;
  $("#stopTask").disabled = !task.running;
  const signature = JSON.stringify(task.logs || []);
  if (signature !== state.taskSignature) {
    state.taskSignature = signature;
    const log = $("#taskLog");
    if (!task.logs?.length) {
      log.innerHTML = '<div class="terminal-placeholder">等待任务启动…</div>';
    } else {
      log.innerHTML = task.logs.map(item => `<div class="terminal-line ${escapeHtml(item.level)}"><time>${escapeHtml(item.time)}</time><span>${escapeHtml(item.message)}</span></div>`).join("");
      log.scrollTop = log.scrollHeight;
    }
  }
}

async function loadOverview() {
  state.overview = await api("/api/overview");
  renderOverview();
  $("#serverLight").classList.add("online");
  $("#serverText").textContent = "服务已连接";
}

async function loadMailboxes() {
  const data = await api("/api/mailboxes");
  state.mailboxes = data.items || [];
  state.mailboxPath = data.path || "";
  renderMailboxes();
}

async function loadAccounts() {
  const data = await api("/api/accounts");
  state.accounts = data.items || [];
  renderAccounts();
}

async function loadFailures() {
  const data = await api("/api/failures");
  state.failures = data.items || [];
  renderFailures();
}

async function loadConfig() {
  const data = await api("/api/config");
  state.configFields = data.fields || [];
  state.configDraft = Object.fromEntries(state.configFields.map(field => [field.key, field.value]));
  $("#configPath").textContent = data.path || "config.json";
  renderConfig();
}

async function loadProxyPool() {
  const [configuration, status] = await Promise.all([api("/api/proxy-pool/config"), api("/api/proxy-pool/status")]);
  state.proxyConfig = configuration;
  state.proxyPool = status;
  populateProxyForm();
  renderProxyPool();
}

async function loadTask() {
  renderTask(await api("/api/task"));
}

async function refreshAll(showToast = false) {
  try {
    await Promise.all([loadOverview(), loadMailboxes(), loadAccounts(), loadFailures(), loadTask(), loadProxyPool()]);
    if (showToast) toast("数据已刷新");
  } catch (error) {
    $("#serverText").textContent = "连接失败";
    $("#serverLight").classList.remove("online");
    toast(error.message, "error");
  }
}

async function startTask(kind, options) {
  try {
    const task = await api("/api/task/start", {method:"POST", body:JSON.stringify({kind, options})});
    if (kind === "register") state.registrationCountTouched = false;
    renderTask(task);
    navigate("tasks");
    toast("任务已启动");
    await loadOverview();
  } catch (error) { toast(error.message, "error"); }
}

function selectedValues(selector) {
  return $$(selector).filter(item => item.checked).map(item => item.value);
}

async function showCredential(email) {
  try {
    const item = await api(`/api/account/credential?email=${encodeURIComponent(email)}`);
    $("#credentialTitle").textContent = item.email;
    $("#credentialEmail").textContent = item.email || "";
    $("#credentialPassword").textContent = item.password || "";
    $("#credentialSso").textContent = item.sso || "";
    openModal("#credentialModal");
  } catch (error) { toast(error.message, "error"); }
}

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
    toast("已复制到剪贴板");
  } catch {
    const area = document.createElement("textarea");
    area.value = value; document.body.append(area); area.select(); document.execCommand("copy"); area.remove();
    toast("已复制到剪贴板");
  }
}

document.addEventListener("click", async event => {
  const nav = event.target.closest("[data-page]");
  if (nav) navigate(nav.dataset.page);
  const goto = event.target.closest("[data-goto]");
  if (goto) navigate(goto.dataset.goto);
  const credential = event.target.closest(".credential-button");
  if (credential) showCredential(credential.dataset.email);
  const cpaBackfill = event.target.closest(".cpa-backfill-button");
  if (cpaBackfill) startTask("backfill", {limit:1, email:cpaBackfill.dataset.email, probe:true});
  const credentialRefresh = event.target.closest(".refresh-credential-button");
  if (credentialRefresh) {
    const email = credentialRefresh.dataset.email;
    if (confirm(`将刷新 ${email} 的 CPA 凭证。会优先使用 refresh_token，失败时自动重新执行 OIDC 获取，是否继续？`)) {
      startTask("backfill", {limit:1, email, probe:true, refresh_existing:true});
    }
  }
  const accountDelete = event.target.closest(".account-delete-button");
  if (accountDelete) deleteSuccessAccount(accountDelete.dataset.email);
  const cpaPush = event.target.closest(".cpa-push-button");
  if (cpaPush) pushCpa([cpaPush.dataset.email], true);
  const accountPushGroup = event.target.closest("[data-account-push-group]");
  if (accountPushGroup) {
    state.accountPushGroup = accountPushGroup.dataset.accountPushGroup;
    renderAccounts();
  }
  const modelRefresh = event.target.closest(".refresh-models-button");
  if (modelRefresh) refreshAccountModels(modelRefresh.dataset.email);
  const quotaRefresh = event.target.closest(".refresh-quota-button");
  if (quotaRefresh) refreshAccountQuota(quotaRefresh.dataset.email);
  const modelTag = event.target.closest(".model-tag");
  if (modelTag) testAccountModel(modelTag.dataset.email, modelTag.dataset.model);
  const copy = event.target.closest("[data-copy-target]");
  if (copy) copyText($(`#${copy.dataset.copyTarget}`).textContent);
  const reveal = event.target.closest(".reveal-config");
  if (reveal) {
    const input = $("input", reveal.parentElement);
    input.type = input.type === "password" ? "text" : "password";
  }
  const configTab = event.target.closest("[data-config-group]");
  if (configTab) {
    captureConfigDraft();
    state.configActiveGroup = configTab.dataset.configGroup;
    renderConfig();
  }
});

async function runModelAction(email, action, work) {
  if (state.modelActions[email]) return;
  state.modelActions[email] = action;
  renderAccounts();
  try {
    await work();
  } finally {
    delete state.modelActions[email];
    await loadAccounts();
  }
}

async function refreshAccountModels(email) {
  try {
    await runModelAction(email, "models", async () => {
      const result = await api("/api/account/models/refresh", {method:"POST", body:JSON.stringify({email})});
      if (!result.ok) throw new Error(result.error || `模型接口返回 HTTP ${result.status || 0}`);
      const prefix = result.credential_refreshed ? "凭证已自动刷新，" : "";
      toast(`${prefix}模型列表已更新：${result.models.length ? result.models.join(", ") : "未返回模型"}`);
    });
  } catch (error) {
    if (!offerFullCredentialRefresh(error, email)) toast(error.message, "error");
  }
}

async function testAccountModel(email, model) {
  try {
    await runModelAction(email, `test:${model}`, async () => {
      const result = await api("/api/account/model/test", {method:"POST", body:JSON.stringify({email, model})});
      if (!result.ok) throw new Error(`${model} 测试失败：${result.error || `HTTP ${result.status || 0}`}`);
      const rateText = modelRateLimitText({rate_limits:result.rate_limits});
      const prefix = result.credential_refreshed ? "凭证已自动刷新 · " : "";
      toast(`${prefix}${model} 测试通过 · HTTP ${result.status}${rateText ? ` · ${rateText}` : ""}`);
    });
  } catch (error) {
    if (!offerFullCredentialRefresh(error, email)) toast(error.message, "error");
  }
}

async function refreshAccountQuota(email) {
  try {
    await runModelAction(email, "quota", async () => {
      const result = await api("/api/account/quota/refresh", {method:"POST", body:JSON.stringify({email})});
      const available = (result.results || []).filter(item => item.ok).length;
      const total = (result.results || []).length;
      if (!result.ok) {
        const denied = (result.results || []).filter(item => item.reason === "permission_denied").map(item => item.model);
        if (denied.length) throw new Error(`账户没有聊天权限：${denied.join(", ")}。模型列表可见不代表可以调用，可删除该异常账户或在 xAI 控制台检查权限。`);
        const failed = (result.results || []).filter(item => !item.ok).map(item => `${item.model}(HTTP ${item.status || 0})`).join(", ");
        throw new Error(result.error || `额度状态刷新完成：${available}/${total} 个模型可用${failed ? `，受限：${failed}` : ""}`);
      }
      const prefix = result.credential_refreshed ? "凭证已自动刷新，" : "";
      toast(`${prefix}额度状态已刷新：${available}/${total} 个模型可用`);
    });
  } catch (error) {
    if (!offerFullCredentialRefresh(error, email)) toast(error.message, "error");
  }
}

async function deleteSuccessAccount(email) {
  const confirmed = confirm(`确定删除 ${email} 吗？\n\n将同时删除：成功账户记录、CPA 凭证、模型缓存、相关失败记录，以及对应的主邮箱配置。\n\n如果该账户使用“+别名”，主邮箱配置也会删除，之后不能再用它生成其他别名。已使用标记会保留，避免再次注册。此操作不可撤销。`);
  if (!confirmed) return;
  try {
    const result = await api("/api/accounts/delete", {method:"POST", body:JSON.stringify({emails:[email]})});
    toast(`删除完成：账户记录 ${result.account_rows} 条、邮箱配置 ${result.mailbox_rows} 条、CPA ${result.cpa_files} 个`);
    await Promise.all([loadAccounts(), loadMailboxes(), loadFailures(), loadOverview()]);
  } catch (error) { toast(error.message, "error"); }
}

async function pushCpa(emails = [], force = false) {
  if (state.cpaPushRunning) return;
  state.cpaPushRunning = true;
  renderAccounts();
  try {
    const result = await api("/api/cpa/push", {method:"POST", body:JSON.stringify({emails, force})});
    if (!result.ok) {
      const detail = (result.failures || []).slice(0, 2).map(item => `${item.name}: ${item.error}`).join("；");
      throw new Error(`CPA 推送部分失败：已上传 ${result.pushed}/${result.total}，已热加载 ${result.recognized}，已跳过 ${result.skipped || 0}${detail ? `；${detail}` : ""}`);
    }
    if (!result.pending) toast(`没有待推送凭证，已跳过 ${result.skipped} 个成功记录`);
    else toast(`已推送到 ${result.target}：新增 ${result.recognized} 个，跳过 ${result.skipped || 0} 个已成功凭证`);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.cpaPushRunning = false;
    await loadAccounts();
  }
}

async function pushAllCpa() {
  return pushCpa([], false);
}

function offerFullCredentialRefresh(error, email) {
  if (error?.code !== "credential_refresh_required") return false;
  const accepted = confirm("该账户的 refresh_token 已失效，需要执行完整 OIDC 凭证重取。是否现在启动？");
  if (accepted) {
    startTask("backfill", {limit:1, email, probe:true, refresh_existing:true});
  } else {
    toast("凭证仍然失效，可稍后点击“刷新凭证”重新获取", "error");
  }
  return true;
}

$$(".nav-item").forEach(button => button.addEventListener("click", () => navigate(button.dataset.page)));
$("#refreshButton").addEventListener("click", () => refreshAll(true));
$("#pushCpa").addEventListener("click", pushAllCpa);
[$("#quickExtra"), $("#registerExtra")].forEach(input => input.addEventListener("input", () => {
  state.registrationCountTouched = true;
  updateRegistrationControls();
}));
$("#mailboxSearch").addEventListener("input", renderMailboxes);
$("#accountSearch").addEventListener("input", renderAccounts);
$("#failureSearch").addEventListener("input", renderFailures);
$("#selectAllMailboxes").addEventListener("change", event => $$(".mail-check").forEach(item => item.checked = event.target.checked));
$("#selectAllFailures").addEventListener("change", event => $$(".failure-check").forEach(item => item.checked = event.target.checked));

$("#openImport").addEventListener("click", () => openModal("#importModal"));
$("#closeImport").addEventListener("click", () => closeModal("#importModal"));
$("#closeCredential").addEventListener("click", () => closeModal("#credentialModal"));
$$(".modal-backdrop").forEach(backdrop => backdrop.addEventListener("click", event => { if (event.target === backdrop) closeModal(`#${backdrop.id}`); }));

async function prepareImport(file) {
  if (!file) return;
  state.importText = await file.text();
  const count = state.importText.split(/\r?\n/).filter(line => line.trim() && line.includes("----")).length;
  $("#mailFileName").textContent = `${file.name} · ${(file.size / 1024).toFixed(1)} KB`;
  $("#importPreviewCount").textContent = count;
  $("#confirmImport").disabled = count === 0;
}

$("#mailFile").addEventListener("change", event => prepareImport(event.target.files[0]));
const dropZone = $(".drop-zone");
dropZone.addEventListener("dragover", event => { event.preventDefault(); dropZone.style.borderColor = "#2878ff"; });
dropZone.addEventListener("dragleave", () => dropZone.style.borderColor = "");
dropZone.addEventListener("drop", event => { event.preventDefault(); dropZone.style.borderColor = ""; prepareImport(event.dataTransfer.files[0]); });

$("#confirmImport").addEventListener("click", async () => {
  try {
    const result = await api("/api/mailboxes/import", {method:"POST", body:JSON.stringify({text:state.importText, mode:$("#importMode").value})});
    closeModal("#importModal");
    toast(`导入完成：共 ${result.total} 条，新增 ${result.added} 条，更新 ${result.updated} 条${result.invalid.length ? `，${result.invalid.length} 条无效` : ""}`);
    state.importText = ""; $("#mailFile").value = ""; $("#confirmImport").disabled = true;
    await Promise.all([loadMailboxes(), loadOverview()]);
  } catch (error) { toast(error.message, "error"); }
});

$("#deleteMailboxes").addEventListener("click", async () => {
  const emails = selectedValues(".mail-check");
  if (!emails.length) return toast("请先选择邮箱", "error");
  if (!confirm(`确定从邮箱池删除所选 ${emails.length} 条凭证吗？`)) return;
  try {
    const result = await api("/api/mailboxes/delete", {method:"POST", body:JSON.stringify({emails})});
    toast(`已删除 ${result.removed} 条邮箱凭证`);
    await Promise.all([loadMailboxes(), loadOverview()]);
  } catch (error) { toast(error.message, "error"); }
});

$("#deleteInvalidMailboxes").addEventListener("click", async () => {
  const count = state.mailboxes.filter(item => item.status === "oauth_expired").length;
  if (!count) return toast("当前没有授权失效邮箱", "error");
  if (!confirm(`确定一键删除 ${count} 个授权失效的主邮箱吗？\n\n这些邮箱的 refresh_token 已无法使用，不会再参与注册。相关注册失败记录也会一并清理。`)) return;
  try {
    const result = await api("/api/mailboxes/delete-invalid", {method:"POST", body:"{}"});
    toast(`已删除 ${result.removed} 个授权失效邮箱，清理 ${result.failure_rows} 条失败记录`);
    await Promise.all([loadMailboxes(), loadFailures(), loadOverview()]);
  } catch (error) { toast(error.message, "error"); }
});

$("#retryFailures").addEventListener("click", async () => {
  const emails = selectedValues(".failure-check");
  if (!emails.length) return toast("请先选择失败记录", "error");
  if (!confirm("将清除所选邮箱的失败标记，使其可以再次参与任务。是否继续？")) return;
  try {
    const result = await api("/api/failures/retry", {method:"POST", body:JSON.stringify({emails})});
    toast(`已清除 ${result.removed} 条失败记录`);
    await Promise.all([loadFailures(), loadMailboxes(), loadOverview()]);
  } catch (error) { toast(error.message, "error"); }
});

$("#quickStart").addEventListener("click", () => startTask("register", {extra:$("#quickExtra").value, threads:$("#quickThreads").value}));
$("#startRegister").addEventListener("click", () => startTask("register", {extra:$("#registerExtra").value, threads:$("#registerThreads").value, mint_workers:$("#registerMintWorkers").value}));
$("#startBackfill").addEventListener("click", () => startTask("backfill", {limit:$("#backfillLimit").value, email:$("#backfillEmail").value, probe:$("#backfillProbe").checked}));
$("#backfillMissingCpa").addEventListener("click", () => {
  const missing = state.accounts.filter(item => !item.has_cpa);
  if (!missing.length) return toast("所有成功账户都已经有 CPA 凭证");
  if (!confirm(`将为 ${missing.length} 个缺少凭证的成功账户依次获取 CPA，是否继续？`)) return;
  startTask("backfill", {limit:0, email:"", probe:true});
});
$("#stopTask").addEventListener("click", async () => {
  if (!confirm("确定停止当前任务吗？正在处理的账户可能会被标记为失败。")) return;
  try { renderTask(await api("/api/task/stop", {method:"POST", body:"{}"})); toast("任务已停止"); } catch (error) { toast(error.message, "error"); }
});

$("#reloadConfig").addEventListener("click", async () => { try { await loadConfig(); toast("已恢复磁盘上的配置"); } catch (error) { toast(error.message, "error"); } });
$("#saveConfig").addEventListener("click", async () => {
  captureConfigDraft();
  const values = Object.fromEntries(Object.entries(state.configDraft).filter(([key]) => !isProxyConfigKey(key)));
  try {
    const result = await api("/api/config", {method:"POST", body:JSON.stringify({values})});
    toast(`配置已保存，共 ${result.saved} 项`);
    if (result.proxy_pool_warning) toast(`代理池配置已保存，但启动失败：${result.proxy_pool_warning}`, "error");
    await Promise.all([loadConfig(), loadOverview(), loadProxyPool()]);
  } catch (error) { toast(error.message, "error"); }
});

$$("[data-proxy-mode]").forEach(button => button.addEventListener("click", () => {
  $("#proxyEnabledInput").checked = true;
  updateProxyModeUI(button.dataset.proxyMode);
}));

$("#proxyEnabledInput").addEventListener("change", () => updateProxyModeUI(state.proxyMode));

$("#addSubscription").addEventListener("click", () => {
  const name = $("#subscriptionNameInput").value.trim();
  const url = $("#subscriptionUrlInput").value.trim();
  if (!name || !url) return toast("请填写订阅分组名称和链接", "error");
  state.proxySubscriptions.push({id:`new_sub_${Date.now()}`, name, url, enabled:true, url_masked:"新订阅链接已填写"});
  $("#subscriptionNameInput").value = "";
  $("#subscriptionUrlInput").value = "";
  $("#proxyEnabledInput").checked = true;
  updateProxyModeUI("clash_subscription");
  renderProxySources();
});

$("#addStaticProxies").addEventListener("click", () => {
  const lines = $("#staticProxyImport").value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  if (!lines.length) return toast("请粘贴静态代理，每行一条", "error");
  for (const [index, raw] of lines.entries()) {
    const parts = raw.split(":");
    if (parts.length < 4 || !/^\d+$/.test(parts[1] || "")) return toast(`第 ${index + 1} 行格式不正确`, "error");
    state.proxyStaticProxies.push({id:`new_static_${Date.now()}_${index}`, name:`静态代理 ${state.proxyStaticProxies.length + 1}`, host:parts[0], port:Number(parts[1]), username_masked:"凭证已填写", raw, enabled:true});
  }
  $("#staticProxyImport").value = "";
  $("#proxyEnabledInput").checked = true;
  updateProxyModeUI("static_pool");
  renderProxySources();
});

function updateProxySource(event, type) {
  if (!event.target.classList.contains("proxy-source-toggle")) return;
  const id = event.target.closest("[data-source-id]").dataset.sourceId;
  const items = type === "subscription" ? state.proxySubscriptions : state.proxyStaticProxies;
  const item = items.find(row => row.id === id);
  if (item) item.enabled = event.target.checked;
}
$("#subscriptionList").addEventListener("change", event => updateProxySource(event, "subscription"));
$("#staticProxyList").addEventListener("change", event => updateProxySource(event, "static"));

function deleteProxySource(event) {
  const button = event.target.closest(".proxy-source-delete");
  if (!button) return;
  const id = button.closest("[data-source-id]").dataset.sourceId;
  if (button.dataset.sourceType === "subscription") state.proxySubscriptions = state.proxySubscriptions.filter(row => row.id !== id);
  else state.proxyStaticProxies = state.proxyStaticProxies.filter(row => row.id !== id);
  renderProxySources();
}
$("#subscriptionList").addEventListener("click", deleteProxySource);
$("#staticProxyList").addEventListener("click", deleteProxySource);

async function saveProxyWorkspace(showToast = true) {
  try {
    const result = await api("/api/proxy-pool/config", {method:"POST", body:JSON.stringify(proxyFormValues())});
    if (showToast) toast("代理配置已保存并应用");
    if (result.proxy_pool_warning) toast(`代理配置已保存，但应用失败：${result.proxy_pool_warning}`, "error");
    await Promise.all([loadConfig(), loadOverview(), loadProxyPool()]);
    return result;
  } catch (error) {
    toast(error.message, "error");
    throw error;
  }
}

$("#saveProxyConfig").addEventListener("click", () => saveProxyWorkspace().catch(() => {}));

$("#proxyRandomInput").addEventListener("change", async event => {
  try {
    await saveProxyWorkspace(false);
    toast(event.target.checked ? "已开启跨组随机，默认节点已取消" : "已关闭随机，可点击节点固定出口");
  } catch { /* saveProxyWorkspace already reported */ }
});

$("#refreshProxyPool").addEventListener("click", async () => {
  try {
    await saveProxyWorkspace(false);
    await api("/api/proxy-pool/refresh", {method:"POST", body:"{}"});
    await loadProxyPool();
    toast("代理节点已刷新");
  } catch (error) { toast(error.message, "error"); }
});

$("#proxyNodeGroups").addEventListener("click", async event => {
  const card = event.target.closest(".proxy-node-card");
  if (!card) return;
  const name = card.dataset.nodeId;
  const testingOnly = Boolean(event.target.closest(".node-test-button"));
  state.proxyNodeTests[name] = {testing:true};
  renderProxyPool();
  try {
    const result = await api(testingOnly ? "/api/proxy-pool/node/test" : "/api/proxy-pool/node/select", {method:"POST", body:JSON.stringify({name, target:$("#proxyHealthUrlInput").value.trim()})});
    state.proxyNodeTests[name] = {delay:result.delay};
    if (!testingOnly) {
      $("#proxyRandomInput").checked = false;
      toast(`已固定到 ${result.display_name || name} · ${result.delay}ms`);
      await loadProxyPool();
    } else {
      renderProxyPool();
      toast(`${result.display_name || name} 测试成功 · ${result.delay}ms`);
    }
  } catch (error) {
    state.proxyNodeTests[name] = {error:error.message};
    renderProxyPool();
    toast(error.message, "error");
  }
});

setInterval(async () => {
  try {
    await loadTask();
    if ($("#page-overview").classList.contains("active")) await loadOverview();
    if ($("#page-accounts").classList.contains("active")) await loadAccounts();
  } catch { /* transient server restart */ }
}, 1800);

(async function boot() {
  navigate(location.hash.slice(1) in pages ? location.hash.slice(1) : "overview");
  await refreshAll();
  try { await Promise.all([loadConfig(), loadProxyPool()]); } catch (error) { toast(error.message, "error"); }
})();
