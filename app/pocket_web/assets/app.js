/* 瓦尔基里 · 水镜
 * 手机端只做三件事：看见她、翻识海、替她记。
 * 所有记忆请求都落到电脑上的 PocketServer；浏览器不另存一份记忆。
 */

const APP = {
  token: localStorage.getItem("valkyrie.token") || "",
  health: null,
  online: false,
  paired: false,
  state: null,
  facts: [],
  preAnswer: { items: [], stats: {} },
  meaning: { items: [], chapters: [], stats: {} },
  persona: { layers: {}, active_count: 0, candidate_count: 0 },
  memory: { items: [], total: 0, limit: 60, offset: 0, query: "" },
  memoryLoaded: false,
  memoryFilter: "all",
  view: "home",
  chat: [],
  booted: false,
  voiceOn: localStorage.getItem("valkyrie.voice") !== "off",
  voiceId: localStorage.getItem("valkyrie.voiceId") || "",
  voices: [],
  voiceDefault: "",
  chatBusy: false,
  /* 自然回应与沉默：最近一轮她没说话时留在这里，下一句或换页就散。 */
  hush: null,
  gate: null,
  chatPinned: true,
  recorder: null,
  camera: null,
  audio: null,
  ttsAbort: null,
  speechStream: null,
  proactiveTimer: null,
  proactiveBusy: false,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
let installPrompt = null;

/* 浏览器支持 PWA 时把「安装到桌面」露出来；不支持就不假装有。 */
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
  const button = $("#installWeb");
  if (button) button.hidden = false;
});

function showBootError(message) {
  const el = document.getElementById("bootError");
  if (!el) return;
  el.hidden = false;
  el.textContent = `启动时卡住了：\n${message}`;
  console.error(message);
}

window.addEventListener("error", (event) => {
  showBootError(event.message || "未知脚本错误");
});
window.addEventListener("unhandledrejection", (event) => {
  showBootError(event.reason?.message || String(event.reason || "未处理的异步错误"));
});

const CLAIM_LABELS = {
  fact: "事实",
  wish: "愿望",
  quote: "转述",
  guess: "推测",
};

const REGION_LABELS = {
  experience: "亲历",
  relation: "关系",
  self: "自我",
  knowledge: "知识",
};

const CLAIM_OPTIONS = [
  ["fact", "事实"],
  ["wish", "愿望"],
  ["quote", "转述"],
  ["guess", "推测"],
];

const REGION_OPTIONS = [
  ["experience", "亲历"],
  ["relation", "关系"],
  ["self", "自我"],
  ["knowledge", "知识"],
];

class ApiError extends Error {
  constructor(message, status = 0, code = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function safeColor(value, fallback = "#c3ccdb") {
  return /^#[0-9a-f]{6}$/i.test(String(value || "")) ? value : fallback;
}

function clamp(value, lo, hi) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : lo;
}

function pct(value) {
  return `${Math.round(clamp(value, 0, 1) * 100)}%`;
}

function storeToken(value) {
  APP.token = String(value || "").trim();
  if (APP.token) localStorage.setItem("valkyrie.token", APP.token);
  else localStorage.removeItem("valkyrie.token");
  // Android 常驻服务拿不到 WebView 的 localStorage，配对码要同步给它。
  try {
    window.AndroidPocket?.setToken?.(APP.token);
  } catch (err) { /* 浏览器里没有这个桥 */ }
}

function toast(message, ms = 2600) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("is-visible");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.remove("is-visible"), ms);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (APP.token) headers["X-Valkyrie-Token"] = APP.token;
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers,
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch (err) {
    if (err?.name === "AbortError") {
      throw new ApiError("取消", 0, "ABORTED");
    }
    APP.online = false;
    throw new ApiError("这台手机找不到她。检查连接，再试一次。", 0, "OFFLINE");
  }
  let data = {};
  try {
    data = await response.json();
  } catch (err) {
    data = { ok: false, error: `服务返回了无法识别的内容（${response.status}）` };
  }
  if (response.status === 401) {
    APP.paired = false;
    throw new ApiError(data.error || "先输入配对码。", 401, data.code || "PAIR_REQUIRED");
  }
  if (!response.ok || data.ok === false) {
    throw new ApiError(data.error || `请求失败（${response.status}）`,
      response.status, data.code || "HTTP_ERROR");
  }
  return data;
}

/* ---------- 启动与连接 ---------- */

