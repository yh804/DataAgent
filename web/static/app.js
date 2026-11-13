/* 数据分析 Agent 单页应用(原生 JS,无构建步骤)。
   SSE 用 fetch + ReadableStream 手动解析(EventSource 不支持 POST)。
   所有 Markdown 一律 marked 解析 + DOMPurify 消毒后才进 innerHTML。 */

"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  threadId: localStorage.getItem("da_thread_id") || "",
  busy: false, // 正在执行(含等待审批)时禁止重复发送
  attachment: null, // 当前附件:{name} 为服务端数据集名
  uploading: false, // 上传中:禁止发送,chip 显示「上传中…」
  view: "chat", // 当前视图:chat / report / charts / code / trace
  databases: [], // 侧栏数据库连接(仅元数据,不含密码)
  database: null, // 当前选中用于提问的数据库连接
  editingDb: null, // 正在编辑的连接;null 表示新增
  browseDb: null, // 数据库详情页当前连接
  dbSemanticsTable: "", // 字段面板当前表名(切换表前据此保存)
  dbSemanticsSaved: null, // 当前表最近一次成功保存的说明:{table, description, columns}
  lastSqlCard: null, // 最近一次 sql 事件的独立卡片(结果卡会复用其折叠块)
  lastSqlBlock: null, // 最近一次 sql 事件的 <details> 节点
  lastSqlText: "", // 最近一次 SQL,用于判断能否复用
  dbPending: null, // 正在查询数据库的卡片状态:{card, badge, wait, block, elapsed,...}
  abortController: null, // 当前 SSE 请求的取消控制器(仅数据库查询阶段可点取消)
  llmReady: false, // 模型是否可用(本机 key 或服务端 .env);false 时禁用发送
};

/* ---------- 模型设置(BYOK):密钥只存 localStorage ---------- */

const LLM_SETTINGS_KEY = "llmSettings";
const LLM_DEFAULT_BASE = "https://api.deepseek.com/v1";
const LLM_DEFAULT_MODEL = "deepseek-chat";

function readLlmSettings() {
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(LLM_SETTINGS_KEY) || "{}") || {};
  } catch {
    saved = {}; // 本地数据损坏时按未配置处理,不影响页面启动
  }
  return {
    apiKey: String(saved.apiKey || ""),
    baseUrl: String(saved.baseUrl || LLM_DEFAULT_BASE),
    model: String(saved.model || LLM_DEFAULT_MODEL),
  };
}

let llmSettings = readLlmSettings();

function llmHeaders() {
  // 空值不加头:服务端据此回落到 .env(自托管模式)
  const headers = {};
  if (llmSettings.apiKey) headers["X-LLM-Key"] = llmSettings.apiKey;
  if (llmSettings.baseUrl) headers["X-LLM-Base"] = llmSettings.baseUrl;
  if (llmSettings.model) headers["X-LLM-Model"] = llmSettings.model;
  return headers;
}

function updateSendState() {
  const btn = $("#btnSend");
  if (btn) btn.disabled = state.busy || state.uploading || !state.llmReady;
}

function setLlmReady(ready) {
  state.llmReady = !!ready;
  const banner = $("#llmBanner");
  if (banner) banner.classList.toggle("hidden", state.llmReady);
  setStatus(
    "#badgeLlm",
    "#dotLlm",
    state.llmReady,
    state.llmReady ? "LLM 已配置" : "LLM 未配置",
    "LLM 未配置"
  );
  renderLlmFooter();
  updateSendState();
}

function renderLlmFooter() {
  const node = $("#llmFooter");
  if (!node) return;
  const model = llmSettings.model || "—";
  if (llmSettings.apiKey) {
    // 只显示后 4 位,绝不把完整密钥渲染到页面
    node.textContent = `模型: ${model} · 本机配置 · …${llmSettings.apiKey.slice(-4)}`;
  } else {
    node.textContent = state.llmReady
      ? `模型: ${model} · 服务端配置`
      : "模型: 未配置";
  }
}

function openSettings() {
  $("#llmKey").value = llmSettings.apiKey;
  $("#llmBase").value = llmSettings.baseUrl;
  $("#llmModel").value = llmSettings.model;
  $("#llmKey").type = "password";
  $("#btnToggleKey").textContent = "显示";
  const box = $("#llmTestResult");
  box.classList.add("hidden");
  box.classList.remove("ok", "err");
  box.textContent = "";
  $("#settingsModal").classList.remove("hidden");
  $("#llmKey").focus();
}

function closeSettings() {
  $("#settingsModal").classList.add("hidden");
}

function toggleKeyVisibility() {
  const input = $("#llmKey");
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  $("#btnToggleKey").textContent = show ? "隐藏" : "显示";
}

async function testLlmConnection() {
  const baseUrl = $("#llmBase").value.trim();
  const model = $("#llmModel").value.trim();
  const apiKey = $("#llmKey").value.trim();
  const box = $("#llmTestResult");
  const btn = $("#btnTestLlm");
  box.classList.remove("hidden", "ok", "err");
  box.textContent = "测试中…";
  btn.disabled = true;
  try {
    const data = await apiJson("/api/llm/test", "POST", {
      base_url: baseUrl,
      model: model,
      api_key: apiKey,
    });
    box.classList.add(data.ok ? "ok" : "err");
    box.textContent = data.ok
      ? `连接成功 · ${data.latency_ms} ms`
      : `失败: ${data.error || "未知错误"}`;
  } catch (err) {
    box.classList.add("err");
    box.textContent = `失败: ${err.message || err}`;
  } finally {
    btn.disabled = false;
  }
}

function saveLlmSettings(event) {
  if (event) event.preventDefault();
  llmSettings = {
    apiKey: $("#llmKey").value.trim(),
    baseUrl: $("#llmBase").value.trim() || LLM_DEFAULT_BASE,
    model: $("#llmModel").value.trim() || LLM_DEFAULT_MODEL,
  };
  localStorage.setItem(LLM_SETTINGS_KEY, JSON.stringify(llmSettings));
  if (llmSettings.apiKey) {
    // 有本机 key 即为已配置;密钥是否有效由实际提问 / 测试连接暴露
    setLlmReady(true);
  } else {
    // 没填本机 key:回到服务端判定(.env 可能已配置)
    loadHealth();
  }
  renderLlmFooter();
  closeSettings();
  toast("模型设置已保存到本浏览器", "info");
}

function clearLlmSettings() {
  localStorage.removeItem(LLM_SETTINGS_KEY);
  llmSettings = { apiKey: "", baseUrl: LLM_DEFAULT_BASE, model: LLM_DEFAULT_MODEL };
  $("#llmKey").value = "";
  $("#llmBase").value = LLM_DEFAULT_BASE;
  $("#llmModel").value = LLM_DEFAULT_MODEL;
  const box = $("#llmTestResult");
  box.classList.add("hidden");
  box.classList.remove("ok", "err");
  box.textContent = "";
  // 交给服务端重新判定:自托管模式下 .env 有 key 时仍可继续使用
  loadHealth();
  renderLlmFooter();
  toast("已清除本机模型配置", "info");
}


// 右侧面板当前展示的 run_id;删除该 run 后据此清空面板
let currentRunId = "";

/* 渲染令牌:每次清空/切换会话自增。异步渲染(await 之后)先比对令牌,
   过期就直接丢弃,避免旧会话的卡片追加到新会话里。 */
let renderToken = 0;
function beginRender() { return ++renderToken; }
function isStale(token) { return token !== renderToken; }

const VIEWS = ["chat", "report", "charts", "code", "trace", "databases", "history"];
// 移动端工具条标题与 document.title 后缀共用这张表
const VIEW_LABELS = {
  chat: "对话", report: "报告", charts: "图表", code: "代码", trace: "Trace",
  databases: "数据库", history: "历史",
};
// 结果类视图:没有 currentRunId 时不允许切换,回落到对话
const RESULT_VIEWS = ["report", "charts", "code", "trace"];

/* ---------- 通用工具 ---------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderMarkdown(container, mdText) {
  // 先 marked 解析,再 DOMPurify 消毒,绝不直接 innerHTML 塞原文
  const text = mdText || "";
  if (!window.marked) {
    // 本地 marked 加载失败时退化为纯文本,否则 ### 和 * 会原样露出来
    container.textContent = text;
    return;
  }
  const html = marked.parse(text);
  container.innerHTML = window.DOMPurify ? DOMPurify.sanitize(html) : html;
}

async function apiGet(url) {
  const resp = await fetch(url, { headers: llmHeaders() });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `请求失败(${resp.status})`);
  }
  return resp.json();
}

async function apiJson(url, method = "GET", body = null) {
  // 数据库管理端点统一走 JSON;错误 detail 直接展示给用户
  const headers = llmHeaders();
  if (body) headers["Content-Type"] = "application/json";
  const resp = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `请求失败(${resp.status})`);
  return data;
}

/* SSE:POST + ReadableStream 手动解析,按 event 名分发 */
async function ssePost(url, body, handlers, signal) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...llmHeaders() },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail || `请求失败(${resp.status})`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE 帧以空行分隔;末段可能不完整,留在 buffer 里等下一块
    const frames = buffer.split("\n\n");
    buffer = frames.pop();
    for (const frame of frames) {
      let event = "message";
      const dataLines = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith(":")) continue; // keepalive 注释行
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;
      let data = {};
      try { data = JSON.parse(dataLines.join("\n")); } catch { continue; }
      const handler = handlers[event];
      if (handler) handler(data);
    }
  }
}

/* ---------- 健康状态(圆点 + 文本) ---------- */

async function loadHealth() {
  try {
    const h = await apiGet("/api/health");
    setStatus("#badgeDocker", "#dotDocker", h.docker, "Docker 可用", "Docker 不可用");
    const ckpt = $("#badgeCkpt");
    ckpt.textContent = `检查点 ${h.checkpointer}`;
    $("#dotCkpt").style.background = h.checkpointer === "sqlite" ? "#16a34a" : "#d97706";
    // llm_configured 已包含「请求头带 key」的情况:本机填过 key 即为 true
    setLlmReady(!!h.llm_configured);
  } catch {
    // 健康检查失败时按本机配置兜底,避免有 key 却被锁住发送
    setLlmReady(!!llmSettings.apiKey);
    setStatus("#badgeDocker", "#dotDocker", false, "Docker 未知", "Docker 未知");
  }
}

function setStatus(sel, dotSel, ok, okText, badText) {
  // 旧版胶囊徽章改为「圆点 + 文本」:文字仍走 badgeLlm/badgeDocker 原 id
  $(sel).textContent = ok ? okText : badText;
  $(dotSel).style.background = ok ? "#16a34a" : "#dc2626";
}

/* ---------- 视图状态机 ---------- */

function setView(name) {
  // 非法名字直接忽略;结果类视图在没有 run 时不允许进入
  if (!VIEWS.includes(name)) return;
  if (RESULT_VIEWS.includes(name) && !currentRunId) {
    name = "chat";
  }
  state.view = name;

  // 四个容器四选一:对话 / 结果 / 数据库 / 历史
  $("#chatView").classList.toggle("hidden", name !== "chat");
  const isResult = RESULT_VIEWS.includes(name);
  $("#resultView").classList.toggle("hidden", !isResult);
  // #resultView 用 flex-col 布局,hidden 移除后需要恢复 flex
  $("#resultView").classList.toggle("flex", isResult);
  $("#databaseView").classList.toggle("hidden", name !== "databases");
  $("#databaseView").classList.toggle("flex", name === "databases");
  $("#historyView").classList.toggle("hidden", name !== "history");
  $("#historyView").classList.toggle("flex", name === "history");
  for (const view of RESULT_VIEWS) {
    $(`#tab-${view}`).classList.toggle("hidden", view !== name);
  }

  // 同步导航高亮与 aria-current,供键盘与读屏使用
  document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
    const active = item.dataset.view === name;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });

  // 移动端标题与浏览器标签后缀
  $("#mobileTitle").textContent = VIEW_LABELS[name] || "对话";
  document.title = name === "chat" ? "数据分析 Agent" : `数据分析 Agent · ${VIEW_LABELS[name]}`;

  if (name === "trace") loadTrace();
  if (name === "databases") loadDatabases();
  if (name === "history") {
    loadSessions();
    loadRuns();
    loadDbQueries();
  }
}

