"use strict";
const $ = (id) => document.getElementById(id);
const names = {
  bilibili: "哔哩哔哩",
  youtube: "YouTube",
  github: "GitHub",
  v2ex: "V2EX",
  xiaohongshu: "小红书",
  twitter: "X / Twitter",
  reddit: "Reddit",
  facebook: "Facebook",
  instagram: "Instagram",
  linkedin: "LinkedIn",
  boss: "BOSS 直聘",
  xueqiu: "雪球",
  xiaoyuzhou: "小宇宙",
  web: "公开 Web",
};
const icons = {
  bilibili: "B",
  youtube: "▶",
  github: "GH",
  v2ex: "V₂",
  xiaohongshu: "红",
  twitter: "𝕏",
  reddit: "r",
  facebook: "f",
  instagram: "◎",
  linkedin: "in",
  boss: "聘",
  xueqiu: "球",
  xiaoyuzhou: "宇",
  web: "↗",
};
const actionNames = {
  search: "搜索",
  comments: "评论",
  read: "读取",
  hot: "热门",
  subtitle: "字幕",
  transcribe: "转录",
  profile: "资料",
  tweets: "推文",
  thread: "线程",
  subreddit: "社区",
  posts: "帖子",
  jobs: "职位",
  node: "节点",
  user: "用户",
  quote: "行情",
  hot_posts: "热门帖子",
  hot_stocks: "热门股票",
};
const state = {
  csrf: "",
  browserEnabled: true,
  channels: [],
  selected: "",
  view: "channels",
  capabilities: {},
  credentials: {},
  doctor: {},
  jobs: [],
  loginJob: null,
  frameBusy: false,
};
let toastTimer;
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function button(text, cls, callback) {
  const node = el("button", cls, text);
  node.type = "button";
  node.addEventListener("click", callback);
  return node;
}
function toast(text, error = false) {
  $("toast").textContent = text;
  $("toast").className = "toast" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").classList.add("hidden"), 6500);
}
async function api(path, { method = "GET", body } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/admin/login") showLogin();
    const error = new Error(
      data.error?.message || data.detail || `请求失败 (${response.status})`,
    );
    error.response = data;
    throw error;
  }
  return data;
}
function showLogin() {
  state.csrf = "";
  state.loginJob = null;
  $("workspace").classList.add("hidden");
  $("login-view").classList.remove("hidden");
  $("logout").classList.add("hidden");
  $("connection").textContent = "管理未登录";
  $("browser-frame").removeAttribute("src");
  if ($("browser-dialog").open) $("browser-dialog").close();
}
async function enter(session) {
  state.csrf = session.csrf;
  $("login-view").classList.add("hidden");
  $("workspace").classList.remove("hidden");
  $("logout").classList.remove("hidden");
  $("connection").textContent = "管理会话已连接";
  $("insecure-banner").classList.toggle("hidden", !session.insecure_http);
  await refreshChannels();
  await refreshCredentials().catch((e) => toast(e.message, true));
  await refreshJobs();
}
async function refreshChannels() {
  const data = await api("/admin/api/channels");
  state.channels = data.channels;
  state.browserEnabled = data.browser_enabled !== false;
  $("native-banner").classList.toggle("hidden", state.browserEnabled);
  $("upstream-version").textContent = data.agent_reach_version;
  $("total-count").textContent = `共 ${data.channels.length} 个渠道`;
  $("channel-total").textContent = data.channels.length;
  $("enabled-count").textContent = data.channels.filter(
    (c) => c.enabled && c.actions.length,
  ).length;
  $("action-count").textContent = data.channels
    .filter((c) => c.enabled)
    .reduce((n, c) => n + c.actions.length, 0);
  if (!state.selected && data.channels.length)
    state.selected = data.channels[0].name;
  renderChannels();
  populateSelects();
}
function statusFor(channel) {
  const current = state.doctor[channel.name];
  const saved = state.credentials.channels?.[channel.name];
  if (!channel.enabled) return ["未开放", ""];
  if (!state.browserEnabled && !channel.actions.length)
    return ["浏览器已禁用", "amber"];
  if (["ready", "verified", "search_ready"].includes(current?.status))
    return ["最近验证通过", "green"];
  if (current?.status === "needs_setup" || current?.status === "not_added")
    return ["待准备依赖", "amber"];
  if (current?.status === "needs_login") return ["需要登录", "amber"];
  if (current?.status === "broken") return ["最近验证失败", "red"];
  if (current?.status === "cooldown") return ["平台冷却中", "amber"];
  if (current?.status === "needs_user_action") return ["需要人工处理", "amber"];
  if (saved?.verified) return ["凭据曾验证", "green"];
  if (saved?.saved) return ["会话已保存", "green"];
  return ["已开放 · 待验证", ""];
}
function renderChannels() {
  const filter = $("channel-search").value.trim().toLowerCase();
  const list = $("channels-list");
  list.replaceChildren();
  for (const channel of state.channels.filter((c) =>
    (c.name + names[c.name] + c.description).toLowerCase().includes(filter),
  )) {
    const row = el(
      "div",
      "channel-row" + (state.selected === channel.name ? " selected" : ""),
    );
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute(
      "aria-label",
      `查看 ${names[channel.name] || channel.name}`,
    );
    row.addEventListener("click", () => {
      state.selected = channel.name;
      renderChannels();
    });
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target === row) {
        state.selected = channel.name;
        renderChannels();
      }
    });
    const label = el("div", "platform-name");
    label.append(el("span", "platform-icon", icons[channel.name] || "↗"));
    const description = el("span");
    description.append(
      el("strong", "", names[channel.name] || channel.name),
      el("small", "", `${channel.name} · ${channel.actions.length} 个动作`),
    );
    label.append(description);
    const [text, color] = statusFor(channel);
    const toggle = button(
      "",
      "switch" + (channel.enabled ? " on" : ""),
      async (e) => {
        e.stopPropagation();
        toggle.disabled = true;
        try {
          await savePolicy(channel, { enabled: !channel.enabled });
          toast(channel.enabled ? "已关闭查询访问" : "已开放该渠道的只读接口");
        } catch (error) {
          toast(error.message, true);
        } finally {
          toggle.disabled = false;
        }
      },
    );
    toggle.setAttribute("role", "switch");
    toggle.setAttribute("aria-checked", String(channel.enabled));
    toggle.setAttribute(
      "aria-label",
      `${channel.enabled ? "关闭" : "开放"} ${names[channel.name] || channel.name}`,
    );
    row.append(label, el("span", "badge " + color, text), toggle);
    list.append(row);
  }
  if (!list.childElementCount)
    list.append(el("div", "empty-row", "没有匹配的渠道"));
  renderDetail();
}
function renderDetail() {
  const channel = state.channels.find((c) => c.name === state.selected);
  const panel = $("channel-detail");
  panel.replaceChildren();
  if (!channel) return;
  const header = el("div", "detail-header");
  header.append(el("span", "platform-icon", icons[channel.name] || "↗"));
  const title = el("div");
  title.append(
    el("h3", "", names[channel.name] || channel.name),
    el("p", "muted", channel.description),
  );
  header.append(title);
  panel.append(header);
  const [text, color] = statusFor(channel);
  panel.append(el("span", "badge " + color, text));
  const capabilities = el("div", "detail-section");
  capabilities.append(el("div", "detail-label", "只读能力"));
  const chips = el("div", "chips");
  for (const action of channel.available_actions)
    chips.append(
      el("span", "chip", `${actionNames[action] || action} / ${action}`),
    );
  capabilities.append(chips);
  panel.append(capabilities);
  const scope = el("div", "detail-section");
  scope.append(el("div", "detail-label", "允许的 HTTPS 域名"));
  const hosts = el("div", "chips");
  for (const host of channel.hosts) hosts.append(el("span", "chip", host));
  scope.append(hosts);
  panel.append(scope);
  const actions = el("div", "detail-actions");
  const install = button("准备依赖", "secondary", () =>
    startJob("/admin/api/install", { platform: channel.name }),
  );
  install.disabled = channel.can_install === false;
  const verify = button("验证能力", "quiet", () =>
    startJob("/admin/api/verify", { platform: channel.name }),
  );
  verify.disabled = !channel.actions.length;
  actions.append(
    button("配置策略", "secondary", () => openPolicy(channel)),
    install,
    verify,
    button("凭据与登录 ↗", "quiet", () => {
      setView("credentials");
      $("credential-channel").value = channel.name;
      renderCredentialState();
    }),
  );
  panel.append(actions);
  panel.append(
    el(
      "p",
      "detail-note",
      "平台启用不代表已安装或已登录。凭据和登录会话保存在自己的服务端，查询令牌不能执行管理操作。",
    ),
  );
  const info = state.doctor[channel.name];
  if (info?.problems?.length)
    panel.append(el("p", "notice amber", info.problems.join("；")));
}
async function savePolicy(channel, changes) {
  await api(`/admin/api/channels/${encodeURIComponent(channel.name)}`, {
    method: "PUT",
    body: {
      hosts: channel.hosts,
      enabled: channel.enabled,
      actions: channel.selected_actions,
      ...changes,
    },
  });
  await refreshChannels();
}
function openPolicy(channel) {
  $("policy-title").textContent =
    (names[channel.name] || channel.name) + " · 策略";
  $("policy-form").dataset.platform = channel.name;
  $("policy-hosts").value = channel.hosts.join("\n");
  $("policy-actions").replaceChildren();
  for (const action of channel.available_actions) {
    const label = el("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = action;
    input.checked =
      channel.selected_actions === null ||
      channel.selected_actions.includes(action);
    label.append(input, document.createTextNode(action));
    $("policy-actions").append(label);
  }
  $("policy-dialog").showModal();
}
function setView(view) {
  state.view = view;
  const titles = {
    channels: ["渠道管理", "按需连接平台，让能力和权限一目了然。"],
    credentials: ["凭据与登录", "在自己的环境里保存会话，按平台选择认证方式。"],
    playground: ["只读操作台", "从上游动作声明生成参数，直接验证真实能力。"],
  };
  document
    .querySelectorAll(".view")
    .forEach((node) =>
      node.classList.toggle("hidden", node.id !== "view-" + view),
    );
  document
    .querySelectorAll(".nav-item")
    .forEach((node) =>
      node.classList.toggle("active", node.dataset.view === view),
    );
  $("page-title").textContent = titles[view][0];
  $("breadcrumb").textContent = titles[view][0];
  $("page-description").textContent = titles[view][1];
  if (view === "credentials")
    refreshCredentials().catch((e) => toast(e.message, true));
}
function populateSelects() {
  for (const id of ["credential-channel", "action-channel"]) {
    const previous = $(id).value;
    $(id).replaceChildren();
    for (const channel of state.channels.filter(
      (c) => id !== "action-channel" || (c.enabled && c.action_schemas.length),
    )) {
      const option = el("option", "", names[channel.name] || channel.name);
      option.value = channel.name;
      $(id).append(option);
    }
    if ([...$(id).options].some((o) => o.value === previous))
      $(id).value = previous;
  }
  renderActions();
  renderCredentialState();
}
async function refreshCredentials() {
  const data = await api("/admin/api/credentials");
  if (data.capabilities) state.capabilities = data.capabilities.channels || {};
  if (data.status) state.credentials = data.status;
  $("key-count").textContent = Object.values(
    state.credentials.keys || {},
  ).filter(Boolean).length;
  renderCredentialState();
  renderChannels();
}
function renderCredentialState() {
  const key = $("key-type").value;
  $("key-official").href = {
    "github-token": "https://github.com/settings/tokens",
    "groq-key": "https://console.groq.com/keys",
    "openai-key": "https://platform.openai.com/api-keys",
  }[key];
  $("key-status").textContent = state.credentials.keys?.[key]
    ? "已配置 · 原值不回显"
    : "尚未配置";
  const platform = $("credential-channel").value;
  const auth = state.capabilities[platform] || {};
  const saved = state.credentials.channels?.[platform] || {};
  const official = $("site-official");
  official.classList.add("hidden");
  official.removeAttribute("href");
  if (auth.login_url) {
    const url = new URL(auth.login_url);
    if (url.protocol === "https:" && !url.username && !url.password) {
      official.href = url.href;
      official.classList.remove("hidden");
    }
  }
  $("cookie-status").textContent = saved.saved
    ? saved.verified
      ? "已保存 · 上次认证验证通过"
      : "已保存 · 等待实际动作验证"
    : "未发现已保存会话";
  $("cookie-support").textContent = auth.cookie_import
    ? "可导入本平台 Cookie。授权域：" + (auth.domains || []).join("、")
    : !state.browserEnabled && !["bilibili", "youtube"].includes(platform)
      ? "当前原生模式不支持此平台的浏览器会话导入，请在支持浏览器的执行端配置。"
      : "此平台未提供 Cookie 导入；使用对应的 API 凭据或公开内容能力。";
  $("cookie-value").disabled = !auth.cookie_import;
  $("import-cookie").disabled = !auth.cookie_import;
  $("open-login").disabled = !auth.login;
  $("clear-cookie").disabled = !auth.cookie_import && !auth.login;
}
async function startJob(path, body, openBrowser = false) {
  try {
    const job = await api(path, { method: "POST", body: body || {} });
    if (openBrowser) {
      state.loginJob = job.id;
      $("browser-title").textContent =
        (names[body.platform] || body.platform) + " · 专用登录";
      $("browser-frame").removeAttribute("src");
      $("browser-placeholder").classList.remove("hidden");
      $("browser-url").textContent = "正在准备依赖与浏览器…";
      $("browser-dialog").showModal();
    }
    await refreshJobs();
    toast("管理任务已开始");
    return job;
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}
async function refreshJobs() {
  const data = await api("/admin/api/jobs");
  const old = new Map(state.jobs.map((j) => [j.id, j.state]));
  state.jobs = data.jobs;
  const list = $("jobs-list");
  list.replaceChildren();
  if (!data.jobs.length)
    list.append(
      el("div", "empty-row", "还没有管理任务。读取状态或配置一个渠道开始。"),
    );
  for (const job of [...data.jobs].reverse()) {
    const row = el("div", "job-row");
    const text = el("div");
    const labels = {
      status: "读取状态",
      install: "准备依赖",
      verify: "验证能力",
      credential: "配置凭据",
      cookies: "导入 Cookie",
      cookie_delete: "清除 Cookie",
      login: "平台登录",
      logout: "退出平台",
    };
    text.append(
      el(
        "strong",
        "",
        `${labels[job.kind] || job.kind} · ${names[job.platform] || job.platform}`,
      ),
      el("p", "", job.code ? `${job.code} · ${job.message}` : job.message),
    );
    const actions = el("div", "job-actions");
    const done = ["succeeded", "failed", "cancelled"].includes(job.state);
    actions.append(
      el(
        "span",
        "badge " +
          (job.state === "succeeded"
            ? "green"
            : job.state === "failed"
              ? "red"
              : "amber"),
        {
          queued: "等待中",
          running: "执行中",
          waiting_user: "等待登录",
          succeeded: "已完成",
          failed: "未完成",
          cancelled: "已取消",
        }[job.state] || job.state,
      ),
    );
    if (!done)
      actions.append(
        button("取消", "quiet", () =>
          api(`/admin/api/jobs/${job.id}/cancel`, { method: "POST", body: {} })
            .then(refreshJobs)
            .catch((e) => toast(e.message, true)),
        ),
      );
    row.append(text, actions);
    list.append(row);
    if (
      job.state === "succeeded" &&
      old.has(job.id) &&
      old.get(job.id) !== "succeeded"
    ) {
      if (job.kind === "status") {
        for (const channel of job.result?.channels || [])
          state.doctor[channel.name || channel.channel] = channel;
        renderChannels();
      }
      if (job.kind !== "status")
        refreshCredentials().catch((e) => toast(e.message, true));
    }
    if (job.id === state.loginJob) {
      if (job.state === "waiting_user") refreshFrame();
      else if (done) {
        state.loginJob = null;
        if ($("browser-dialog").open) $("browser-dialog").close();
        $("browser-frame").removeAttribute("src");
        toast(
          job.state === "succeeded"
            ? job.result?.verified
              ? "登录已验证并保存"
              : "会话已保存，请通过实际动作验证"
            : job.message,
          job.state === "failed",
        );
      }
    }
  }
}
function renderActions() {
  const channel = state.channels.find(
    (c) => c.name === $("action-channel").value,
  );
  const previous = $("action-name").value;
  $("action-name").replaceChildren();
  for (const action of channel?.action_schemas || []) {
    const option = el(
      "option",
      "",
      `${actionNames[action.name] || action.name} / ${action.name}`,
    );
    option.value = action.name;
    $("action-name").append(option);
  }
  if ([...$("action-name").options].some((o) => o.value === previous))
    $("action-name").value = previous;
  renderParams();
}
function renderParams() {
  const channel = state.channels.find(
    (c) => c.name === $("action-channel").value,
  );
  const action = channel?.action_schemas.find(
    (a) => a.name === $("action-name").value,
  );
  $("action-params").replaceChildren();
  for (const param of action?.params || []) {
    const label = el("label", "", param.name + (param.required ? " *" : ""));
    const input = el("input");
    input.name = param.name;
    input.required = param.required;
    input.type = param.type === "int" ? "number" : "text";
    input.placeholder = param.help || param.name;
    if (param.type === "int") {
      input.min = "1";
      input.max = "50";
    }
    if (
      !param.required &&
      param.default !== null &&
      param.default !== undefined
    )
      input.value = param.default;
    input.maxLength = param.name === "target" ? 2048 : 300;
    label.append(input);
    $("action-params").append(label);
  }
  $("action-runtime").textContent = !action
    ? ""
    : action.browser_backend
      ? action.public_web_available
        ? "站内工具路径需要浏览器。上游在特定条件下会先尝试公开网页路径；公开结果不等于站内完整数据。"
        : "此动作需要服务端浏览器运行；导入 Cookie 不能替代浏览器内核。"
      : "此动作不依赖完整浏览器，通过原生工具或 HTTP 后端执行；仍需准备对应工具与凭据。";
  $("run-action").disabled = !action;
}
async function refreshFrame() {
  if (state.frameBusy || !state.loginJob || !$("browser-dialog").open) return;
  const job = state.jobs.find((j) => j.id === state.loginJob);
  if (job?.state !== "waiting_user") return;
  state.frameBusy = true;
  try {
    const frame = await api("/admin/api/login/frame");
    $("browser-frame").src = "data:image/png;base64," + frame.image;
    $("browser-placeholder").classList.add("hidden");
    $("browser-url").textContent = frame.url || "专用浏览器";
  } catch (error) {
    $("browser-url").textContent = error.message;
  } finally {
    state.frameBusy = false;
  }
}
async function browserInput(body) {
  try {
    await api("/admin/api/login/input", { method: "POST", body });
    await refreshFrame();
  } catch (error) {
    toast(error.message, true);
  }
}
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.submitter;
  submit.disabled = true;
  $("login-message").textContent = "正在建立管理会话…";
  const token = $("admin-token").value;
  $("admin-token").value = "";
  try {
    const session = await api("/admin/login", {
      method: "POST",
      body: { token },
    });
    $("login-message").textContent = "";
    await enter(session);
  } catch (error) {
    $("login-message").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
$("logout").addEventListener("click", async () => {
  try {
    await api("/admin/logout", { method: "POST", body: {} });
    state.credentials = {};
    state.jobs = [];
    $("key-value").value = "";
    $("cookie-value").value = "";
    $("action-result").textContent = "选择平台与动作，结果将在这里显示。";
    showLogin();
  } catch (error) {
    toast(error.message, true);
  }
});
document.querySelectorAll(".nav-item").forEach((node) =>
  node.addEventListener("click", () => {
    if (state.csrf) setView(node.dataset.view);
  }),
);
$("channel-search").addEventListener("input", renderChannels);
$("refresh-status").addEventListener("click", () =>
  startJob("/admin/api/status"),
);
$("refresh-credentials").addEventListener("click", () =>
  refreshCredentials().catch((e) => toast(e.message, true)),
);
$("key-type").addEventListener("change", renderCredentialState);
$("credential-channel").addEventListener("change", () => {
  $("cookie-value").value = "";
  renderCredentialState();
});
$("key-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const value = $("key-value").value;
  $("key-value").value = "";
  if (value)
    startJob("/admin/api/credentials", { key: $("key-type").value, value });
  else toast("请先输入新凭据", true);
});
$("delete-key").addEventListener("click", () => {
  if (
    confirm("清除服务端保存的这项 API 凭据？环境变量提供的凭据不受此操作影响。")
  )
    startJob("/admin/api/credentials", {
      key: $("key-type").value,
      value: null,
    });
});
$("cookie-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const cookies = $("cookie-value").value;
  $("cookie-value").value = "";
  startJob("/admin/api/cookies", {
    platform: $("credential-channel").value,
    cookies,
  });
});
$("open-login").addEventListener("click", () =>
  startJob(
    "/admin/api/login",
    { platform: $("credential-channel").value },
    true,
  ),
);
$("clear-cookie").addEventListener("click", () => {
  const platform = $("credential-channel").value;
  if (
    confirm(
      "清除所选平台的保存会话？其他平台不受影响；YouTube 不会退出 Google 账号。",
    )
  )
    startJob(
      state.capabilities[platform]?.cookie_delete
        ? "/admin/api/cookies/delete"
        : "/admin/api/logout-platform",
      { platform },
    );
});
$("policy-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const channel = state.channels.find(
    (c) => c.name === $("policy-form").dataset.platform,
  );
  const hosts = $("policy-hosts")
    .value.split(/[\n,]/)
    .map((h) => h.trim())
    .filter(Boolean);
  const actions = [
    ...$("policy-actions").querySelectorAll("input:checked"),
  ].map((n) => n.value);
  try {
    await savePolicy(channel, { hosts, actions });
    $("policy-dialog").close();
    toast("渠道策略已保存并生效");
  } catch (error) {
    toast(error.message, true);
  }
});
document
  .querySelectorAll(".close-dialog")
  .forEach((node) =>
    node.addEventListener("click", () => $("policy-dialog").close()),
  );