async function refreshHealth() {
  try {
    const headers = APP.token ? { "X-Valkyrie-Token": APP.token } : {};
    const response = await fetch("/api/health", {
      headers,
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(response.status);
    APP.health = await response.json();
    APP.online = true;
    APP.paired = Boolean(APP.health.paired);
    APP.gate = APP.health.response_gate || APP.health.gate || null;
    if (APP.health.name) document.title = `${APP.health.name} · 水镜`;
    return APP.health;
  } catch (err) {
    APP.online = false;
    APP.paired = false;
    APP.health = APP.health || null;
    APP.gate = null;
    return null;
  }
}

async function loadData({ quiet = false } = {}) {
  if (!APP.token) {
    renderAll();
    return false;
  }
  try {
    const [state, memory, facts, preAnswer, meaning, persona] = await Promise.all([
      api("/api/state"),
      api("/api/memory?limit=60&offset=0"),
      api("/api/facts"),
      api("/api/pre-answer-feedback?limit=20").catch(() => ({ items: [], stats: {} })),
      api("/api/meaning?limit=12").catch(() => ({ items: [], chapters: [], stats: {} })),
      api("/api/persona").catch(() => ({ layers: {}, active_count: 0, candidate_count: 0 })),
    ]);
    APP.state = state;
    APP.voices = Array.isArray(state.voices)
      ? state.voices.filter((v) => v && v.id && v.name)
      : [];
    APP.voiceDefault = String(state.voice_default || "");
    APP.memory.items = memory.items || [];
    APP.memory.total = memory.total || 0;
    APP.memory.offset = APP.memory.items.length;
    APP.memoryLoaded = true;
    APP.facts = facts.items || [];
    APP.preAnswer = preAnswer || { items: [], stats: {} };
    APP.meaning = meaning || { items: [], chapters: [], stats: {} };
    APP.persona = persona || { layers: {}, active_count: 0, candidate_count: 0 };
    APP.online = true;
    APP.paired = true;
    renderAll();
    return true;
  } catch (err) {
    APP.online = false;
    APP.memoryLoaded = true;
    if (!(err instanceof ApiError) || err.code !== "PAIR_REQUIRED") {
      if (!quiet) toast(err.message || "没有接上。");
    }
    renderAll();
    if (err.code === "PAIR_REQUIRED") openPairing();
    return false;
  }
}

async function refreshAll({ quiet = false } = {}) {
  await refreshHealth();
  if (APP.health?.paired && APP.token) {
    const ok = await loadData({ quiet });
    await loadProactiveRecent();
    return ok;
  }
  renderAll();
  if (!APP.token || APP.health?.paired === false) openPairing();
  return false;
}

/* ---------- 她主动发来的消息 ---------- */

const PROACTIVE_KEY = "valkyrie.proactive.last";

function proactiveLastId() {
  const n = Number(localStorage.getItem(PROACTIVE_KEY) || "0");
  return Number.isFinite(n) && n > 0 ? n : 0;
}

function startProactivePolling() {
  if (APP.proactiveTimer) window.clearInterval(APP.proactiveTimer);
  APP.proactiveTimer = window.setInterval(pollProactive, 15000);
  window.setTimeout(pollProactive, 1800);
}

async function loadProactiveRecent() {
  if (!APP.health?.proactive || !APP.token || !APP.online || !APP.paired) return;
  try {
    const data = await api("/api/proactive?since_id=0&limit=3&bootstrap=1");
    const last = Number(data.last_id || 0);
    const local = proactiveLastId();
    if (last > local) localStorage.setItem(PROACTIVE_KEY, String(last));
    let added = false;
    for (const item of data.items || []) {
      const id = Number(item.id || 0);
      if (!id || APP.chat.some((m) => m.proactive && Number(m.id) === id)) continue;
      APP.chat.push({
        role: "her",
        text: String(item.text || ""),
        note: "她先开口",
        proactive: true,
        id,
      });
      added = true;
    }
    if (added) {
      renderChatMessages();
      if (APP.view === "dialogue") renderDialogue();
    }
  } catch (err) {
    if (err?.code === "PAIR_REQUIRED") openPairing();
  }
}

async function pollProactive() {
  if (!APP.health?.proactive || !APP.token || !APP.online || !APP.paired) return;
  if (APP.proactiveBusy) return;
  APP.proactiveBusy = true;
  try {
    const since = proactiveLastId();
    const bootstrap = since ? "" : "&bootstrap=1";
    const data = await api(`/api/proactive?since_id=${since}&limit=10${bootstrap}`);
    const last = Number(data.last_id || 0);
    if (last > 0) localStorage.setItem(PROACTIVE_KEY, String(last));
    const items = Array.isArray(data.items) ? data.items : [];
    let added = false;
    for (const item of items) {
      const id = Number(item.id || 0);
      if (!id || APP.chat.some((m) => m.proactive && Number(m.id) === id)) continue;
      APP.chat.push({
        role: "her",
        text: String(item.text || ""),
        note: "她先开口",
        proactive: true,
        id,
      });
      if (APP.view !== "dialogue") toast(`她先开口：${item.text}`, 4800);
      added = true;
    }
    if (added) {
      renderChatMessages();
      if (APP.view === "dialogue") renderDialogue();
    }
  } catch (err) {
    // 轮询失败不打扰：下一轮再试，页面本身的连接状态由 loadData 管。
    if (err?.code === "PAIR_REQUIRED") openPairing();
  } finally {
    APP.proactiveBusy = false;
  }
}

function setStatus(text, tone = "") {
  const label = $("#statusLabel");
  const dot = $("#statusDot");
  const chip = $("#statusChip");
  if (label) label.textContent = text;
  if (dot) dot.hidden = false;
  chip.dataset.tone = tone;
}

function renderStatus() {
  if (!APP.online) {
    setStatus("还没接上", "offline");
    return;
  }
  if (!APP.paired) {
    setStatus("待配对", "");
    return;
  }
  if (APP.state?.sleep?.sleeping) {
    setStatus("她睡着", "sleep");
    return;
  }
  setStatus("在线", "online");
}

/* ---------- 渲染：首页 ---------- */

function currentHerColor() {
  return safeColor(APP.state?.mood_color, "#c3ccdb");
}

function renderHome() {
  const state = APP.state?.state || null;
  const sleep = APP.state?.sleep || null;
  const demo = Boolean(APP.state?.demo || APP.health?.demo);
  const badge = $("#demoBadge");
  badge.hidden = !demo;

  const mirror = $("#mirror");
  mirror.style.setProperty("--her-color", currentHerColor());
  mirror.classList.toggle("is-sleeping", Boolean(sleep?.sleeping));

  const stateText = $("#stateText");
  const stateMeta = $("#stateMeta");
  if (!APP.token) {
    stateText.textContent = "先把她接上。";
    stateMeta.textContent = "配对码在电脑上，不在云端。";
  } else if (!APP.online) {
    stateText.textContent = "这台手机现在找不到她。";
    stateMeta.textContent = "她还在电脑上；检查 Wi-Fi 或那根线。";
  } else if (!state) {
    stateText.textContent = "正在找她。";
    stateMeta.textContent = "识海还在，只是这一页还没读出来。";
  } else if (sleep?.sleeping) {
    const hours = Number(sleep.slept_h || 0);
    stateText.textContent = `她还在睡。已经 ${hours.toFixed(1)} 小时。`;
    stateMeta.textContent = `${state.existence || ""} · 打开这页不会叫醒她。`;
  } else {
    stateText.textContent = state.feel_text || "她在，安静地听着。";
    const days = Math.max(1, Math.floor(Number(state.days || 0)) + 1);
    stateMeta.textContent = `第 ${days} 天 · 精力 ${pct(state.energy)} · ${Number(state.turns || 0)} 轮`;
  }

  const chatButton = $("#chatButton");
  chatButton.innerHTML = `<span>${APP.health?.chat ? "和她说一句" : "对话未开"}</span>`;

  renderStateQuiet();
  renderHomeMemories();
  renderHomeFacts();

  const note = $("#sleepNote");
  if (demo) {
    note.textContent = "这是示例数据。示例里的记忆只写进 pocket_demo.db，不碰她真实的库。";
  } else if (APP.state?.readonly) {
    note.textContent = "服务端是只读模式：你能翻，但不能替她改。";
  } else if (sleep?.sleeping) {
    note.textContent = `已睡 ${Number(sleep.slept_h || 0).toFixed(1)} 小时。打开这页不消费休眠；只有真正开口才会叫醒她。`;
  } else if (APP.state?.error) {
    note.textContent = "她的状态这次没读出来；识海和记忆仍然在电脑上。";
  } else {
    note.textContent = "她的脑子在电脑上。关掉这页，她还在；有线只是换了一条路。";
  }
}

function renderHomeMemories() {
  const box = $("#homeMemories");
  const items = (APP.memory.items || []).slice(0, 3);
  if (!items.length) {
    box.innerHTML = `<div class="empty"><strong>她的识海还空着。</strong>先替她记住一件小事，不用等到重要。</div>`;
    return;
  }
  box.innerHTML = items.map((item) => `
    <button class="preview-item" type="button" data-memory-id="${Number(item.id)}">
      <p class="preview-text">${esc(item.text)}</p>
      <div class="preview-meta">
        <span class="dot" style="--dot:${safeColor(item.color)}"></span>
        <span>${esc(formatDayShort(item.day, item.ts))}</span>
        <span>·</span>
        <span>${esc(CLAIM_LABELS[item.claim] || item.claim || "事实")}</span>
      </div>
    </button>`).join("");
}

function renderHomeFacts() {
  const box = $("#homeFacts");
  const items = (APP.facts || []).slice(0, 3);
  if (!items.length) {
    box.innerHTML = `<div class="empty">她还没有记住关于你的条目。<button class="text-button" type="button" data-action="add-fact">先写一条</button></div>`;
    return;
  }
  box.innerHTML = items.map((fact) => `
    <div class="fact-row">
      <span class="fact-key">${esc(fact.key)}</span>
      <span class="fact-value">${esc(fact.value)}</span>
    </div>`).join("");
}

/* ---------- 渲染：她长成什么 ---------- */

function activePersonaLayers() {
  const layers = APP.persona?.layers || {};
  return Object.values(layers).map((layer) => layer?.active).filter(Boolean);
}

function confirmedChapters() {
  return (APP.meaning?.chapters || []).filter((item) => item.usable);
}

function confirmedMeanings() {
  return (APP.meaning?.items || []).filter((item) => item.usable);
}

function growthRow({ tone = "", label, copy, detail = "", meta = "" }) {
  return `
    <button class="growth-row ${tone}" type="button" data-nav="memory">
      <span class="growth-mark" aria-hidden="true"></span>
      <span class="growth-body">
        <span class="growth-label">${esc(label)}</span>
        <span class="growth-copy">${esc(copy)}</span>
        ${detail ? `<span class="growth-detail">${esc(detail)}</span>` : ""}
        ${meta ? `<span class="growth-meta">${esc(meta)}</span>` : ""}
      </span>
    </button>`;
}

function renderGrowth() {
  const section = $("#growthSection");
  const list = $("#growthList");
  if (!section || !list) return;

  const rows = [];
  if (APP.token && APP.online) {
    for (const item of activePersonaLayers().slice(0, 2)) {
      const count = Number(item.source_count || 0);
      rows.push(growthRow({
        tone: "is-persona",
        label: item.layer_label || "正在长成",
        copy: item.value || "",
        meta: count ? `已确认 · ${count} 条事件` : "已确认",
      }));
    }

    const chapter = confirmedChapters()[0];
    const meaning = confirmedMeanings()[0];
    if (chapter) {
      const count = Number(chapter.source_count || 0);
      rows.push(growthRow({
        tone: "is-chapter",
        label: "共同章节",
        copy: chapter.title || "",
        detail: chapter.meaning || "",
        meta: count ? `已确认 · ${count} 条事件` : "已确认",
      }));
    } else if (meaning) {
      const count = Number(meaning.source_count || 0);
      rows.push(growthRow({
        tone: "is-meaning",
        label: "她理解到",
        copy: meaning.title || "",
        detail: meaning.claim || "",
        meta: count ? `已确认 · ${count} 条事件` : "已确认",
      }));
    }

    if (!rows.length && Number(APP.persona?.candidate_count || 0) > 0) {
      rows.push(growthRow({
        tone: "is-pending",
        label: "还没定下来",
        copy: "有一条变化还在识海等你确认。",
        meta: "手机不改，去电脑识海看",
      }));
    }
  }

  section.hidden = !rows.length;
  list.innerHTML = rows.slice(0, 3).join("");
}

/* ---------- 渲染：识海 ---------- */

function filteredItems() {
  const items = APP.memory.items || [];
  const now = Date.now() / 1000;
  if (APP.memoryFilter === "feeling") {
    return items.filter((item) => item.emo && item.emo !== "平静" || Number(item.emo_i || 0) >= 0.35);
  }
  if (APP.memoryFilter === "week") {
    return items.filter((item) => now - Number(item.ts || 0) <= 7 * 86400);
  }
  if (APP.memoryFilter === "wish") {
    return items.filter((item) => item.claim === "wish" || item.claim === "guess");
  }
  return items;
}

function renderMemory() {
  const state = APP.state?.state || null;
  const stats = APP.state?.memory || APP.health?.memory || {};
  const summary = $("#memorySummary");
  if (!APP.token) summary.textContent = "先配对，才能翻开她的识海。";
  else if (!APP.memoryLoaded) summary.textContent = "正在翻。";
  else if (!APP.online) summary.textContent = "现在翻不开。先检查连接。";
  else if (!APP.memory.total) summary.textContent = "她的识海还空着。";
  else {
    const facts = Number(stats.facts || APP.facts.length || 0);
    summary.textContent = `一共 ${APP.memory.total} 条记忆，${facts} 件关于你的事。`;
  }

  const list = $("#memoryList");
  const items = filteredItems();
  if (!items.length && !APP.memoryLoaded) {
    // 还在翻：给骨架，别给一句「还没有」把她的识海说成空的。
    list.innerHTML = Array.from({ length: 4 }, (_, index) => `
      <div class="memory-skeleton" aria-hidden="true">
        <span class="sk-line ${index % 2 ? "sk-wide" : "sk-mid"}"></span>
        <span class="sk-line sk-narrow"></span>
      </div>`).join("");
  } else if (!items.length) {
    const searching = Boolean(APP.memory.query);
    const filtered = APP.memoryFilter !== "all";
    const offline = Boolean(APP.token) && !APP.online;
    const title = offline ? "现在翻不开。" : searching ? "没有找到。" : filtered ? "这一栏里还没有。" : "还没有。";
    const copy = offline
      ? "她还在电脑上；检查 Wi-Fi 或那根线。"
      : searching ? "换个说法，或者只搜一个字。" : "记忆不用等大事。记一笔就够。";
    list.innerHTML = `<div class="empty"><strong>${title}</strong>${copy}</div>`;
  } else {
    list.innerHTML = items.map((item) => {
      const retracted = item.status && item.status !== "active";
      return `
        <button class="memory-item${retracted ? " is-retracted" : ""}" type="button" data-memory-id="${Number(item.id)}">
          <span class="memory-date">
            <span class="dot" style="--dot:${safeColor(item.color)}"></span>
            <span>${esc(formatDay(item.day, item.ts))}</span>
            <span>${esc(formatClock(item.ts))}</span>
          </span>
          <p class="memory-text">${esc(item.text)}</p>
          <span class="memory-meta">
            <span>${esc(REGION_LABELS[item.region] || item.region || "亲历")}</span>
            <span class="sep">/</span>
            <span>${esc(CLAIM_LABELS[item.claim] || "事实")}</span>
            <span class="sep">/</span>
            <span>重要 ${pct(item.imp)}</span>
            ${retracted ? `<span class="status">已撤回</span>` : ""}
          </span>
        </button>`;
    }).join("");
  }

  $("#loadMore").hidden = APP.memory.items.length >= APP.memory.total || !APP.memory.total;
  renderMeaning();
  renderPreAnswer();
  renderP3();
  renderFacts();
}

/* ---------- 渲染：关系气候 / 承诺（P3 只读） ---------- */
const P3_SILENCE_LABELS = {
  user_quiet: "你要求安静", sleeping: "她在休眠",
  low_energy: "精力太低", no_value: "没有足够理由",
  no_response: "连续没回应", already_handled: "已由别的通道处理",
};

function renderP3() {
  const section = $("#p3Section");
  const list = $("#p3List");
  if (!section || !list) return;
  const p3 = APP.state?.p3 || {};
  if (!APP.token || !APP.online || !p3.available) {
    section.hidden = true;
    list.innerHTML = "";
    return;
  }
  const climate = p3.climate || {};
  const promises = Array.isArray(p3.promises) ? p3.promises : [];
  const silence = Array.isArray(p3.silence) ? p3.silence : [];
  const count = Number(climate.evidence_count || 0);
  const days = Number(climate.day_count || 0);
  const counter = Array.isArray(climate.counter_evidence)
    ? climate.counter_evidence.length : 0;
  const cards = [];

  const climateLabel = String(climate.label || "还看不出来");
  cards.push(`
    <article class="meaning-card">
      <p class="meaning-label">关系气候</p>
      <h3 class="meaning-title">${esc(climateLabel)}</h3>
      <p class="meaning-claim">${esc(climate.note || "还没有足够的事件，先保持未知。")}</p>
      <p class="meaning-meta">${count} 件证据 · ${days} 天${counter ? ` · ${counter} 件反证` : ""}</p>
    </article>`);

  for (const promise of promises.slice(0, 2)) {
    const due = Number(promise.due_at || 0) > 0
      ? new Date(Number(promise.due_at) * 1000).toLocaleString("zh-CN", {
          month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
        })
      : "没有固定期限";
    cards.push(`
      <article class="meaning-card">
        <p class="meaning-label">还没做完的事</p>
        <h3 class="meaning-title">${esc(promise.text || "")}</h3>
        <p class="meaning-claim">${esc(promise.condition || "只在条件满足时提一次，不追问。")}</p>
        <p class="meaning-meta">有效 · ${esc(due)}</p>
      </article>`);
  }

  if (silence.length) {
    const last = silence[0];
    cards.push(`
      <article class="meaning-card">
        <p class="meaning-label">最近为什么安静</p>
        <h3 class="meaning-title">${esc(P3_SILENCE_LABELS[last.reason_code] || last.reason_code || "沉默")}</h3>
        <p class="meaning-claim">${esc(last.reason || "")}</p>
        <p class="meaning-meta">${last.ts ? new Date(Number(last.ts) * 1000).toLocaleString("zh-CN") : ""}</p>
      </article>`);
  }

  section.hidden = false;
  list.innerHTML = cards.join("");
}

function renderMeaning() {
  const section = $("#meaningSection");
  const list = $("#meaningList");
  if (!section || !list) return;

  const entries = [];
  for (const item of confirmedChapters().slice(0, 1)) {
    entries.push({
      label: "共同章节",
      title: item.title || "",
      body: item.meaning || "",
      meta: `已确认 · ${Number(item.source_count || 0)} 条事件`,
      sources: item.sources || [],
      count: Number(item.source_count || 0),
    });
  }
  for (const item of confirmedMeanings().slice(0, 2)) {
    entries.push({
      label: "她理解到",
      title: item.title || "",
      body: item.claim || "",
      meta: `已确认 · ${Number(item.source_count || 0)} 条事件`,
      sources: item.sources || [],
      count: Number(item.source_count || 0),
    });
  }

  if (!APP.token || !APP.online || !entries.length) {
    section.hidden = true;
    list.innerHTML = "";
    return;
  }
  section.hidden = false;
  list.innerHTML = entries.map((item) => {
    const sources = (item.sources || []).slice(0, 4);
    return `
      <article class="meaning-card">
        <p class="meaning-label">${esc(item.label)}</p>
        <h3 class="meaning-title">${esc(item.title)}</h3>
        ${item.body ? `<p class="meaning-claim">${esc(item.body)}</p>` : ""}
        <p class="meaning-meta">${esc(item.meta)}</p>
        ${sources.length ? `
          <details class="meaning-sources">
            <summary>看这 ${item.count} 条来源</summary>
            <ul>${sources.map((source) => `
              <li><span>${esc(source.day || "")}</span>${esc(source.text || "")}</li>`).join("")}
            </ul>
          </details>` : ""}
      </article>`;
  }).join("");
}

const PRE_ANSWER_LABELS = {
  offered: "待反馈", accepted: "接住了", corrected: "纠正了",
  rejected: "拒绝了", no_response: "没回应", retracted: "已撤回",
};

function renderPreAnswer() {
  const block = $("#preAnswerSection");
  const list = $("#preAnswerList");
  if (!block || !list) return;
  const items = Array.isArray(APP.preAnswer?.items) ? APP.preAnswer.items : [];
  const show = Boolean(APP.token && APP.online && items.length);
  block.hidden = !show;
  if (!show) { list.innerHTML = ""; return; }
  list.innerHTML = items.slice(0, 4).map((item) => {
    const action = String(item.action || "");
    const cls = action === "retracted" ? "retracted" : action;
    const label = PRE_ANSWER_LABELS[action] || item.action_label || action || "—";
    const strategy = item.strategy_label || item.strategy || "一次回答";
    const reason = String(item.reason || "").trim();
    const source = String(item.source_text || "").trim();
    return `
      <div class="pre-answer-item ${esc(cls)}">
        <div class="pre-answer-top">
          <span class="pre-answer-strategy">${esc(strategy)}</span>
          <span class="pre-answer-action">${esc(label)}</span>
          <span class="pre-answer-time">${item.recorded_at ? formatClock(item.recorded_at) : ""}</span>
        </div>
        <p class="pre-answer-reason">${esc(reason || "当时只记了结果，没有留下原因。")}</p>
        ${source ? `<p class="pre-answer-quote">你下一句：${esc(source)}</p>` : ""}
        ${item.can_retract ? `<button class="pre-answer-retract" type="button" data-preanswer-retract="${esc(item.id)}">撤回这次误判</button>` : ""}
      </div>`;
  }).join("");
}

async function retractPreAnswer(id, btn) {
  if (!id) return;
  if (!confirm("撤回这次误判？只影响下一次同一种分寸，不改记忆、关系和人格。")) return;
  if (btn) { btn.disabled = true; btn.textContent = "撤回中…"; }
  try {
    await api(`/api/pre-answer-feedback/${encodeURIComponent(id)}/retract`, {
      method: "POST",
      body: JSON.stringify({ reason: "App 认为这次判断错了" }),
    });
    toast("撤回了。下一次同一种分寸按当前证据重新判断。");
    await loadData({ quiet: true });
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = "撤回这次误判"; }
    toast(err.message);
  }
}

function renderFacts() {
  const list = $("#factsList");
  const items = APP.facts || [];
  if (!items.length) {
    list.innerHTML = `<div class="empty">还没有。她记住你，是从一句具体的话开始的。</div>`;
    return;
  }
  list.innerHTML = items.map((fact) => `
    <div class="fact-row">
      <span class="fact-key">${esc(fact.key)}</span>
      <span class="fact-value">${esc(fact.value)}</span>
      <button class="fact-del" type="button" data-fact-delete="${esc(fact.key)}" aria-label="删除 ${esc(fact.key)}">×</button>
    </div>`).join("");
}

/* ---------- 渲染：连接 ---------- */

function renderConnect() {
  const card = $(".connection-card");
  const online = APP.online && APP.paired;
  card.dataset.state = online ? "online" : "offline";
  $("#connectionState").textContent = online ? "已接上" : "还没连上";
  const address = $("#connectionAddress");
  if (online) {
    address.textContent = `${location.origin}  ·  记忆存在电脑的 memory.db`;
  } else if (APP.token) {
    address.textContent = "这台设备带着配对码，但还没有找到服务。";
  } else {
    address.textContent = "还没有配对码。";
  }
  const tokenInput = $("#tokenInput");
  if (document.activeElement !== tokenInput) tokenInput.value = APP.token;
}

function renderAll() {
  renderStatus();
  renderHome();
  renderGrowth();
  renderDialogue();
  renderMemory();
  renderConnect();
}

/* ---------- 记忆详情 ---------- */

async function openMemoryDetail(id) {
  openSheet(`
    <div class="sheet-head">
      <h2 class="sheet-title">翻一页</h2>
      <button class="sheet-close" type="button" data-close aria-label="关闭">×</button>
    </div>
    <p class="sheet-sub">正在从电脑上的识海取这一条。</p>
    <p class="empty">等一下。</p>
  `);
  try {
    const data = await api(`/api/memory/${Number(id)}`);
    renderMemoryDetail(data.item);
  } catch (err) {
    openSheet(`
      <div class="sheet-head">
        <h2 class="sheet-title">没翻到</h2>
        <button class="sheet-close" type="button" data-close aria-label="关闭">×</button>
      </div>
      <p class="sheet-sub">${esc(err.message)}</p>
      <div class="form-actions"><button class="button button-quiet" type="button" data-close>收起</button></div>
    `);
  }
}

function renderMemoryDetail(item) {
  APP.detailItem = item;
  const retracted = item.status && item.status !== "active";
  const related = item.related || [];
  openSheet(`
    <div class="sheet-head">
      <h2 class="sheet-title">${esc(formatDay(item.day, item.ts))}</h2>
      <button class="sheet-close" type="button" data-close aria-label="关闭">×</button>
    </div>
    <p class="sheet-copy">${esc(item.text)}</p>
    <div class="sheet-meta">
      <span class="chip" style="--dot:${safeColor(item.color)}">${esc(item.emo || "平静")}</span>
      <span class="chip">${esc(REGION_LABELS[item.region] || item.region || "亲历")}</span>
      <span class="chip">${esc(CLAIM_LABELS[item.claim] || "事实")}</span>
      <span class="chip">重要 ${pct(item.imp)}</span>
      <span class="chip">${esc(formatClock(item.ts))}</span>
      ${retracted ? `<span class="chip" style="color:var(--coral);border-color:rgba(223,130,114,.3)">已撤回</span>` : ""}
    </div>
    ${item.source ? `<p class="section-note">来源：${esc(item.source)}${item.about ? ` · 关于${esc(item.about)}` : ""}</p>` : ""}
    ${retracted && item.retract_reason ? `<p class="notice is-danger">撤回原因：${esc(item.retract_reason)}</p>` : ""}
    ${related.length ? `
      <div class="related-block">
        <h3>同一天或同一个人的几页</h3>
        ${related.map((r) => `<button class="related-item" type="button" data-memory-id="${Number(r.id)}">${esc(r.text)}</button>`).join("")}
      </div>` : ""}
    <div class="sheet-actions">
      ${retracted ? "" : `<button class="button button-quiet" type="button" data-detail-action="correct" data-id="${Number(item.id)}">更正</button>
      <button class="button button-quiet" type="button" data-detail-action="retract" data-id="${Number(item.id)}">撤回</button>`}
      <button class="button button-primary" type="button" data-close>合上</button>
    </div>
  `);
}

/* ---------- 表单：记一笔 ---------- */

function openAddMemory() {
  openSheet(`
    <div class="sheet-head">
      <h2 class="sheet-title">记一笔</h2>
      <button class="sheet-close" type="button" data-close aria-label="关闭">×</button>
    </div>
    <p class="sheet-sub">这条会写进她电脑上的识海。越具体，以后越容易被想起。</p>
    <form id="memoryForm">
      <div class="field">
        <label class="field-label" for="memoryText">她该记住什么</label>
        <textarea id="memoryText" name="text" maxlength="500" required placeholder="一句话就够。比如：他今天把窗子推开，说屋里有点闷。"></textarea>
      </div>
      <div class="field">
        <div class="field-label"><span>重要程度</span><span class="field-hint" id="impValue">0.55</span></div>
        <div class="range-row">
          <input id="memoryImp" name="imp" type="range" min="5" max="100" value="55">
          <span class="field-hint">低 / 高</span>
        </div>
      </div>
      <div class="field-row">
        <div class="field">
          <label class="field-label" for="memoryClaim">这是</label>
          <select id="memoryClaim" name="claim">${claimOptions()}</select>
        </div>
        <div class="field">
          <label class="field-label" for="memoryWho">关于</label>
          <select id="memoryWho" name="who">
            <option value="他" selected>他</option>
            <option value="她">她</option>
            <option value="我">我</option>
            <option value="其他">其他</option>
          </select>
        </div>
      </div>
      <details>
        <summary class="field-hint" style="cursor:pointer;margin-bottom:12px">更多（归区 / 情绪 / 来源）</summary>
        <div class="field-row">
          <div class="field">
            <label class="field-label" for="memoryRegion">归到</label>
            <select id="memoryRegion" name="region">${regionOptions()}</select>
          </div>
          <div class="field">
            <label class="field-label" for="memoryEmo">情绪</label>
            <select id="memoryEmo" name="emo">
              <option value="">让她自己认</option>
              <option value="平静">平静</option>
              <option value="开心">开心</option>
              <option value="低落">低落</option>
              <option value="委屈">委屈</option>
              <option value="不安">不安</option>
              <option value="期待">期待</option>
              <option value="亲近">亲近</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label class="field-label" for="memorySource">来源</label>
          <input id="memorySource" name="source" type="text" maxlength="20" value="App 手动记录">
        </div>
      </details>
      <div class="form-actions">
        <button class="button button-quiet" type="button" data-close>取消</button>
        <button class="button button-primary" type="submit">记下</button>
      </div>
    </form>
  `);
  const form = $("#memoryForm");
  const imp = $("#memoryImp");
  imp.addEventListener("input", () => { $("#impValue").textContent = (Number(imp.value) / 100).toFixed(2); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    const text = form.elements.text.value.trim();
    if (!text) return;
    submit.disabled = true;
    try {
      await api("/api/memory", {
        method: "POST",
        body: JSON.stringify({
          text,
          imp: Number(imp.value) / 100,
          claim: form.elements.claim.value,
          who: form.elements.who.value,
          region: form.elements.region.value,
          emo: form.elements.emo.value,
          source: form.elements.source.value.trim() || "App 手动记录",
        }),
      });
      closeSheet();
      toast("记下了。");
      await loadData();
    } catch (err) {
      submit.disabled = false;
      if (err.code === "PAIR_REQUIRED") openPairing();
      else toast(err.message);
    }
  });
}

function openCorrect(item) {
  openSheet(`
    <div class="sheet-head">
      <h2 class="sheet-title">更正这一条</h2>
      <button class="sheet-close" type="button" data-close aria-label="关闭">×</button>
    </div>
    <p class="sheet-sub">旧的那条不会被抹掉，会被标成「已被更正」；她以后只依据新的一条。</p>
    <form id="correctForm">
      <div class="field">
        <label class="field-label" for="correctText">改成</label>
        <textarea id="correctText" name="text" maxlength="500" required></textarea>
      </div>
      <div class="field">
        <label class="field-label" for="correctReason">为什么改</label>
        <input id="correctReason" name="reason" type="text" maxlength="80" value="App 更正">
      </div>
      <div class="form-actions">
        <button class="button button-quiet" type="button" data-close>取消</button>
        <button class="button button-primary" type="submit">更正</button>
      </div>
    </form>
  `);
  const form = $("#correctForm");
  form.elements.text.value = item.text || "";
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      await api(`/api/memory/${Number(item.id)}/correct`, {
        method: "POST",
        body: JSON.stringify({
          text: form.elements.text.value.trim(),
          reason: form.elements.reason.value.trim() || "App 更正",
        }),
      });
      closeSheet();
      toast("已经改了。旧原文还在库里做证据。");
      await loadData();
    } catch (err) {
      submit.disabled = false;
      toast(err.message);
    }
  });
}