function updateNavDisabled() {
  // 没有 currentRunId 时结果类导航禁用;禁用只影响点击,不影响键盘 Tab 聚焦
  document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
    const isResult = RESULT_VIEWS.includes(item.dataset.view);
    const disabled = isResult && !currentRunId;
    item.classList.toggle("disabled", disabled);
    if (disabled) item.setAttribute("aria-disabled", "true");
    else item.removeAttribute("aria-disabled");
  });
}

/* ---------- 数据附件(输入框 📎 + 拖拽) ---------- */

const ATTACH_SUFFIXES = [".csv", ".xlsx", ".xls", ".json"];
const ATTACH_MAX_BYTES = 20 * 1024 * 1024; // 与服务端 MAX_UPLOAD_BYTES 保持一致

function setAttachHint(text) {
  $("#attachHint").textContent = text || "";
  updateAttachBar();
}

function updateAttachBar() {
  const hint = $("#attachHint").textContent;
  const show = state.uploading || !!state.attachment || !!state.database || !!hint;
  $("#attachBar").classList.toggle("hidden", !show);
}

const DB_KIND_LABEL = { mysql: "MySQL", sqlite: "SQLite" };

function renderDbChip() {
  // 数据库 chip 与 CSV 附件 chip 互斥:选库时会先清空附件
  const chip = $("#dbChip");
  chip.innerHTML = "";
  chip.classList.remove("hidden");
  if (state.database) {
    const kind = DB_KIND_LABEL[state.database.kind] || state.database.kind;
    chip.append(el("span", "", "🗄"), el("span", "attach-chip-name", `${kind}：${state.database.label}`));
    const remove = el("button", "attach-chip-x", "×");
    remove.type = "button";
    remove.title = "取消选择数据库";
    remove.onclick = clearDatabase;
    chip.appendChild(remove);
  } else {
    chip.classList.add("hidden");
  }
  updateAttachBar();
}

function clearDatabase() {
  state.database = null;
  renderDbChip();
  $("#questionInput").focus();
}

function clearDatabaseSelection() {
  // 选附件时静默清掉数据库选择,不抢输入框焦点
  if (!state.database) return;
  state.database = null;
  renderDbChip();
}

function renderAttachChip() {
  const chip = $("#attachChip");
  chip.innerHTML = "";
  chip.classList.remove("hidden");
  if (state.uploading) {
    chip.append(el("span", "", "📄"), el("span", "attach-chip-name", "上传中…"));
  } else if (state.attachment) {
    chip.append(el("span", "", "📄"), el("span", "attach-chip-name", state.attachment.name));
    const remove = el("button", "attach-chip-x", "×");
    remove.type = "button";
    remove.title = "移除附件";
    remove.onclick = removeAttachment;
    chip.appendChild(remove);
  } else {
    chip.classList.add("hidden");
  }
  $("#btnAttach").disabled = state.uploading;
  updateSendState();
  updateAttachBar();
}

function removeAttachment() {
  state.attachment = null;
  setAttachHint("");
  renderAttachChip();
  $("#questionInput").focus();
}

async function loadDatasetOptions() {
  const menu = $("#attachMenu");
  const list = await apiGet("/api/datasets").catch(() => []);
  // 懒加载期间菜单可能已被关闭,不再重绘,避免闪回
  if (menu.classList.contains("hidden")) return;
  renderAttachMenu(list);
}

function openAttachMenu() {
  const menu = $("#attachMenu");
  if (!menu.classList.contains("hidden")) {
    closeAttachMenu();
    return;
  }
  menu.classList.remove("hidden");
  menu.innerHTML = "";
  menu.appendChild(el("div", "attach-menu-loading", "加载中…"));
  loadDatasetOptions();
}

function closeAttachMenu() {
  $("#attachMenu").classList.add("hidden");
}

function renderAttachMenu(list) {
  const menu = $("#attachMenu");
  menu.innerHTML = "";
  const uploadItem = el("button", "attach-menu-item", "💻 从电脑上传");
  uploadItem.type = "button";
  uploadItem.onclick = () => {
    closeAttachMenu();
    $("#fileInput").click();
  };
  menu.appendChild(uploadItem);
  menu.appendChild(el("div", "attach-menu-divider", ""));
  menu.appendChild(el("div", "attach-menu-caption", "已有数据集"));
  if (!list.length) {
    menu.appendChild(el("div", "attach-menu-empty", "暂无数据集"));
    return;
  }
  // 服务端已按 mtime 倒序,这里只取前 8 个,避免菜单过长
  for (const item of list.slice(0, 8)) {
    const btn = el("button", "attach-menu-item truncate", item.name);
    btn.type = "button";
    btn.title = item.name;
    btn.onclick = () => selectExisting(item.name);
    menu.appendChild(btn);
  }
}

function selectExisting(name) {
  state.attachment = { name };
  clearDatabaseSelection();
  setAttachHint("");
  renderAttachChip();
  closeAttachMenu();
  $("#questionInput").focus();
}

function validateAttach(file) {
  const name = String(file.name || "");
  const suffix = name.slice(name.lastIndexOf(".")).toLowerCase();
  if (!ATTACH_SUFFIXES.includes(suffix)) return "只支持 CSV / Excel / JSON 文件";
  if (file.size > ATTACH_MAX_BYTES) return "文件超过 20MB 上限";
  return "";
}

function handlePick(file) {
  if (!file) return;
  if (state.uploading) {
    setAttachHint("正在上传,请稍候…");
    return;
  }
  const error = validateAttach(file);
  if (error) {
    setAttachHint(error);
    return;
  }
  uploadFile(file);
}

async function uploadFile(file) {
  state.uploading = true;
  setAttachHint("");
  renderAttachChip();
  const fd = new FormData();
  fd.append("file", file);
  try {
    const resp = await fetch("/api/datasets", { method: "POST", body: fd });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      setAttachHint(`上传失败:${body.detail || resp.status}`);
      return;
    }
    state.attachment = { name: body.name };
    clearDatabaseSelection();
    // 多 sheet 的 Excel 会被拆成多个 CSV,明确告诉用户实际使用的是哪一个
    if (body.names && body.names.length > 1) {
      setAttachHint(`已拆分为 ${body.names.length} 个 CSV,使用 ${body.name}`);
    }
  } catch (err) {
    setAttachHint(`上传失败:${err.message || err}`);
  } finally {
    state.uploading = false;
    renderAttachChip();
  }
}

function handleDropFiles(files) {
  if (!files || !files.length) return;
  if (files.length > 1) toast("一次只能附一个文件", "error");
  handlePick(files[0]);
}

/* ---------- 数据库连接管理(侧栏 + 弹窗 + 浏览) ---------- */

async function loadDatabases() {
  let list = [];
  try {
    list = await apiJson("/api/databases");
  } catch {
    list = []; // 侧栏数据库列表失败不阻断对话
  }
  state.databases = list;
  // 选中的库可能已在别处被删除:清掉 chip,避免发送时 404
  if (state.database && !list.some((item) => item.id === state.database.id)) {
    state.database = null;
    renderDbChip();
  }
  renderDatabaseList();
}

function renderDatabaseList() {
  const ul = $("#databaseList");
  ul.innerHTML = "";
  const counter = $("#dbCount");
  if (counter) counter.textContent = String(state.databases.length);
  if (!state.databases.length) {
    ul.appendChild(el("li", "db-empty", "暂无连接"));
    return;
  }
  for (const record of state.databases) {
    // 选中态优先看正在浏览的连接,其次才是「用这个库提问」选中的连接
    const isActive = state.browseDb
      ? state.browseDb.id === record.id
      : (state.database && state.database.id === record.id);
    const li = el("li", "db-item" + (isActive ? " active" : ""));
    const status = record.last_status || "unknown";
    const dot = el("span", "db-dot " + status);
    dot.title = status === "ok" ? "最近测试成功" : status === "error" ? "最近测试失败" : "尚未测试";
    const kind = DB_KIND_LABEL[record.kind] || record.kind;
    const name = el("span", "db-name", record.label);
    name.title = `${kind} · ${record.host || record.path || record.label}`;
    const more = el("button", "db-more", "⋯");
    more.type = "button";
    more.title = "更多操作";
    more.onclick = (event) => {
      event.stopPropagation();
      toggleDbMenu(li, record);
    };
    li.append(dot, name, el("span", "db-kind", kind), more);
    li.onclick = () => openDbBrowser(record);
    ul.appendChild(li);
  }
}

// 当前展开的数据库操作菜单所属的列表项。菜单挂到 body 上做 fixed 定位,
// 否则会被侧栏列表的 overflow:auto 裁掉(只露出上半截)。
let dbMenuOwner = null;

function closeDbMenu() {
  document.querySelectorAll(".db-menu").forEach((node) => node.remove());
  dbMenuOwner = null;
}

function toggleDbMenu(item, record) {
  if (dbMenuOwner === item) {
    closeDbMenu();
    return;
  }
  closeDbMenu();
  const menu = el("div", "db-menu");
  const test = el("button", "", "测试连接");
  test.type = "button";
  test.onclick = (event) => {
    event.stopPropagation();
    closeDbMenu();
    testDatabase(record);
  };
  const edit = el("button", "", "编辑");
  edit.type = "button";
  edit.onclick = (event) => {
    event.stopPropagation();
    closeDbMenu();
    openDbModal(record);
  };
  const remove = el("button", "danger", "删除");
  remove.type = "button";
  remove.onclick = (event) => {
    event.stopPropagation();
    closeDbMenu();
    deleteDatabase(record);
  };
  menu.append(test, edit, remove);

  // 挂在 body 末尾,用视口坐标定位到「⋯」按钮下方;right 与按钮右缘对齐
  document.body.appendChild(menu);
  const rect = item.getBoundingClientRect();
  menu.style.top = `${Math.round(rect.bottom + 4)}px`;
  menu.style.right = `${Math.max(8, Math.round(window.innerWidth - rect.right))}px`;
  dbMenuOwner = item;
}

async function testDatabase(record) {
  toast(`正在测试 ${record.label}…`);
  try {
    const result = await apiJson("/api/databases/test", "POST", { id: record.id });
    toast(`连接成功 · ${result.version} · ${result.elapsed_ms}ms`, "success");
  } catch (err) {
    toast(`连接失败:${err.message}`, "error");
  } finally {
    loadDatabases(); // 刷新状态圆点
  }
}

async function deleteDatabase(record) {
  const ok = await showConfirm({
    title: "删除数据库连接",
    body: `确定删除「${record.label}」？服务端保存的密码也会一并删除。`,
    confirmText: "删除",
    danger: true,
  });
  if (!ok) return;
  try {
    await apiJson(`/api/databases/${encodeURIComponent(record.id)}`, "DELETE");
    toast("已删除", "success");
    if (state.database && state.database.id === record.id) {
      state.database = null;
      renderDbChip();
    }
    // 正在浏览的连接被删掉:右半边回落占位态,避免展示已失效的表结构
    if (state.browseDb && state.browseDb.id === record.id) closeDbDetail();
    await loadDatabases();
  } catch (err) {
    toast(`删除失败:${err.message}`, "error");
  }
}

function setDbTest(text, kind) {
  const node = $("#dbTestResult");
  node.textContent = text || "";
  node.className = "db-test-result" + (text ? "" : " hidden")
    + (kind === "error" ? " error" : kind === "pending" ? " pending" : "");
}

