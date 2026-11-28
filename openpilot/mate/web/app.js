/* mate UI — vanilla JS 单页（v6.1 V2，无构建链）
 *
 * 数据面 = mate API 面：/health /state /ws /params /param_set /command*
 * /param_changes* /webrtc/*。与 API 同仓同版本演进，协议语义见 mate/API.md。
 *
 * token 约定：URL ?token= → sessionStorage（随后从地址栏抹掉）→
 * fetch 带 Bearer 头；浏览器 WebSocket 无法自定义 header，走 ?token= query。
 * MateToken 未配置时无感直进（与 mate 既有 fail-open 口径一致）。
 */
'use strict';

/* ── token 与 API 基础 ──────────────────────────────────────────────── */
const qs = new URLSearchParams(location.search);
const urlToken = qs.get('token');
if (urlToken) {
  sessionStorage.setItem('mateToken', urlToken);
  qs.delete('token');
  history.replaceState(null, '', location.pathname +
    (qs.toString() ? '?' + qs : '') + location.hash);
}
const TOKEN = sessionStorage.getItem('mateToken') || '';

function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (TOKEN) headers['Authorization'] = 'Bearer ' + TOKEN;
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  const init = Object.assign({}, opts, { headers });
  if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
  return fetch(path, init).then(async (resp) => {
    let data = null;
    let parsed = false;
    try { data = await resp.json(); parsed = true; } catch { /* 非 JSON 响应 */ }
    if (!resp.ok) {
      const err = new Error((data && data.error) || ('HTTP ' + resp.status));
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    if (!parsed) {
      // 200 但响应体不是合法 JSON（如服务端漏出裸 NaN）——显式报错，
      // 不再让调用方拿着 null 解引用出天书 TypeError
      const err = new Error('响应不是合法 JSON（HTTP ' + resp.status + '）');
      err.status = resp.status;
      throw err;
    }
    return data;
  });
}

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const q = TOKEN ? '?token=' + encodeURIComponent(TOKEN) : '';
  return `${proto}://${location.host}/ws${q}`;
}

function toast(msg, isErr) {
  const box = document.getElementById('toast');
  const el = document.createElement('div');
  el.className = 't' + (isErr ? ' err' : '');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtTime(unixSec) {
  if (!unixSec) return '—';
  const d = new Date(unixSec * 1000);
  return d.toLocaleTimeString('zh-CN', { hour12: false }) +
    '.' + String(d.getMilliseconds()).padStart(3, '0');
}

function fmtVal(v) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(2);
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (Array.isArray(v)) {  // 数组截断：[5, 15, 30 …×7]，防栅格/序列撑爆表格
    const head = v.slice(0, 3).map(fmtVal).join(', ');
    return v.length > 3 ? `[${head} …×${v.length}]` : `[${head}]`;
  }
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/* ── 页签 ───────────────────────────────────────────────────────────── */
document.querySelectorAll('#tabs .tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('#tabs .tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.page').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('page-' + tab.dataset.page).classList.add('active');
    if (tab.dataset.page === 'params') loadParamsPage();
    if (tab.dataset.page === 'audit') loadAuditPage();
    if (tab.dataset.page === 'video') checkVideoSchema();
    if (tab.dataset.page === 'status') loadStatusPage();
  });
});

/* ── WS：连接 / 重连 / 分发 ─────────────────────────────────────────── */
let ws = null;
let wsBackoff = 1000;
let wsAlive = false;

function wsConnect() {
  const badge = document.getElementById('badge-ws');
  ws = new WebSocket(wsUrl());
  ws.onopen = () => {
    wsAlive = true;
    wsBackoff = 1000;
    badge.textContent = 'WS 已连';
    badge.className = 'badge ok';
    ws.send(JSON.stringify({ type: 'snapshot' })); // 打开即拉全量快照
  };
  ws.onclose = () => {
    wsAlive = false;
    badge.textContent = 'WS 重连中…';
    badge.className = 'badge bad';
    setTimeout(wsConnect, wsBackoff);
    wsBackoff = Math.min(wsBackoff * 2, 15000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'hello') {
      document.getElementById('proto-ver').textContent = 'v' + msg.data.version;
    } else if (msg.type === 'error') {
      badge.textContent = 'WS 鉴权失败';
      badge.className = 'badge bad';
      toast('WS 鉴权失败：token 不对（URL 加 ?token= 重新进入）', true);
    } else if (msg.type === 'snapshot') {
      renderSnapshots(msg.data && msg.data.data ? msg.data.data : msg.data);
    } else {
      dispatch(msg.type, msg.data);
    }
  };
}