function openRetract(item) {
  openSheet(`
    <div class="sheet-head">
      <h2 class="sheet-title">撤回这一条</h2>
      <button class="sheet-close" type="button" data-close aria-label="关闭">×</button>
    </div>
    <p class="sheet-copy">${esc(item.text)}</p>
    <p class="notice is-danger">撤回不是删除。原文还在库里，只是不再参与她的回忆、联想和残响。</p>
    <form id="retractForm">
      <div class="field">
        <label class="field-label" for="retractReason">为什么撤回</label>
        <input id="retractReason" name="reason" type="text" maxlength="80" value="App 要求撤回">
      </div>
      <div class="form-actions">
        <button class="button button-quiet" type="button" data-close>再想想</button>
        <button class="button button-primary" type="submit">撤回</button>
      </div>
    </form>
  `);
  const form = $("#retractForm");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      await api(`/api/memory/${Number(item.id)}/retract`, {
        method: "POST",
        body: JSON.stringify({ reason: form.elements.reason.value.trim() || "App 要求撤回" }),
      });
      closeSheet();
      toast("撤回了。她还留着原文，但不会再想起它。");
      await loadData();
    } catch (err) {
      submit.disabled = false;
      toast(err.message);
    }
  });
}

/* ---------- 表单：事实 ---------- */

