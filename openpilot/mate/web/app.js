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
    try { data = await resp.json(); } catch { /* 非 JSON 响应忽略 */ }
    if (!resp.ok) {
      const err = new Error((data && data.error) || ('HTTP ' + resp.status));
      err.status = resp.status;
      err.data = data;
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
    renderSnapshots(snap.data || {});
  } catch (e) { toast('拉取 /state 失败：' + e.message, true); }
}

function renderKv(el, pairs) {
  el.innerHTML = pairs.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd${typeof v === 'number' ? ' class="num"' : ''}>${esc(fmtVal(v))}</dd>`).join('');
}

on('opstate', (d) => {
  renderKv(document.getElementById('opstate-kv'), Object.entries(d || {}));
});

const FLAT_FRAME_KEYS = 24; // frame 表上限，避免爆屏
on('frame', (d) => {
  const pairs = [];
  const sections = d && typeof d === 'object' ? d : {};
  for (const [sec, sub] of Object.entries(sections)) {
    if (!sub || typeof sub !== 'object') continue;
    for (const [k, v] of Object.entries(sub)) {
      if (pairs.length >= FLAT_FRAME_KEYS) break;
      pairs.push([`${sec}.${k}`, v]);
    }
    if (pairs.length >= FLAT_FRAME_KEYS) break;
  }
  if (pairs.length) renderKv(document.getElementById('frame-kv'), pairs);
});

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
loadStatusPage();
wsConnect();