function openDbModal(record = null) {
  state.editingDb = record || null;
  $("#dbModalTitle").textContent = record ? "编辑 MySQL 数据库" : "添加 MySQL 数据库";
  $("#dbLabel").value = record ? record.label : "";
  $("#dbHost").value = record ? record.host || "" : "";
  $("#dbPort").value = record ? record.port || 3306 : 3306;
  $("#dbName").value = record ? record.database || "" : "";
  $("#dbUser").value = record ? record.user || "" : "";
  $("#dbPassword").value = "";
  $("#dbPassword").placeholder = record ? "留空表示不修改已保存密码" : "只存服务端,不会下发浏览器";
  $("#dbSsl").checked = !!(record && record.ssl);
  setDbTest("", "ok");
  $("#dbModal").classList.remove("hidden");
  setTimeout(() => $("#dbLabel").focus(), 0);
}

function closeDbModal() {
  $("#dbModal").classList.add("hidden");
  state.editingDb = null;
}

function dbFormPayload() {
  return {
    label: $("#dbLabel").value.trim(),
    kind: "mysql",
    host: $("#dbHost").value.trim(),
    port: Number($("#dbPort").value) || 3306,
    database: $("#dbName").value.trim(),
    user: $("#dbUser").value.trim(),
    password: $("#dbPassword").value,
    ssl: $("#dbSsl").checked,
  };
}

async function testDbForm() {
  const payload = dbFormPayload();
  if (!payload.label || !payload.host || !payload.database || !payload.user) {
    setDbTest("请先填写名称、主机、库名与用户名", "error");
    return;
  }
  setDbTest("正在连接…", "pending");
  if (state.editingDb) payload.id = state.editingDb.id; // 密码留空时后端沿用已保存密码
  try {
    const result = await apiJson("/api/databases/test", "POST", payload);
    setDbTest(`连接成功 · ${result.version} · ${result.elapsed_ms}ms`, "ok");
  } catch (err) {
    setDbTest(`连接失败:${err.message}`, "error");
  }
}

async function saveDbForm(event) {
  event.preventDefault();
  const payload = dbFormPayload();
  try {
    if (state.editingDb) {
      await apiJson(`/api/databases/${encodeURIComponent(state.editingDb.id)}`, "PUT", payload);
      toast("已保存", "success");
    } else {
      const record = await apiJson("/api/databases", "POST", payload);
      toast(`已添加 ${record.label}`, "success");
    }
    closeDbModal();
    await loadDatabases();
  } catch (err) {
    setDbTest(`保存失败:${err.message}`, "error");
  }
}

async function openDbBrowser(record) {
  // 切换连接前先保存上一张表的说明,避免已输入内容丢失
  await saveDbSemantics();
  state.browseDb = record;
  // 旧连接的说明快照已保存,清空后由 loadDbColumns 重新建立
  state.dbSemanticsTable = "";
  state.dbSemanticsSaved = null;
  const detail = $("#dbDetail");
  detail.classList.remove("db-placeholder");
  $("#dbBrowserTitle").textContent = `${DB_KIND_LABEL[record.kind] || record.kind}：${record.label}`;
  $("#btnDbBrowserClose").classList.remove("hidden");
  $("#btnDbUse").classList.remove("hidden");
  const list = $("#dbTableList");
  list.innerHTML = "";
  list.appendChild(el("li", "db-table-empty", "加载中…"));
  $("#dbColumns").textContent = "选择左侧的表或视图查看字段";
  renderDatabaseList();            // 让当前连接在左栏高亮
  try {
    const data = await apiJson(`/api/databases/${encodeURIComponent(record.id)}/tables`);
    renderDbTables(data.tables || []);
  } catch (err) {
    list.innerHTML = "";
    list.appendChild(el("li", "db-table-empty", `加载失败:${err.message}`));
  }
}

function closeDbDetail() {
  // 收起右侧面板,回到虚线占位态;左栏取消高亮
  state.browseDb = null;
  state.dbSemanticsTable = "";
  state.dbSemanticsSaved = null;
  $("#dbDetail").classList.add("db-placeholder");
  $("#dbBrowserTitle").textContent = "选择左侧的连接查看表结构";
  $("#dbTableList").innerHTML = "";
  $("#dbColumns").textContent = "从左侧选择一张表或视图";
  $("#btnDbBrowserClose").classList.add("hidden");
  $("#btnDbUse").classList.add("hidden");
  renderDatabaseList();
}

function renderDbTables(tables) {
  const ul = $("#dbTableList");
  ul.innerHTML = "";
  if (!tables.length) {
    ul.appendChild(el("li", "db-table-empty", "没有表或视图"));
    return;
  }
  for (const table of tables) {
    const li = el("li", "db-table-item");
    const type = table.type === "VIEW" ? "VIEW" : "TABLE";
    li.append(
      el("span", "db-table-name", table.name),
      el("span", "db-table-type" + (type === "VIEW" ? " view" : ""), type)
    );
    li.title = table.name;
    li.onclick = () => {
      ul.querySelectorAll(".db-table-item").forEach((node) => node.classList.remove("active"));
      li.classList.add("active");
      loadDbColumns(table.name);
    };
    ul.appendChild(li);
  }
  ul.firstElementChild.click(); // 默认展开第一张表
}

async function loadDbColumns(name) {
  const record = state.browseDb;
  if (!record) return;
  // 切换表之前先保存上一张表的说明
  await saveDbSemantics();
  if (!state.browseDb || state.browseDb.id !== record.id) return;
  $("#dbColumns").textContent = "加载中…";
  try {
    const [data, semantics] = await Promise.all([
      apiJson(`/api/databases/${encodeURIComponent(record.id)}/tables/${encodeURIComponent(name)}`),
      apiJson(`/api/databases/${encodeURIComponent(record.id)}/semantics`).catch(() => ({ tables: {} })),
    ]);
    const saved = (semantics.tables || {})[name] || {};
    renderDbColumns(data.table, saved);
  } catch (err) {
    $("#dbColumns").textContent = `加载失败:${err.message}`;
  }
}

function renderDbColumns(table, semantics = {}) {
  const box = $("#dbColumns");
  box.innerHTML = "";
  const head = el("div", "db-column-head");
  const type = table.type === "VIEW" ? "VIEW" : "TABLE";
  head.append(el("strong", "", table.name), el("span", "db-table-type" + (type === "VIEW" ? " view" : ""), type));
  box.appendChild(head);

  // 表说明:人工维护,保存后注入 SQL 生成提示词
  const descWrap = el("div", "db-table-desc");
  descWrap.appendChild(el("label", "db-desc-label", "表说明"));
  const tableDesc = document.createElement("input");
  tableDesc.type = "text";
  tableDesc.id = "dbTableDesc";
  tableDesc.className = "db-desc-input";
  tableDesc.maxLength = 200;
  tableDesc.placeholder = "补充这张表的业务含义(可留空)";
  tableDesc.value = semantics.description || "";
  descWrap.appendChild(tableDesc);
  box.appendChild(descWrap);

  state.dbSemanticsTable = table.name;
  state.dbSemanticsSaved = {
    table: table.name,
    description: tableDesc.value,
    columns: { ...(semantics.columns || {}) },
  };
  tableDesc.addEventListener("blur", () => saveDbSemantics());
  tableDesc.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); tableDesc.blur(); }
  });

  const tbl = el("table", "db-col-table");
  const thead = el("thead");
  const headRow = el("tr");
  for (const title of ["字段", "类型", "可空", "主键", "说明"]) headRow.appendChild(el("th", "", title));
  thead.appendChild(headRow);
  tbl.appendChild(thead);

  const tbody = el("tbody");
  for (const column of table.columns || []) {
    const row = el("tr");
    const nameCell = el("td", "", column.name);
    if (column.key === "PRI") nameCell.appendChild(el("span", "db-col-pk", "PK"));
    const nullable = column.nullable === true ? "YES" : column.nullable === false ? "NO" : "—";
    row.append(nameCell, el("td", "", column.type), el("td", "", nullable), el("td", "", column.key || "—"));

    const descCell = el("td");
    const colDesc = document.createElement("input");
    colDesc.type = "text";
    colDesc.className = "db-col-desc";
    colDesc.maxLength = 100;
    colDesc.dataset.col = column.name;
    colDesc.placeholder = "业务含义";
    colDesc.value = (semantics.columns || {})[column.name] || "";
    colDesc.addEventListener("blur", () => saveDbSemantics());
    colDesc.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); colDesc.blur(); }
    });
    descCell.appendChild(colDesc);
    row.appendChild(descCell);
    tbody.appendChild(row);
  }
  tbl.appendChild(tbody);
  box.appendChild(tbl);
}

async function saveDbSemantics() {
  // 把当前字段面板的表说明 / 列说明写回服务端;失败回滚到最近一次成功保存的值
  const record = state.browseDb;
  const snapshot = state.dbSemanticsSaved;
  if (!record || !snapshot || !state.dbSemanticsTable) return;
  const descInput = $("#dbTableDesc");
  const description = descInput ? descInput.value.trim() : "";
  const columns = {};
  let changed = description !== (snapshot.description || "");
  document.querySelectorAll("#dbColumns input.db-col-desc").forEach((input) => {
    const value = input.value.trim();
    if (value) columns[input.dataset.col] = value;
    if (value !== ((snapshot.columns || {})[input.dataset.col] || "")) changed = true;
  });
  if (!changed) return;
  try {
    await apiJson(
      `/api/databases/${encodeURIComponent(record.id)}/semantics/${encodeURIComponent(snapshot.table)}`,
      "PUT",
      { description, columns }
    );
    // 期间已切表 / 切连接:不要用旧表结果覆盖新的面板状态
    if (state.dbSemanticsSaved !== snapshot) return;
    state.dbSemanticsSaved = { table: snapshot.table, description, columns };
  } catch (err) {
    if (state.dbSemanticsSaved !== snapshot) return;
    if (descInput) descInput.value = snapshot.description || "";
    document.querySelectorAll("#dbColumns input.db-col-desc").forEach((input) => {
      input.value = (snapshot.columns || {})[input.dataset.col] || "";
    });
    toast(`说明保存失败:${err.message}`, "error");
  }
}

function useDatabase() {
  const record = state.browseDb;
  if (!record) return;
  if (state.attachment) removeAttachment(); // chip 互斥:选库即清空 CSV 附件
  state.database = record;
  renderDbChip();
  setView("chat");
  $("#questionInput").focus();
  toast(`已选择 ${record.label}`, "success");
}

function bindDbEvents() {
  $("#btnAddDatabase").onclick = () => openDbModal();
  $("#btnDbClose").onclick = closeDbModal;
  $("#dbForm").addEventListener("submit", saveDbForm);
  $("#btnDbTest").onclick = testDbForm;
  $("#dbModal").addEventListener("click", (event) => {
    if (event.target === $("#dbModal")) closeDbModal();
  });
  $("#btnDbBrowserClose").onclick = closeDbDetail;
  $("#btnDbUse").onclick = useDatabase;
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".db-menu") && !event.target.closest(".db-more")) closeDbMenu();
  });
  // 菜单用视口坐标定位:窗口缩放或任意滚动后位置会错位,直接收起比跟随更简单可靠
  window.addEventListener("resize", closeDbMenu);
  window.addEventListener("scroll", closeDbMenu, true);
}

/* ---------- 会话列表与恢复 ---------- */