function openAddFact() {
  openSheet(`
    <div class="sheet-head">
      <h2 class="sheet-title">她该记住的你</h2>
      <button class="sheet-close" type="button" data-close aria-label="关闭">×</button>
    </div>
    <p class="sheet-sub">事实是短句，不是日记。比如「喜欢」+「靠窗的位置」。</p>
    <form id="factForm">
      <div class="field-row">
        <div class="field">
          <label class="field-label" for="factKey">条目</label>
          <input id="factKey" name="key" type="text" maxlength="40" placeholder="喜欢" required>
        </div>
        <div class="field">
          <label class="field-label" for="factConf">确定程度</label>
          <select id="factConf" name="conf">
            <option value="0.9">很确定</option>
            <option value="0.7" selected>大概</option>
            <option value="0.5">还不确定</option>
          </select>
        </div>
      </div>
      <div class="field">
        <label class="field-label" for="factValue">内容</label>
        <input id="factValue" name="value" type="text" maxlength="200" placeholder="靠窗的位置" required>
      </div>
      <div class="form-actions">
        <button class="button button-quiet" type="button" data-close>取消</button>
        <button class="button button-primary" type="submit">记住</button>
      </div>
    </form>
  `);
  const form = $("#factForm");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      await api("/api/facts", {
        method: "POST",
        body: JSON.stringify({
          key: form.elements.key.value.trim(),
          value: form.elements.value.value.trim(),
          conf: Number(form.elements.conf.value),
          source: "App 手动",
        }),
      });
      closeSheet();
      toast("记住了。");
      await loadData();
    } catch (err) {
      submit.disabled = false;
      toast(err.message);
    }
  });
}

async function deleteFact(key) {
  if (!window.confirm(`删除「${key}」这条事实？只删这一条，不动她的记忆。`)) return;
  try {
    await api(`/api/facts/${encodeURIComponent(key)}?confirm=DELETE`, { method: "DELETE" });
    toast("删掉了。");
    await loadData();
  } catch (err) {
    toast(err.message);
  }
}

/* ---------- 自然回应与沉默 ----------
 * 服务端这一轮可能什么都不说：ok:true、silent:true、reply:""。
 * 这不是失败——聊天流停住，界面上只留一句不占气泡的话。
 */

const HUSH_COPY = {
  silent: { line: "她这次没接这句。", hint: "她在，只是没说。" },
  defer: { line: "她把这句先放下了。", hint: "不是没看见，是现在不接。" },
};

function responseGateOf(data) {
  return data?.response_gate || data?.gate || null;
}

function silenceModeOf(data) {
  const mode = String(data?.mode || data?.response_gate?.mode || "").toLowerCase();
  if (mode === "defer") return "defer";
  if (mode === "silent") return "silent";
  return "silent";
}

/* silent 的判定只认三种明确信号，不靠「reply 空」猜（空回复另有 empty 那条路）。 */
function isSilentResult(data) {
  if (!data || data.error) return false;
  if (data.silent === true) return true;
  const mode = String(data.mode || "").toLowerCase();
  if (mode !== "silent" && mode !== "defer") return false;
  return !String(data.reply || "").trim();
}

/* 服务端给的 reason 可能是给人看的，也可能是内部码；只放前一种。 */
function readableReason(reason) {
  const text = String(reason || "").trim();
  if (!text) return "";
  if (text.length > 28) return "";
  if (/[\\/:_|=<>{}]|[a-z]{4,}/i.test(text)) return "";
  return text;
}

function noteSilence(data) {
  const mode = silenceModeOf(data);
  APP.hush = {
    mode,
    reason: readableReason(data?.reason),
    at: Date.now(),
    gate: responseGateOf(data),
  };
}

function clearSilence() {
  if (!APP.hush) return;
  APP.hush = null;
  renderHush();
  renderStateQuiet();
}

/* 不占气泡的一行：接在聊天流末尾，只说明她这次没说。 */
function renderHush() {
  const el = $("#chatHush");
  if (!el) return;
  if (!APP.hush || APP.view !== "dialogue") {
    el.hidden = true;
    el.textContent = "";
    el.removeAttribute("data-mode");
    el.removeAttribute("title");
    return;
  }
  const copy = HUSH_COPY[APP.hush.mode] || HUSH_COPY.silent;
  el.hidden = false;
  el.dataset.mode = APP.hush.mode;
  el.title = copy.hint;
  el.textContent = APP.hush.reason
    ? `${copy.line}（${APP.hush.reason}）`
    : copy.line;
}

/* 首页这一行只说「她眼下话少」这种慢状态，跟她的心情并排不抢；
 * 一轮的沉默只留在对话页，换页就散，不跟到首页来。 */
function renderStateQuiet() {
  const el = $("#stateQuiet");
  if (!el) return;
  const sleeping = Boolean(APP.state?.sleep?.sleeping);
  const ready = APP.online && APP.paired && APP.state && !sleeping;
  const quietMode = Boolean(APP.gate?.social_silence || APP.gate?.mode === "silent");
  if (!ready || !quietMode) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = "这会儿她话少。";
}

/* ---------- 对话 ---------- */

function openChat() {
  if (!APP.health?.chat) {
    openSheet(`
      <div class="sheet-head">
        <h2 class="sheet-title">对话没开</h2>
        <button class="sheet-close" type="button" data-close aria-label="关闭">×</button>
      </div>
      <p class="sheet-copy">她现在还在睡。打开 App 可以翻记忆，但我没有把「开口」这件事一起接上。</p>
      <p class="notice">要跟她说，用电脑启动：<code>talk.cmd</code>。发送第一句会叫醒她，也会消耗掉休眠记录。</p>
      <div class="form-actions"><button class="button button-primary" type="button" data-close>知道了</button></div>
    `);
    return;
  }
  switchView("dialogue");
}

function mediaSupported() {
  return Boolean(window.isSecureContext
    && navigator.mediaDevices
    && typeof navigator.mediaDevices.getUserMedia === "function");
}

function mediaErrorMessage(err, kind) {
  if (!window.isSecureContext) {
    return `${kind}只能在安全地址上用。当前是 ${location.origin}，换成 USB 的 http://127.0.0.1:8766 再试。`;
  }
  if (err?.name === "NotAllowedError" || err?.name === "SecurityError") {
    return `${kind}被手机拦住了。到系统设置里允许「水镜」使用${kind}，再回来试。`;
  }
  if (err?.name === "NotFoundError" || err?.name === "DevicesNotFoundError") {
    return `这台手机没有找到可用的${kind}。`;
  }
  if (err?.name === "NotReadableError" || err?.name === "TrackStartError") {
    return `${kind}被另一个应用占着。先把别的相机/录音关掉。`;
  }
  return `${kind}没打开：${err?.message || "未知错误"}`;
}

function renderDialogue() {
  const sub = $("#dialogueSub");
  if (!sub) return;
  const chatOn = Boolean(APP.health?.chat);
  const supported = mediaSupported();
  const online = APP.online && APP.paired;

  if (!online) {
    sub.textContent = "这台手机还没接上她。先回连接页看看。";
  } else if (!chatOn) {
    sub.textContent = "现在只能翻记忆。要说话，电脑上用 talk.cmd 启动。";
  } else {
    sub.textContent = "直接说，或者把眼前的东西给她看。";
  }

  const warn = $("#mediaWarn");
  if (!warn) return;
  if (!chatOn) {
    warn.hidden = false;
    warn.innerHTML = "对话没开：电脑上用 <code>talk.cmd</code> 启动，发送第一句会叫醒她。";
  } else if (!supported) {
    warn.hidden = false;
    warn.innerHTML = `语音和摄像头只在这条 <code>http://127.0.0.1:8766</code> 的 USB 地址上可用；当前 ${esc(location.origin)} 是局域网 http，浏览器会把它们锁掉。`;
  } else {
    warn.hidden = true;
    warn.textContent = "";
  }

  const canUse = online && chatOn;
  ["#chatInput", "#micButton", "#cameraButton", "#voiceToggle", "#voiceSelect"].forEach((sel) => {
    const el = $(sel);
    if (el) el.disabled = !canUse;
  });
  if ($("#micButton")) $("#micButton").disabled = !canUse || !supported;
  if ($("#cameraButton")) $("#cameraButton").disabled = !canUse || !supported;
  if ($("#chatInput")) $("#chatInput").disabled = !canUse;
  if ($("#composeHint")) {
    $("#composeHint").textContent = supported
      ? "声音只转成文字，照片只给这一轮看；不存手机。"
      : "语音/摄像头需要 USB 的 127.0.0.1 地址。";
  }
  renderVoiceSelect();
  renderVoiceToggle();
  renderChatMessages();
}

function voiceList() {
  return Array.isArray(APP.voices) ? APP.voices : [];
}

function selectedVoiceId() {
  const list = voiceList();
  if (!list.length) return APP.voiceId || "";
  const ids = new Set(list.map((voice) => voice.id));
  if (APP.voiceId && ids.has(APP.voiceId)) return APP.voiceId;
  if (APP.voiceDefault && ids.has(APP.voiceDefault)) return APP.voiceDefault;
  return list[0].id;
}

function selectedVoiceName() {
  const id = selectedVoiceId();
  const item = voiceList().find((voice) => voice.id === id);
  return item?.name || id || "默认音色";
}