const dispatchers = {};
function dispatch(kind, data) {
  (dispatchers[kind] || []).forEach((fn) => { try { fn(data); } catch (e) { console.error(e); } });
}
function on(kind, fn) {
  (dispatchers[kind] = dispatchers[kind] || []).push(fn);
}

/* ── 徽章（/health 5s 轮询 + WS 心跳） ──────────────────────────────── */
let lastOnroad = undefined; // 三态缓存（true/false/undefined=未知），调参页锁定判断用

async function pollHealth() {
  try {
    const h = await api('/health');
    lastOnroad = h.onroad;
    const onroad = document.getElementById('badge-onroad');
    onroad.textContent = h.onroad === true ? 'ONROAD' : (h.onroad === false ? 'offroad' : '状态未知');
    onroad.className = 'badge ' + (h.onroad === true ? 'bad' : (h.onroad === false ? 'ok' : 'warn'));
    const poller = document.getElementById('badge-poller');
    const fresh = h.ok === true;
    poller.textContent = fresh ? 'poller 心跳正常' : 'poller 异常';
    poller.className = 'badge ' + (fresh ? 'ok' : 'bad');
    document.getElementById('badge-clients').textContent = 'clients ' + h.clients;
  } catch { /* 网络抖动忽略，下轮再试 */ }
}
setInterval(pollHealth, 5000);
pollHealth();

/* ── 状态页 ─────────────────────────────────────────────────────────── */
async function loadStatusPage() {
  try {
    const snap = await api('/state');
    renderSnapshots((snap && snap.data) || {});
  } catch (e) { toast('拉取 /state 失败：' + e.message, true); }
}