async function loadSessions() {
  const list = await apiGet("/api/sessions").catch(() => []);
  $("#sessionCount").textContent = String(list.length);
  const hc = $("#historyCount");
  if (hc) hc.textContent = String(list.length);
  const ul = $("#sessionList");
  ul.innerHTML = "";
  for (const s of list) {
    const li = el("li", "session-item group" + (s.thread_id === state.threadId ? " active" : ""));
    const title = el("div", "truncate", s.title_display || s.title || "(未命名会话)");
    // 兜底标题(乱码):悬浮提示给出完整 thread_id,方便定位坏数据
    if (s.title_corrupted) {
      title.title = `thread_id: ${s.thread_id}`;
    }
    const meta = el("div", "text-xs text-slate-400", (s.updated_at || "").replace("T", " "));
    li.append(title, meta);
    // 删除按钮:默认隐藏,hover / focus 时出现;点击不触发 selectSession
    const delBtn = el("button", "session-del", "");
    delBtn.type = "button";
    delBtn.setAttribute("aria-label", "删除会话");
    delBtn.title = "删除会话";
    delBtn.innerHTML = "🗑";
    delBtn.onclick = (e) => { e.stopPropagation(); confirmDeleteSession(s); };
    li.appendChild(delBtn);
    // 点击立即高亮/置 threadId;渲染期间 aria-busy 防重复点击导致重复渲染
    li.onclick = async () => {
      if (li.getAttribute("aria-busy") === "true") return;
      document.querySelectorAll("#sessionList > li").forEach((node) => {
        node.classList.remove("active");
        node.removeAttribute("aria-busy");
      });
      li.classList.add("active");
      li.setAttribute("aria-busy", "true");
      state.threadId = s.thread_id;
      try {
        await selectSession(s.thread_id);
        setView("chat");
      } finally {
        li.removeAttribute("aria-busy");
      }
    };
    ul.appendChild(li);
  }
}

async function confirmDeleteSession(session) {
  const body = el("div", "space-y-2 text-sm text-slate-600");
  body.append(
    el("p", "", `将永久删除该会话的对话记录、待审批计划与执行轨迹,不可恢复。`),
  );
  const runCount = countRunsForThread(session.thread_id);
  if (runCount > 0) {
    body.append(
      el("p", "text-red-600", `该会话的 ${runCount} 份运行记录(报告、图表、代码)会一并删除。`),
    );
  }
  const ok = await showConfirm({
    title: "删除会话？",
    body,
    confirmText: "永久删除",
    danger: true,
  });
  if (!ok) return;
  try {
    // 会话与运行记录绑定:后端会连带删掉该 thread 的全部 runs,前端不再提供开关
    const resp = await fetch(`/api/sessions/${session.thread_id}`, { method: "DELETE" });
    if (resp.status === 404) {
      // 已不存在:当作已删除处理
      removeSessionFromList(session.thread_id);
      await loadRuns();
      return;
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      const msg = resp.status === 409 ? "正在执行,请稍后再删" : (err.detail || `删除失败(${resp.status})`);
      toast(msg);
      return;
    }
    // 先记下该会话的 run ids:删完再查会拿不到,无法判断当前结果页是否已失效
    const removedRunIds = (window._runCache || [])
      .filter((r) => r.thread_id === session.thread_id)
      .map((r) => r.run_id);
    removeSessionFromList(session.thread_id);
    await loadRuns();
    if (currentRunId && removedRunIds.includes(currentRunId)) clearResultPanels();
    toast(runCount > 0 ? `已删除会话及其 ${runCount} 份运行记录` : "已删除会话");
  } catch (err) {
    toast(String(err.message || err));
  }
}

function countRunsForThread(threadId) {
  // 从已渲染的历史运行项里数该 thread 的 run 数量(li 标题里带 run_id,但没 thread_id)
  // 更准确:调用 /api/runs 过滤——这里简单起见用全局缓存
  return (window._runCache || []).filter((r) => r.thread_id === threadId).length;
}

function removeSessionFromList(threadId) {
  // 重新拉列表最干净
  loadSessions();
  if (state.threadId === threadId) {
    state.threadId = "";
    localStorage.removeItem("da_thread_id");
    clearChat();
    // 清空结果面板
    ["tab-report", "tab-charts", "tab-code", "tab-trace"].forEach((id) => {
      const elNode = document.getElementById(id);
      if (elNode) elNode.innerHTML = "";
    });
    currentRunId = "";
    updateNavDisabled();
    setView("chat");
  }
}

async function selectSession(threadId) {
  state.threadId = threadId;
  localStorage.setItem("da_thread_id", threadId);
  clearChat(); // 自增令牌:此前所有在途渲染作废
  const token = renderToken; // 取清空后的新令牌
  loadSessions();
  // 切会话后回到对话视图,避免残留上一个会话的结果页
  setView("chat");
  let snap;
  try {
    snap = await apiGet(`/api/sessions/${threadId}`);
  } catch {
    return; // 会话不存在(可能已被清理):保持空白对话
  }
  // await 期间用户可能又切走了:过期就丢弃,绝不追加到新会话
  if (isStale(token)) return;
  if (snap.question) addUserBubble(snap.question);
  // 审批中:恢复审批卡(刷新页面后也能继续审批;reasons/columns 由快照补全)
  if (snap.status === "pending" && snap.pending && snap.pending.plan) {
    addPlanCard({
      thread_id: threadId,
      question: snap.pending.question || snap.question,
      dataset_name: snap.pending.dataset_name || "",
      plan: snap.pending.plan || [],
      reason: snap.pending.reason || "",
      reasons: snap.pending.reasons || [],
      columns: snap.pending.columns || [],
    });
    return;
  }
  if (snap.error_analysis) {
    addErrorCard(snap.error_analysis);
    return;
  }
  if (snap.final_answer) {
    await addResultCard({
      run_id: snap.run_id,
      status: snap.next_action === "clarify" ? "clarify"
        : snap.next_action === "deliver" ? "deliver" : "success",
      deliver_run_id: snap.deliver_run_id || "",
      final_answer: snap.final_answer,
      approved_by: "none",
      warnings: snap.warnings || [],
    }, token);
  }
}

function newSession() {
  state.threadId = crypto.randomUUID().replace(/-/g, "");
  localStorage.setItem("da_thread_id", state.threadId);
  clearChat();
  loadSessions();
  setView("chat");
  $("#questionInput").focus();
}

/* ---------- 对话流渲染 ---------- */

function clearChat() {
  // 任何清空都必须让在途渲染作废,否则旧会话的异步结果会追加回来
  renderToken += 1;
  $("#chatFlow").innerHTML = "";
  $("#chatEmpty")?.remove();
}

function scrollChat() {
  const flow = $("#chatFlow");
  flow.scrollTop = flow.scrollHeight;
}

function addUserBubble(text, datasetName, dbLabel) {
  $("#chatEmpty")?.remove();
  const bubble = el("div", "msg-user");
  bubble.appendChild(document.createTextNode(text));
  // 附件/数据库标签放在问题下方,让回看会话时知道这轮用了哪个数据源
  if (datasetName) bubble.appendChild(el("div", "msg-user-attach", `📎 ${datasetName}`));
  if (dbLabel) bubble.appendChild(el("div", "msg-user-attach db", `🗄 ${dbLabel}`));
  $("#chatFlow").appendChild(bubble);
  scrollChat();
}

const NODE_LABELS = { planner: "意图识别", review_plan: "计划审批", code_analyst: "代码执行", summarize: "汇总" };

function addProgressRow() {
  const row = el("div", "flex gap-2 flex-wrap items-center");
  for (const node of ["planner", "review_plan", "code_analyst", "summarize"]) {
    const chip = el("span", "node-chip", NODE_LABELS[node] || node);
    chip.dataset.node = node;
    row.appendChild(chip);
  }
  $("#chatFlow").appendChild(row);
  scrollChat();
  return row;
}

function updateProgress(row, data) {
  if (!row) return;
  const chip = row.querySelector(`[data-node="${data.node}"]`);
  if (!chip) return;
  if (data.phase === "start") chip.classList.add("active");
  else { chip.classList.remove("active"); chip.classList.add("done"); }
  scrollChat();
}

function planFirstStepSummary(plan) {
  // 折叠态标题里的首步摘要:超过 24 字截断,保证一行放得下
  const first = String((plan || [])[0] || "").trim();
  return first.length > 24 ? first.slice(0, 24) + "…" : first;
}