function renderVoiceSelect() {
  const button = $("#voiceSelect");
  if (!button) return;
  const list = voiceList();
  if (!list.length) {
    button.hidden = true;
    return;
  }
  button.hidden = false;
  button.textContent = selectedVoiceName();
  button.disabled = !(APP.online && APP.paired && APP.health?.chat);
  button.title = `她的音色：${selectedVoiceName()}`;
  button.setAttribute("aria-label", `她的音色：${selectedVoiceName()}`);
}

function openVoicePicker() {
  if (!(APP.online && APP.paired && APP.health?.chat)) return;
  const list = voiceList();
  if (!list.length) {
    toast("服务端还没给出音色。");
    return;
  }
  const current = selectedVoiceId();
  openSheet(`
    <div class="sheet-head">
      <h2 class="sheet-title">她的音色</h2>
      <button class="sheet-close" type="button" data-close aria-label="关闭">×</button>
    </div>
    <p class="sheet-sub">选一个。下一次她开口就用这个声音，这台手机会记住。</p>
    <div class="voice-choices">
      ${list.map((voice) => `
        <button class="voice-choice${voice.id === current ? " is-active" : ""}" type="button" data-voice-id="${esc(voice.id)}">
          <span class="voice-choice-name">${esc(voice.name)}</span>
          <span class="voice-choice-mark">${voice.id === current ? "正在用" : "选这个"}</span>
        </button>`).join("")}
    </div>
  `);
  $$("[data-voice-id]", $("#sheetRoot")).forEach((button) => {
    button.addEventListener("click", () => {
      const id = String(button.dataset.voiceId || "");
      if (!id) return;
      APP.voiceId = id;
      localStorage.setItem("valkyrie.voiceId", id);
      stopAudio();
      closeSheet();
      renderVoiceSelect();
      toast(`音色：${selectedVoiceName()}`);
    });
  });
}

function renderVoiceToggle() {
  const button = $("#voiceToggle");
  if (!button) return;
  button.setAttribute("aria-pressed", APP.voiceOn ? "true" : "false");
  button.textContent = APP.voiceOn ? "声音 开" : "声音 关";
}

/* 「她正在想」不是一句灰字，是她真的在那儿想：三个点。 */
function thinkingRow(label) {
  const row = document.createElement("div");
  row.className = "chat-thinking";
  const dots = document.createElement("span");
  dots.className = "chat-thinking-dots";
  dots.setAttribute("aria-hidden", "true");
  dots.innerHTML = "<i></i><i></i><i></i>";
  const text = document.createElement("span");
  text.className = "chat-thinking-label";
  text.textContent = label || "她正在想。";
  row.appendChild(dots);
  row.appendChild(text);
  return row;
}

/* 只有贴着底部时才自动跟下去；往上翻旧话回看时别把她拽回来。 */
function stickChatToBottom(log) {
  const box = log || $("#chatLog");
  if (!box) return;
  requestAnimationFrame(() => {
    if (APP.chatPinned === false) return;
    box.scrollTop = box.scrollHeight;
  });
}

/* 键盘弹起时把可视高度写进 --kb-h；布局据此收一收，输入栏就留在键盘上面。 */
function setupViewportInsets() {
  const root = document.documentElement;
  const viewport = window.visualViewport;
  const apply = () => {
    const layoutH = window.innerHeight;
    const visualH = viewport ? viewport.height : layoutH;
    const inset = Math.max(0, Math.round(layoutH - visualH - (viewport?.offsetTop || 0)));
    root.style.setProperty("--kb-h", `${inset}px`);
    document.body.classList.toggle("kb-open", inset > 100);
  };
  if (viewport) {
    viewport.addEventListener("resize", apply);
    viewport.addEventListener("scroll", apply);
  }
  window.addEventListener("orientationchange", () => window.setTimeout(apply, 240));
  apply();
}

function renderChatMessages() {
  const log = $("#chatLog");
  if (!log) return;
  log.innerHTML = "";
  if (!APP.chat.length) {
    const box = document.createElement("div");
    box.className = "chat-welcome";
    box.innerHTML = `
      <span class="chat-welcome-mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none">
          <path d="M4.5 6.3h15v9.2H9.4L5.2 19v-3.5H4.5V6.3Z" stroke="currentColor" stroke-width="1.45" stroke-linejoin="round"/>
          <path d="M8 10.8h8M8 13.2h5" stroke="currentColor" stroke-width="1.45" stroke-linecap="round"/>
        </svg>
      </span>
      <p>我在。</p>
      <span>打字、说一句，或者把眼前的东西给我看。</span>`;
    log.appendChild(box);
    renderHush();
    return;
  }

  APP.chat.forEach((message, index) => {
    if (message.role === "her") {
      const wrap = document.createElement("div");
      wrap.className = "chat-reply" + (message.proactive ? " is-proactive" : "");
      const row = document.createElement("div");
      row.className = "chat-reply-row";
      const p = document.createElement("p");
      p.className = "chat-message is-her";
      p.textContent = message.text;
      row.appendChild(p);
      const speak = document.createElement("button");
      speak.className = "speak-button";
      speak.type = "button";
      speak.dataset.speakIndex = String(index);
      speak.setAttribute("aria-label", "听她说这句");
      speak.innerHTML = `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 9.5h3.2L12 5.8v12.4L7.2 14.5H4V9.5Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M15.2 9.1a4 4 0 0 1 0 5.8M17.7 6.8a7.2 7.2 0 0 1 0 10.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`;
      row.appendChild(speak);
      wrap.appendChild(row);
      if (message.note) {
        const note = document.createElement("p");
        note.className = "chat-note";
        note.textContent = message.note;
        wrap.appendChild(note);
      }
      log.appendChild(wrap);
      return;
    }

    if (message.role === "pending") {
      log.appendChild(thinkingRow(message.text));
      return;
    }

    const p = document.createElement("p");
    p.className = `chat-message ${message.role === "me" ? "is-me" : message.role === "error" ? "is-error" : "is-note"}`;
    if (message.image) {
      const img = document.createElement("img");
      img.className = "chat-thumb";
      img.alt = "刚给她看的照片";
      img.src = message.image;
      p.appendChild(img);
    }
    if (message.text) {
      const text = document.createElement("span");
      text.textContent = message.text;
      p.appendChild(text);
    }
    if (message.note) {
      const note = document.createElement("span");
      note.className = "chat-note";
      note.textContent = message.note;
      p.appendChild(note);
    }
    log.appendChild(p);
  });
  renderHush();
  stickChatToBottom(log);
}

function setChatBusy(busy) {
  APP.chatBusy = Boolean(busy);
  // 输入框不锁：她在想的时候你还能接着打字，锁了会把手机键盘一起收掉。
  const send = $(".send-button");
  if (send) {
    send.disabled = busy;
    send.classList.toggle("is-busy", Boolean(busy));
  }
  // 她还在回这一句时，别再按语音/摄像头塞第二句进去。
  const tools = ["#micButton", "#cameraButton"];
  tools.forEach((sel) => {
    const el = $(sel);
    if (el) el.disabled = busy;
  });
  const form = $("#chatForm");
  if (form) form.setAttribute("aria-busy", busy ? "true" : "false");
  document.body.classList.toggle("chat-busy", Boolean(busy));
}

function supportsChatStream() {
  return typeof fetch === "function" && typeof TextDecoder !== "undefined";
}