function renderKv(el, pairs) {
  el.innerHTML = pairs.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd${typeof v === 'number' ? ' class="num"' : ''}>${esc(fmtVal(v))}</dd>`).join('');
}

on('opstate', (d) => {
  renderKv(document.getElementById('opstate-kv'), Object.entries(d || {}));
});

/* frame 表按段渲染（v6.3 修复）：原全局 24 键上限被 car 段 34 键吃满，
 * radar/carrot 永不可见；改每段限键 + 数组截断。road 不入表（下方专属验收卡）。 */
const FRAME_KEYS_PER_SECTION = 12;
const FRAME_TABLE_SKIP = new Set(['road']);

on('frame', (d) => {
  const sections = d && typeof d === 'object' ? d : {};
  if (sections.road) {
    roadFeed.last = sections.road;
    roadFeed.at = Date.now();
    renderRoadSvg(sections.road);
  }
  const pairs = [];
  for (const [sec, sub] of Object.entries(sections)) {
    if (FRAME_TABLE_SKIP.has(sec) || !sub || typeof sub !== 'object') continue;
    let n = 0;
    for (const [k, v] of Object.entries(sub)) {
      if (n >= FRAME_KEYS_PER_SECTION) break;
      pairs.push([`${sec}.${k}`, v]);
      n++;
    }
  }
  if (pairs.length) renderKv(document.getElementById('frame-kv'), pairs);
});

/* ── road 验收卡（v6.3）────────────────────────────────────────────
 * 使命是语义验收而非美观：镜像（左线应画左侧）、probs null 化、leads 排序
 * 三类错误肉眼可辨——rev2 §9 第一风险的浏览器侧防线，独立于 carrotmate 端。
 * 坐标映射：距离线性（d=0 底、dists 末端顶），横向线性（±8m 截断），
 * 契约左正 → 画面左（xOf 取负），杜绝和 SR 客户端犯同款符号错。 */
const ROAD_VIEW = { W: 640, TOP: 18, BOT: 348, LAT_MAX: 8 };
const roadFeed = { last: null, at: 0 };

function roadXY(ym, d, dMax) {
  const { W, TOP, BOT, LAT_MAX } = ROAD_VIEW;
  const lat = Math.max(-LAT_MAX, Math.min(LAT_MAX, ym)) * (W / 2 - 10) / LAT_MAX;
  return [W / 2 - lat, BOT - (d / dMax) * (BOT - TOP)];
}

function roadPath(arr, dists, dMax) {
  // 逐点可 null（断线跳绘）；整线 null 由调用方跳过
  let path = '', pen = false;
  arr.forEach((v, i) => {
    if (v === null || v === undefined) { pen = false; return; }
    const [x, y] = roadXY(v, dists[i], dMax);
    path += (pen ? 'L' : 'M') + x.toFixed(1) + ',' + y.toFixed(1);
    pen = true;
  });
  return path;
}

function roadSvgLine(arr, dists, dMax, stroke, opacity, dash, width) {
  const path = roadPath(arr, dists, dMax);
  if (!path) return '';
  return `<path d="${path}" fill="none" stroke="${stroke}" stroke-opacity="${opacity}"` +
    `${dash ? ' stroke-dasharray="6 5"' : ''} stroke-width="${width}" stroke-linecap="round"/>`;
}

function renderRoadSvg(r) {
  const svg = document.getElementById('road-svg');
  if (!svg) return;
  const dists = r && Array.isArray(r.dists) ? r.dists : [];
  if (!r || !dists.length) {
    svg.innerHTML = '<text x="320" y="186" text-anchor="middle" fill="#8a8f98" font-size="15">' +
      '无 road 段（offroad / 帧门未过 / 几何全 null）</text>';
    return;
  }
  const { W, TOP, BOT, LAT_MAX } = ROAD_VIEW;
  const dMax = dists[dists.length - 1] || 160;
  const probs = Array.isArray(r.probs) ? r.probs : [];
  let g = '';
  for (const d of [25, 50, 75, 100, 125, 150]) {
    if (d > dMax) break;
    const y = (BOT - (d / dMax) * (BOT - TOP)).toFixed(1);
    g += `<line x1="10" y1="${y}" x2="${W - 10}" y2="${y}" stroke="#2c313a"/>` +
      `<text x="14" y="${+y - 4}" fill="#6b7280" font-size="11">${d}m</text>`;
  }
  g += `<line x1="${W / 2}" y1="${TOP}" x2="${W / 2}" y2="${BOT}" stroke="#3a4150" stroke-dasharray="2 4"/>`;
  g += '<text x="14" y="34" fill="#6b7280" font-size="12">左（契约左正）←</text>' +
    `<text x="${W - 96}" y="34" fill="#6b7280" font-size="12">→ 右</text>`;
  (r.edges || []).forEach((e) => {
    if (e) g += roadSvgLine(e, dists, dMax, '#d9a441', 0.85, true, 2);
  });
  (r.lines || []).forEach((l, i) => {
    if (!l) return; // 整线 null：虚位（不画假想补全）
    const inner = i === 1 || i === 2;
    const op = Math.max(0.25, probs[i] === undefined ? 0.9 : probs[i]);
    g += roadSvgLine(l, dists, dMax, inner ? '#cfd8dc' : '#78909c', op, false, inner ? 2.5 : 1.5);
  });
  if (Array.isArray(r.laneCenters)) g += roadSvgLine(r.laneCenters, dists, dMax, '#4caf50', 0.7, true, 1.5);
  (r.leads || []).forEach((ld) => {
    const [px, py] = roadXY(ld.y || 0, ld.d || 0, dMax);
    const lw = (1.85 * (W / 2 - 10) / LAT_MAX).toFixed(1);
    const lh = Math.max(9, 4.6 * (BOT - TOP) / dMax).toFixed(1);
    g += `<rect x="${(px - lw / 2).toFixed(1)}" y="${(py - lh / 2).toFixed(1)}" width="${lw}" height="${lh}"` +
      ` rx="3" fill="#546e7a" fill-opacity="0.9" stroke="#90a4ae"/>`;
    if (typeof ld.heading === 'number') {
      const hx = (px - Math.sin(ld.heading) * 15).toFixed(1); // 左正 heading → 画面左偏
      const hy = (py - Math.cos(ld.heading) * 15).toFixed(1);
      g += `<line x1="${px.toFixed(1)}" y1="${py.toFixed(1)}" x2="${hx}" y2="${hy}" stroke="#e57373" stroke-width="2"/>`;
    }
  });
  // 自车盒（底缘中央，给横向比例参照）
  const ew = (1.9 * (W / 2 - 10) / LAT_MAX).toFixed(1);
  g += `<rect x="${(W / 2 - ew / 2).toFixed(1)}" y="${BOT - 30}" width="${ew}" height="26" rx="4" fill="#1e88e5" fill-opacity="0.8"/>`;
  svg.innerHTML = g;
}

// 陈旧淡出（对齐客户端「本地收包时刻」口径，不依赖设备墙钟）
setInterval(() => {
  const badge = document.getElementById('road-age');
  const svg = document.getElementById('road-svg');
  if (!badge || !svg) return;
  if (!roadFeed.at) { badge.textContent = '—'; badge.className = 'badge'; return; }
  const s = Math.round((Date.now() - roadFeed.at) / 1000);
  badge.textContent = (roadFeed.last && roadFeed.last.frameId ? '#' + roadFeed.last.frameId + ' · ' : '') +
    (s <= 2 ? '2s 内' : s + 's 前');
  badge.className = 'badge ' + (s <= 3 ? 'ok' : 'bad');
  svg.style.opacity = s <= 3 ? '1' : '0.3';
}, 1000);

const eventLog = [];
on('events', (d) => {
  const active = (d && d.active) || [];
  document.getElementById('events-active').innerHTML = active.length
    ? active.map((e) => `<li><span class="chip">${esc(e.name || e)}</span></li>`).join('')
    : '<li style="color:var(--muted)">无活动告警</li>';
  const added = (d && d.added) || [];
  const removed = (d && d.removed) || [];
  if (added.length || removed.length) {
    eventLog.unshift({ ts: Date.now() / 1000, text:
      (added.length ? `+ ${added.map((e) => e.name || e).join(', ')}` : '') +
      (removed.length ? `  - ${removed.map((e) => e.name || e).join(', ')}` : '') });
    eventLog.length = Math.min(eventLog.length, 50);
    document.getElementById('events-log').innerHTML =
      eventLog.map((e) => `<li>${fmtTime(e.ts)}  ${esc(e.text)}</li>`).join('');
  }
});

on('sound', (d) => {
  if (d && d.sound && d.sound !== 'none') toast('提示音：' + d.sound);
});

on('confirm', (d) => onConfirmEvent(d));

function renderSnapshots(sections) {
  const box = document.getElementById('snapshots');
  if (!box || !sections || typeof sections !== 'object') return;
  // road 初值：frame 未到（offroad 打开面板）时先画快照段，frame 到达后以 2Hz 覆盖
  if (!roadFeed.at && sections.road && sections.road.data) {
    roadFeed.last = sections.road.data;
    roadFeed.at = Date.now();
    renderRoadSvg(sections.road.data);
  }
  const names = Object.keys(sections).sort();
  box.innerHTML = names.map((name) => {
    const s = sections[name] || {};
    const body = JSON.stringify(s.data ?? s, null, 1);
    const age = s.ts ? ` · ${Math.max(0, Math.round(Date.now() / 1000 - s.ts))}s 前` : '';
    return `<details class="snap"><summary>${esc(name)}${esc(age)}</summary>` +
      `<pre>${esc(body.length > 4000 ? body.slice(0, 4000) + ' …(截断)' : body)}</pre></details>`;
  }).join('');
}

/* ── 调参页（ParamSpec 驱动，加键零前端改动） ───────────────────────── */
let paramSchema = null; // {name: {kind, lo, hi, offroad_only, group, desc}}

async function loadParamsPage() {
  try {
    if (!paramSchema) {
      const schema = await api('/params/schema');
      paramSchema = schema.params;
    }
    const names = Object.keys(paramSchema).join(',');
    const resp = await api('/params?names=' + encodeURIComponent(names));
    renderParamGroups(resp.values);
  } catch (e) {
    document.getElementById('param-groups').innerHTML =
      `<p class="hint">加载失败：${esc(e.message)}</p>`;
  }
}

function renderParamGroups(values) {
  const box = document.getElementById('param-groups');
  if (!paramSchema) { box.innerHTML = '<p class="hint">加载 schema 中…</p>'; return; }
  const byGroup = {};
  for (const [name, spec] of Object.entries(paramSchema)) {
    (byGroup[spec.group] = byGroup[spec.group] || []).push([name, spec]);
  }
  // 锁定口径与 mate 门控一致：onroad true 或状态未知（undefined/null）都锁
  const locked = lastOnroad === true || lastOnroad === undefined || lastOnroad === null;
  box.innerHTML = Object.entries(byGroup).map(([group, items]) => `
    <div class="pgroup">
      <h3>${esc(group)} ${locked && items.some(([, s]) => s.offroad_only)
        ? '<span class="badge warn">控车键已锁定</span>' : ''}</h3>
      ${items.map(([name, spec]) => paramRow(name, spec, values[name], locked)).join('')}
    </div>`).join('');
  wireParamEvents();
  document.getElementById('params-summary').textContent =
    `${Object.keys(paramSchema).length} 个键 · ${locked ? 'onroad（或状态未知），控车键只读' : 'offroad，可写'}`;
}

function paramRow(name, spec, value, locked) {
  const disabled = spec.offroad_only && locked;
  const lockNote = disabled ? ' <span class="locked">🔒 offroad_only</span>' : '';
  let control = '';
  if (spec.kind === 'bool') {
    control = `<label class="toggle"><input type="checkbox" data-pk="${esc(name)}"
      ${value === true ? 'checked' : ''} ${disabled ? 'disabled' : ''}><i></i></label>`;
  } else {
    const min = spec.lo !== null && spec.lo !== undefined ? `min="${spec.lo}"` : '';
    const max = spec.hi !== null && spec.hi !== undefined ? `max="${spec.hi}"` : '';
    const step = spec.kind === 'float' ? 'step="0.1"' : 'step="1"';
    control = `<input type="number" data-pk="${esc(name)}" value="${value ?? ''}"
      ${min} ${max} ${step} ${disabled ? 'disabled' : ''}>
      <button data-save="${esc(name)}" ${disabled ? 'disabled' : ''}>写入</button>`;
  }
  return `<div class="prow" data-prok="${esc(name)}">
    <div class="pname"><strong>${esc(name)}</strong><small>${esc(spec.desc)}${lockNote}</small></div>
    <div class="pval">${control}</div>
  </div>`;
}

function wireParamEvents() {
  document.querySelectorAll('#param-groups input[type=checkbox][data-pk]').forEach((el) => {
    el.addEventListener('change', () => writeParam(el.dataset.pk, el.checked, el));
  });
  document.querySelectorAll('#param-groups button[data-save]').forEach((el) => {
    el.addEventListener('click', () => {
      const name = el.dataset.save;
      const input = document.querySelector(`#param-groups input[data-pk="${CSS.escape(name)}"]`);
      if (input && input.value !== '') writeParam(name, Number(input.value), input);
    });
  });
}