$("action-channel").addEventListener("change", renderActions);
$("action-name").addEventListener("change", renderParams);
$("action-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const params = {};
  for (const input of $("action-params").querySelectorAll("input")) {
    if (input.value !== "")
      params[input.name] =
        input.type === "number" ? Number(input.value) : input.value;
  }
  $("run-action").disabled = true;
  $("result-status").textContent = "执行中";
  try {
    const result = await api(
      `/admin/api/actions/${encodeURIComponent($("action-channel").value)}/${encodeURIComponent($("action-name").value)}`,
      { method: "POST", body: { params } },
    );
    $("action-result").textContent = JSON.stringify(result, null, 2);
    $("result-status").textContent = result.ok ? "已返回" : "未完成";
  } catch (error) {
    $("action-result").textContent = error.response
      ? JSON.stringify(error.response, null, 2)
      : error.message;
    $("result-status").textContent = "未完成";
  } finally {
    $("run-action").disabled = false;
  }
});
$("open-schema").addEventListener("click", async () => {
  try {
    const data = await api("/admin/api/openapi");
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
    );
    const a = el("a");
    a.href = url;
    a.download = "reach-bridge-openapi.json";
    a.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    toast(error.message, true);
  }
});
$("browser-frame").addEventListener("click", (event) => {
  const img = event.currentTarget;
  const r = img.getBoundingClientRect();
  browserInput({
    action: "click",
    x: Math.round(((event.clientX - r.left) * img.naturalWidth) / r.width),
    y: Math.round(((event.clientY - r.top) * img.naturalHeight) / r.height),
  });
  $("browser-stage").focus();
});
$("browser-stage").addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    browserInput({
      action: "scroll",
      delta: Math.max(-2000, Math.min(2000, Math.round(event.deltaY))),
    });
  },
  { passive: false },
);
$("type-browser").addEventListener("click", () => {
  const text = $("browser-text").value;
  $("browser-text").value = "";
  if (text) browserInput({ action: "type", text });
});
$("browser-text").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    $("type-browser").click();
  }
});
$("browser-stage").addEventListener("keydown", (event) => {
  const keys = [
    "Enter",
    "Tab",
    "Escape",
    "Backspace",
    "Delete",
    "ArrowLeft",
    "ArrowRight",
    "ArrowUp",
    "ArrowDown",
    "Home",
    "End",
    "PageUp",
    "PageDown",
  ];
  if (keys.includes(event.key)) {
    event.preventDefault();
    browserInput({ action: "key", key: event.key });
  } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey) {
    event.preventDefault();
    browserInput({ action: "type", text: event.key });
  }
});
$("tab-browser").addEventListener("click", () =>
  browserInput({ action: "key", key: "Tab" }),
);
$("enter-browser").addEventListener("click", () =>
  browserInput({ action: "key", key: "Enter" }),
);
$("reload-browser").addEventListener("click", () =>
  browserInput({ action: "reload" }),
);
$("finish-login").addEventListener("click", () =>
  api("/admin/api/login/finish", { method: "POST", body: {} })
    .then(refreshJobs)
    .catch((e) => toast(e.message, true)),
);
async function cancelLogin() {
  if (state.loginJob)
    await api(`/admin/api/jobs/${state.loginJob}/cancel`, {
      method: "POST",
      body: {},
    });
  state.loginJob = null;
  $("browser-dialog").close();
  $("browser-frame").removeAttribute("src");
  await refreshJobs();
}
$("cancel-login").addEventListener("click", () =>
  cancelLogin().catch((e) => toast(e.message, true)),
);
$("browser-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  cancelLogin().catch((e) => toast(e.message, true));
});
let polling = false;
setInterval(async () => {
  if (state.csrf && !polling) {
    polling = true;
    try {
      await refreshJobs();
    } catch (error) {
      if (state.csrf) toast(error.message, true);
    } finally {
      polling = false;
    }
  }
}, 2000);
setInterval(() => {
  if (state.loginJob) refreshFrame();
}, 1500);
(async () => {
  try {
    const info = await api("/admin/bootstrap");
    $("admin-disabled").classList.toggle("hidden", info.enabled);
    $("insecure-login").classList.toggle("hidden", !info.insecure_http);
    if (!info.enabled) {
      $("login-form").querySelector("button[type=submit]").disabled = true;
      $("admin-token").disabled = true;
    }
    try {
      await enter(await api("/admin/api/session"));
    } catch {
      showLogin();
    }
  } catch (error) {
    $("login-message").textContent = error.message;
    $("connection").textContent = "连接未完成";
  }
})();