/* 读 POST 的 SSE：服务端逐段发 `data: {...}`，这里按空行切事件。 */
async function streamChatRequest(payload, onEvent) {
  const headers = { "Content-Type": "application/json" };
  if (APP.token) headers["X-Valkyrie-Token"] = APP.token;
  let response;
  try {
    response = await fetch("/api/chat/stream", {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch (err) {
    APP.online = false;
    throw new ApiError("这台手机找不到她。检查连接，再试一次。", 0, "OFFLINE");
  }
  if (response.status === 401) {
    let data = {};
    try { data = await response.json(); } catch (e) { /* ignore */ }
    APP.paired = false;
    throw new ApiError(data.error || "先输入配对码。", 401,
      data.code || "PAIR_REQUIRED");
  }
  if (!response.ok) {
    let data = {};
    try { data = await response.json(); } catch (e) { /* ignore */ }
    throw new ApiError(data.error || `请求失败（${response.status}）`,
      response.status, data.code || "HTTP_ERROR");
  }
  if (!response.body || typeof response.body.getReader !== "function") {
    throw new ApiError("这个浏览器不支持流式回复。", 0, "STREAM_UNSUPPORTED");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let split;
      while ((split = buffer.search(/\r?\n\r?\n/)) >= 0) {
        const raw = buffer.slice(0, split);
        const after = buffer.slice(split);
        const sep = after.match(/^\r?\n\r?\n/);
        buffer = after.slice(sep ? sep[0].length : 2);
        const line = raw.split(/\r?\n/).find((part) => part.startsWith("data:"));
        if (!line) continue;
        const body = line.slice(5).trim();
        if (!body) continue;
        let event;
        try { event = JSON.parse(body); } catch (e) { continue; }
        onEvent(event);
      }
    }
    buffer += decoder.decode();
    const line = buffer.split(/\r?\n/).find((part) => part.startsWith("data:"));
    if (line) {
      try { onEvent(JSON.parse(line.slice(5).trim())); } catch (e) { /* ignore */ }
    }
  } finally {
    try { reader.releaseLock(); } catch (e) { /* ignore */ }
  }
}

function applyChatResult(data, pending, userMessage) {
  APP.chat = APP.chat.filter((m) => m !== pending);
  if (data.empty) {
    if (userMessage && userMessage.kind === "voice") userMessage.note = "没听清。";
    APP.chat.push({ role: "error", text: "没听清，再说一次，或者打字。" });
    return;
  }
  // 她这一轮什么都没说：不加气泡，只留一行轻状态。
  if (isSilentResult(data)) {
    noteSilence(data);
    if (data.state && APP.state) APP.state.state = data.state;
    if (data.memory && APP.state) APP.state.memory = data.memory;
    renderChatMessages();
    renderHome();
    return;
  }
  if (userMessage && data.transcript && userMessage.kind === "voice") {
    userMessage.text = `（语音）${data.transcript}`;
  }
  const notes = [];
  if (data.vision) notes.push(`她看到：${data.vision}`);
  if (data.transcript) notes.push(`听成：${data.transcript}`);
  APP.chat.push({
    role: "her",
    text: data.reply || "（这一轮没有话）",
    note: notes.join("\n"),
  });
  if (data.state && APP.state) APP.state.state = data.state;
  if (data.memory && APP.state) APP.state.memory = data.memory;
  renderHome();
  if (APP.voiceOn && data.reply) speakReply(data.reply, false);
}

async function sendChatLegacy(payload, pending, userMessage) {
  const data = await api("/api/chat", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  applyChatResult(data, pending, userMessage);
}

async function sendChatStreaming(payload, pending, userMessage) {
  let her = null;
  let sawDelta = false;
  let finished = false;
  const speech = APP.voiceOn ? createSpeechStreamer() : null;
  const ensureHer = () => {
    if (her) return her;
    APP.chat = APP.chat.filter((m) => m !== pending);
    her = { role: "her", text: "", note: "" };
    APP.chat.push(her);
    return her;
  };

  try {
    await streamChatRequest(payload, (event) => {
      const kind = String(event?.type || "");
      if (kind === "meta") {
        if (event.transcript && userMessage && userMessage.kind === "voice") {
          userMessage.text = `（语音）${event.transcript}`;
        }
        return;
      }
      if (kind === "delta") {
        const piece = String(event.text || "");
        if (!piece) return;
        sawDelta = true;
        const message = ensureHer();
        message.text += piece;
        if (speech) speech.push(piece);
        renderChatMessages();
        return;
      }
      if (kind === "error") {
        throw new Error(event.error || "这一轮没有答上来。");
      }
      if (kind === "done") {
        finished = true;
        const data = event.result || {};
        APP.chat = APP.chat.filter((m) => m !== pending);
        // 她这一轮选择了不说：聊天流就停在这里，不补气泡、不出声。
        if (isSilentResult(data)) {
          if (speech) {
            speech.stop();
            if (APP.speechStream === speech) APP.speechStream = null;
          }
          noteSilence(data);
          if (data.state && APP.state) APP.state.state = data.state;
          if (data.memory && APP.state) APP.state.memory = data.memory;
          renderChatMessages();
          renderHome();
          return;
        }
        // 沉默之外的 ok:false 才是真的没答上来，别拿「没有说话」蒙混过去。
        if (data.ok === false) {
          throw new Error(data.error || "这一轮没有答上来。");
        }
        if (data.empty) {
          if (userMessage && userMessage.kind === "voice") userMessage.note = "没听清。";
          APP.chat.push({ role: "error", text: "没听清，再说一次，或者打字。" });
          if (speech) {
            speech.stop();
            if (APP.speechStream === speech) APP.speechStream = null;
          }
          renderChatMessages();
          return;
        }
        const reply = String(data.reply || her?.text || "（这一轮没有话）");
        const message = her || ensureHer();
        message.text = reply;
        const notes = [];
        if (data.vision) notes.push(`她看到：${data.vision}`);
        if (data.transcript) notes.push(`听成：${data.transcript}`);
        message.note = notes.join("\n");
        if (data.state && APP.state) APP.state.state = data.state;
        if (data.memory && APP.state) APP.state.memory = data.memory;
        renderHome();
        if (speech) {
          if (!sawDelta) speech.push(reply);
          speech.finish();
        }
        renderChatMessages();
      }
    });
    if (!finished) throw new Error("这一轮没有答完。");
  } catch (err) {
    if (speech) {
      speech.stop();
      if (APP.speechStream === speech) APP.speechStream = null;
    }
    if (her) {
      her.note = [her.note, "（说到一半断了）"].filter(Boolean).join("\n");
    } else {
      APP.chat = APP.chat.filter((m) => m !== pending);
    }
    renderChatMessages();
    throw err;
  }
}

async function sendChatPayload(payload, userMessage = null) {
  if (!APP.health?.chat) {
    toast("对话没开。电脑上用 talk.cmd 启动。");
    return;
  }
  if (APP.chatBusy) {
    toast("她还在回上一句。");
    return;
  }
  setChatBusy(true);
  // 新的一句来了，上一轮「她没说话」就散了，别让状态越积越多。
  APP.hush = null;
  APP.chatPinned = true;
  if (userMessage) APP.chat.push(userMessage);
  const pending = {
    role: "pending",
    text: payload.image ? "她正在看。" : payload.audio ? "她正在听。" : "她正在想。",
  };
  APP.chat.push(pending);
  renderChatMessages();
  try {
    if (supportsChatStream()) {
      try {
        await sendChatStreaming(payload, pending, userMessage);
        return;
      } catch (err) {
        // 旧服务或老内核没有流式：退回整段回复，别让对话直接断掉。
        if (!(err?.code === "STREAM_UNSUPPORTED" || err?.status === 404)) throw err;
      }
    }
    await sendChatLegacy(payload, pending, userMessage);
  } catch (err) {
    APP.chat = APP.chat.filter((m) => m !== pending);
    APP.chat.push({ role: "error", text: err.message || "这一轮没接上。" });
    if (err.code === "PAIR_REQUIRED") openPairing();
  } finally {
    setChatBusy(false);
    renderChatMessages();
    renderDialogue();
  }
}

function sendChatText() {
  const input = $("#chatInput");
  const text = input ? input.value.trim() : "";
  if (!text) return;
  // 她还在回上一句：留住你打的字，别清空一句没发出去的话。
  if (APP.chatBusy) {
    toast("她还在回上一句。");
    return;
  }
  if (input) input.value = "";
  sendChatPayload({ text }, { role: "me", text });
}

/* ---------- 语音输入 ---------- */

function mergeFloat32(chunks) {
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const out = new Float32Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

function downsampleTo16k(samples, inputRate) {
  const target = 16000;
  if (Math.abs(inputRate - target) < 1) return samples;
  const ratio = inputRate / target;
  const length = Math.max(1, Math.floor(samples.length / ratio));
  const out = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const pos = i * ratio;
    const index = Math.floor(pos);
    const next = Math.min(samples.length - 1, index + 1);
    const frac = pos - index;
    out[i] = samples[index] * (1 - frac) + samples[next] * frac;
  }
  return out;
}

function encodeWav(samples, inputRate) {
  const pcm = downsampleTo16k(samples, inputRate);
  const buffer = new ArrayBuffer(44 + pcm.length * 2);
  const view = new DataView(buffer);
  const writeText = (offset, text) => {
    for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
  };
  writeText(0, "RIFF");
  view.setUint32(4, 36 + pcm.length * 2, true);
  writeText(8, "WAVE");
  writeText(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true);
  view.setUint32(28, 16000 * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeText(36, "data");
  view.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i += 1) {
    const s = Math.max(-1, Math.min(1, pcm[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buffer;
}

function bytesToBase64(bytes) {
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function updateRecordingClock() {
  const rec = APP.recorder;
  if (!rec) return;
  const elapsed = (performance.now() - rec.started) / 1000;
  const clock = $("#recordTime");
  if (clock) clock.textContent = `${elapsed.toFixed(1)}s`;
}

async function startRecording() {
  if (!mediaSupported()) {
    toast(mediaErrorMessage({ name: "SecurityError" }, "麦克风"));
    return;
  }
  if (APP.recorder || APP.chatBusy) return;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
  } catch (err) {
    toast(mediaErrorMessage(err, "麦克风"));
    return;
  }
  const AudioCtor = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtor) {
    stream.getTracks().forEach((t) => t.stop());
    toast("这个 WebView 没有音频接口。");
    return;
  }
  let ctx;
  try {
    ctx = new AudioCtor();
    await ctx.resume();
    const source = ctx.createMediaStreamSource(stream);
    const processor = ctx.createScriptProcessor(4096, 1, 1);
    const mute = ctx.createGain();
    mute.gain.value = 0;
    const chunks = [];
    processor.onaudioprocess = (event) => {
      chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
    };
    source.connect(processor);
    processor.connect(mute);
    mute.connect(ctx.destination);
    const rec = {
      stream, ctx, source, processor, mute, chunks,
      started: performance.now(), timer: null, auto: null,
    };
    rec.timer = window.setInterval(updateRecordingClock, 100);
    rec.auto = window.setTimeout(() => stopRecording(true), 15000);
    APP.recorder = rec;
    $("#recordBar").hidden = false;
    $("#micButton").classList.add("is-recording");
    updateRecordingClock();
  } catch (err) {
    try { await ctx?.close(); } catch (e) { /* ignore */ }
    stream.getTracks().forEach((t) => t.stop());
    toast(`录音没开：${err?.message || "未知错误"}`);
  }
}

async function stopRecording(send = true) {
  const rec = APP.recorder;
  if (!rec) return;
  APP.recorder = null;
  window.clearInterval(rec.timer);
  window.clearTimeout(rec.auto);
  try { rec.source.disconnect(); } catch (e) { /* ignore */ }
  try { rec.processor.disconnect(); } catch (e) { /* ignore */ }
  try { rec.mute.disconnect(); } catch (e) { /* ignore */ }
  rec.stream.getTracks().forEach((t) => t.stop());
  const sampleRate = rec.ctx.sampleRate;
  try { await rec.ctx.close(); } catch (e) { /* ignore */ }
  $("#recordBar").hidden = true;
  $("#micButton").classList.remove("is-recording");
  if (!send) return;
  const samples = mergeFloat32(rec.chunks);
  if (samples.length < sampleRate * 0.3) {
    toast("太短了，再说长一点。");
    return;
  }
  const wav = encodeWav(samples, sampleRate);
  const b64 = bytesToBase64(new Uint8Array(wav));
  sendChatPayload({ audio: b64 }, { role: "me", text: "（语音）", kind: "voice" });
}

function toggleRecording() {
  if (APP.recorder) {
    stopRecording(true);
  } else {
    startRecording();
  }
}

/* ---------- 摄像头 ---------- */

async function getCameraStream() {
  try {
    return await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 960 } },
      audio: false,
    });
  } catch (err) {
    if (err?.name === "OverconstrainedError" || err?.name === "NotFoundError") {
      return navigator.mediaDevices.getUserMedia({ video: true, audio: false });
    }
    throw err;
  }
}

function releaseCamera() {
  if (!APP.camera?.stream) return;
  APP.camera.stream.getTracks().forEach((track) => track.stop());
  APP.camera = null;
}

function openCamera() {
  if (!mediaSupported()) {
    toast(mediaErrorMessage({ name: "SecurityError" }, "摄像头"));
    return;
  }
  if (!APP.health?.chat) {
    toast("对话没开。电脑上用 talk.cmd 启动。");
    return;
  }
  openSheet(`
    <div class="sheet-head">
      <h2 class="sheet-title">让她看</h2>
      <button class="sheet-close" type="button" data-close aria-label="关闭">×</button>
    </div>
    <p class="sheet-sub">这一眼只给这一轮看，不存手机，也不单独写成记忆。</p>
    <div class="camera-stage">
      <video id="cameraVideo" playsinline muted autoplay aria-label="摄像头预览"></video>
      <img id="cameraPreview" alt="刚拍下的照片" hidden>
      <p id="cameraStatus" class="camera-status">正在打开摄像头…</p>
    </div>
    <div class="camera-controls">
      <button id="cameraShoot" class="camera-shoot" type="button" aria-label="拍照" hidden></button>
      <button id="cameraRetake" class="camera-retake" type="button" hidden>重拍</button>
      <button id="cameraSend" class="camera-send" type="button" hidden>发给她</button>
    </div>
    <p class="camera-hint">照片会经电脑交给阿里云百炼的看图模型；她回完这一轮，照片就不留了。</p>
  `);
  const video = $("#cameraVideo");
  const preview = $("#cameraPreview");
  const status = $("#cameraStatus");
  const shoot = $("#cameraShoot");
  const retake = $("#cameraRetake");
  const send = $("#cameraSend");
  let dataUrl = "";

  getCameraStream().then((stream) => {
    APP.camera = { stream };
    video.srcObject = stream;
    video.play().catch(() => {});
    status.hidden = true;
    shoot.hidden = false;
  }).catch((err) => {
    status.textContent = mediaErrorMessage(err, "摄像头");
  });

  shoot.addEventListener("click", () => {
    if (!APP.camera?.stream) return;
    const canvas = document.createElement("canvas");
    const width = video.videoWidth || 960;
    const scale = Math.min(1, 1280 / width);
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round((video.videoHeight || 720) * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    dataUrl = canvas.toDataURL("image/jpeg", 0.72);
    preview.src = dataUrl;
    preview.hidden = false;
    video.hidden = true;
    shoot.hidden = true;
    retake.hidden = false;
    send.hidden = false;
  });

  retake.addEventListener("click", () => {
    dataUrl = "";
    preview.hidden = true;
    video.hidden = false;
    shoot.hidden = false;
    retake.hidden = true;
    send.hidden = true;
  });

  send.addEventListener("click", () => {
    if (!dataUrl) return;
    const sent = dataUrl;
    closeSheet();
    sendChatPayload({ image: sent.split(",")[1] }, {
      role: "me", text: "（照片）", image: sent,
    });
  });
}

/* ---------- 她的声音 ---------- */

function stopAudio() {
  if (APP.speechStream) {
    const streamer = APP.speechStream;
    APP.speechStream = null;
    try { streamer.stop(); } catch (e) { /* ignore */ }
  }
  if (APP.pcmPlayer) {
    try { APP.pcmPlayer.stop(); } catch (e) { /* ignore */ }
    APP.pcmPlayer = null;
  }
  if (APP.audio) {
    try { APP.audio.pause(); } catch (e) { /* ignore */ }
    APP.audio = null;
  }
  if (APP.ttsAbort) {
    APP.ttsAbort.abort();
    APP.ttsAbort = null;
  }
}

/* 先合成第一句，后面的句子在她说话时预取；不用等整段音频合成完才开口。 */
function speechChunks(text) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (!clean) return [];
  const sentences = clean.match(/[^。！？!?；;…]+[。！？!?；;…]*/g) || [clean];
  const chunks = [];
  const push = (s) => {
    const t = s.trim();
    if (t) chunks.push(t);
  };
  let current = "";
  for (const sentence of sentences) {
    const s = sentence.trim();
    if (!s) continue;
    if (current && current.length + s.length > 28) {
      push(current);
      current = s;
    } else {
      current += s;
    }
  }
  push(current);
  // 单句过长时只在逗号处切，避免一句拖三四秒才开口。
  return chunks.flatMap((chunk) => {
    if (chunk.length <= 28) return [chunk];
    const parts = chunk.match(/[^，,、]+[，,、]?/g) || [chunk];
    const out = [];
    let buf = "";
    for (const part of parts) {
      if (buf && buf.length + part.length > 28) {
        out.push(buf);
        buf = part;
      } else {
        buf += part;
      }
    }
    if (buf) out.push(buf);
    return out;
  });
}

function fetchSpeechChunk(text, voice, signal) {
  return api("/api/tts", {
    method: "POST",
    body: JSON.stringify({ text, voice }),
    signal,
  }).then((data) => new Audio(`data:${data.mime || "audio/mpeg"};base64,${data.audio}`));
}

function fetchSpeechStream(text, voice, signal) {
  const headers = { "Content-Type": "application/json" };
  if (APP.token) headers["X-Valkyrie-Token"] = APP.token;
  return fetch("/api/tts/stream", {
    method: "POST",
    headers,
    body: JSON.stringify({ text, voice }),
    signal,
  });
}

/* 24k PCM 边收边排：首块到就能发声，不用等整段 mp3 和 base64。 */
function createPcmPlayer() {
  const AudioCtor = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtor) return null;
  let ctx;
  try {
    ctx = new AudioCtor({ sampleRate: 24000 });
  } catch (e) {
    try { ctx = new AudioCtor(); } catch (e2) { return null; }
  }
  if (!ctx) return null;
  let playhead = 0;
  let stopped = false;
  let started = false;
  let plays = 0;
  const sources = new Set();
  try { ctx.resume?.().catch(() => {}); } catch (e) { /* ignore */ }

  return {
    get started() { return started; },
    get plays() { return plays; },
    async playResponse(response) {
      if (!response.body || !response.body.getReader) {
        throw new Error("这个 WebView 不支持流式音频。");
      }
      try { await ctx.resume?.(); } catch (e) { /* ignore */ }
      const reader = response.body.getReader();
      let carry = new Uint8Array(0);
      let lastEnd = Promise.resolve();
      try {
        while (!stopped) {
          const { value, done } = await reader.read();
          if (done) break;
          if (!value || !value.length) continue;
          let bytes;
          if (carry.length) {
            bytes = new Uint8Array(carry.length + value.length);
            bytes.set(carry, 0);
            bytes.set(value, carry.length);
          } else {
            bytes = value;
          }
          const even = bytes.length & ~1;
          carry = even === bytes.length ? new Uint8Array(0) : bytes.slice(even);
          const count = even >> 1;
          if (!count) continue;

          const buffer = ctx.createBuffer(1, count, 24000);
          const data = buffer.getChannelData(0);
          for (let i = 0; i < count; i += 1) {
            const lo = bytes[2 * i];
            const hi = bytes[2 * i + 1];
            let sample = lo | (hi << 8);
            if (sample & 0x8000) sample -= 0x10000;
            data[i] = sample / 32768;
          }
          const src = ctx.createBufferSource();
          src.buffer = buffer;
          src.connect(ctx.destination);
          const at = Math.max(ctx.currentTime + 0.02, playhead);
          playhead = at + buffer.duration;
          started = true;
          lastEnd = new Promise((resolve) => {
            src.onended = () => { sources.delete(src); resolve(); };
          });
          sources.add(src);
          plays += 1;
          src.start(at);
        }
      } finally {
        try { reader.releaseLock(); } catch (e) { /* ignore */ }
      }
      await lastEnd;
    },
    stop() {
      stopped = true;
      for (const src of sources) {
        try { src.stop(); } catch (e) { /* ignore */ }
      }
      sources.clear();
      try { ctx.close(); } catch (e) { /* ignore */ }
    },
  };
}

function playSpeechAudio(audio) {
  return new Promise((resolve, reject) => {
    const done = () => {
      audio.removeEventListener("ended", done);
      audio.removeEventListener("error", fail);
      resolve();
    };
    const fail = () => {
      audio.removeEventListener("ended", done);
      audio.removeEventListener("error", fail);
      reject(new Error("这一句没放出来。"));
    };
    audio.addEventListener("ended", done, { once: true });
    audio.addEventListener("error", fail, { once: true });
    audio.play().catch(fail);
  });
}

/* 边收边读：文本一到就按句合成，第一块 PCM 到了就排进 AudioContext。 */
function createSpeechStreamer(voiceId = "") {
  const voice = voiceId || selectedVoiceId();
  const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
  const queue = [];
  let busy = false;
  let stopped = false;
  let ended = false;
  let buffer = "";
  let player = null;

  const streamer = {
    push(text) {
      if (stopped) return;
      buffer += String(text || "");
      pump(false);
    },
    finish() {
      if (stopped) return;
      pump(true);
      ended = true;
      maybeClear();
    },
    stop() {
      stopped = true;
      ended = true;
      buffer = "";
      queue.length = 0;
      try { controller?.abort(); } catch (e) { /* ignore */ }
      if (player) {
        try { player.stop(); } catch (e) { /* ignore */ }
        if (APP.pcmPlayer === player) APP.pcmPlayer = null;
        player = null;
      }
    },
  };

  function ensurePlayer() {
    if (player) return player;
    player = createPcmPlayer();
    if (player) APP.pcmPlayer = player;
    return player;
  }

  function maybeClear() {
    if (ended && !busy && !queue.length && APP.speechStream === streamer) {
      APP.speechStream = null;
    }
  }

  function enqueue(text) {
    const clean = String(text || "").trim();
    if (!clean) return;
    const p = ensurePlayer();
    queue.push({
      text: clean,
      stream: !!p,
      pending: p
        ? fetchSpeechStream(clean, voice, controller?.signal)
        : fetchSpeechChunk(clean, voice, controller?.signal),
    });
    drain();
  }

  async function drain() {
    if (busy) return;
    busy = true;
    try {
      while (!stopped && queue.length) {
        const item = queue.shift();
        let audio = null;
        const before = player ? player.plays : 0;
        try {
          if (item.stream && player) {
            const response = await item.pending;
            if (!response.ok) {
              throw new Error(`流式 TTS 返回 ${response.status}`);
            }
            await player.playResponse(response);
            continue;
          }
          audio = await item.pending;
          if (stopped) return;
          APP.audio = audio;
          await playSpeechAudio(audio);
        } catch (err) {
          if (stopped) return;
          // 首块前失败才回退到旧的整段 /api/tts；已经出过声不能重播。
          if (item.stream && player && player.plays > before) continue;
          try {
            audio = await fetchSpeechChunk(item.text, voice, controller?.signal);
            if (stopped) return;
            APP.audio = audio;
            await playSpeechAudio(audio);
          } catch (e2) {
            if (stopped) return;
          }
        } finally {
          if (APP.audio === audio) APP.audio = null;
        }
      }
    } finally {
      busy = false;
      maybeClear();
    }
  }

  function pump(final) {
    while (true) {
      const match = buffer.match(/^([\s\S]*?[。！？!?；;…]+)/);
      if (!match) break;
      buffer = buffer.slice(match[0].length);
      enqueue(match[0]);
    }
    if (buffer.length >= 32) {
      const cut = Math.max(
        buffer.lastIndexOf("，"), buffer.lastIndexOf(","), buffer.lastIndexOf("、"));
      if (cut > 0 && cut < buffer.length - 1) {
        const head = buffer.slice(0, cut + 1);
        buffer = buffer.slice(cut + 1);
        enqueue(head);
      } else if (buffer.length >= 40) {
        const head = buffer.slice(0, 40);
        buffer = buffer.slice(40);
        enqueue(head);
      }
    }
    if (final && buffer.trim()) {
      enqueue(buffer);
      buffer = "";
    }
  }

  APP.speechStream = streamer;
  return streamer;
}

async function speakReply(text, manual = false) {
  if (!text) return;
  if (!manual && !APP.voiceOn) return;
  stopAudio();
  const chunks = speechChunks(text);
  if (!chunks.length) return;
  const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
  APP.ttsAbort = controller;
  const voice = selectedVoiceId();
  let next = fetchSpeechChunk(chunks[0], voice, controller?.signal);
  try {
    for (let i = 0; i < chunks.length; i += 1) {
      const audio = await next;
      if (controller?.signal.aborted) return;
      APP.audio = audio;
      if (i + 1 < chunks.length) {
        next = fetchSpeechChunk(chunks[i + 1], voice, controller?.signal);
        next.catch(() => {});
      }
      await playSpeechAudio(audio);
      if (APP.audio === audio) APP.audio = null;
    }
  } catch (err) {
    if (manual && err?.name !== "AbortError") toast(err?.message || "这声没出来。");
  } finally {
    if (APP.ttsAbort === controller) APP.ttsAbort = null;
    if (APP.audio) APP.audio = null;
  }
}

function toggleVoice() {
  APP.voiceOn = !APP.voiceOn;
  localStorage.setItem("valkyrie.voice", APP.voiceOn ? "on" : "off");
  renderVoiceToggle();
  if (!APP.voiceOn) stopAudio();
}

/* ---------- 配对 ---------- */

let pairingOpen = false;

function openPairing() {
  if (pairingOpen) return;
  pairingOpen = true;
  openSheet(`
    <div class="sheet-head">
      <h2 class="sheet-title">把她接上</h2>
      <button class="sheet-close" type="button" data-close aria-label="关闭">×</button>
    </div>
    <p class="sheet-sub">输入电脑上 <code>pocket.cmd</code> 打印的配对码。它只在这台手机和那台电脑之间用。</p>
    <form id="pairForm" class="code-row">
      <input id="pairInput" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="64" placeholder="六位数字" value="${esc(APP.token)}" aria-label="配对码">
      <button class="button button-primary button-small" type="submit">接上</button>
    </form>
    <p id="pairError" class="notice is-danger" hidden></p>
    <p class="section-note" style="margin-top:18px">手机和电脑在同一个 Wi-Fi，或者 Android 走 USB 反向转发。服务只在你的局域网里。</p>
  `);
  setTimeout(() => $("#pairInput")?.focus(), 180);
  const form = $("#pairForm");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = form.elements[0]?.value?.trim() || $("#pairInput").value.trim();
    const error = $("#pairError");
    if (!code) {
      error.hidden = false;
      error.textContent = "配对码不能为空。";
      return;
    }
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const response = await fetch("/api/health", {
        headers: { "X-Valkyrie-Token": code },
        cache: "no-store",
      });
      const data = await response.json();
      if (!data.paired) {
        error.hidden = false;
        error.textContent = "配对码不对。看电脑窗口里打印的那六位。";
        return;
      }
      storeToken(code);
      closeSheet();
      pairingOpen = false;
      APP.paired = true;
      await loadData();
      toast("接上了。");
    } catch (err) {
      error.hidden = false;
      error.textContent = "找不到那台电脑。先确认服务开着。";
    } finally {
      submit.disabled = false;
    }
  });
}