async function writeParam(name, value, el) {
  try {
    const resp = await api('/param_set', { method: 'POST', body: { name, value } });
    toast(`${name} 已写入：${fmtVal(resp.value)}`);
    if (el) {
      if (el.type === 'checkbox') el.checked = resp.value === true;
      else el.value = resp.value;
    }
  } catch (e) {
    toast(`${name} 写入失败：${e.message}`, true);
  }
}

document.getElementById('params-refresh').addEventListener('click', () => {
  paramSchema = null;
  loadParamsPage();
});

/* ── 命令页 + confirm 弹窗 ──────────────────────────────────────────── */
// 与 server.py COMMANDS 保持一致（仅 3 条，改动频率低，硬编码并注明）
const COMMANDS = [
  { name: 'trigger_backup', title: '创建参数备份', confirm: false },
  { name: 'clear_logs', title: '清理过期日志', confirm: true },
  { name: 'reboot_device', title: '重启设备', confirm: true },
];

function renderCommandCards() {
  document.getElementById('command-cards').innerHTML = COMMANDS.map((c) => `
    <div class="card">
      <h3>${esc(c.title)}</h3>
      <p class="hint">${esc(c.name)}${c.confirm ? ' · 需确认（60s 超时默认拒绝）' : ' · 立即执行'}</p>
      <button data-cmd="${esc(c.name)}" class="primary">执行</button>
    </div>`).join('');
  document.querySelectorAll('#command-cards button[data-cmd]').forEach((el) => {
    el.addEventListener('click', () => runCommand(el.dataset.cmd, el));
  });
}