function extractPlanColumns(text) {
  // 从计划文本里抽出被引号/书名号包住的候选列名,用于与数据集 schema 比对
  const re = /[「『"'`]([^「』"'`]{1,30})[」』"'`]/g;
  const out = new Set();
  let m;
  while ((m = re.exec(text)) !== null) {
    const token = m[1].trim();
    if (token) out.add(token);
  }
  return [...out];
}

function renderPlanColumnCheck(container, planText, columns) {
  // 计划自检:计划里出现的列名若不在数据集 schema 里,标黄提示(不阻断执行)
  container.innerHTML = "";
  if (!columns || !columns.length) return;
  const known = new Set(columns);
  const unknown = extractPlanColumns(planText).filter((c) => !known.has(c));
  if (!unknown.length) return;
  container.appendChild(el("div", "text-xs text-amber-600 mt-1",
    `⚠ 计划中提到的列在数据集里不存在: ${unknown.join("、")}(不阻断执行,请确认)`));
}

function addPlanCard(data) {
  const plan = data.plan || [];
  const card = el("div", "card plan-card");

  // 标题行(默认折叠;点击展开计划全文与编辑框)
  const head = el("div", "plan-head flex items-center gap-2");
  const toggle = el("span", "text-slate-400 text-xs", "▸");
  const title = el("h3", "mb-0 flex-1",
    `分析计划 · ${data.dataset_name || "未指定数据集"} · 共 ${plan.length} 步 · ${planFirstStepSummary(plan)}`);
  head.append(toggle, title);
  card.appendChild(head);

  // 策略理由:让人知道这一轮为什么被问
  const reasons = data.reasons || [];
  if (reasons.length) {
    card.appendChild(el("p", "text-xs text-amber-600 mt-1", `需要你确认:${reasons.join(";")}`));
  }

  // 展开区:计划全文(可编辑) + 列名自检
  const detail = el("div", "hidden mt-2");
  const ta = el("textarea", "w-full border border-slate-300 rounded-lg px-2 py-1 text-sm mb-1");
  ta.rows = Math.max(plan.length, 2);
  ta.value = plan.join("\n"); // 一行一步,可编辑
  detail.appendChild(ta);
  const checkBox = el("div");
  detail.appendChild(checkBox);
  if (data.reason) detail.appendChild(el("p", "text-xs text-slate-500 mt-1", `依据:${data.reason}`));
  card.appendChild(detail);

  let expanded = false;
  const setExpanded = (v) => {
    expanded = v;
    detail.classList.toggle("hidden", !expanded);
    toggle.textContent = expanded ? "▾" : "▸";
    if (expanded) renderPlanColumnCheck(checkBox, ta.value, data.columns || []);
  };
  head.onclick = () => setExpanded(!expanded);
  ta.addEventListener("input", () => renderPlanColumnCheck(checkBox, ta.value, data.columns || []));

  // 信任复选框:勾选后,本轮批准的同时把该数据集加入会话信任列表
  const trustLabel = el("label", "flex items-center gap-1 text-xs text-slate-500 mt-2");
  const trustBox = document.createElement("input");
  trustBox.type = "checkbox";
  trustLabel.append(trustBox, document.createTextNode("本会话信任该数据集,不再询问"));
  card.appendChild(trustLabel);

  // 按钮:运行(主) / 编辑 / 取消
  const btns = el("div", "flex gap-2 mt-2");
  const btnRun = el("button", "btn-primary text-xs", "运行 (Enter)");
  const btnEdit = el("button", "btn-ghost", "编辑");
  const btnReject = el("button", "btn-ghost", "取消 (Esc)");
  btns.append(btnRun, btnEdit, btnReject);
  card.appendChild(btns);
  $("#chatFlow").appendChild(card);
  scrollChat();

  let submitted = false;
  const currentPlan = () => ta.value.split("\n").map((s) => s.trim()).filter(Boolean);
  const submit = (action, editedPlan) => {
    if (submitted) return;
    submitted = true;
    btnRun.disabled = btnEdit.disabled = btnReject.disabled = true;
    trustBox.disabled = true;
    runApproval(data.thread_id, action, editedPlan, trustBox.checked);
  };
  // 「运行」时若文本被改动过,自动按「编辑后批准」提交
  const submitRun = () => {
    const edited = currentPlan();
    const original = plan.map((s) => String(s).trim()).filter(Boolean);
    const changed = JSON.stringify(edited) !== JSON.stringify(original);
    submit(changed ? "edit" : "approve", changed ? edited : plan);
  };
  btnRun.onclick = submitRun;
  btnEdit.onclick = () => { setExpanded(true); ta.focus(); };
  btnReject.onclick = () => submit("reject", []);
  // Enter = 运行,Esc = 取消(textarea 内 Enter 换行,Ctrl+Enter 运行)
  card.tabIndex = 0;
  card.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); submit("reject", []); return; }
    if (e.key !== "Enter") return;
    if (e.target === ta) {
      if (e.ctrlKey || e.metaKey) { e.preventDefault(); submitRun(); }
      return;
    }
    e.preventDefault();
    submitRun();
  });
  card.focus();
}

function buildSummaryMarkdown(summary, result) {
  // 把结构化 summary 拼成带标题/列表的 Markdown;为空时回退到 final_answer
  const lines = [];
  if (summary.headline) lines.push("### 结论", "", summary.headline, "");
  if (summary.findings && summary.findings.length) {
    lines.push("### 关键发现", "");
    for (const item of summary.findings) lines.push(`- ${item}`);
    lines.push("");
  }
  if (summary.recommendations && summary.recommendations.length) {
    lines.push("### 建议", "");
    for (const item of summary.recommendations) lines.push(`- ${item}`);
    lines.push("");
  }
  if (summary.caveats && summary.caveats.length) {
    lines.push("### 局限", "");
    for (const item of summary.caveats) lines.push(`- ${item}`);
    lines.push("");
  }
  if (!lines.length) return (result && result.final_answer) || "";
  return lines.join("\n");
}

async function typewriterMarkdown(node, mdText, token = renderToken) {
  // 前端逐字渲染:每帧追加约 1/80 文本,首屏几乎立刻有内容
  const text = mdText || "";
  if (!text) return true;
  const step = Math.max(1, Math.ceil(text.length / 80));
  for (let i = 0; i < text.length; i += step) {
    // 打字机跨 await 持续写入:每帧前都确认会话没被切走
    if (isStale(token)) return false;
    renderMarkdown(node, text.slice(0, i + step));
    await new Promise((resolve) => setTimeout(resolve, 16));
    if (isStale(token)) return false;
    scrollChat();
  }
  if (isStale(token)) return false;
  renderMarkdown(node, text);
  return true;
}

function buildFollowups(run) {
  // 优先使用 LLM 在汇总时一起生成的推荐追问(不额外调 LLM);
  // 不足 3 条时用列结构模板补足
  const result = (run && run.result) || {};
  const summary = result.summary || {};
  const questions = (summary.followups || [])
    .map((q) => String(q || "").trim())
    .filter(Boolean);
  if (questions.length >= 3) return questions.slice(0, 3);

  const columns = (result.schema && result.schema.columns) || [];
  const numeric = columns
    .filter((c) => /int|float/.test(String(c.dtype || "")))
    .map((c) => c.name);
  const categorical = columns
    .filter((c) => {
      const dtype = String(c.dtype || "");
      const n = Number(c.n_unique || 0);
      return !/int|float/.test(dtype) && n > 1 && n <= 20;
    })
    .map((c) => c.name);
  const findings = summary.findings || [];

  // 模板候选补足:与 LLM 推荐去重,凑满 3 条为止
  const push = (q) => {
    if (questions.length < 3 && !questions.includes(q)) questions.push(q);
  };
  if (categorical.length) push(`按「${categorical[0]}」拆分，看看各组差异？`);
  if (numeric.length) push(`「${numeric[0]}」的分布有没有异常值？`);
  if (findings.length) push("这些差异在统计上显著吗？");

  const fallback = ["换个维度再对比看看？", "能给我一份结论摘要吗？", "这些结论有哪些局限？"];
  for (const item of fallback) {
    if (questions.length >= 3) break;
    if (!questions.includes(item)) questions.push(item);
  }
  return questions.slice(0, 3);
}

function renderFollowups(run) {
  // 推荐追问放在本轮结果下方;点击即填入输入框并发送
  document.querySelectorAll(".followup-row").forEach((n) => n.remove());
  const questions = buildFollowups(run);
  if (!questions.length) return;
  const row = el("div", "followup-row");
  for (const question of questions) {
    const chip = el("button", "followup-chip", question);
    chip.type = "button";
    chip.onclick = () => {
      $("#questionInput").value = question;
      sendQuestion();
    };
    row.appendChild(chip);
  }
  $("#chatFlow").appendChild(row);
  scrollChat();
}

function addAutoPassRow(answer) {
  // 策略自动通过的轮次不弹审批卡:结果卡上方一行说明 + 「查看计划」折叠区
  const wrap = el("div", "auto-pass");
  const line = el("div", "flex items-center gap-2 text-xs text-slate-500");
  line.appendChild(el("span", "",
    `已自动通过 · ${(answer.reasons || []).join(";") || "风险低"}`));
  const btn = el("button", "btn-ghost text-xs", "查看计划");
  line.appendChild(btn);
  wrap.appendChild(line);
  const detail = el("div", "hidden mt-1");
  const ol = el("ol", "list-decimal list-inside text-xs text-slate-500");
  for (const step of answer.plan || []) ol.appendChild(el("li", "", step));
  detail.appendChild(ol);
  wrap.appendChild(detail);
  btn.onclick = () => {
    const hidden = detail.classList.toggle("hidden");
    btn.textContent = hidden ? "查看计划" : "收起计划";
  };
  $("#chatFlow").appendChild(wrap);
  scrollChat();
}

function buildSqlBlock(sqlText, rowCount, failed, cached = false) {
  // 可折叠 + 可复制:SQL 可能很长,默认收起,结果卡只露出摘要行
  const details = el("details", "sql-details");
  const summary = el("summary", "sql-summary");
  summary.appendChild(el("span", "", failed ? "执行的 SQL · 失败" : `执行的 SQL · 返回 ${rowCount || 0} 行`));
  if (cached) summary.appendChild(el("span", "badge badge-gray sql-cache-badge", "缓存 · 10 分钟内有效"));
  const copy = el("button", "sql-copy", "复制");
  copy.type = "button";
  copy.onclick = (event) => {
    event.preventDefault();
    event.stopPropagation();
    copyText(sqlText);
  };
  summary.appendChild(copy);
  details.appendChild(summary);
  details.appendChild(el("pre", "sql-pre", sqlText || "(未生成 SQL)"));
  return details;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text || "");
    toast("已复制", "success");
  } catch {
    toast("复制失败,请手动选择文本", "error");
  }
}

function addDbPendingCard(data) {
  // db_start:查询开始,先给出可折叠 SQL 与「正在查询数据库…」状态
  const card = el("div", "card sql-card db-pending");
  const header = el("div", "flex items-center gap-2 mb-1");
  const badge = el("span", "badge badge-gray", "查询中");
  header.append(el("h3", "mb-0", "SQL 查询"), badge);
  const wait = el("p", "text-xs text-slate-500", "正在查询数据库… 已等待 0s");
  const block = buildSqlBlock(data.sql || "", 0, false);
  const actions = el("div", "flex items-center gap-2 mt-2");
  const cancelBtn = el("button", "btn-ghost text-xs", "取消");
  cancelBtn.type = "button";
  actions.appendChild(cancelBtn);
  card.append(header, wait, block, actions);
  cancelBtn.onclick = () => {
    if (!state.abortController) return;
    cancelBtn.disabled = true;
    cancelBtn.textContent = "取消中…";
    // 中断 SSE:服务端感知连接关闭后置位 cancel_event,数据库侧同步停止
    state.abortController.abort();
    markDbCancelled();
  };
  $("#chatFlow").appendChild(card);
  scrollChat();
  state.dbPending = {
    card, header, badge, wait, block, actions, elapsed: 0,
  };
  // 结果卡后续复用同一块 SQL,避免展示两遍
  state.lastSqlCard = card;
  state.lastSqlBlock = block;
  state.lastSqlText = data.sql || "";
}

function updateDbProgress(elapsed) {
  const pending = state.dbPending;
  if (!pending) return;
  pending.elapsed = Math.max(pending.elapsed, Number(elapsed) || 0);
  pending.wait.textContent = `正在查询数据库… 已等待 ${pending.elapsed}s`;
}

function markDbCancelled() {
  const pending = state.dbPending;
  if (!pending) return;
  state.dbPending = null;
  pending.badge.className = "badge badge-amber";
  pending.badge.textContent = "已取消";
  pending.wait.textContent = `已取消(已等待 ${pending.elapsed}s)`;
  pending.wait.className = "text-xs text-amber-600";
  if (pending.actions) pending.actions.remove();
  // 取消后保留折叠块供排查,但不再让结果卡复用它
  state.lastSqlCard = null;
  state.lastSqlBlock = null;
  state.lastSqlText = "";
}

function finishDbCard(data) {
  // sql 事件到达:复用查询中卡片,更新状态与真实行数,不再新开一张
  const pending = state.dbPending;
  if (!pending) return false;
  state.dbPending = null;
  const card = pending.card;
  card.classList.remove("db-pending");
  if (!data.ok) card.classList.add("border-red-200");
  pending.badge.className = "badge " + (data.ok ? "badge-green" : "badge-red");
  pending.badge.textContent = data.ok ? "已执行" : "失败";
  if (data.db_label) {
    pending.header.appendChild(el("span", "badge badge-gray", data.db_label));
  }
  const waited = pending.elapsed ? `已等待 ${pending.elapsed}s` : "";
  if (data.ok) {
    pending.wait.textContent = "查询完成" + (waited ? `(${waited})` : "");
  } else {
    pending.wait.textContent = "查询失败" + (waited ? `(${waited})` : "");
    pending.wait.className = "text-xs text-amber-600";
    if (data.error) {
      const err = el("p", "sql-error", data.error);
      card.insertBefore(err, pending.block);
      // 服务端超时文案已带同样建议时不再重复提示
      if (!String(data.error).includes("可加 WHERE 过滤")) {
        card.insertBefore(
          el("p", "text-xs text-amber-600", "可加 WHERE 过滤或改用采样"),
          err
        );
      }
    }
  }
  const block = buildSqlBlock(data.sql || "", data.row_count || 0, !data.ok, !!data.cached);
  pending.block.replaceWith(block);
  if (pending.actions) pending.actions.remove();
  state.lastSqlCard = card;
  state.lastSqlBlock = block;
  state.lastSqlText = data.sql || "";
  scrollChat();
  return true;
}

function addSqlCard(data) {
  // 若已有「正在查询」卡片,就地完成它,保留已等待秒数
  if (finishDbCard(data)) return;
  // sql 事件先于 status 到达:立即展示真正执行的 SQL 与行数
  const card = el("div", "card sql-card" + (data.ok ? "" : " border-red-200"));
  const header = el("div", "flex items-center gap-2 mb-1");
  header.append(
    el("h3", "mb-0", "SQL 查询"),
    el("span", "badge " + (data.ok ? "badge-green" : "badge-red"), data.ok ? "已执行" : "失败")
  );
  if (data.db_label) header.appendChild(el("span", "badge badge-gray", data.db_label));
  card.appendChild(header);
  if (!data.ok && data.error) card.appendChild(el("p", "sql-error", data.error));
  const block = buildSqlBlock(data.sql || "", data.row_count || 0, !data.ok, !!data.cached);
  card.appendChild(block);
  $("#chatFlow").appendChild(card);
  scrollChat();
  state.lastSqlCard = card;
  state.lastSqlBlock = block;
  state.lastSqlText = data.sql || "";
}

function reuseSqlBlock(card, sql, rowCount, failed, cached = false) {
  // 结果卡复用 sql 事件留下的折叠块,避免同一 SQL 展示两遍
  if (state.lastSqlBlock && state.lastSqlText === sql) {
    card.appendChild(state.lastSqlBlock);
    if (state.lastSqlCard) state.lastSqlCard.remove();
    state.lastSqlCard = null;
    state.lastSqlBlock = null;
    return;
  }
  card.appendChild(buildSqlBlock(sql, rowCount, failed, cached));
}

async function addResultCard(answer, token = renderToken) {
  // 入口就过期的调用直接丢弃,连「自动通过」行也不追加
  if (isStale(token)) return;
  let currentRun = null;
  const approvedBy = answer.approved_by || "none";
  if (approvedBy === "policy") addAutoPassRow(answer);
  const card = el("div", "card");
  const header = el("div", "flex items-center gap-2 mb-1");
  const warnings = answer.warnings || [];
  const badge = el("span",
    "badge " + (warnings.length ? "badge-amber" : "badge-green"),
    warnings.length ? "警告" : "成功");
  // 审批来源徽章:人工批准 / 策略自动通过 / 未审批(discover 类)
  const approvalBadge = el("span",
    "badge " + (approvedBy === "policy" ? "badge-green" : "badge-gray"),
    { human: "人工批准", policy: "策略自动通过", none: "未审批" }[approvedBy] || "未审批");
  header.append(el("h3", "mb-0", "分析结果"), badge, approvalBadge);
  card.appendChild(header);

  // 数据库问答:结果卡保留实际执行的 SQL(失败由 addErrorCard 处理)
  if (answer.sql) reuseSqlBlock(card, answer.sql, answer.sql_row_count || 0, false, !!answer.cached);

  if (answer.status === "clarify") {
    // clarify 轮次没有 run_id:回答本身就是结论(要用户补什么),不能渲染成「未完成」
    badge.className = "badge badge-amber";
    badge.textContent = "待补充";
    approvalBadge.textContent = "未执行";
    const body = el("div", "md-body");
    card.appendChild(body);
    // 打字机跨会话继续写也会被令牌拦住
    if (!(await typewriterMarkdown(body, answer.final_answer || "", token))) return;
  } else if (answer.status === "deliver") {
    // 交付轮次:复用上一轮归档(report.html / result.csv),本轮没有新 run_id,
    // 不能落进下面的「未完成」分支
    badge.className = "badge badge-green";
    badge.textContent = "已复用";
    approvalBadge.textContent = "未重跑";
    const body = el("div", "md-body");
    card.appendChild(body);
    if (!(await typewriterMarkdown(body, answer.final_answer || "", token))) return;
    if (answer.deliver_run_id) {
      const actions = el("div", "flex items-center gap-2 mt-2");
      const openBtn = el("button", "btn-ghost text-xs", "打开归档");
      openBtn.type = "button";
      openBtn.onclick = () => loadRunPanel(answer.deliver_run_id);
      actions.appendChild(openBtn);
      for (const [label, path] of [["下载 HTML", "report.html"], ["下载 CSV", "result.csv"]]) {
        const link = el("a", "btn-ghost text-xs", label);
        link.href = `/api/runs/${answer.deliver_run_id}/${path}`;
        link.setAttribute("download", "");
        actions.appendChild(link);
      }
      card.appendChild(actions);
    }
  } else if (!answer.run_id) {
    // 没有 run_id = 这一轮没有归档(中断/超时),不能渲染成"成功"的空卡片
    badge.className = "badge badge-amber";
    badge.textContent = "未完成";
    card.appendChild(el("p", "text-xs text-slate-500",
      "本轮未完成（可能中断或超时），请重新提问；历史运行可在左侧查看。"));
  } else {
    try {
      const run = await apiGet(`/api/runs/${answer.run_id}`);
      // 拉取归档期间用户可能切走:过期就丢弃,不要落到错误会话
      if (isStale(token)) return;
      currentRun = run;
      const summary = (run.result && run.result.summary) || {};
      const md = buildSummaryMarkdown(summary, run.result);
      const body = el("div", "md-body");
      card.appendChild(body);
      if (!(await typewriterMarkdown(body, md, token))) return;

      // 图表直接放进聊天卡片:点击放大,带下载按钮
      const charts = (run.result && run.result.charts) || [];
      if (charts.length) {
        const box = el("div", "chat-charts");
        for (const path of charts) {
          const name = String(path).split(/[\\/]/).pop();
          const url = `/api/runs/${answer.run_id}/charts/${encodeURIComponent(name)}`;
          const item = el("div", "chat-chart");
          const img = el("img", "chat-chart-img");
          img.src = url;
          img.alt = name;
          img.onclick = () => {
            $("#imgModalImg").src = url;
            $("#imgModal").classList.remove("hidden");
          };
          const actions = el("div", "chat-chart-actions");
          const link = document.createElement("a");
          link.href = url;
          link.download = name;
          link.className = "btn-ghost text-xs";
          link.textContent = "下载图片";
          actions.appendChild(link);
          item.append(img, actions);
          box.appendChild(item);
        }
        card.appendChild(box);
      }

      const btn = el("button", "btn-ghost text-xs mt-1", "查看完整报告");
      btn.onclick = () => { loadRunPanel(answer.run_id); };
      card.appendChild(btn);
    } catch {
      card.appendChild(el("p", "text-xs text-slate-500", "运行归档读取失败"));
    }
  }
  for (const w of warnings) card.appendChild(el("p", "text-xs text-amber-600 mt-1", `⚠ ${w}`));
  // 最终落盘前再确认一次,防止 await 期间已切会话
  if (isStale(token)) return;
  $("#chatFlow").appendChild(card);
  scrollChat();
  if (currentRun) renderFollowups(currentRun);
}

function appendList(card, title, items) {
  if (!items || !items.length) return;
  card.appendChild(el("div", "text-xs font-semibold text-slate-500 mt-2", title));
  const ul = el("ul", "list-disc list-inside text-sm");
  for (const item of items) ul.appendChild(el("li", "", item));
  card.appendChild(ul);
}

function addErrorCard(errorAnalysis, data = {}) {
  const card = el("div", "card border-red-200");
  const header = el("div", "flex items-center gap-2 mb-1");
  header.append(el("h3", "mb-0", "分析失败"), el("span", "badge badge-red", "失败"));
  if (data.db_label) header.appendChild(el("span", "badge badge-gray", data.db_label));
  card.appendChild(header);
  // 失败也要展示真正执行的 SQL,方便用户定位是生成错还是数据错
  if (data.sql) reuseSqlBlock(card, data.sql, 0, true);
  card.appendChild(el("pre", "text-xs whitespace-pre-wrap text-slate-600", errorAnalysis || "未知错误"));
  $("#chatFlow").appendChild(card);
  scrollChat();
}

/* ---------- 执行流(提问 / 审批共用 SSE 处理) ---------- */

function addClarifyCard(data, token = renderToken) {
  // 术语澄清:本轮尚未生成 / 执行 SQL,确认或跳过后才会重跑
  const terms = Array.isArray(data.terms) ? data.terms : [];
  if (!terms.length) return;
  const card = el("div", "card clarify-card");
  const header = el("div", "flex items-center gap-2 mb-1");
  header.append(el("h3", "mb-0", "术语确认"), el("span", "badge badge-amber", "待确认"));
  card.appendChild(header);
  card.appendChild(
    el("p", "text-xs text-slate-500", "以下业务词无法从表结构确定口径,确认后才会生成并执行 SQL。")
  );

  const rows = [];
  for (const term of terms) {
    const block = el("div", "clarify-term");
    const title = el("div", "clarify-term-title");
    title.appendChild(el("strong", "", term.term));
    if (term.reason) title.appendChild(el("span", "clarify-reason", term.reason));
    block.appendChild(title);

    const controls = el("div", "clarify-controls");
    const select = document.createElement("select");
    select.className = "clarify-select";
    const candidates = (term.candidates || []).slice(0, 3);
    if (candidates.length) {
      for (const candidate of candidates) {
        const option = document.createElement("option");
        option.value = candidate;
        option.textContent = candidate;
        select.appendChild(option);
      }
    } else {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "无推荐口径";
      select.appendChild(option);
    }
    const custom = document.createElement("input");
    custom.type = "text";
    custom.className = "clarify-input";
    custom.placeholder = "或自定义口径,如 orders.支付金额";
    const rememberLabel = el("label", "clarify-remember");
    const remember = document.createElement("input");
    remember.type = "checkbox";
    remember.checked = true;
    rememberLabel.append(remember, document.createTextNode(" 记住(下次不再询问)"));
    controls.append(select, custom, rememberLabel);
    block.appendChild(controls);
    rows.push({ term: term.term, select, custom, remember });
    card.appendChild(block);
  }

  const actions = el("div", "clarify-actions");
  const okBtn = el("button", "btn-primary text-sm", "确认并继续");
  const skipBtn = el("button", "btn-ghost text-sm", "跳过,按推荐继续");
  okBtn.type = "button";
  skipBtn.type = "button";
  actions.append(okBtn, skipBtn);
  card.appendChild(actions);
  $("#chatFlow").appendChild(card);
  scrollChat();

  const setBusy = (busy) => {
    okBtn.disabled = busy;
    skipBtn.disabled = busy;
    okBtn.textContent = busy ? "处理中…" : "确认并继续";
  };

  const submit = async (body) => {
    const threadId = data.thread_id || state.threadId;
    if (!threadId) return;
    setBusy(true);
    state.busy = true;
    updateSendState();
    const row = addProgressRow();
    try {
      await ssePost(
        `/api/sessions/${encodeURIComponent(threadId)}/clarify`,
        body,
        makeHandlers(row, token)
      );
    } catch (err) {
      if (!isStale(token)) addErrorCard(String(err.message || err));
      state.busy = false;
      updateSendState();
      setBusy(false);
    }
  };

  okBtn.onclick = () => {
    const answers = rows
      .map(({ term, select, custom, remember }) => {
        const definition = (custom.value || select.value || "").trim();
        return { term, definition, sql_hint: definition, remember: remember.checked };
      })
      .filter((item) => item.definition);
    if (!answers.length) {
      toast("请先填写或选择术语口径", "error");
      return;
    }
    submit({ answers, skip: false });
  };
  skipBtn.onclick = () => submit({ answers: [], skip: true });
}

function makeHandlers(progressRow, token = renderToken) {
  return {
    status: (d) => {
      if (isStale(token)) return;
      updateProgress(progressRow, d);
    },
    queued: () => {
      if (isStale(token)) return;
      addSystemHint("已有分析在执行,当前请求排队中…");
    },
    plan: (d) => {
      if (isStale(token)) return;
      addPlanCard(d);
    },
    plan_auto: (d) => {
      if (isStale(token)) return;
      // 策略自动通过:不弹卡,把「计划审批」chip 标记为「自动通过」
      const chip = progressRow && progressRow.querySelector('[data-node="review_plan"]');
      if (chip) {
        chip.textContent = "自动通过";
        chip.classList.add("auto");
      }
    },
    sql: (d) => {
      if (isStale(token)) return;
      addSqlCard(d);
    },
    db_start: (d) => {
      if (isStale(token)) return;
      addDbPendingCard(d);
    },
    db_progress: (d) => {
      if (isStale(token)) return;
      updateDbProgress(d.elapsed);
    },
    clarify: (d) => {
      if (isStale(token)) return;
      addClarifyCard(d, token);
    },
    answer: (d) => {
      if (isStale(token)) return;
      if (d.status === "error") addErrorCard(d.error_analysis, d);
      else addResultCard(d, token);
    },
    done: () => {
      // done 必须无条件处理:即使已切走会话,也要解除发送锁定
      state.busy = false;
      updateSendState();
      loadSessions();
      loadRuns();
    },
  };
}

function addSystemHint(text) {
  const hint = el("div", "text-xs text-slate-400 text-center", text);
  $("#chatFlow").appendChild(hint);
  scrollChat();
}

/* ---------- 顶部提示条(轻量 toast) ---------- */

function toast(text, type = "info") {
  const node = el("div", "toast toast-" + type, text);
  document.body.appendChild(node);
  // 触发重排后加 show 做淡入
  requestAnimationFrame(() => node.classList.add("show"));
  setTimeout(() => {
    node.classList.remove("show");
    setTimeout(() => node.remove(), 200);
  }, 2400);
}

/* ---------- 通用确认弹窗 ---------- */

function showConfirm({ title, body, confirmText = "确定", danger = false }) {
  // 复用同一个 modal 节点(index.html 里的 #confirmModal)
  const modal = document.getElementById("confirmModal");
  const titleEl = document.getElementById("confirmTitle");
  const bodyEl = document.getElementById("confirmBody");
  const btnOk = document.getElementById("confirmOk");
  const btnCancel = document.getElementById("confirmCancel");
  titleEl.textContent = title || "确认";
  bodyEl.innerHTML = "";
  if (typeof body === "string") bodyEl.textContent = body;
  else if (body) bodyEl.appendChild(body);
  btnOk.textContent = confirmText;
  btnOk.className = "btn " + (danger ? "btn-danger" : "btn-primary");
  btnOk.disabled = false;
  modal.classList.remove("hidden");

  return new Promise((resolve) => {
    let settled = false;
    const cleanup = () => {
      if (settled) return;
      settled = true;
      modal.classList.add("hidden");
      btnOk.onclick = null;
      btnCancel.onclick = null;
      document.removeEventListener("keydown", onKey);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); cleanup(); resolve(false); }
      else if (e.key === "Enter") { e.preventDefault(); doOk(); }
    };
    const doOk = () => {
      btnOk.disabled = true;
      btnOk.textContent = "处理中…";
      // 给一个微小延时让用户看到「处理中」状态
      setTimeout(() => { cleanup(); resolve(true); }, 120);
    };
    btnOk.onclick = doOk;
    btnCancel.onclick = () => { cleanup(); resolve(false); };
    document.addEventListener("keydown", onKey);
  });
}

async function sendQuestion() {
  const input = $("#questionInput");
  const question = input.value.trim();
  if (state.busy || state.uploading) return;
  if (!state.llmReady) {
    // 按钮虽然禁用了,Enter 仍可能触发,这里兜底并引导去设置
    toast("请先在 ⚙️ 模型设置里填入 API Key", "error");
    openSettings();
    return;
  }
  // 附件不能代替问题:先写问题,附件才会随请求提交
  if (!question) {
    setAttachHint("请先输入问题");
    input.focus();
    return;
  }
  const attachment = state.attachment;
  const database = state.database;
  // 必须在 newSession() 之后取令牌:它内部会 clearChat() 自增令牌,
  // 提前取会把自己刚发起的这一轮判成过期
  if (!state.threadId) newSession();
  const token = renderToken;
  state.busy = true;
  updateSendState();
  input.value = "";
  setAttachHint("");
  // 新一轮重置 SQL 复用状态,避免把上一轮的 SQL 折叠块搬进本轮结果卡
  state.lastSqlCard = null;
  state.lastSqlBlock = null;
  state.lastSqlText = "";
  state.dbPending = null;
  // 取消按钮只中断本轮 SSE;请求结束后在 finally 里清空
  const controller = new AbortController();
  state.abortController = controller;
  const dbLabel = database
    ? `${DB_KIND_LABEL[database.kind] || database.kind}：${database.label}`
    : "";
  addUserBubble(question, attachment ? attachment.name : "", dbLabel);
  // 新一轮提问时清掉上一轮的推荐追问
  document.querySelectorAll(".followup-row").forEach((n) => n.remove());
  const row = addProgressRow();
  const handlers = makeHandlers(row, token);
  const answerHandler = handlers.answer;
  let failed = false;
  handlers.answer = (d) => {
    // 用户中途切走会话:不再往新会话里追加本轮结果
    if (isStale(token)) return;
    if (d.status === "error") failed = true;
    answerHandler(d);
  };
  const body = { question };
  if (attachment) body.dataset_name = attachment.name;
  if (database) body.database_id = database.id; // 浏览器只传连接 id,不传 SQL
  try {
    await ssePost(
      `/api/sessions/${state.threadId}/messages`,
      body,
      handlers,
      controller.signal
    );
    // 请求成功且本轮没有报错才清空附件;失败保留 chip,方便直接重试
    if (!failed) removeAttachment();
  } catch (err) {
    if (err && err.name === "AbortError") {
      // 用户主动取消:卡片已标记「已取消」,不再追加失败卡
      if (state.dbPending) markDbCancelled();
      state.busy = false;
      updateSendState();
      return;
    }
    // 切走会话后本轮报错也不该出现在别的会话里,只在仍属于本会话时提示
    if (!isStale(token)) addErrorCard(String(err.message || err));
    state.busy = false;
    updateSendState();
  } finally {
    state.abortController = null;
    state.dbPending = null;
  }
}

async function runApproval(threadId, action, plan, trust) {
  // 审批请求同样绑定发起时所在会话的令牌,切走后回调不得再追加
  const token = renderToken;
  const row = addProgressRow();
  state.busy = true;
  try {
    await ssePost(`/api/sessions/${threadId}/approval`,
      { action, plan, trust: !!trust }, makeHandlers(row, token));
  } catch (err) {
    if (!isStale(token)) addErrorCard(String(err.message || err));
    state.busy = false;
    updateSendState();
  }
}

/* ---------- 结果页:报告 / 图表 / 代码 / Trace ---------- */

async function loadRunPanel(runId) {
  currentRunId = runId;
  updateNavDisabled();
  // 切换 run 时先重置下载按钮,避免残留上一次的可点状态
  const btnHtml = $("#btnDownloadHtml");
  const btnCsv = $("#btnDownloadCsv");
  btnHtml.removeAttribute("href");
  btnHtml.classList.add("disabled");
  btnCsv.removeAttribute("href");
  btnCsv.classList.add("disabled");
  btnCsv.title = "";
  const run = await apiGet(`/api/runs/${runId}`).catch(() => null);
  if (!run) return;
  // 结果页顶部摘要:数据集 · 问题 · run_id,给用户定位当前看的是哪次运行
  const question = (run.result && run.result.question) || run.question || "(无)";
  const dataset = (run.result && (run.result.dataset || run.result.dataset_name)) || "";
  $("#resultMeta").textContent = `${dataset || "未指定数据集"} · ${question} · ${runId}`;
  // 下载链接:HTML 报告一定生成;CSV 只有 result_csv=true 时才可点
  btnHtml.href = `/api/runs/${runId}/report.html`;
  btnHtml.classList.remove("disabled");
  if (run.result && run.result.result_csv) {
    btnCsv.href = `/api/runs/${runId}/result.csv`;
    btnCsv.classList.remove("disabled");
    btnCsv.title = "";
  } else {
    btnCsv.title = "本次结果不是表格";
  }
  // 报告
  renderMarkdown($("#tab-report"), run.report_md || "(暂无报告)");
  // 图表:路径统一取文件名,走 /api 接口,绝不直接引用本地路径
  const chartsBox = $("#tab-charts");
  chartsBox.innerHTML = "";
  const charts = (run.result && run.result.charts) || [];
  if (!charts.length) chartsBox.appendChild(el("div", "empty-hint", "本次运行没有图表"));
  for (const p of charts) {
    const name = String(p).split(/[\\/]/).pop();
    const img = el("img", "chart-thumb");
    img.src = `/api/runs/${runId}/charts/${encodeURIComponent(name)}`;
    img.alt = name;
    img.onclick = () => { $("#imgModalImg").src = img.src; $("#imgModal").classList.remove("hidden"); };
    chartsBox.appendChild(img);
  }
  // 代码
  $("#codeContent").textContent = (run.result && run.result.code) || "(暂无代码)";
  setView("report");
}

async function loadTrace() {
  const box = $("#tab-trace");
  box.innerHTML = "";
  if (!state.threadId) { box.appendChild(el("div", "empty-hint", "先发起一次分析")); return; }
  const trace = await apiGet(`/api/sessions/${state.threadId}/trace`).catch(() => null);
  if (!trace) { box.appendChild(el("div", "empty-hint", "该会话暂无 trace")); return; }
  const nodes = (trace.steps || []).filter((s) => s.type === "node");
  const maxMs = Math.max(...nodes.map((s) => s.duration_ms || 0), 1);
  for (const step of nodes) {
    const row = el("div", "trace-bar-row");
    row.appendChild(el("span", "trace-bar-name", NODE_LABELS[step.name] || step.name));
    const bar = el("div", "trace-bar");
    bar.style.width = `${Math.max((step.duration_ms / maxMs) * 100, 2)}%`;
    row.appendChild(bar);
    row.appendChild(el("span", "trace-bar-ms", `${step.duration_ms}ms`));
    box.appendChild(row);
  }
  box.appendChild(el("div", "text-xs text-slate-500 mt-2",
    `总耗时 ${trace.total_ms}ms · token 用量 ${trace.total_tokens ?? "—"}`));
}

/* ---------- 历史运行 ---------- */

// 数据库结果 CSV 的命名形态:命中时用库名代替原始文件名展示
const DB_DATASET_RE = /^db_.+_\d{8}_\d{6}(?:_\d{3})?\.csv$/i;

function dbRunLabel(run) {
  if (!DB_DATASET_RE.test(run.dataset || "")) return "";
  const label = run.db_label || "数据库";
  // 后端能从审计里补出表名时优先显示「库 · 表」,否则退回「库: 行数」
  if (run.db_table) return `${label} · ${run.db_table}`;
  return run.db_row_count == null ? label : `${label}: ${run.db_row_count}`;
}

async function loadRuns() {
  const list = await apiGet("/api/runs?limit=50").catch(() => []);
  window._runCache = list;
  $("#runCount").textContent = String(list.length);
  const ul = $("#runList");
  ul.innerHTML = "";
  for (const run of list) {
    const li = el("li", "run-item");
    li.dataset.runId = run.run_id;
    const ok = run.status === "success";
    li.append(
      el("div", "truncate", `${ok ? "✅" : "❌"} ${run.question || run.run_id}`),
      el("div", "text-slate-400", `${(run.ts || "").slice(5, 16)} · ${run.duration_ms ?? "—"}ms`),
    );
    // 数据库来源的运行:显示「库名: 行数」,hover 保留原始文件名便于排查
    const dbText = dbRunLabel(run);
    if (dbText) {
      const tag = el("div", "text-slate-400", dbText);
      tag.title = run.dataset || "";
      li.appendChild(tag);
    }
    // 删除按钮:复用会话删除样式,默认隐藏,hover 出现;点击不触发 li 打开详情
    const delBtn = el("button", "session-del", "");
    delBtn.type = "button";
    delBtn.setAttribute("aria-label", "删除运行记录");
    delBtn.title = "删除运行记录";
    delBtn.innerHTML = "🗑";
    delBtn.onclick = (e) => { e.stopPropagation(); confirmDeleteRun(run); };
    li.appendChild(delBtn);
    li.onclick = () => { loadRunPanel(run.run_id); };
    ul.appendChild(li);
  }
  $("#btnClearRuns").disabled = list.length === 0;
}

async function confirmDeleteRun(run) {
  const body = el("div", "space-y-2 text-sm text-slate-600");
  body.append(
    el("p", "", `问题：${run.question || "(无)"}`),
    el("p", "", `运行 ID：${run.run_id}　时间：${(run.ts || "").slice(5, 16)}`),
    el("p", "text-red-600", "将删除该次运行归档的报告、代码、图表与索引记录，不可恢复。"),
  );
  const ok = await showConfirm({
    title: "删除这条运行记录？",
    body,
    confirmText: "永久删除",
    danger: true,
  });
  if (!ok) return;

  try {
    const resp = await fetch(`/api/runs/${encodeURIComponent(run.run_id)}`, { method: "DELETE" });
    if (resp.status === 404) {
      removeRunFromList(run.run_id);
      toast("记录已不存在");
      return;
    }
    if (resp.status === 409) {
      toast("该运行所属会话正在执行，请稍后再删");
      return;
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      toast(err.detail || `删除失败(${resp.status})`);
      return;
    }
    removeRunFromList(run.run_id);
    // 当前视图若指向已删除的 run:清空四个面板并回落到对话
    if (currentRunId === run.run_id) clearResultPanels();
    await loadSessions(); // 会话的 last_run_id 可能已被清空
    toast("已删除运行记录");
  } catch (err) {
    toast(String(err.message || err));
  }
}

function removeRunFromList(runId) {
  // 只移除这一条,不整表重绘,保证滚动位置不跳
  for (const li of $("#runList").children) {
    if (li.dataset.runId === runId) {
      li.remove();
      break;
    }
  }
  const left = $("#runList").children.length;
  $("#runCount").textContent = String(left);
  $("#btnClearRuns").disabled = left === 0;
}

// 清空结果页四个面板并回到对话:单条删除、批量清空、会话删除共用
function clearResultPanels() {
  $("#tab-report").innerHTML = "";
  $("#tab-charts").innerHTML = "";
  $("#codeContent").textContent = "";
  $("#tab-trace").innerHTML = "";
  currentRunId = "";
  updateNavDisabled();
  setView("chat");
}

async function confirmClearRuns() {
  const count = (window._runCache || []).length;
  if (!count) {
    toast("还没有运行记录");
    return;
  }
  const body = el("div", "space-y-2 text-sm text-slate-600");
  body.append(
    el("p", "", `将永久删除全部 ${count} 条运行记录及其报告、图表、代码归档。`),
    el("p", "text-red-600", "会话与对话记录不受影响,但会话里指向这些运行的引用会被清空。"),
  );
  const ok = await showConfirm({
    title: "清空全部运行记录？",
    body,
    confirmText: "全部删除",
    danger: true,
  });
  if (!ok) return;
  try {
    const resp = await fetch("/api/runs", { method: "DELETE" });
    if (resp.status === 409) {
      toast("有会话正在执行,请稍后再清空");
      return;
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      toast(err.detail || `清空失败(${resp.status})`);
      return;
    }
    const data = await resp.json().catch(() => ({}));
    clearResultPanels();
    window._runCache = [];
    $("#runList").innerHTML = "";
    $("#runCount").textContent = "0";
    $("#btnClearRuns").disabled = true;
    await loadSessions(); // last_run_id 已被后端清空
    const removed = (data.deleted && data.deleted.runs) ?? count;
    toast(`已清空 ${removed} 条运行记录`);
  } catch (err) {
    toast(String(err.message || err));
  }
}

/* ---------- 数据库查询历史 ---------- */

const DB_QUERY_STATUS = {
  ok: ["成功", "ok"],
  failed: ["失败", "failed"],
  timeout: ["超时", "timeout"],
  clarify: ["待澄清", "clarify"],
};

async function loadDbQueries() {
  const list = await apiGet("/api/db/queries?limit=50").catch(() => []);
  $("#dbQueryCount").textContent = String(list.length);
  const ul = $("#dbQueryList");
  ul.innerHTML = "";
  if (!list.length) {
    ul.appendChild(
      el("li", "empty-hint text-xs",
        "还没有数据库查询记录；在输入框选一个库提问后会出现在这里"));
    return;
  }
  for (const query of list) {
    const meta = DB_QUERY_STATUS[query.status] || DB_QUERY_STATUS.failed;
    const li = el("li", "run-item db-query-item");
    const head = el("div", "flex items-center gap-2");
    head.append(
      el("span", `dbq-badge ${meta[1]}`, meta[0]),
      el("span", "truncate dbq-question", query.question || "(无问题)"),
    );
    const sub = el("div", "dbq-meta",
      `${(query.ts || "").slice(5, 16)} · ${query.db_label || "数据库"} · ` +
      `${query.row_count ?? 0} 行 · ${query.elapsed_ms ?? 0}ms`);
    li.append(head, sub);
    li.onclick = () => showDbQueryDetail(query, li);
    ul.appendChild(li);
  }
}

async function showDbQueryDetail(query, rowNode) {
  const box = $("#dbQueryDetail");
  const grid = $("#historyGrid");
  box.innerHTML = "";
  box.classList.remove("hidden");
  grid.classList.add("has-detail");
  for (const node of $("#dbQueryList").children) node.classList.remove("active");
  if (rowNode) rowNode.classList.add("active");

  const meta = DB_QUERY_STATUS[query.status] || DB_QUERY_STATUS.failed;
  const head = el("div", "dbq-detail-head");
  head.append(
    el("span", `dbq-badge ${meta[1]}`, meta[0]),
    el("span", "dbq-detail-title", query.db_label || "数据库"),
    el("span", "dbq-meta", query.ts || ""),
  );
  const close = el("button", "sidebar-clear", "关闭");
  close.type = "button";
  close.onclick = () => {
    box.classList.add("hidden");
    grid.classList.remove("has-detail");
    for (const node of $("#dbQueryList").children) node.classList.remove("active");
  };
  head.appendChild(close);
  box.appendChild(head);
  box.appendChild(el("div", "dbq-question", query.question || "(无问题)"));

  // 失败 / 超时:先展示错误原因,并且不再提供「打开运行」
  if (query.error) {
    box.appendChild(el("div", "dbq-section-title", "错误原因"));
    box.appendChild(el("div", "dbq-error", query.error));
  }

  box.appendChild(el("div", "dbq-section-title", "SQL"));
  box.appendChild(el("pre", "dbq-sql", query.sql || "(未生成 SQL)"));
  if (query.sql) {
    const copy = el("button", "sidebar-clear", "复制 SQL");
    copy.type = "button";
    copy.onclick = async () => {
      try {
        await navigator.clipboard.writeText(query.sql);
        toast("SQL 已复制");
      } catch (err) {
        toast(String(err.message || err));
      }
    };
    const actions = el("div", "dbq-actions");
    actions.appendChild(copy);
    box.appendChild(actions);
  }

  // 结果预览:优先运行归档 source.csv,其次数据目录同名 CSV
  box.appendChild(el("div", "dbq-section-title", "结果预览（前 20 行）"));
  const params = new URLSearchParams({ limit: "20" });
  if (query.run_id) params.set("run_id", query.run_id);
  if (query.csv_name) params.set("csv_name", query.csv_name);
  const preview = await apiGet(`/api/db/queries/preview?${params.toString()}`).catch(() => null);
  if (!preview || !preview.available) {
    box.appendChild(el("div", "empty-hint text-xs", "原始结果已不可用（数据文件可能已被清理）"));
  } else {
    const table = el("table", "dbq-table");
    const thead = el("thead");
    const headRow = el("tr");
    for (const column of preview.columns) headRow.appendChild(el("th", "", column));
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el("tbody");
    for (const row of preview.rows) {
      const tr = el("tr");
      for (const column of preview.columns) {
        const value = row[column];
        tr.appendChild(el("td", "", value === null || value === undefined ? "" : String(value)));
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    const wrap = el("div", "");
    wrap.style.overflow = "auto";
    wrap.style.maxHeight = "260px";
    wrap.appendChild(table);
    box.appendChild(wrap);
  }

  if (query.run_id && meta[1] === "ok") {
    const open = el("button", "btn-primary text-sm", "打开运行");
    open.type = "button";
    open.onclick = () => loadRunPanel(query.run_id);
    const actions = el("div", "dbq-actions");
    actions.appendChild(open);
    box.appendChild(actions);
  }
}

/* ---------- 移动端侧栏 ---------- */

function openSidebar() {
  $("#sidebar").classList.add("open");
  $("#sidebarBackdrop").classList.add("show");
}

function closeSidebar() {
  $("#sidebar").classList.remove("open");
  $("#sidebarBackdrop").classList.remove("show");
}

/* ---------- 事件绑定与初始化 ---------- */

function bindEvents() {
  $("#btnSend").onclick = sendQuestion;
  $("#questionInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendQuestion(); }
  });
  $("#questionInput").addEventListener("input", () => {
    // 用户开始写问题后,「请先输入问题」这类提示就没必要继续占位了
    if ($("#attachHint").textContent === "请先输入问题") setAttachHint("");
  });
  $("#btnNewSession").onclick = newSession;

  // 📎 菜单:上传新文件 / 选择已有数据集
  $("#btnAttach").onclick = (e) => {
    e.stopPropagation();
    if (state.uploading) return;
    openAttachMenu();
  };
  // 菜单内的点击不触发「点外部关闭」;选项自己负责关闭
  $("#attachMenu").addEventListener("click", (e) => e.stopPropagation());
  $("#fileInput").addEventListener("change", (e) => {
    if (e.target.files[0]) handlePick(e.target.files[0]);
    e.target.value = "";
  });
  document.addEventListener("click", (e) => {
    if ($("#attachMenu").classList.contains("hidden")) return;
    if (e.target.closest("#attachMenu") || e.target.closest("#btnAttach")) return;
    closeAttachMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeAttachMenu();
      closeSidebar();
      closeDbMenu();
      closeDbModal();
      closeDbDetail();
      closeSettings();
    }
  });

  // 模型设置(BYOK)
  $("#btnSettings").onclick = openSettings;
  $("#llmBanner").onclick = openSettings;
  $("#btnCloseSettings").onclick = closeSettings;
  $("#btnToggleKey").onclick = toggleKeyVisibility;
  $("#btnTestLlm").onclick = testLlmConnection;
  $("#btnClearLlm").onclick = clearLlmSettings;
  $("#settingsForm").addEventListener("submit", saveLlmSettings);
  $("#settingsModal").addEventListener("click", (e) => {
    if (e.target === $("#settingsModal")) closeSettings();
  });

  // 拖拽上传:整个对话区显示「松开上传」遮罩
  const chatMain = $("#chatMain");
  const overlay = $("#dropOverlay");
  let dragDepth = 0;
  const hideOverlay = () => { dragDepth = 0; overlay.classList.add("hidden"); };
  const hasFiles = (e) =>
    !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
  // document 级 preventDefault:任何位置松手都不会让浏览器直接打开文件
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => {
    e.preventDefault();
    hideOverlay();
    if (!e.target.closest || !e.target.closest("#chatMain")) return;
    handleDropFiles(e.dataTransfer.files);
  });
  document.addEventListener("dragleave", (e) => {
    if (!e.relatedTarget) hideOverlay(); // 拖出窗口/按 Esc 取消
  });
  chatMain.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    dragDepth += 1;
    overlay.classList.remove("hidden");
  });
  chatMain.addEventListener("dragover", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    overlay.classList.remove("hidden");
  });
  chatMain.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    dragDepth -= 1;
    if (dragDepth <= 0) hideOverlay();
  });

  // 视图导航:click 与键盘 Enter/Space 都走 setView(button 默认支持空格回车)
  document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
    item.addEventListener("click", () => {
      const view = item.dataset.view;
      if (RESULT_VIEWS.includes(view) && !currentRunId) {
        toast("先完成一次分析");
        return;
      }
      setView(view);
      closeSidebar(); // 移动端选完导航自动收起
    });
  });

  // 结果页返回对话
  $("#btnBackChat").onclick = () => setView("chat");

  // 历史视图:列表已改为整页两栏,不再有折叠分组
  $("#btnClearRuns").onclick = confirmClearRuns;

  // 移动端侧栏
  $("#btnMenu").onclick = openSidebar;
  $("#sidebarBackdrop").onclick = closeSidebar;

  $("#imgModal").onclick = () => $("#imgModal").classList.add("hidden");
}

(async function init() {
  bindEvents();
  bindDbEvents();
  updateNavDisabled();
  // 先按本地状态渲染:health 返回前发送保持禁用,避免误发到未配置的模型
  renderLlmFooter();
  updateSendState();
  setView("chat");
  await Promise.all([loadHealth(), loadSessions(), loadRuns(), loadDatabases()]);
  // 刷新后恢复上次会话:审批中的会话会把审批卡重新渲染出来
  if (state.threadId) selectSession(state.threadId);
})();