/* ---------- sheet ---------- */

function openSheet(html) {
  const root = $("#sheetRoot");
  root.innerHTML = `
    <div class="sheet-backdrop" data-backdrop>
      <section class="sheet" role="dialog" aria-modal="true" aria-label="操作面板">
        <div class="sheet-handle" aria-hidden="true"></div>
        ${html}
      </section>
    </div>`;
  document.body.classList.add("sheet-open");
  const backdrop = $("[data-backdrop]", root);
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) closeSheet();
  });
  $$("[data-close]", root).forEach((button) => {
    button.addEventListener("click", closeSheet);
  });
}

function closeSheet() {
  releaseCamera();
  $("#sheetRoot").innerHTML = "";
  document.body.classList.remove("sheet-open");
  pairingOpen = false;
}

/* ---------- 工具 ---------- */

function formatDay(day, ts) {
  if (day) {
    const d = new Date(`${day}T00:00:00`);
    if (!Number.isNaN(d.getTime())) {
      return `${d.getMonth() + 1}月${d.getDate()}日 · ${weekday(d)}`;
    }
  }
  if (ts) {
    const d = new Date(Number(ts) * 1000);
    return `${d.getMonth() + 1}月${d.getDate()}日 · ${weekday(d)}`;
  }
  return "未标日期";
}