async function runCommand(name, el) {
  el.disabled = true;
  try {
    const resp = await api('/command', { method: 'POST', body: { command: name } });
    if (resp.status === 'executed') toast(`${name} 已执行：${resp.detail || ''}`);
    else if (resp.status === 'pending') toast(`${name} 等待确认（弹窗已弹出）`);
  } catch (e) {
    toast(`${name} 失败：${e.message}`, true);
  } finally {
    el.disabled = false;
  }
}

let confirmTimer = null;
let pendingConfirmId = '';
function onConfirmEvent(d) {
  const overlay = document.getElementById('confirm-overlay');
  if (!d) return;
  if (d.status === 'pending') {
    pendingConfirmId = d.id;
    document.getElementById('confirm-title').textContent = d.title || d.command;
    document.getElementById('confirm-desc').textContent = `命令 ${d.command}，60s 内未确认默认拒绝`;
    overlay.classList.remove('hidden');
    startCountdown(d.timeout_s || 60);
  } else {
    stopCountdown();
    overlay.classList.add('hidden');
    if (d.status === 'blocked_offroad') {
      toast('已拦截：批准时车辆已 onroad（或状态未知），命令未执行', true);
    } else if (d.status === 'expired') {
      toast('确认超时，默认拒绝', true);
    } else if (d.status === 'denied') {
      toast('已拒绝');
    } else if (d.status === 'approved') {
      toast('已批准，正在执行…');
    }
  }
}

