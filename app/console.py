"""内置 Web 控制台：单文件 HTML/JS/CSS，无 CDN、无构建依赖。

GET /console 直接返回本页面；数据全部来自既有只读接口（/health /config
/tasks /task_result）、热调接口（/admin/config）与配置管理接口
（/admin/settings、/admin/restart）。设计约束：
- 不引外部资源，离线可用（与项目零依赖哲学一致）；
- 不写任何业务逻辑，只是接口的薄视图；
- 设了 ADMIN_TOKEN 时，修改类操作带 X-Admin-Token 头（由页面提示输入）。
"""


def console_page() -> str:
    return _PAGE


_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>explore 控制台</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--line:#334155;--fg:#e2e8f0;--dim:#94a3b8;
--acc:#38bdf8;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 "Segoe UI","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;gap:12px;padding:12px 20px;
background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
header h1{font-size:16px;margin:0}
header .ver{color:var(--dim);font-size:12px}
header .sp{flex:1}
button{background:#334155;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:5px 12px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--acc)}
button.pri{background:var(--acc);color:#06283a;border-color:var(--acc);font-weight:600}
button.danger{background:transparent;color:var(--bad);border-color:var(--bad)}
button.sm{padding:3px 9px;font-size:12px}
nav{display:flex;gap:4px;padding:8px 20px;background:var(--card);
border-bottom:1px solid var(--line);flex-wrap:wrap}
nav button{border:none;background:transparent;color:var(--dim);padding:6px 12px}
nav button.on{color:var(--acc);border-bottom:2px solid var(--acc);border-radius:0}
main{padding:16px 20px;max-width:1180px;margin:0 auto}
section{display:none}
section.on{display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
.card h3{margin:0 0 6px;font-size:12px;color:var(--dim);font-weight:500}
.card .v{font-size:18px;font-weight:600}
.card .s{font-size:12px;color:var(--dim)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.acc{color:var(--acc)}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:8px;overflow:hidden;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:500;background:#182234;font-size:12px}
tr:last-child td{border-bottom:none}
input,textarea,select{background:#0f172a;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:5px 8px;font:13px/1.4 inherit;width:100%}
textarea{resize:vertical;min-height:52px;font-family:Consolas,monospace}
.badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;
border:1px solid var(--line);color:var(--dim);white-space:nowrap}
.badge.changed,.badge.overlay{color:var(--warn);border-color:var(--warn)}
.badge.env{color:var(--acc);border-color:var(--acc)}
.badge.resident{color:var(--ok);border-color:var(--ok)}
.hint{background:#182234;border:1px solid var(--line);border-radius:8px;
padding:10px 12px;color:var(--dim);font-size:12px;margin:10px 0;line-height:1.7}
.group-title{margin:20px 0 8px;font-size:14px;color:var(--acc)}
.group-title small{color:var(--dim);font-size:12px;margin-left:8px}
.slotcard{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:12px;margin:10px 0}
.slotcard .head{display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap}
.slotcard .head b{font-size:15px}
.frow{display:grid;grid-template-columns:180px 1fr 200px;gap:10px;align-items:start;
padding:7px 0;border-top:1px dashed var(--line)}
.frow .fname b{display:block}
.frow .fname small{color:var(--dim);font-size:11px;line-height:1.5;display:block;margin-top:2px}
.frow .fops{display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap}
.desc{color:var(--dim);font-size:12px}
pre{background:#0b1220;border:1px solid var(--line);border-radius:8px;
padding:10px;white-space:pre-wrap;word-break:break-all;font-size:12px;max-height:340px;overflow:auto}
.bubble{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:10px;margin:6px 0;white-space:pre-wrap;word-break:break-word}
.bubble .who{font-size:11px;color:var(--dim);margin-bottom:4px}
#toast{position:fixed;right:16px;bottom:16px;background:var(--card);
border:1px solid var(--acc);border-radius:8px;padding:10px 14px;display:none;z-index:9;
max-width:460px;font-size:13px}
#mask{position:fixed;inset:0;background:rgba(2,6,23,.6);display:none;z-index:8}
#mask.on{display:block}
.tasklink{color:var(--acc);cursor:pointer;text-decoration:underline}
</style>
</head>
<body>
<header>
  <h1>explore 智能路由控制台</h1><span class="ver" id="ver"></span><span class="sp"></span>
  <span class="desc" id="uptime"></span>
  <button class="danger" onclick="doRestart()">重启服务</button>
  <button onclick="refreshAll()">刷新全部</button>
</header>
<nav id="nav">
  <button data-t="ov" class="on">总览</button>
  <button data-t="pm">参数调整<span style="font-size:10px">（热生效）</span></button>
  <button data-t="cf">配置管理<span style="font-size:10px">（重启生效）</span></button>
  <button data-t="sl">槽位与显存</button>
  <button data-t="tk">任务</button>
  <button data-t="ch">对话试用</button>
</nav>
<main>

<section id="s-ov" class="on">
  <div class="cards" id="ov-cards"></div>
  <div class="hint">前台 = 常驻即时回复模型；slot.current = 当前驻留的本地后台槽位；
  api 槽位（线上模型）不占显存、不参与互斥切换。
  <b>改配置去哪：</b>运行期参数（阈值/词表/输出上限等）在「参数调整」，立即生效；
  槽位/模型/端口/num_ctx 等启动期参数在「配置管理」，保存后点右上角「重启服务」生效。</div>
  <pre id="ov-raw"></pre>
</section>

<section id="s-pm">
  <div class="hint">以下参数<b>立即生效</b>并持久化到 <code>settings.local.yaml</code>（重启后仍生效；
  环境变量优先级更高）。「已改」徽标表示与基础值不同，点「重置」恢复基础值。
  槽位/模型/端口/num_ctx 等启动期参数在「<b>配置管理</b>」面板修改。</div>
  <div id="pm-list"></div>
</section>

<section id="s-cf">
  <div class="hint" id="cf-note"></div>
  <div class="row">
    <button class="pri" onclick="showAddSlot()">＋ 添加槽位</button>
    <span class="desc">换 API / 换本地模型：直接改下面对应槽位的字段 → 保存 → 重启服务。
    所有字段都有说明；「覆盖层」徽标 = 该值来自控制台的修改（可一键移除恢复 settings.yaml 原值）。</span>
  </div>
  <div id="add-slot" style="display:none" class="slotcard">
    <div class="head"><b>新槽位</b></div>
    <div class="frow"><div class="fname"><b>槽位 key</b><small>标识符（字母/数字/-/_），如 cloud2</small></div>
      <div><input id="ns-key"></div><div></div></div>
    <div class="frow"><div class="fname"><b>type</b><small>api = 线上模型；local = 本地 GGUF</small></div>
      <div><select id="ns-type" onchange="nsRender()"><option value="api">api（线上）</option>
        <option value="local">local（本地）</option></select></div><div></div></div>
    <div id="ns-fields"></div>
    <div class="row"><button class="pri" onclick="addSlot()">创建槽位</button>
      <button onclick="document.getElementById('add-slot').style.display='none'">取消</button></div>
  </div>
  <div id="cf-list"></div>
</section>

<section id="s-sl">
  <div class="group-title">槽位</div>
  <table id="sl-table"><thead><tr>
    <th>key</th><th>type</th><th>role</th><th>model</th><th>devices</th>
    <th>quant</th><th>resident</th><th>api 信息</th></tr></thead><tbody></tbody></table>
  <div class="group-title">显存规划 vram_plan</div>
  <table id="vp-table"><thead><tr>
    <th>槽位</th><th>vram_mb</th><th>available</th><th>reason</th></tr></thead><tbody></tbody></table>
  <div class="hint">api 槽位恒可用（无本地显存）；本地槽位装不下时标记不可用，
  对应档位任务沿 tier_fallbacks → default_slot → 前台 自动降级。</div>
</section>

<section id="s-tk">
  <div class="row"><button class="pri" onclick="loadTasks()">刷新</button>
  <span class="desc">点击 task_id 查看详情与最终结果</span></div>
  <table id="tk-table"><thead><tr>
    <th>task_id</th><th>tier</th><th>chosen_model</th><th>status</th>
    <th>finish</th><th>进度</th><th>时间</th></tr></thead><tbody></tbody></table>
  <div class="group-title">任务详情</div>
  <pre id="tk-detail">（点击上方 task_id 查看）</pre>
</section>

<section id="s-ch">
  <div class="row"><label>会话</label><input id="ch-session" style="width:220px"></div>
  <div class="row"><label>档位</label><select id="ch-tier" style="width:180px">
    <option value="">自动分级</option></select>
  <label>等待</label><select id="ch-wait" style="width:140px">
    <option value="0">不等（只看即时回复）</option>
    <option value="120" selected>等后台 120 秒</option></select>
  <button class="pri" onclick="sendChat()">发送</button></div>
  <div class="row"><textarea id="ch-text"
    placeholder="输入消息：短句走前台直答；含「详细/分析/方案」等词或长文本走后台槽位"></textarea></div>
  <div class="bubble"><div class="who">instant_reply（前台即时回复）</div>
    <div id="ch-instant">—</div></div>
  <div class="bubble"><div class="who">final_reply（后台结果，原样递回）</div>
    <div id="ch-final">—</div></div>
  <pre id="ch-meta"></pre>
</section>

</main>
<div id="mask"></div>
<div id="toast"></div>
<script>
let ADMIN = sessionStorage.getItem("explore_admin_token") || "";
let CFG = null;
// 本次会话启动前已记录的服务 started_at：重启确认用它判断「确实是新实例」
let LAST_STARTED_AT = null;

function toast(msg, bad, ms) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.style.display = "block";
  t.style.borderColor = bad ? "var(--bad)" : "var(--acc)";
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", ms || 4000);
}
function busy(on, text) {
  document.getElementById("mask").classList.toggle("on", !!on);
  if (on && text) toast(text, false, 60000);
}
async function api(path, opts = {}) {
  const headers = Object.assign({"Content-Type": "application/json"}, opts.headers || {});
  if (ADMIN) headers["X-Admin-Token"] = ADMIN;
  const r = await fetch(path, Object.assign({headers}, opts));
  if (r.status === 401) {
    const tok = prompt("已启用 ADMIN_TOKEN，请输入管理令牌：");
    if (tok) { ADMIN = tok; sessionStorage.setItem("explore_admin_token", tok);
      return api(path, opts); }
    throw new Error("需要管理令牌");
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || (r.status + " " + r.statusText));
  return data;
}
function esc(s) {
  // textContent→innerHTML 只转义 & < >；属性上下文（value="..."）与 JS 字符串
  // 上下文（onclick='...'）还需要转义引号，否则配置值/槽位 key 可破出引号注入
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

document.getElementById("nav").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll("#nav button").forEach(x => x.classList.toggle("on", x === b));
  document.querySelectorAll("main section").forEach(s =>
    s.classList.toggle("on", s.id === "s-" + b.dataset.t));
  const loaders = {ov: loadHealth, pm: loadAdmin, cf: loadSettings,
                   sl: loadSlots, tk: loadTasks};
  const fn = loaders[b.dataset.t];
  if (fn) fn().catch(err => toast(err.message, true));
});

async function loadHealth() {
  const h = await api("/health");
  const c = h.config || {};
  if (h.started_at != null) LAST_STARTED_AT = h.started_at;
  document.getElementById("ver").textContent =
    (c.tier_order || []).length + " 档 · 后端 " + (c.backend || "?");
  if (h.uptime_s != null) {
    const s = h.uptime_s;
    document.getElementById("uptime").textContent =
      "运行 " + (s >= 3600 ? Math.floor(s / 3600) + "h" : "") +
      Math.floor((s % 3600) / 60) + "m" + (s % 60) + "s";
  }
  const fe = h.frontend === "ready";
  const cards = [
    ["前台", fe ? "ready" : "down", fe ? "ok" : "bad",
      (c.front_type || "local") + " · " + (c.front_slot || "?") + " · 窗口 " + (c.front_num_ctx || c.num_ctx)],
    ["后端", c.backend || "?", "", "GPU " + (c.gpu_count ?? 0) + " · " + (c.total_vram_mb || 0) + " MB"],
    ["num_ctx", c.num_ctx || "?", "", "来源 " + (c.num_ctx_source || "?")],
    ["队列", h.queue_depth ?? 0, (h.queue_depth || 0) > 0 ? "warn" : "ok", "上限 " + ((c.limits || {}).queue_maxsize ?? "?")],
    ["后台槽位", (h.slot || {}).current || "idle", "acc",
      "切换 " + ((h.slot || {}).switches ?? 0) + " 次"
      + (((h.slot || {}).api_slots_active || []).length
        ? " · api 活跃 " + ((h.slot || {}).api_slots_active || []).join(",")
        : "")],
    ["显存", (h.vram || {}).free_mb ?? "?", "", "空闲 MB / 共 " + ((h.vram || {}).total_mb ?? "?") + " MB"],
    ["嵌入", ((h.embedding || {}).enabled) ? ((h.embedding || {}).dim + " 维") : "降级",
      ((h.embedding || {}).enabled) ? "ok" : "warn",
      (h.embedding || {}).error
        || ((h.embedding || {}).provider === "ollama"
          ? "ollama " + ((h.embedding || {}).model || "")
          : "ONNX 语义线索")],
    ["档位", (c.tier_order || []).join(" → "), "", Object.keys(c.tier_thresholds || {}).map(k => k + ":" + c.tier_thresholds[k]).join(" ")],
  ];
  document.getElementById("ov-cards").innerHTML = cards.map(([t, v, cls, sub]) =>
    `<div class="card"><h3>${esc(t)}</h3><div class="v ${cls}">${esc(v)}</div><div class="s">${esc(sub)}</div></div>`).join("");
  document.getElementById("ov-raw").textContent = JSON.stringify(
    {backend: c.backend, front: {slot: c.front_slot, type: c.front_type, num_ctx: c.front_num_ctx},
     slot: h.slot, queue_depth: h.queue_depth, openai: c.openai, limits: c.limits}, null, 2);
  return h;
}

async function loadAdmin() {
  CFG = await api("/admin/config");
  const groups = {};
  for (const [k, meta] of Object.entries(CFG.hot_params || {}))
    (groups[meta.group] = groups[meta.group] || {})[k] = meta;
  const order = ["routing", "frontend", "compaction", "runtime", "openai"];
  let html = "";
  for (const g of order) {
    if (!groups[g]) continue;
    html += `<div class="group-title">${esc(g)} <small>热生效</small></div><table><tbody>`;
    for (const [k, m] of Object.entries(groups[g])) {
      const isStruct = typeof m.current === "object" && m.current !== null;
      const changed = JSON.stringify(m.current) !== JSON.stringify(m.base);
      const val = isStruct ? JSON.stringify(m.current) : String(m.current);
      html += `<tr><td style="min-width:220px"><b>${esc(k)}</b><br>
        <span style="color:var(--dim);font-size:11px">${esc(m.label || "")}</span></td>
        <td>${isStruct
          ? `<textarea id="pm-${k}">${esc(val)}</textarea>`
          : `<input id="pm-${k}" value="${esc(val)}">`}</td>
        <td style="min-width:150px">
          ${changed ? '<span class="badge changed">已改</span> ' : '<span class="badge">基础值</span> '}
          <div style="color:var(--dim);font-size:11px;margin-top:4px">base: ${esc(typeof m.base === "object" ? JSON.stringify(m.base) : m.base)}</div>
          <div class="row" style="margin-top:6px">
            <button class="pri sm" onclick="saveParam('${k}')">保存</button>
            ${changed ? `<button class="danger sm" onclick="resetParam('${k}')">重置</button>` : ""}
          </div></td></tr>`;
    }
    html += "</tbody></table>";
  }
  document.getElementById("pm-list").innerHTML = html;
}

async function saveParam(k) {
  const el = document.getElementById("pm-" + k);
  let v = el.value;
  try { v = JSON.parse(el.value); } catch (e) { /* 保持字符串 */ }
  try {
    const r = await api("/admin/config", {method: "POST", body: JSON.stringify({[k]: v})});
    toast("已生效并持久化: " + k + " = " + JSON.stringify(r.applied[k]));
    loadAdmin();
  } catch (e) { toast(e.message, true); }
}
async function resetParam(k) {
  try {
    await api("/admin/config/" + k, {method: "DELETE"});
    toast("已恢复基础值: " + k);
    loadAdmin();
  } catch (e) { toast(e.message, true); }
}

/* ---- 配置管理（启动期参数，重启生效）---- */
const GROUP_TITLES = {
  slots: "槽位 slots", runtime: "运行时 runtime", context: "上下文 context",
  embedding: "嵌入 embedding", budget: "显存预算 budget", routing: "路由 routing"};

function badge(src) {
  if (src === "env") return '<span class="badge env">env</span>';
  if (src === "overlay") return '<span class="badge overlay">覆盖层</span>';
  return '<span class="badge">settings.yaml</span>';
}
function fieldInput(f) {
  const id = "cf-" + f.path;
  const v = f.value;
  if (f.type === "bool")
    return `<select id="${id}"><option value="true"${v === true ? " selected" : ""}>true</option>
      <option value="false"${v === false ? " selected" : ""}>false</option></select>`;
  if (f.type === "list" || f.type === "dict")
    return `<textarea id="${id}">${esc(JSON.stringify(v ?? (f.type === "list" ? [] : {})))}</textarea>`;
  return `<input id="${id}" value="${esc(v ?? "")}">`;
}
function parseField(f) {
  const el = document.getElementById("cf-" + f.path);
  const raw = el.value;
  if (f.type === "bool") return raw === "true";
  if (f.type === "int") { const n = parseInt(raw, 10);
    if (isNaN(n)) throw new Error(f.path + " 需要整数"); return n; }
  if (f.type === "float") { const n = parseFloat(raw);
    if (isNaN(n)) throw new Error(f.path + " 需要数字"); return n; }
  if (f.type === "list" || f.type === "dict") {
    try { return JSON.parse(raw); } catch (e) { throw new Error(f.path + " 不是合法 JSON"); }
  }
  return raw;
}
async function loadSettings() {
  const data = await api("/admin/settings");
  document.getElementById("cf-note").innerHTML = esc(data.note || "");
  const byGroup = {};
  for (const f of data.fields || []) (byGroup[f.group] = byGroup[f.group] || []).push(f);
  const order = ["slots", "runtime", "context", "embedding", "budget", "routing"];
  let html = "";
  for (const g of order) {
    const list = byGroup[g] || [];
    if (!list.length) continue;
    html += `<div class="group-title">${esc(GROUP_TITLES[g] || g)} <small>重启生效</small></div>`;
    if (g === "slots") {
      const slots = {};
      for (const f of list) { const key = f.path.split(".")[1]; (slots[key] = slots[key] || []).push(f); }
      for (const [key, fl] of Object.entries(slots)) {
        const isFront = key === data.front_slot;
        const type = (fl.find(f => f.path.endsWith(".type")) || {}).value || "local";
        html += `<div class="slotcard"><div class="head"><b>${esc(key)}</b>
          <span class="badge">${esc(type)}</span>
          ${isFront ? '<span class="badge resident">前台 resident</span>' : ""}
          <span class="sp" style="flex:1"></span>
          ${isFront ? "" : `<button class="danger sm" onclick="delSlot('${esc(key)}')">删除槽位</button>`}
          </div>`;
        for (const f of fl) html += fieldRow(f);
        html += "</div>";
      }
    } else {
      html += "<table><tbody>";
      for (const f of list) html += fieldRow(f);
      html += "</tbody></table>";
    }
  }
  document.getElementById("cf-list").innerHTML = html;
  window._cfFields = {};
  for (const f of data.fields || []) window._cfFields[f.path] = f;
}
function fieldRow(f) {
  const ovDiff = f.source === "overlay" &&
    JSON.stringify(f.overlay_value) !== JSON.stringify(f.value);
  const ovNote = ovDiff
    ? `<div style="color:var(--warn);font-size:11px;margin-top:3px">覆盖层值: ${esc(typeof f.overlay_value === "object" ? JSON.stringify(f.overlay_value) : f.overlay_value)}（重启后生效）</div>`
    : "";
  return `<div class="frow">
    <div class="fname"><b>${esc(f.path)}</b><small>${esc(f.desc || "")}</small></div>
    <div>${fieldInput(f)}${ovNote}</div>
    <div class="fops">${badge(f.source)}
      <button class="pri sm" onclick="saveField('${esc(f.path)}')">保存</button>
      ${f.source === "overlay" ? `<button class="sm" onclick="resetField('${esc(f.path)}')">移除覆盖</button>` : ""}
    </div></div>`;
}
async function saveField(path) {
  const f = window._cfFields[path];
  if (!f) return;
  let v;
  try { v = parseField(f); } catch (e) { return toast(e.message, true); }
  try {
    await api("/admin/settings", {method: "POST",
      body: JSON.stringify({updates: {[path]: v}})});
    toast("已保存 " + path + "（写入覆盖层并通过启动预检）；重启服务后生效");
    loadSettings();
  } catch (e) { toast(e.message, true); }
}
async function resetField(path) {
  try {
    await api("/admin/settings", {method: "POST", body: JSON.stringify({resets: [path]})});
    toast("已移除覆盖，恢复 settings.yaml 原值（重启后生效）: " + path);
    loadSettings();
  } catch (e) { toast(e.message, true); }
}
async function delSlot(key) {
  if (!confirm("彻底删除槽位 " + key + "？（含 settings.yaml 来源；档位映射若还指向它会预检失败）")) return;
  try {
    await api("/admin/settings", {method: "POST", body: JSON.stringify({removes: ["slots." + key]})});
    toast("槽位 " + key + " 已删除（通过预检，重启后生效）");
    loadSettings();
  } catch (e) { toast(e.message, true); }
}
const NS_TPL = {
  api: [["role", "heavy"], ["base_url", ""], ["model", ""], ["api_key_env", ""],
        ["context_window", 65536], ["max_output_tokens", 4096]],
  local: [["role", "heavy"], ["model", "models/xxx.gguf"], ["quant", "Q4_K_M"],
          ["weight_mb", 5000], ["vram_mb", 7000], ["placement", "auto"]]};
function showAddSlot() {
  document.getElementById("add-slot").style.display = "block";
  nsRender();
}
function nsRender() {
  const type = document.getElementById("ns-type").value;
  document.getElementById("ns-fields").innerHTML = NS_TPL[type].map(([k, v]) =>
    `<div class="frow"><div class="fname"><b>${k}</b></div>
     <div><input id="ns-${k}" value="${esc(v)}"></div><div></div></div>`).join("");
}
async function addSlot() {
  const key = document.getElementById("ns-key").value.trim();
  if (!/^[A-Za-z0-9_-]+$/.test(key)) return toast("槽位 key 只允许字母/数字/-/_", true);
  const type = document.getElementById("ns-type").value;
  const updates = {["slots." + key + ".type"]: type};
  for (const [k] of NS_TPL[type]) {
    const el = document.getElementById("ns-" + k);
    let v = el.value;
    if (["context_window", "max_output_tokens", "weight_mb", "vram_mb"].includes(k)) {
      v = parseInt(v, 10); if (isNaN(v)) return toast(k + " 需要整数", true);
    }
    updates["slots." + key + "." + k] = v;
  }
  try {
    await api("/admin/settings", {method: "POST", body: JSON.stringify({updates})});
    toast("槽位 " + key + " 已创建并通过预检；重启服务后生效。记得在「配置管理 → routing」把它接进档位映射/降级链。");
    document.getElementById("add-slot").style.display = "none";
    loadSettings();
  } catch (e) { toast(e.message, true); }
}

async function loadSlots() {
  if (!CFG) CFG = await api("/admin/config");
  const c = CFG;
  document.querySelector("#sl-table tbody").innerHTML = Object.entries(c.slots || {}).map(([k, s]) =>
    `<tr><td><b>${esc(k)}</b>${s.resident ? ' <span class="badge resident">resident</span>' : ""}</td>
     <td>${esc(s.type || "local")}</td><td>${esc(s.role || "")}</td>
     <td>${esc(s.model || "")}</td><td>${esc((s.devices || []).join(","))}</td>
     <td>${esc(s.quant || "")}</td><td>${s.resident ? "true" : "false"}</td>
     <td>${s.type === "api" ? esc(s.base_url || "") + (s.api_key_source ? '<br><span class="badge env">key: ' + esc(s.api_key_source) + "</span>" : "") + "<br>窗口 " + esc(s.context_window || "") : "—"}</td></tr>`).join("");
  document.querySelector("#vp-table tbody").innerHTML = Object.entries(c.vram_plan || {}).map(([k, v]) =>
    `<tr><td>${esc(k)}</td><td>${esc(v.vram_mb)}</td>
     <td class="${v.available ? "ok" : "bad"}">${v.available ? "available" : "unavailable"}</td>
     <td>${esc(v.reason || "")}</td></tr>`).join("");
}

async function loadTasks() {
  const data = await api("/tasks?limit=50");
  document.querySelector("#tk-table tbody").innerHTML = (data.tasks || []).map(t =>
    `<tr><td><span class="tasklink" onclick="showTask('${esc(t.task_id)}')">${esc((t.task_id || "").slice(0, 8))}</span></td>
     <td>${esc(t.tier || "")}</td><td>${esc(t.model_name || "")}</td>
     <td class="${t.status === "completed" ? "ok" : t.status === "failed" ? "bad" : "warn"}">${esc(t.status)}</td>
     <td>${esc(t.finish_reason || "")}</td><td>${esc(t.progress ?? 0)}%</td>
     <td>${esc(t.created_at ? new Date(t.created_at * 1000).toLocaleTimeString() : "")}</td></tr>`).join("")
    || '<tr><td colspan="7" style="color:var(--dim)">暂无任务</td></tr>';
}
async function showTask(id) {
  const t = await api("/task_result?task_id=" + encodeURIComponent(id));
  document.getElementById("tk-detail").textContent = JSON.stringify(
    {task_id: t.task_id, status: t.task_status, tier: t.tier, chosen_model: t.chosen_model,
     finish_reason: t.finish_reason, prefill_ms: t.prefill_ms,
     instant_reply: t.instant_reply, final_reply: t.final_reply,
     release_info: t.release_info, error_msg: t.error_msg}, null, 2);
}

async function sendChat() {
  const text = document.getElementById("ch-text").value.trim();
  if (!text) return toast("请输入消息", true);
  const body = {user_input: text, session_id: document.getElementById("ch-session").value || "console"};
  const tier = document.getElementById("ch-tier").value;
  if (tier) body.prefer_tier = tier;
  const wait = parseInt(document.getElementById("ch-wait").value || "0");
  document.getElementById("ch-instant").textContent = "生成中…";
  document.getElementById("ch-final").textContent = "—";
  document.getElementById("ch-meta").textContent = "";
  try {
    let r;
    if (wait > 0) {
      // final 模式：服务端等待 OPENAI_WAIT_TIMEOUT_S，阻塞返回完整结果
      r = await api("/v1/chat/completions", {method: "POST", body: JSON.stringify({
        model: "explore", messages: [{role: "user", content: text}]})});
      const m = (r.choices || [{}])[0].message || {};
      const meta = r.explore || {};
      document.getElementById("ch-instant").textContent = meta.instant_reply || m.content || "";
      document.getElementById("ch-final").textContent =
        meta.process_mode === "direct" ? "（简单任务由前台直答，无后台结果）" : (m.content || "（等待超时，可稍后在「任务」页查看）");
      document.getElementById("ch-meta").textContent = JSON.stringify(meta, null, 2);
    } else {
      r = await api("/chat", {method: "POST", body: JSON.stringify(body)});
      document.getElementById("ch-instant").textContent = r.instant_reply || "";
      document.getElementById("ch-final").textContent = r.最终回复 || "（后台任务进行中）";
      document.getElementById("ch-meta").textContent = JSON.stringify(
        {task_id: r.task_id, task_type: r.task_type, process_mode: r.process_mode,
         selection_reason: r.selection_reason, context_usage: r.context_usage}, null, 2);
      if (r.process_mode === "async" && r.task_id) pollFinal(r.task_id);
    }
  } catch (e) { toast(e.message, true);
    document.getElementById("ch-instant").textContent = "请求失败：" + e.message; }
}
async function pollFinal(taskId, tries = 60) {
  for (let i = 0; i < tries; i++) {
    const t = await api("/task_result?task_id=" + encodeURIComponent(taskId));
    if (t.task_status === "completed" || t.task_status === "failed") {
      document.getElementById("ch-final").textContent =
        t.final_reply || ("（" + t.task_status + "）" + (t.error_msg || ""));
      return;
    }
    await new Promise(r => setTimeout(r, 2000));
  }
  document.getElementById("ch-final").textContent = "（轮询超时，任务可能仍在后台执行）";
}

/* ---- 重启 ---- */
async function doRestart() {
  if (!confirm("重启服务？重启前会自动做配置预检（预检失败不会重启）。正在执行的任务会被标记中断，pending 任务重启后重新排队。")) return;
  busy(true, "正在重启服务…");
  let prevStarted = null;
  try {
    try { const h0 = await api("/health"); prevStarted = h0.started_at; } catch (e) {}
    await api("/admin/restart", {method: "POST"});
  } catch (e) { busy(false); return toast(e.message, true); }
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 1500));
    try {
      const h = await api("/health");
      // 必须确认是新实例（started_at 变化）：旧实例优雅退出前 /health 仍可用
      const isNew = prevStarted == null || h.started_at !== prevStarted;
      if (h.frontend === "ready" && isNew) {
        busy(false);
        toast("服务已重启完成 ✓");
        refreshAll();
        return;
      }
    } catch (e) { /* 重启窗口期连不上属预期，继续轮询 */ }
  }
  busy(false);
  toast("等待超时：服务未在 120 秒内恢复，请看 logs/api.log", true);
}

async function refreshAll() {
  try {
    await loadHealth();
    await loadAdmin();
    CFG && loadSlots();
    loadTasks();
    const c = (CFG || {}).tier_order || [];
    const sel = document.getElementById("ch-tier");
    sel.innerHTML = '<option value="">自动分级</option>' +
      c.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
  } catch (e) { toast(e.message, true); }
}
document.getElementById("ch-session").value = "console-" + Math.random().toString(36).slice(2, 7);
refreshAll();
</script>
</body>
</html>
"""