function formatDayShort(day, ts) {
  if (day) {
    const d = new Date(`${day}T00:00:00`);
    if (!Number.isNaN(d.getTime())) return `${d.getMonth() + 1}.${String(d.getDate()).padStart(2, "0")}`;
  }
  if (ts) {
    const d = new Date(Number(ts) * 1000);
    return `${d.getMonth() + 1}.${String(d.getDate()).padStart(2, "0")}`;
  }
  return "";
}

function formatClock(ts) {
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return "";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function weekday(d) {
  return ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][d.getDay()] || "";
}

function claimOptions() {
  return CLAIM_OPTIONS.map(([value, label], index) =>
    `<option value="${value}"${index === 0 ? " selected" : ""}>${label}</option>`).join("");
}

function regionOptions() {
  return REGION_OPTIONS.map(([value, label], index) =>
    `<option value="${value}"${index === 0 ? " selected" : ""}>${label}</option>`).join("");
}

/* ---------- 事件 ---------- */

function bindEvents() {
  document.addEventListener("click", (event) => {
    const nav = event.target.closest("[data-nav]");
    if (nav) {
      event.preventDefault();
      switchView(nav.dataset.nav);
      return;
    }
    const action = event.target.closest("[data-action]");
    if (action) {
      event.preventDefault();
      const kind = action.dataset.action;
      if (kind === "add-memory") openAddMemory();
      else if (kind === "add-fact") openAddFact();
      else if (kind === "chat") openChat();
      return;
    }
    const preAnswerRetract = event.target.closest("[data-preanswer-retract]");
    if (preAnswerRetract) {
      event.preventDefault();
      retractPreAnswer(preAnswerRetract.dataset.preanswerRetract, preAnswerRetract);
      return;
    }
    const speak = event.target.closest("[data-speak-index]");
    if (speak) {
      event.preventDefault();
      const message = APP.chat[Number(speak.dataset.speakIndex)];
      if (message?.text) speakReply(message.text, true);
      return;
    }
    const memory = event.target.closest("[data-memory-id]");
    if (memory) {
      event.preventDefault();
      openMemoryDetail(memory.dataset.memoryId);
      return;
    }
    const factDelete = event.target.closest("[data-fact-delete]");
    if (factDelete) {
      event.preventDefault();
      deleteFact(factDelete.dataset.factDelete);
      return;
    }
    const detailAction = event.target.closest("[data-detail-action]");
    if (detailAction) {
      event.preventDefault();
      const kind = detailAction.dataset.detailAction;
      const id = Number(detailAction.dataset.id);
      const item = APP.detailItem && Number(APP.detailItem.id) === id
        ? APP.detailItem
        : APP.memory.items.find((m) => Number(m.id) === id);
      if (item && kind === "correct") openCorrect(item);
      if (item && kind === "retract") openRetract(item);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeSheet();
  });

  $("#chatForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    sendChatText();
  });
  $("#micButton")?.addEventListener("click", toggleRecording);
  $("#stopRecord")?.addEventListener("click", () => stopRecording(true));
  $("#cameraButton")?.addEventListener("click", openCamera);
  $("#voiceToggle")?.addEventListener("click", toggleVoice);
  $("#voiceSelect")?.addEventListener("click", openVoicePicker);

  $("#memorySearch").addEventListener("input", debounce((event) => {
    APP.memory.query = event.target.value.trim();
    searchMemory();
  }, 280));

  /* 聊天流：贴着底部才自动跟下去；往上翻旧话时别被拽回来。 */
  const chatLog = $("#chatLog");
  if (chatLog) {
    chatLog.addEventListener("scroll", () => {
      APP.chatPinned = chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight < 56;
    }, { passive: true });
  }

  /* 输入栏与键盘：输入框聚焦时收起底部导航，别让导航条压住正在打的字。 */
  document.addEventListener("focusin", (event) => {
    if (!event.target?.closest?.("#chatForm")) return;
    document.body.classList.add("compose-focused");
    stickChatToBottom(chatLog);
  });
  document.addEventListener("focusout", () => {
    window.setTimeout(() => {
      if (!document.activeElement?.closest?.("#chatForm")) {
        document.body.classList.remove("compose-focused");
      }
    }, 60);
  });
  setupViewportInsets();

  $$(".filter").forEach((button) => {
    button.addEventListener("click", () => {
      $$(".filter").forEach((b) => b.classList.toggle("is-active", b === button));
      APP.memoryFilter = button.dataset.filter || "all";
      renderMemory();
    });
  });

  $("#loadMore").addEventListener("click", loadMoreMemory);

  $("#mirror").addEventListener("click", () => {
    if (APP.health?.chat) openChat();
    else switchView("memory");
  });

  $("#saveToken").addEventListener("click", async () => {
    const value = $("#tokenInput").value.trim();
    if (!value) {
      toast("先填配对码。");
      return;
    }
    storeToken(value);
    toast("试试看。");
    await refreshAll();
  });

  $("#testConnection").addEventListener("click", async () => {
    toast("正在找她。");
    await refreshAll();
    toast(APP.online && APP.paired ? "接上了。" : "还没接上。看电脑窗口里的地址。");
  });

  $("#copyAddress").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(location.origin);
      toast("地址复制了。");
    } catch (err) {
      toast(location.origin);
    }
  });

  const installButton = $("#installWeb");
  if (installButton) {
    const standalone = window.matchMedia?.("(display-mode: standalone)")?.matches
      || window.navigator.standalone;
    if (standalone) installButton.hidden = true;
    installButton.addEventListener("click", async () => {
      if (!installPrompt) {
        toast("这个浏览器没给安装入口；用 web.cmd 打开也能一直用。");
        return;
      }
      installPrompt.prompt();
      const choice = await installPrompt.userChoice.catch(() => null);
      if (choice?.outcome === "accepted") toast("装好了。");
      installPrompt = null;
      installButton.hidden = true;
    });
  }
  window.addEventListener("appinstalled", () => {
    const button = $("#installWeb");
    if (button) button.hidden = true;
    toast("装好了。下次从桌面直接打开。");
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (APP.recorder) stopRecording(false);
      releaseCamera();
      stopAudio();
    } else {
      if (APP.token && !APP.online) refreshAll({ quiet: true });
      pollProactive();
    }
  });

  window.addEventListener("beforeunload", () => {
    if (APP.recorder) stopRecording(false);
    releaseCamera();
    stopAudio();
  });

  window.setInterval(() => {
    if (document.hidden || !APP.token) return;
    // 服务端重启后，页面不能一直停在「还没连上」；离线时也要自己回头找她。
    if (APP.online) loadData({ quiet: true });
    else refreshAll({ quiet: true });
  }, 15000);
}

function switchView(view) {
  const allowed = new Set(["home", "dialogue", "memory", "connect"]);
  const leavingDialogue = APP.view === "dialogue" && view !== "dialogue";
  APP.view = allowed.has(view) ? view : "home";
  $$(".view").forEach((section) => { section.hidden = section.id !== `view-${APP.view}`; });
  $$(".tab").forEach((tab) => tab.classList.toggle("is-active", tab.dataset.nav === APP.view));
  // 换页就把「她这次没接」收掉：那只是一轮的事，不该跟着走到别的页。
  if (leavingDialogue) APP.hush = null;
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (APP.view === "dialogue") renderDialogue();
  else renderHush();
  // 冷启动直接从 #memory 进来时，先给骨架，别让人对着一片空白等。
  if (APP.view === "memory" && !APP.memoryLoaded) renderMemory();
  if (APP.view === "home") renderStateQuiet();
  if (APP.view === "memory") {
    setTimeout(() => $("#memorySearch")?.focus({ preventScroll: true }), 220);
  }
}

async function searchMemory() {
  APP.memory.query = $("#memorySearch").value.trim();
  try {
    const data = await api(`/api/memory?limit=60&offset=0&q=${encodeURIComponent(APP.memory.query)}`);
    APP.memory.items = data.items || [];
    APP.memory.total = data.total || 0;
    APP.memory.offset = APP.memory.items.length;
    renderMemory();
  } catch (err) {
    toast(err.message);
  }
}

async function loadMoreMemory() {
  try {
    const data = await api(`/api/memory?limit=60&offset=${APP.memory.offset}&q=${encodeURIComponent(APP.memory.query)}`);
    APP.memory.items = APP.memory.items.concat(data.items || []);
    APP.memory.total = data.total || APP.memory.total;
    APP.memory.offset = APP.memory.items.length;
    renderMemory();
  } catch (err) {
    toast(err.message);
  }
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

/* Android 壳在 onPause 时调这个，把看不见的麦克风/摄像头收掉。 */
window.__valkyrieReleaseMedia = () => {
  if (APP.recorder) stopRecording(false);
  releaseCamera();
  stopAudio();
};

/* ---------- boot ---------- */

async function boot() {
  if ("serviceWorker" in navigator) {
    const local = location.hostname === "127.0.0.1" || location.hostname === "localhost";
    if (location.protocol === "https:" || local) {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    }
  }

  const params = new URLSearchParams(location.search);
  const urlToken = params.get("token");
  if (urlToken) {
    storeToken(urlToken);
    params.delete("token");
    const suffix = params.toString();
    history.replaceState(null, "", `${location.pathname}${suffix ? `?${suffix}` : ""}${location.hash}`);
  }

  bindEvents();
  startProactivePolling();
  const hashView = location.hash.replace("#", "");
  if (["home", "dialogue", "memory", "connect"].includes(hashView)) switchView(hashView);
  else switchView("home");

  const health = await refreshHealth();
  if (!APP.token) {
    APP.memoryLoaded = true;
    renderAll();
    openPairing();
    APP.booted = true;
    return;
  }
  if (!health) {
    // 离线壳能打开，但电脑服务没响应：不要把「找不到服务」误报成「配对码错了」。
    APP.memoryLoaded = true;
    renderAll();
    APP.booted = true;
    return;
  }
  if (!APP.health?.paired) {
    APP.memoryLoaded = true;
    renderAll();
    openPairing();
    APP.booted = true;
    return;
  }
  await loadData();
  await loadProactiveRecent();
  if (!APP.online) toast("服务在，但数据没读出来。");
  APP.booted = true;
}

boot();