function startCountdown(sec) {
  const el = document.getElementById('confirm-countdown');
  let left = sec;
  el.textContent = `${left}s 内未确认将默认拒绝`;
  stopCountdown();
  confirmTimer = setInterval(() => {
    left -= 1;
    if (left <= 0) { stopCountdown(); return; }
    el.textContent = `${left}s 内未确认将默认拒绝`;
  }, 1000);
}
function stopCountdown() { if (confirmTimer) { clearInterval(confirmTimer); confirmTimer = null; } }

document.getElementById('confirm-approve').addEventListener('click', async () => {
  try {
    const resp = await api('/command/confirm', { method: 'POST', body: { id: pendingConfirmId, approve: true } });
    toast(resp.status === 'executed' ? '已执行' : (resp.status || '已提交'), resp.ok === false);
  } catch (e) { toast('确认失败：' + e.message, true); }
});
document.getElementById('confirm-deny').addEventListener('click', async () => {
  try {
    await api('/command/confirm', { method: 'POST', body: { id: pendingConfirmId, approve: false } });
  } catch (e) { toast('拒绝失败：' + e.message, true); }
});

/* ── 审计页 ─────────────────────────────────────────────────────────── */
async function loadAuditPage() {
  const verifyEl = document.getElementById('audit-verify');
  const tbody = document.querySelector('#audit-table tbody');
  try {
    const v = await api('/param_changes/verify');
    verifyEl.textContent = v.valid ? `链完整 · ${v.checked} 条` : `断链 @${v.broken_at} · ${v.reason}`;
    verifyEl.className = 'badge ' + (v.valid ? 'ok' : 'bad');
    const params = new URLSearchParams();
    if (document.getElementById('audit-name').value) params.set('name', document.getElementById('audit-name').value);
    if (document.getElementById('audit-source').value) params.set('source', document.getElementById('audit-source').value);
    params.set('limit', document.getElementById('audit-limit').value || '50');
    const resp = await api('/param_changes?' + params.toString());
    tbody.innerHTML = resp.records.map((r) => `
      <tr>
        <td>${fmtTime(r.ts)}</td>
        <td>${esc(r.name)}</td>
        <td>${esc(fmtVal(r.prev))} → ${esc(fmtVal(r.next))}</td>
        <td class="src ${esc(r.source)}">${esc(r.source)}</td>
        <td>${r.engaged === null || r.engaged === undefined ? '—' : (r.engaged ? '是' : '否')}</td>
      </tr>`).join('') || '<tr><td colspan="5" style="color:var(--muted)">无记录</td></tr>';
  } catch (e) {
    verifyEl.textContent = '查询失败';
    verifyEl.className = 'badge bad';
    toast('审计查询失败：' + e.message, true);
  }
}
document.getElementById('audit-refresh').addEventListener('click', loadAuditPage);

/* ── 视频页（WebRTC，信令经 mate，媒体点对点） ──────────────────────── */
let pc = null;

async function checkVideoSchema() {
  const el = document.getElementById('video-status');
  try {
    await api('/webrtc/schema');
    el.textContent = 'webrtcd 就绪';
    el.style.color = 'var(--ok)';
  } catch (e) {
    el.textContent = e.status === 503 ? '直播未开启（设 IsLiveStreaming 后重试）' : ('webrtcd 异常：' + e.message);
    el.style.color = 'var(--warn)';
  }
}

async function startVideo() {
  const statusEl = document.getElementById('video-status');
  const video = document.getElementById('video-el');
  const camera = document.getElementById('video-camera').value;
  stopVideo();
  try {
    pc = new RTCPeerConnection();
    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.addTransceiver('audio', { direction: 'recvonly' });
    pc.ontrack = (ev) => { video.srcObject = ev.streams[0] || new MediaStream([ev.track]); };
    pc.onconnectionstatechange = () => {
      statusEl.textContent = '连接状态：' + pc.connectionState;
      statusEl.style.color = pc.connectionState === 'connected' ? 'var(--ok)' : 'var(--muted)';
    };
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    // 等 ICE 收集完再发（webrtcd 需要 candidates）
    await new Promise((resolve) => {
      if (pc.iceGatheringState === 'complete') return resolve();
      const t = setTimeout(resolve, 1500); // 兜底
      pc.onicegatheringstatechange = () => {
        if (pc.iceGatheringState === 'complete') { clearTimeout(t); resolve(); }
      };
    });
    statusEl.textContent = '信令中…';
    const answer = await api('/webrtc/offer', {
      method: 'POST',
      body: { sdp: pc.localDescription.sdp, camera },
    });
    await pc.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
    statusEl.textContent = `已应答（${answer.camera}）`;
  } catch (e) {
    statusEl.textContent = '失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
    stopVideo();
  }
}

function stopVideo() {
  if (pc) { try { pc.close(); } catch { /* 忽略 */ } pc = null; }
  const video = document.getElementById('video-el');
  if (video) video.srcObject = null;
  document.getElementById('video-status').textContent = '';
}
document.getElementById('video-start').addEventListener('click', startVideo);
document.getElementById('video-stop').addEventListener('click', stopVideo);

/* ── 启动 ───────────────────────────────────────────────────────────── */
renderCommandCards();
renderParamGroups({});
renderRoadSvg(null);
loadStatusPage();
wsConnect();
