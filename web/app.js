'use strict';

/* ---------------------------------------------------------------------------
 * wk-switch 前端
 *
 * 单页三步：导入 → 转换 → 导出。所有状态在后端会话里，前端只保留 job id。
 * 没有构建步骤，也没有外部依赖——本地工具断网也要能用。
 * ------------------------------------------------------------------------- */

const state = {
  job: null,
  files: [],
  profile: null,
  converted: false,
  busy: false,
  profiles: [],
  // 预览表里当前显示的行（服务端只给前 200 条），勾选/风控都按 key 认
  rows: [],
  // 勾选的行 key。空集合 = 「没表态」，导出和留存时按全部处理
  picked: new Set(),
  // {key: true/false}。true=风控，false=显式正常（把它从风控区拿出来）。
  // 没有这个键 = 跟随「这批是风控账号」那个整批开关。
  marks: new Map(),
};

const $ = (sel) => document.querySelector(sel);

const STATE_COLOR = {
  valid: '#16a34a',
  expiring: '#d97706',
  refreshable: '#f59e0b',
  expired: '#dc2626',
  envelope: '#7c3aed',
  api_key: '#0891b2',
  unknown: '#94a3b8',
  empty: '#cbd5e1',
};

const STATE_PILL = {
  valid: 'ok',
  expiring: 'warn',
  refreshable: 'warn',
  expired: 'bad',
  envelope: 'brand',
  api_key: 'brand',
  unknown: 'mute',
  empty: 'mute',
};

const FIELD_LABEL = {
  access_token: 'access_token',
  refresh_token: 'refresh_token',
  device_token: 'device_token',
  email: 'email',
  phone: '手机号',
  nickname: '昵称',
  enterprise_id: '企业 ID',
  proxy: '代理',
  expires_at: '到期时间',
};

/* ---------- 工具 ---------- */

function toast(message, bad) {
  const el = $('#toast');
  el.textContent = message;
  el.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.className = 'toast'; }, 3200);
}

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function bytesText(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(2) + ' MB';
}

async function api(path, body) {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  if (resp.status === 401) {
    // 会话过期：直接回登录页，别让用户对着「HTTP 401」猜
    location.replace('/login');
    throw new Error('未登录');
  }
  const data = await resp.json().catch(() => ({ ok: false, error: '响应不是 JSON' }));
  if (!resp.ok || data.ok === false) {
    throw new Error(data.error || ('HTTP ' + resp.status));
  }
  return data;
}

/** GET 也要处理 401：否则未登录时页面只是一片空白。 */
async function apiGet(path) {
  const resp = await fetch(path);
  if (resp.status === 401) {
    location.replace('/login');
    throw new Error('未登录');
  }
  return resp.json();
}

function toBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || '');
      const comma = result.indexOf(',');
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(new Error('读取 ' + file.name + ' 失败'));
    reader.readAsDataURL(file);
  });
}

function setStep(n) {
  document.querySelectorAll('.step').forEach((el) => {
    const step = Number(el.dataset.step);
    el.classList.toggle('active', step === n);
    el.classList.toggle('done', step < n);
  });
}

/* ---------- 图表（纯 SVG，无依赖） ---------- */

function donutChart(items, size) {
  const total = items.reduce((sum, it) => sum + it.count, 0);
  const r = size / 2 - 9;
  const c = 2 * Math.PI * r;
  if (!total) {
    return `<svg class="donut" width="${size}" height="${size}"><circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="#eceff7" stroke-width="14"/></svg>`;
  }
  let offset = 0;
  const arcs = items.map((it) => {
    const frac = it.count / total;
    const dash = frac * c;
    const color = STATE_COLOR[it.key] || '#94a3b8';
    const seg = `<circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none"
      stroke="${color}" stroke-width="14"
      stroke-dasharray="${dash.toFixed(2)} ${(c - dash).toFixed(2)}"
      stroke-dashoffset="${(-offset).toFixed(2)}"
      transform="rotate(-90 ${size / 2} ${size / 2})"
      stroke-linecap="butt"/>`;
    offset += dash;
    return seg;
  }).join('');
  return `<svg class="donut" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="#f1f3f9" stroke-width="14"/>
    ${arcs}
    <text x="50%" y="47%" text-anchor="middle" font-size="17" font-weight="650" fill="#1c2130">${total}</text>
    <text x="50%" y="61%" text-anchor="middle" font-size="10" fill="#8b93a7">账号</text>
  </svg>`;
}

function renderStats(stats) {
  const stateItems = (stats.states || []).filter((s) => s.count > 0);
  const legend = stateItems.map((s) => `
    <div class="legend-item">
      <i style="background:${STATE_COLOR[s.key] || '#94a3b8'}"></i>
      <span>${esc(s.label)}</span><b>${s.count}</b>
    </div>`).join('');

  const bars = (stats.fields || []).map((f) => `
    <div class="bar-row">
      <span title="${esc(f.key)}">${esc(FIELD_LABEL[f.key] || f.key)}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${f.ratio}%"></div></div>
      <em>${f.ratio}%</em>
    </div>`).join('');

  const realmLine = (stats.realms || [])
    .map((r) => `${esc(r.label)} ${r.count}`).join(' · ');

  $('#stats').innerHTML = `
    <div class="stat-box">
      <h3>凭据状态分布${realmLine ? '　|　' + realmLine : ''}</h3>
      <div class="donut-row">
        ${donutChart(stateItems, 118)}
        <div class="legend">${legend || '<span class="hint">无数据</span>'}</div>
      </div>
    </div>
    <div class="stat-box">
      <h3>字段填充率</h3>
      <div class="bars">${bars}</div>
    </div>`;
}

/* ---------- 预览表 ---------- */

/** 一行的风控标记该怎么显示。
 *  三态：没标过（跟随整批）、标了风控、标了正常。 */
function riskChip(r) {
  const mark = state.marks.get(r.key);
  if (mark === true) {
    return '<button class="risk-chip on" data-risk-key="' + esc(r.key) +
      '" title="已标为风控。点一下改成「正常」，再点回「跟随整批」">风控</button>';
  }
  if (mark === false) {
    return '<button class="risk-chip safe" data-risk-key="' + esc(r.key) +
      '" title="已标为正常：即使勾了「这批是风控账号」，这一行也不会进风控区">正常</button>';
  }
  return '<button class="risk-chip" data-risk-key="' + esc(r.key) +
    '" title="跟随下面的「这批是风控账号」。点一下标为风控，再点标为正常，第三次取消">—</button>';
}

/** 风控标记的循环顺序：跟随整批 → 风控 → 正常 → 跟随整批。
 *  不做「点一下直接切两态」是因为「跟随整批」本身是一个有意义的选项：
 *  用户给整批勾了风控，个别号标成正常，剩下的还得跟着整批走。 */
function cycleRisk(key) {
  const cur = state.marks.get(key);
  if (cur === undefined) state.marks.set(key, true);
  else if (cur === true) state.marks.set(key, false);
  else state.marks.delete(key);
  renderPreview(state.rows, state.total);
}

function riskPayload() {
  const out = {};
  state.marks.forEach((flag, key) => { out[key] = !!flag; });
  return out;
}

function selectedKeys() {
  return [...state.picked];
}

/** 勾选状态变了就重画那一行的底色和上下两个全选框。 */
function refreshPickBar() {
  const total = state.rows.length;
  const n = state.picked.size;
  const label = $('#pick-count');
  if (label) {
    label.textContent = n
      ? `已勾选 ${n} 条，导出和留存只处理这 ${n} 条`
      : `没勾选，导出和留存按全部 ${total} 条算`;
    label.classList.toggle('on', n > 0);
  }

  ['#pick-all', '#pick-all-top'].forEach((sel) => {
    const box = $(sel);
    if (!box) return;
    box.checked = n > 0 && n >= total;
    // 半选：浏览器只在 JS 里给了这个属性，HTML 没有对应写法
    box.indeterminate = n > 0 && n < total;
  });

  const has = n > 0;
  ['#btn-pick-risk', '#btn-pick-safe', '#btn-pick-drop', '#btn-pick-clear']
    .forEach((sel) => { const b = $(sel); if (b) b.disabled = !has; });
}

function renderPreview(rows, total) {
  state.rows = rows || [];
  state.total = total || 0;
  const body = $('#preview-table tbody');
  body.innerHTML = state.rows.map((r) => {
    const on = state.picked.has(r.key);
    return `
    <tr data-key="${esc(r.key)}" class="${on ? 'picked' : ''}">
      <td class="col-pick"><input type="checkbox" class="pick-box" ${on ? 'checked' : ''}
        aria-label="勾选第 ${r.index} 条"></td>
      <td class="mono">${r.index}</td>
      <td class="mono" title="${esc(r.uid)}">${esc(r.uid)}</td>
      <td>${esc(r.nickname) || '<span class="hint">—</span>'}</td>
      <td>${esc(r.email) || '<span class="hint">—</span>'}</td>
      <td>${r.realm === 'cn' ? '国内' : r.realm === 'intl' ? '国际' : '<span class="hint">—</span>'}</td>
      <td><span class="pill ${STATE_PILL[r.state] || 'mute'}">${esc(r.state_label)}</span></td>
      <td class="mono col-exp">${esc(r.expires) || '—'}</td>
      <td class="mono">${esc(r.at)}</td>
      <td>${r.rt ? '<span class="pill ok">有</span>' : '<span class="hint">—</span>'}</td>
      <td class="col-risk">${riskChip(r)}</td>
    </tr>`;
  }).join('');

  $('#preview-hint').textContent = total > state.rows.length
    ? `显示前 ${state.rows.length} 条，共 ${total} 条`
    : `共 ${total} 条`;

  refreshPickBar();
  refreshExportHint();
}

/** 导出提示里带上条数：没勾是全部，勾了就只有勾的那些。
 *  用户问「到底导了几条」时，答案得先在按钮旁边写着。 */
function refreshExportHint() {
  const el = $('#export-hint');
  if (!el || !state.profile) return;
  const p = state.profiles.find((x) => x.id === state.profile);
  const n = state.picked.size || state.total || 0;
  const scope = state.picked.size ? `勾选的 ${n} 条` : `全部 ${n} 条`;
  el.textContent = `将生成：${p ? p.name : ''}（${scope}）`;
}

/** 删除勾选的行。
 *
 *  删的是**这次会话里的副本**，用户拖进来的文件一个字节都不动——重新导入
 *  一次就全回来了。所以这里不需要「删了就找不回」的强确认，但仍要问一句：
 *  勾错一行就点下去的话，后面整段导出都少一个号。 */
async function dropPicked() {
  if (!state.job || !state.picked.size) return;
  const keys = [...state.picked];
  if (!confirm(`把勾选的 ${keys.length} 条从这次导入里删掉？\n\n只删会话里的副本，你手上的文件不动。`)) {
    return;
  }
  try {
    const data = await api('/api/rows/drop', {
      job: state.job, keys, opts: collectOpts(),
    });
    state.picked.clear();
    // 删掉的行连同它的风控标记一起清：key 是序号，留着会贴到下一批复用
    // 同一批 key 的号上。
    keys.forEach((k) => state.marks.delete(k));
    renderPreview(data.preview || [], data.kept || 0);
    renderStats(data.stats);
    renderIssues(data.issues || []);
    $('#convert-count').textContent = data.kept
      ? `删掉 ${data.removed} 条 · 保留 ${data.kept} 条`
      : '这次导入里已经没有账号了';
    toast(`已删除 ${data.removed} 条，还剩 ${data.kept} 条`);
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------- 问题清单 ---------- */

function renderIssues(issues) {
  const box = $('#issues-box');
  const list = $('#issues-list');
  const counter = $('#issues-count');
  const errors = issues.filter((i) => i.level === 'error').length;

  counter.textContent = issues.length;
  counter.className = 'badge' + (errors ? ' bad' : '');

  if (!issues.length) {
    box.open = false;
    list.innerHTML = '<li class="hint">没有发现问题</li>';
    return;
  }

  list.innerHTML = issues.map((i) => `
    <li>
      <span class="lv-${i.level}">${i.level === 'error' ? '跳过' : '提示'}</span>
      ${i.uid ? `<b class="mono">${esc(i.uid)}</b> · ` : ''}${esc(i.message)}
    </li>`).join('');
}

/* ---------- 导入 ---------- */

async function handleFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;

  const payload = [];
  for (const file of files) {
    if (file.size > 16 * 1024 * 1024) {
      toast(file.name + ' 超过 16 MB，已跳过', true);
      continue;
    }
    try {
      payload.push({ name: file.name, data: await toBase64(file) });
    } catch (err) {
      toast(err.message, true);
    }
  }
  await submitFiles(payload);
}

/** 载入内置样例。样例走的是和真实文件完全相同的解析链路，
 *  所以它既能当演示，也能当「界面能不能用」的自检。 */
async function loadSample(name) {
  const url = '/api/sample' + (name ? '?name=' + encodeURIComponent(name) : '');
  try {
    const data = await apiGet(url);
    if (!data.ok) throw new Error(data.error || '样例读取失败');
    await submitFiles([{ name: data.name, data: data.data }]);
  } catch (err) {
    toast(err.message, true);
  }
}

async function submitFiles(payload) {
  if (!payload.length) return;
  try {
    const data = await api('/api/parse', { files: payload });
    state.job = data.job;
    state.converted = false;
    state.profile = null;
    // 换了一批文件，上一批的勾选和风控标记全部作废：key 是 `r1` `r2` 这种
    // 序号，跨批复用会把标记贴到完全不相关的账号上。
    state.picked.clear();
    state.marks.clear();
    state.total = data.total || 0;

    $('#sources').innerHTML = data.sources.map((s) => `
      <div class="src-chip">
        <b>${esc(s.source_label)}</b>
        <span class="cnt">${s.count}</span>
        <small class="hint">${esc(s.name)}</small>
      </div>`).join('');

    $('#notes').innerHTML = (data.notes || []).map((n) => `<p>${esc(n)}</p>`).join('');

    $('#card-convert').classList.remove('hidden');
    $('#card-export').classList.add('hidden');
    setStep(2);

    renderStats(data.stats);
    renderPreview(data.preview, data.total);
    renderIssues([]);
    $('#convert-count').textContent = `已读入 ${data.total} 条`;

    await runConvert();
    $('#card-convert').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------- 转换 ---------- */

function collectOpts() {
  return {
    dedupe_key: $('#opt-dedupe-key').value,
    dedupe_policy: $('#opt-dedupe-policy').value,
    require_token: $('#opt-require-token').checked,
    allow_envelope: $('#opt-allow-envelope').checked,
    allow_expired: $('#opt-allow-expired').checked,
    only_enabled: $('#opt-only-enabled').checked,
    fill_source: 'wk-switch',
  };
}

async function runConvert() {
  if (!state.job || state.busy) return;
  state.busy = true;
  try {
    const data = await api('/api/convert', { job: state.job, opts: collectOpts() });
    state.converted = true;

    const bits = [`保留 ${data.kept} 条`];
    if (data.dropped) bits.push(`跳过 ${data.dropped}`);
    if (data.merged) bits.push(`合并重复 ${data.merged}`);
    if (data.filled_uid) bits.push(`补全 uid ${data.filled_uid}`);
    $('#convert-count').textContent = bits.join(' · ');

    renderStats(data.stats);
    renderPreview(data.preview, data.kept);
    renderIssues(data.issues);

    $('#card-export').classList.remove('hidden');
    setStep(3);
  } catch (err) {
    toast(err.message, true);
  } finally {
    state.busy = false;
  }
}

/* ---------- 导出 ---------- */

async function loadProfiles() {
  const data = await apiGet('/api/profiles');
  state.profiles = data.profiles || [];

  $('#profiles').innerHTML = state.profiles.map((p) => `
    <div class="profile" data-id="${esc(p.id)}">
      <div class="pf-top">
        <b>${esc(p.name)}</b>
        <span class="tag">${esc(p.container)}</span>
      </div>
      <small>${esc(p.project)}</small>
      <p>${esc(p.desc)}</p>
    </div>`).join('');

  document.querySelectorAll('.profile').forEach((el) => {
    el.addEventListener('click', () => {
      document.querySelectorAll('.profile').forEach((x) => x.classList.remove('sel'));
      el.classList.add('sel');
      state.profile = el.dataset.id;
      $('#btn-export').disabled = false;
      refreshExportHint();
    });
  });
}

async function doExport() {
  if (!state.job || !state.profile) {
    toast('先导入文件并选择目标格式', true);
    return;
  }
  const btn = $('#btn-export');
  btn.disabled = true;
  btn.textContent = '生成中…';
  try {
    const data = await api('/api/export', {
      job: state.job,
      profile: state.profile,
      realm: realmOf('export-realm'),
      opts: collectOpts(),
      // 勾了就只导勾的；空数组表示没表态，服务端按全部处理
      keys: selectedKeys(),
    });
    $('#downloads').innerHTML = data.files.map((f) => `
      <div class="dl">
        <div>
          <div class="dl-name">${esc(f.name)}</div>
          <div class="dl-size">${bytesText(f.size)}</div>
        </div>
        <a href="${esc(f.download)}" download="${esc(f.name)}">下载</a>
      </div>`).join('');
    toast('已生成 ' + data.files.length + ' 个文件');
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '生成导出文件';
  }
}

/** 读一组同名 radio 的当前值。 */
function realmOf(name) {
  const el = document.querySelector(`input[name="${name}"]:checked`);
  return el ? el.value : 'all';
}

/* ---------- 账户库 ---------- */

const storeState = {
  rows: [], allRows: [], profile: null, realm: '', q: '', batch: '', zone: '', loaded: false,
  selected: new Set(),
  // 出口下拉的选项来自 mihomo 节点表；busyRisk 是风控区已经占住的端口，
  // 一键分配要避开它们，下拉里则照样列出（那是用户手动自定义的范围）。
  nodes: [], available: [], busyRisk: [], exitsLoaded: false,
  exitProxyLoaded: false,
};

/** 导出时把「按账户出口写 proxy」的界面设置收成后端要的对象。
 *  配置还没读回来时返回 null：别拿 HTML 里的空值去覆盖已保存的自定义主机。 */
function exitProxyPayload() {
  if (!storeState.exitProxyLoaded) return null;
  return {
    enabled: $('#store-exit-proxy').checked,
    host: $('#store-exit-host').value.trim(),
    mode: $('#store-exit-mode').value,
    scope: $('#store-exit-scope').value,
  };
}

/** host 留空时显示实际回落到哪台机器，别让人对着空输入框猜。 */
async function loadExitProxyConfig() {
  try {
    const d = await apiGet('/api/store/exit-proxy/config');
    const c = d.config || {};
    $('#store-exit-proxy').checked = c.enabled !== false;
    $('#store-exit-host').value = c.host || '';
    $('#store-exit-host').placeholder = (d.node_host || '留空沿用节点配置') + '（留空沿用）';
    $('#store-exit-mode').value = c.mode === 'fill' ? 'fill' : 'override';
    $('#store-exit-scope').value = c.scope === 'intl' ? 'intl' : 'all';
    storeState.exitProxyLoaded = true;
  } catch (err) {
    /* 读不到就留 HTML 里的默认值，不挡导出 */
  }
}

/** 导出回报里带上「这次写了几个 proxy」，避免开关勾了却看不出有没有生效。 */
function exitProxyToast(data, prefix) {
  const ep = (data.report && data.report.opts && data.report.opts.exit_proxy) || {};
  if (!ep.enabled) return `${prefix}（未改 proxy）`;
  if (!ep.host) return `${prefix}（出口主机没配，proxy 未写）`;
  return `${prefix}，按出口写 ${ep.applied || 0} 个 proxy` +
    (ep.cleared ? `、${ep.cleared} 个直连` : '') +
    (ep.skipped ? `、跳过 ${ep.skipped} 个` : '');
}

const REALM_PILL = { cn: 'warn', intl: 'brand' };
const REALM_TEXT = { cn: '国内版', intl: '国际版' };

/**
 * 时间一律按北京时间显示。
 *
 * 服务器可能在 UTC 机房，`toLocaleTimeString()` 用的是**浏览器**时区；
 * 手机改过时区、远程桌面接的是别的机器，看到的就不是同一个点。时间点上
 * 绑死 Asia/Shanghai，和库里的存储口径对齐。
 */
const BJ_TZ = 'Asia/Shanghai';

function bjTime(ts) {
  if (!ts) return '';
  try {
    return new Date(ts * 1000).toLocaleTimeString('zh-CN', {
      timeZone: BJ_TZ, hour12: false,
    });
  } catch (err) {
    return '';
  }
}

/** 批次号 `import-20261106-170858` → `11-06 17:08`。认不出就原样给。 */
function batchLabel(batch) {
  const m = String(batch || '').match(/(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/);
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : String(batch || '');
}

/* ---------- 从官网登录账户（官方 OAuth 设备流） ---------- */

const wlState = {
  state: '', timer: null, busy: false, proxyReady: false, target: null,
  exits: null, exitsSeq: 0, directValue: 'wl:direct',
};

/* 登录出口代理：输入框一直可以手填，下拉只是把「目前已支持的端口」列出来。
   option 的 value 就是完整代理串，选完写进输入框，发起登录走的还是原来那个
   `proxy` 参数——后端不用为「从下拉选的」另开一条分支。 */

/** 读端口表。probe=true 才真去探出口：打开弹窗不该替用户发十个请求。 */
async function loadWlExits(probe) {
  const seq = ++wlState.exitsSeq;
  const sel = $('#wl-proxy-pick');
  const btn = $('#btn-wl-exits');
  const hint = $('#wl-exits-hint');
  if (btn) { btn.disabled = true; btn.textContent = probe ? '检测中…' : '读取中…'; }
  if (sel) sel.disabled = true;
  if (hint) {
    hint.classList.remove('bad');
    hint.textContent = probe
      ? '正在逐个探测端口，可能要等十几秒…'
      : '正在读取已支持的端口…';
  }
  try {
    const data = await apiGet('/api/store/login/exits' + (probe ? '?probe=1' : ''));
    if (seq !== wlState.exitsSeq) return;   // 又点了一次检测，旧结果作废
    if (!data.ok) throw new Error(data.error || '端口表读取失败');
    wlState.exits = data;
    wlState.directValue = data.direct_value || 'wl:direct';
    wlRenderExits(data);
  } catch (err) {
    if (seq !== wlState.exitsSeq) return;
    if (sel) sel.innerHTML = '<option value="">端口表读取失败，可直接手填</option>';
    if (hint) {
      hint.textContent = `端口表读取失败：${err.message}；输入框仍可手填`;
      hint.classList.add('bad');
    }
  } finally {
    if (seq !== wlState.exitsSeq) return;
    if (sel) sel.disabled = false;
    if (btn) { btn.disabled = false; btn.textContent = '检测端口'; }
  }
}

/** 后端给的分好组的选项直接铺成 optgroup，前端不拼地址也不排序。 */
function wlRenderExits(data) {
  const el = $('#wl-proxy-pick');
  if (!el) return;
  let html = '<option value="">从已支持的端口下拉选择…</option>';
  (data.groups || []).forEach((g) => {
    html += `<optgroup label="${esc(g.label || g.key)}">`;
    (g.options || []).forEach((o) => {
      html += `<option value="${esc(o.value)}" title="${esc(o.title || o.value)}">`
        + `${esc(o.label)}</option>`;
    });
    html += '</optgroup>';
  });
  el.innerHTML = html;
  wlSyncProxyPick();
  const hint = $('#wl-exits-hint');
  if (!hint) return;
  hint.classList.remove('bad');
  const total = Number(data.total || 0);
  hint.textContent = data.probed
    ? `已检测：${data.ok_count} / ${total} 个端口能出网；选中即填入上方输入框。`
    : `已列出 ${total} 个端口（未探测）。点「检测端口」可测哪些能出网；选中即填入上方输入框。`;
  if (data.api_error) hint.textContent += `｜节点名：${data.api_error}`;
}

/** 输入框当前值 ↔ 下拉选中态。手填的地址不在列表里就退回占位项。 */
function wlSyncProxyPick() {
  const pick = $('#wl-proxy-pick');
  const input = $('#wl-proxy');
  if (!pick || !input) return;
  const cur = input.value.trim();
  const wanted = cur || wlState.directValue;
  let found = false;
  for (const opt of pick.options) {
    if (opt.value === wanted) { found = true; break; }
  }
  pick.value = found ? wanted : '';
}

/** 选下拉 = 显式指定这一趟的出口；手填的值不会被默默改掉。 */
function wlPickProxy() {
  const pick = $('#wl-proxy-pick');
  const input = $('#wl-proxy');
  if (!pick || !input || !pick.value) return;   // 占位项不动输入框
  input.value = pick.value === wlState.directValue ? '' : pick.value;
  input.dataset.touched = '1';
  wlState.proxyReady = true;
  wlSyncProxyPick();
}

function wlTargetView(target) {
  // 目标行只留界面要显示的四个字段，别把整行（含脱敏 token）挂在全局状态里
  return target ? {
    id: Number(target.id), uid: target.uid || '', realm: target.realm || 'intl',
    name: target.name || target.nickname || target.uid || '未命名',
  } : null;
}

function wlModeText() { return wlState.target ? '重新登录' : '官网登录'; }

function wlSetTarget(target) {
  wlState.target = wlTargetView(target);
  const box = $('#wl-target');
  box.hidden = !wlState.target;
  if (wlState.target) {
    $('#wl-target-text').textContent =
      `正在为「${wlState.target.name}」重新登录：token / 资料会覆盖这一行，出口和风控状态保留。`;
    const radio = document.querySelector(`input[name="wl-realm"][value="${wlState.target.realm}"]`);
    if (radio) radio.checked = true;
    $('#wl-tip').textContent = '点「获取重登链接」→ 在浏览器里完成官方登录授权 → 本页自动检测并更新这一行。'
      + '扫到的是别的账号时不会覆盖，会直接提示。';
  } else {
    $('#wl-tip').textContent = '点「获取登录链接」→ 在浏览器里完成官方登录授权 → 本页自动检测并把账号和资料写进本地账户库。'
      + '同一个 uid 重新登录是更新，不会多出一行。';
  }
  $('#wl-title').textContent = wlState.target ? `重新登录：${wlState.target.name}` : '从官网登录账户';
  $('#wl-start').textContent = wlState.target ? '获取重登链接' : '获取登录链接';
}

function wlStep(active, state) {
  for (let i = 1; i <= 3; i += 1) {
    const el = $(`#wl-step${i}`);
    let cls = 'wl-step';
    if (i < active) cls += ' done';
    else if (i === active) cls += ` ${state || 'active'}`;
    el.className = cls;
  }
}

function wlStatus(text, kind) {
  const el = $('#wl-status');
  el.textContent = text || '';
  el.className = 'hint wl-status' + (kind ? ` ${kind}` : '');
}

function wlStop(cancelServer) {
  if (wlState.timer) { clearInterval(wlState.timer); wlState.timer = null; }
  if (cancelServer && wlState.state) {
    api('/api/store/login/cancel', { state: wlState.state }).catch(() => {});
  }
  wlState.state = '';
  wlState.busy = false;
  $('#wl-start').disabled = false;
}

async function openWebLogin(target) {
  $('#wl-modal').hidden = false;
  wlStatus('');
  $('#wl-profile').hidden = true;
  $('#wl-link-box').hidden = true;
  wlStep(0, '');
  wlSetTarget(target || null);
  // 目标行变了就重算默认代理（该行的出口端口可能和上一次不同）
  delete $('#wl-proxy').dataset.touched;
  wlState.proxyReady = false;
  const query = wlState.target ? `?target_id=${encodeURIComponent(wlState.target.id)}` : '';
  // 端口表异步读：问节点名可能要等 1~2 秒，不能挡住输入框和「获取登录链接」。
  loadWlExits(false);
  // 预填固定验证代理 / 目标行出口：授权 / 取 token / 拉资料 / 查积分全走这一条线。
  // 读不到默认值时保持 proxyReady=false，发起时不传 proxy，让后端自己按
  // 目标行出口算——绝不能拿空串把「读失败」变成「显式直连」。
  try {
    const cfg = await apiGet('/api/store/login/config' + query);
    const proxy = cfg.proxy || '';
    if (!$('#wl-proxy').dataset.touched) $('#wl-proxy').value = proxy;
    wlSyncProxyPick();   // 预填的地址本身可能就是下拉里的某个端口
    wlState.proxyReady = true;
  } catch (err) {
    wlState.proxyReady = false;
    wlStatus('默认代理读取失败，发起时将按设置页的固定验证代理出站', '');
  }
}

function closeWebLogin() {
  wlStop(true);
  wlSetTarget(null);
  $('#wl-modal').hidden = true;
}

async function startWebLogin() {
  if (wlState.busy) return;
  wlStop(true);
  wlState.busy = true;
  const btn = $('#wl-start');
  btn.disabled = true;
  btn.textContent = wlState.target ? '正在申请重登…' : '正在申请…';
  $('#wl-link-box').hidden = true;
  $('#wl-profile').hidden = true;
  wlStep(1, 'active');
  wlStatus('正在向官方申请授权…');

  try {
    const realm = document.querySelector('input[name="wl-realm"]:checked').value;
    const payload = {
      realm,
      platform: 'CLI',
      fetch_credits: $('#wl-credits').checked,
    };
    if (wlState.target) payload.target_id = wlState.target.id;
    if (wlState.proxyReady) payload.proxy = $('#wl-proxy').value.trim();
    const data = await api('/api/store/login/start', payload);
    wlState.state = data.state;
    const link = $('#wl-link');
    link.href = data.authUrl;
    link.textContent = data.authUrl;
    $('#wl-link-box').hidden = false;
    wlStep(2, 'active');
    wlStatus(`登录链接已生成，请在浏览器里完成官方登录授权。本次出口：${data.proxy || '直连'}`);
    wlPoll();
    wlState.timer = setInterval(wlPoll, 2500);
    btn.textContent = wlState.target ? '重新获取重登链接' : '重新获取登录链接';
  } catch (err) {
    wlStatus(`发起失败：${err.message}`, 'bad');
    wlStep(0, '');
    btn.textContent = '重试';
  } finally {
    wlState.busy = false;
    btn.disabled = false;
  }
}

async function wlPoll() {
  if (!wlState.state) return;
  try {
    const data = await apiGet('/api/store/login/poll?state=' + encodeURIComponent(wlState.state));
    wlStatus(data.message || data.status, data.status === 'ok' ? 'ok' : '');
    if (data.status === 'ok') {
      clearInterval(wlState.timer);
      wlState.timer = null;
      wlState.state = '';
      $('#wl-link-box').hidden = true;
      wlStep(4, 'done');
      renderWebLoginProfile(data);
      await loadStore();
      toast((data.target ? '已重新登录：' : '已登录并入库：')
        + ((data.account && data.account.name) || '新账号'));
    } else if (data.status === 'mismatch') {
      clearInterval(wlState.timer);
      wlState.timer = null;
      wlState.state = '';
      wlStatus(data.message || '扫到的是另一个账号，本地库没有改动', 'bad');
    } else if (['error', 'expired', 'unknown'].includes(data.status)) {
      clearInterval(wlState.timer);
      wlState.timer = null;
      wlState.state = '';
      wlStatus(data.message || data.status, 'bad');
    }
  } catch (err) {
    wlStatus(`轮询出错：${err.message}`, 'bad');
  }
}

function renderWebLoginProfile(data) {
  const a = data.account || {};
  const c = data.credits || {};
  const s = data.store || {};
  const pairs = [
    ['账号', a.name || '—'],
    ['uid', a.uid || '—'],
    ['邮箱', a.email || a.phone || '—'],
    ['版本', a.realm_label || a.realm || '—'],
    ['域名', a.domain || '—'],
    ['套餐', c.tier || a.tier || '—'],
    ['积分', c.remain == null ? '未查询' : String(c.remain)],
    ['到期', a.expires || '—'],
    ['入库', s.inserted ? '新增 1 条' : (s.updated ? '更新 1 条' : '—')],
    ['模式', data.target ? `重登第 ${data.target.id} 行` : '新增账户'],
    ['出口', data.proxy || '直连'],
  ];
  const el = $('#wl-profile');
  el.innerHTML = '<h4>获取到的账户信息</h4><div class="wl-grid">'
    + pairs.map(([k, v]) => `<div class="wl-kv"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('')
    + '</div>';
  el.hidden = false;
}

async function copyWebLoginLink() {
  const url = $('#wl-link').getAttribute('href') || '';
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
    toast('登录链接已复制');
  } catch (err) {
    toast('复制失败：' + err.message, true);
  }
}

/**
 * 账户库请求的序号：切分区 / 敲搜索词比一个请求返回快得多，旧响应会盖掉
 * 新结果，界面就出现「按钮在风控、表里还是全部」。节点表第一次拉可能卡
 * 一秒，这个竞态在快速连点时必现。
 */
let storeLoadSeq = 0;

/**
 * 按两分区过滤当前搜索结果。zone 由后端每行给，前端不再自己推档位。
 * O(n)，n ≤ 2000；切分区走纯本地，不发请求。
 */
function storeZoneRows(rows) {
  if (!storeState.zone) return rows;
  return rows.filter((r) => (r.zone || (r.recheck_state ? 'risk' : 'normal'))
    === storeState.zone);
}

async function loadStore(opts) {
  const seq = ++storeLoadSeq;
  const noExits = !!(opts && opts.noExits);
  const params = new URLSearchParams();
  if (storeState.realm) params.set('realm', storeState.realm);
  if (storeState.q) params.set('q', storeState.q);
  if (storeState.batch) params.set('batch', storeState.batch);

  const data = await apiGet('/api/store/list?' + params.toString());
  if (seq !== storeLoadSeq) return;
  if (!data.ok) throw new Error(data.error || '账户库读取失败');

  // 节点表不阻塞账户列表：问池面板可能卡好几秒，先让表格出来。
  // 节点到了再重绘一次，出口下拉里才有节点名可选。
  // 首屏那次只取 tab 徽章数量，没必要顺手探测节点（会白占一条 HTTP 连接）。
  const needExits = !noExits && !storeState.exitsLoaded;
  const exitsDone = loadStoreExits();
  if (needExits) {
    exitsDone.finally(() => {
      if (seq === storeLoadSeq && storeState.exitsLoaded) {
        renderStoreTable(storeState.rows);
      }
    });
  }

  // 分区不交给后端：allRows 留着完整的搜索结果，切「正常 / 风控」纯本地
  // 过滤，0ms 生效。账户库每次刷新都可能撞上 3 秒的节点表探测，靠网络
  // 往返做分区会让按钮点了没反应。后端 zone 参数保留给 API 调用方。
  storeState.allRows = data.rows || [];
  storeState.rows = storeZoneRows(storeState.allRows);
  storeState.loaded = true;

  // 库里已经删掉的 id 不能留在选择集里，否则「导出」会带上不存在的行
  const alive = new Set(storeState.rows.map((r) => r.id));
  storeState.selected = new Set([...storeState.selected].filter((id) => alive.has(id)));

  renderStoreStats(data.stats);
  renderStoreTable(storeState.rows);
  refreshSelBar();
}

function renderStoreStats(stats) {
  const total = stats.total || 0;
  // 备份卡要显示「整库 N 条」；表格默认只拉 500 行，不能用 rows.length 顶替。
  storeState.total = total;
  $('#store-count').textContent = total;
  $('#store-db').textContent = stats.db_path || '';

  // 分区计数取整库，不跟搜索词 / 版本筛选联动：按钮回答的是「库里各有多少」。
  const zones = stats.zones || { normal: 0, risk: 0 };
  $('#store-zone-all').textContent = total;
  $('#store-zone-normal').textContent = zones.normal || 0;
  $('#store-zone-risk').textContent = zones.risk || 0;

  const realms = (stats.realms || []).map((r) => `
    <div class="stat-mini">
      <span class="pill ${REALM_PILL[r.key] || 'mute'}">${esc(r.label)}</span>
      <b>${r.count}</b>
    </div>`).join('');

  const states = (stats.states || []).map((s) => `
    <div class="stat-mini">
      <i style="background:${STATE_COLOR[s.key] || '#94a3b8'}"></i>
      <span>${esc(s.label)}</span><b>${s.count}</b>
    </div>`).join('');

  $('#store-stats').innerHTML = `
    <div class="stat-box">
      <h3>库内账号 <b>${total}</b></h3>
      <div class="mini-row">${realms || '<span class="hint">空库</span>'}</div>
    </div>
    <div class="stat-box">
      <h3>凭据状态</h3>
      <div class="mini-row">${states || '<span class="hint">空库</span>'}</div>
    </div>`;

  const history = stats.history || [];
  $('#history-count').textContent = history.length;
  $('#history-list').innerHTML = history.length
    ? history.map((h) => `
      <li>
        <b class="mono">${esc(h.batch)}</b> · 读 ${h.total} · 新增 ${h.inserted} ·
        更新 ${h.updated} · 跳过 ${h.skipped}
        <span class="hint">${esc(h.at)}</span>
      </li>`).join('')
    : '<li class="hint">还没有导入记录</li>';

  fillBatchSelect(stats.batches || []);
}

/**
 * 批次下拉。选中某个批次后列表只出那一批。
 *
 * 选项带上条数：同一批导了 200 条、后来又删了几条，下拉里的数字和库里的
 * 实际条数会对上，用户不用点进去数。
 */
function fillBatchSelect(list) {
  const sel = $('#store-batch');
  if (!sel) return;
  const keep = storeState.batch;
  sel.innerHTML = ['<option value="">全部批次</option>'].concat(
    list.map((b) => `<option value="${esc(b.batch)}">${esc(batchLabel(b.batch))} · ${b.count} 条</option>`)
  ).join('');
  // 选中的批次被清空了就回到「全部」，否则下拉会显示第一项但列表还在过滤
  const exists = list.some((b) => b.batch === keep);
  storeState.batch = exists ? keep : '';
  sel.value = storeState.batch;
}

/* ---------- 账户库出口 ---------- */

/** 并发的 loadStore 共用同一个节点表请求：池面板探测慢，重复打没意义。 */
let storeExitsInflight = null;

/** 读一次 mihomo 节点表 + 两区端口占用。失败不让账户库整页挂掉。 */
async function loadStoreExits(force) {
  if (storeState.exitsLoaded && !force) return;
  if (storeExitsInflight && !force) return storeExitsInflight;
  const run = async () => {
    try {
      const data = await apiGet('/api/store/exits');
      if (!data.ok) throw new Error(data.error || '读取节点表失败');
      storeState.nodes = data.nodes || [];
      storeState.available = data.available || [];
      storeState.busyRisk = data.busy_risk || [];
      storeState.exitsLoaded = true;
      renderStoreExitHint();
    } catch (err) {
      storeState.nodes = [];
      storeState.available = [];
      storeState.busyRisk = [];
      const el = $('#store-exit-hint');
      if (el) {
        el.textContent = `节点表读取失败：${err.message}（点「刷新节点」重试）`;
        el.classList.add('bad');
      }
    }
  };
  storeExitsInflight = run().finally(() => { storeExitsInflight = null; });
  return storeExitsInflight;
}

function renderStoreExitHint() {
  const el = $('#store-exit-hint');
  if (!el) return;
  el.classList.remove('bad');
  const parts = [`可用端口 ${storeState.available.length} 个`];
  if (storeState.busyRisk.length) {
    parts.push(`风控占用 ${storeState.busyRisk.join('、')}（一键分配会避开）`);
  }
  parts.push('出口列可逐行改，直连 = 不走代理；查积分按这一列出站');
  el.textContent = parts.join(' · ');
}

/**
 * 状态列：风控态优先。
 *
 * 以前只画 token_state，一个已经在封号名单里的号照样显示绿色「有效」——
 * 那一列回答的是「token 过没过期」，用户读成「这号正不正常」。现在进了
 * 风控区就先报风控，凭据状态降成小字 + title，需要时还能查。
 */
function statusCell(r) {
  const cred = `<span class="pill ${STATE_PILL[r.state] || 'mute'}">${esc(r.state_label)}</span>`;
  if (!r.recheck_state) return cred;
  const label = r.recheck_label || '封号中';
  return `<span class="pill bad" title="凭据：${esc(r.state_label)}；风控区：${esc(label)}">${esc(label)}</span>
    <small class="hint status-cred">凭据${esc(r.state_label)}</small>`;
}

/** 出口下拉：直连 + 节点表。手动改一行就是「自定义出口」。 */
function storeExitCell(r) {
  const cur = Number(r.exit_port || 0);
  const narrow = window.matchMedia('(max-width: 720px)').matches;
  const seen = new Set([0]);
  const opts = ['<option value="0">直连</option>'];
  storeState.nodes.forEach((n) => {
    const port = Number(n.port || 0);
    if (!port || seen.has(port)) return;
    seen.add(port);
    const name = n.name || n.listener || '';
    // 风控占用只在一键分配时排除，手动选仍然允许：那是用户自己下的判断。
    // 但选项里要写明，免得他以为这是个干净的槽。
    const taken = storeState.busyRisk.includes(port) ? '（风控占用）' : '';
    const text = narrow || !name ? String(port) : `${port} ${esc(name)}${taken}`;
    opts.push(`<option value="${port}"${port === cur ? ' selected' : ''}>${text}</option>`);
  });
  // 库里存着本轮没列出来的端口（池子重排过、还没点检测）也要显示，
  // 否则下拉静默跳回「直连」，看起来像分配丢了。
  if (cur && !seen.has(cur)) {
    opts.push(`<option value="${cur}" selected>${cur}（未检测到）</option>`);
  }
  const curName = (storeState.nodes.find((n) => Number(n.port) === cur) || {});
  const label = cur ? `${cur} ${curName.name || curName.listener || ''}`.trim() : '直连';
  return `<select class="store-exit-pick mono" data-id="${r.id}" title="出口：${esc(label)}">${opts.join('')}</select>`;
}

async function setStoreRowExit(id, port) {
  const num = Number(port) || 0;
  try {
    await api('/api/store/exit', { ids: [Number(id)], port: num });
    const row = storeState.rows.find((r) => r.id === Number(id));
    if (row) row.exit_port = num;   // 不重绘整表，下拉焦点和滚动位置都要保住
    toast(num ? `这一行改走 ${num}` : '这一行改回直连');
  } catch (err) {
    toast(err.message, true);
    await loadStore();
  }
}

/** 一键分配：勾了就只分勾的，没勾 = 当前全部正常账号。风控端口服务端会剔掉。 */
async function assignStoreExits() {
  const btn = $('#btn-store-assign');
  const picked = [...storeState.selected];
  const scope = picked.length ? `勾选的 ${picked.length} 个` : '全部正常账号';
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '分配中…';
  try {
    const data = await api('/api/store/assign', { mode: 'random', ids: picked });
    await loadStore();
    const excl = data.excluded || [];
    toast(`${scope}：${data.assigned} 个已分到 ${(data.ports || []).length} 个端口` +
      (excl.length ? `，避开风控端口 ${excl.join('、')}` : ''));
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

/** 一键直连：只动正常账号，风控那批的出口保持不动。 */
async function clearStoreExits() {
  const ids = storeState.selected.size
    ? [...storeState.selected]
    : storeState.rows.filter((r) => !r.recheck_state).map((r) => r.id);
  const normal = ids.filter((id) => {
    const r = storeState.rows.find((x) => x.id === id);
    return r && !r.recheck_state;
  });
  if (!normal.length) {
    toast('没有可改的正常账号', true);
    return;
  }
  if (!confirm(`把 ${normal.length} 个正常账号的出口改回直连？风控账号不受影响。`)) return;
  try {
    await api('/api/store/exit', { ids: normal, port: 0 });
    await loadStore();
    toast(`${normal.length} 个账号改回直连`);
  } catch (err) {
    toast(err.message, true);
  }
}

/**
 * fixed 布局下 colgroup 的列数必须等于实际显示的列数。
 *
 * 窄屏用 CSS 藏掉六个 td，但 col 元素不会跟着消失：留着 12 个 col 的话，
 * 可见的 7 个单元格会填进前 7 列，后 5 列还各占一份宽度，整张表被压到
 * 左边 7/12。这里按屏宽整组重写 colgroup，两边始终对齐。
 */
function syncStoreCols() {
  const cg = document.querySelector('#store-table colgroup');
  if (!cg) return;
  const narrow = window.matchMedia('(max-width: 720px)').matches;
  const widths = narrow
    ? [24, 88, 48, 58, 52, 76]              // 勾选/名称/版本/状态/积分/出口
    : [36, 132, 64, 88, 62, 92, 148, 112, 86, 92, 44, 92];
  cg.innerHTML = widths.map((w) => `<col style="width:${w}px">`).join('');
}

/**
 * 名称列后面的分区徽标：正常 / 风控一眼分开，不用去读状态列的档位名。
 * 窄屏靠 CSS 收成 6px 圆点，88px 的名称列不会被胶囊挤破。
 */
function zoneFlag(r) {
  // 账户库给 recheck_state，每日活跃接口给 risk/risk_label，两边共用这一个标记。
  const risk = !!(r.recheck_state || r.zone === 'risk' || r.risk);
  const label = risk ? '风控' : '正常';
  const tip = risk
    ? `风控账户 · ${r.recheck_label || r.risk_label || '复检名单'}`
    : '正常账户 · 不在复检名单里';
  return `<span class="zone-flag ${risk ? 'risk' : 'normal'}" title="${esc(tip)}">${label}</span>`;
}

function renderStoreTable(rows) {
  syncStoreCols();
  const body = $('#store-table tbody');
  if (!rows.length) {
    const filtering = storeState.q || storeState.zone || storeState.realm || storeState.batch;
    // 空库与「筛没了」是两回事：后者不该让人以为账号被删掉。
    body.innerHTML = '<tr><td colspan="12" class="empty-cell">' + (filtering
      ? '没有符合当前筛选的账户。切回「全部」或清空搜索词再试。'
      : '账户库是空的。先在「导入转换」里转一遍，再点「保存到账户库」。') + '</td></tr>';
    $('#store-hint').textContent = '共 0 条';
    return;
  }

  const sel = storeState.selected;
  body.innerHTML = rows.map((r) => `
    <tr data-id="${r.id}" data-zone="${r.recheck_state ? 'risk' : 'normal'}" class="${sel.has(r.id) ? 'sel' : ''}${r.recheck_state ? ' risk-row' : ''}">
      <td class="col-check"><input type="checkbox" class="row-check" data-id="${r.id}" ${sel.has(r.id) ? 'checked' : ''}></td>
      <td title="最近更新 ${esc(r.updated_at)}"><b>${esc(r.name)}</b>${zoneFlag(r)}${r.nickname && r.nickname !== r.name ? `<br><small class="hint">${esc(r.nickname)}</small>` : ''}</td>
      <td><span class="pill ${REALM_PILL[r.realm] || 'mute'}">${esc(r.realm_label || REALM_TEXT[r.realm] || '未知')}</span></td>
      <td class="mono col-uid" title="${esc(r.uid)}">${esc(shortUid(r.uid))}</td>
      <td class="col-state">${statusCell(r)}</td>
      <td class="col-credit">${creditCell(r)}</td>
      <td class="col-store-exit">${storeExitCell(r)}</td>
      <td class="col-batch">${batchCell(r)}</td>
      <td class="mono col-exp">${esc(r.expires) || '—'}</td>
      <td class="mono col-at" title="${esc(r.at)}">${esc(r.at)}</td>
      <td class="col-rt">${r.has_rt ? '<span class="pill ok">有</span>' : '<span class="hint">—</span>'}</td>
      <td class="row-actions">
        <button class="link" data-act="relogin" title="从官网重新登录这个账号，按本行覆盖 token 和资料">重登</button>
        <button class="link" data-act="rename">改名</button>
        <button class="link danger" data-act="delete">删除</button>
      </td>
    </tr>`).join('');

  const zoneText = storeState.zone === 'risk' ? '（仅风控账户）'
    : storeState.zone === 'normal' ? '（仅正常账户）' : '';
  $('#store-hint').textContent = `共 ${rows.length} 条${zoneText}`;
}

/**
 * 批次单元格：上一行是导入时间，下一行是批次号。
 *
 * 时间给「几点几分」，批次号给后 6 位（同一批的后 6 位就是它创建的时刻），
 * 完整值都在 title 里——表格宽度有限，两个都铺开会把 uid 列挤没。
 * 导入时就标了风控的，后面挂一个来源标签：那是「怎么进的风控区」，
 * 决定这个号还值不值得去敲门。
 */
function batchCell(r) {
  const when = r.imported_at ? shortTime(r.imported_at) : '';
  const code = r.batch ? String(r.batch).slice(-6) : '';
  // 风控来源做成一个小圆点：胶囊要 40px 宽，会把批次列撑到 124px，
  // 整张表跟着溢出。圆点 + title 一样能看出「怎么进的风控区」，不占地方。
  const risk = r.risk_label
    ? `<i class="dot-risk" title="进风控区：${esc(r.risk_label)}"></i>`
    : '';
  if (!when && !code) {
    return `<span class="hint">—</span>${risk}`;
  }
  const tip = [
    r.batch ? `批次 ${r.batch}` : '',
    r.imported_at ? `导入 ${r.imported_at}` : '',
    r.first_batch && r.first_batch !== r.batch ? `首次批次 ${r.first_batch}` : '',
  ].filter(Boolean).join('\n');
  return `<div class="batch" title="${esc(tip)}">
      <span class="batch-top"><b class="mono">${esc(when || '—')}</b>${risk}</span>
      <small class="mono">${esc(code)}</small>
    </div>`;
}

/**
 * 积分单元格。
 *
 * 三种「没有数字」的情况必须分开显示，否则用户会误判：
 *   没查过（checked_at = 0）→ 「未查」
 *   查了但上游拒绝      → 「失效」，并把原因挂在 title 上
 *   查了但网络故障      → 「未确认」，过一次再试
 * 只有真查到才显示数字，0 也照实显示成 0。
 */
function creditCell(r) {
  const known = typeof r.credit_remain === 'number';
  if (!r.checked_at && !r.credit_state) {
    return '<span class="hint">未查</span>';
  }
  // 这里不用 pill：左边「状态」列已经是 pill 了，两列都出红胶囊会让人
  // 分不清是「凭据过期」还是「积分没查到」。小字 + 颜色足够。
  if (r.credit_state === 'invalid') {
    return '<span class="credit-fail" title="' + esc(r.checked_note || '登录已过期') + '">失效</span>';
  }
  if (r.credit_state === 'skipped') {
    return '<span class="credit-skip" title="' + esc(r.checked_note || '无法校验') + '">不可查</span>';
  }
  if (r.credit_state === 'unknown') {
    return '<span class="credit-skip" title="' + esc(r.checked_note || '未确认') + '">未确认</span>';
  }
  if (!known) {
    return '<span class="hint">未查</span>';
  }
  const remain = Number(r.credit_remain);
  const size = typeof r.credit_size === 'number' ? Number(r.credit_size) : 0;
  const pct = size > 0 ? Math.max(0, Math.min(100, (remain / size) * 100)) : 0;
  const low = size > 0 && pct < 10 ? ' low' : '';
  const tip = size > 0
    ? `剩余 ${fmtNum(remain)} / 共 ${fmtNum(size)}（已用 ${fmtNum(r.credit_used || 0)}）\n查询于 ${r.checked}`
    : `剩余 ${fmtNum(remain)}\n查询于 ${r.checked}`;
  return `<div class="credit${low}" title="${esc(tip)}">
      <span class="credit-num">${fmtNum(remain)}</span>
      ${size > 0 ? `<span class="credit-bar"><i style="width:${pct.toFixed(1)}%"></i></span>` : ''}
    </div>`;
}

/** 积分数字：整数不带小数点，小数留两位。 */
function fmtNum(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  if (Math.abs(n - Math.round(n)) < 0.005) return String(Math.round(n));
  return n.toFixed(2);
}

/* ---------- 备份范围 ---------- */

/**
 * 备份范围跟着账户库表格走：勾了一部分就只备勾的，没勾 / 全勾就备当前
 * 筛选后的可见集，筛选全空且没勾 = 整库。
 *
 * filters 一起交给后端重算，不直接把 ids 当最终结果：搜索框刚敲完字、
 * 请求还没回来时，前端的 rows 可能是上一批数据，后端按同一套 SQL 筛一遍
 * 才不会把看不见的号打进包里。
 */
function backupScope() {
  const rows = storeState.rows || [];
  const visible = new Set(rows.map((r) => r.id));
  const ids = [...storeState.selected].filter((id) => visible.has(id));
  const filters = {
    zone: storeState.zone || '',
    realm: storeState.realm || '',
    q: storeState.q || '',
    batch: storeState.batch || '',
  };
  const filtered = Object.values(filters).some(Boolean);
  const partial = ids.length > 0 && ids.length < rows.length;
  return {
    scope: partial ? 'selected' : (filtered ? 'filtered' : 'all'),
    filters,
    ids: partial ? ids : [],
    count: partial ? ids.length : rows.length,
    filtered,
    partial,
  };
}

/** 范围指纹：筛选 / 勾选一变，上次生成的下载链接就不能再当成这次的结果。 */
function backupScopeKey(s) {
  return JSON.stringify([s.scope, s.filters, s.partial ? s.ids : []]);
}

function backupScopeLabel(s) {
  const bits = [];
  if (s.filters.zone) bits.push(s.filters.zone === 'risk' ? '风控区' : '正常区');
  if (s.filters.realm) bits.push(s.filters.realm === 'cn' ? '国内版' : '国际版');
  if (s.filters.q) bits.push(`搜索「${s.filters.q}」`);
  if (s.filters.batch) bits.push(`批次 ${s.filters.batch}`);
  if (s.partial) return `已勾选 ${s.count} 条（勾选优先，${bits.join(' · ') || '无筛选'}）`;
  if (s.filtered) return `${bits.join(' · ')} · ${s.count} 条（按筛选结果备份）`;
  return `全部 ${storeState.total || s.count} 条账号（无筛选、未勾选）`;
}

/** 分区 / 搜索 / 勾选 / 批次 / 版本任一变化都会走到这里。 */
function renderBackupScope() {
  const el = $('#backup-scope');
  if (!el) return;
  const s = backupScope();
  el.textContent = `当前备份范围：${backupScopeLabel(s)}`;
  el.classList.toggle('bad', s.count === 0);
  const btn = $('#btn-store-backup');
  if (btn) btn.disabled = s.count === 0;

  // 旧下载链接还挂着上一次的范围，标灰并写明要重新生成，避免误下载。
  const box = $('#backup-downloads');
  if (!box) return;
  const stale = !!(box.dataset.scopeKey && box.dataset.scopeKey !== backupScopeKey(s));
  box.classList.toggle('stale', stale);
  let note = $('#backup-stale');
  if (stale && !note) {
    note = document.createElement('p');
    note.id = 'backup-stale';
    note.className = 'hint bad';
    note.textContent = '范围已变化，点「下载备份包」重新生成；上面的链接还是上一次的范围。';
    box.insertAdjacentElement('beforebegin', note);
  } else if (!stale && note) {
    note.remove();
  }
}

/** 同步「全选」勾选态与底部操作条。 */
function refreshSelBar() {
  const total = storeState.rows.length;
  const picked = storeState.selected.size;

  document.querySelectorAll('#store-check-all, #store-check-all-top').forEach((box) => {
    box.checked = total > 0 && picked === total;
    box.indeterminate = picked > 0 && picked < total;
  });

  $('#sel-count').textContent = picked ? `已选 ${picked} 条` : '未选择';
  $('#btn-sel-probe').disabled = picked === 0;
  $('#btn-sel-export').disabled = picked === 0;
  $('#btn-sel-risk').disabled = picked === 0;
  // 一次只重登一个号：官方授权是单账号流程，多选没有明确的覆盖目标
  $('#btn-sel-relogin').disabled = picked !== 1;
  $('#btn-sel-unrisk').disabled = picked === 0;
  $('#btn-sel-delete').disabled = picked === 0;
  $('#btn-sel-clear').disabled = picked === 0;

  // 勾选集一变，备份卡的范围提示也要跟着变（含按钮禁用态）
  renderBackupScope();
}

/** 账户库里直接把选中的号移进 / 移出风控区。
 *  以前只能绕「重新导入 → 逐行标风控 → 保存」，中间任何一步没勾到，
 *  号就留在正常区而不在名单里。这里按行 id 改，落库后立刻重画。 */
async function setStoreRisk(state) {
  const ids = [...storeState.selected];
  if (!ids.length) return;
  try {
    const data = await api('/api/recheck/track', {
      ids, state, note: state ? '账户库标记' : '', track: 'banned',
    });
    storeState.selected.clear();
    await loadStore();
    toast(state
      ? `已标为风控：${data.changed} 条，在复检名单「封号中」这一档`
      : `已移出风控：${data.changed} 条`);
  } catch (err) {
    toast(err.message, true);
  }
}

function toggleSelect(id, on) {
  if (on) storeState.selected.add(id);
  else storeState.selected.delete(id);

  const tr = document.querySelector(`#store-table tr[data-id="${id}"]`);
  if (tr) tr.classList.toggle('sel', on);
  refreshSelBar();
}

function selectAll(on) {
  storeState.selected = on ? new Set(storeState.rows.map((r) => r.id)) : new Set();
  document.querySelectorAll('#store-table .row-check').forEach((box) => {
    box.checked = on;
    const tr = box.closest('tr');
    if (tr) tr.classList.toggle('sel', on);
  });
  refreshSelBar();
}

/**
 * 校验选中的账号（向上游只读查询）。
 *
 * 只读：服务端只打计费查询接口，不刷新 token、不签到。刷新会把别处正在
 * 用同一个账号的服务踢下线，所以这件事由用户在原系统里做，不在这里做。
 */
async function probeSelected() {
  const ids = [...storeState.selected];
  if (!ids.length) {
    toast('先勾选要校验的账号', true);
    return;
  }
  await runProbe('/api/probe', { ids }, $('#btn-sel-probe'), '校验');
}

/** 校验整库（按当前版本筛选）。账号多时要等一会儿。 */
async function probeAll() {
  if (!storeState.rows.length) {
    toast('账户库是空的', true);
    return;
  }
  const realm = storeState.realm || '';
  const n = storeState.rows.length;
  if (!confirm(`向上游逐个查询 ${n} 个账号的积分？\n\n只读操作，不会刷新 token，也不会影响这些账号在别处正在跑的服务。`)) {
    return;
  }
  await runProbe('/api/probe/all', { realm }, $('#btn-probe-all'), '校验全部');
}

async function runProbe(url, payload, btn, label) {
  const hint = $('#probe-hint');
  const old = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = '查询中…';
  }
  if (hint) {
    hint.hidden = false;
    hint.textContent = '正在向上游查询，请稍候…';
  }
  try {
    const data = await api(url, payload);
    const s = data.summary || {};
    const parts = [];
    // 「可用」放最前面：用户真正关心的是这个数，其余是它的解释
    if (typeof s.usable === 'number') parts.push(`可用 ${s.usable}`);
    if (s.valid) parts.push(`凭据有效 ${s.valid}`);
    if (s.exhausted) parts.push(`额度用完 ${s.exhausted}`);
    if (s.invalid) parts.push(`失效 ${s.invalid}`);
    if (s.unknown) parts.push(`未确认 ${s.unknown}`);
    if (s.skipped) parts.push(`不可查 ${s.skipped}`);
    if (s.with_credit) parts.push(`合计剩余 ${fmtNum(s.remain_total)} 积分`);
    // 风控判定单独说一句：这些号已经被挪进风控区，不去看名单会以为丢了
    if (data.restricted) parts.push(`风控 ${data.restricted}（已移入风控区）`);

    if (hint) {
      hint.textContent = `已校验 ${data.written || 0} 个账号：${parts.join(' · ') || '无结果'}`;
      // 全部失败时这行要显眼一点，否则用户以为查成功了
      hint.classList.toggle('bad', !s.valid && !s.with_credit);
    }
    toast(parts.length ? `校验完成：${parts[0]}` : '校验完成');
    await loadStore();
  } catch (err) {
    if (hint) {
      hint.hidden = false;
      hint.textContent = '校验失败：' + err.message;
      hint.classList.add('bad');
    }
    toast(err.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = label || old;
    }
  }
}

/** 导出勾选的行：勾了就只出勾的，不再按版本过滤。 */
async function exportSelected() {
  const ids = [...storeState.selected];
  if (!ids.length) {
    toast('先勾选要导出的账号', true);
    return;
  }
  if (!storeState.profile) {
    toast('先在下面选一种目标格式', true);
    return;
  }
  const btn = $('#btn-sel-export');
  btn.disabled = true;
  btn.textContent = '生成中…';
  try {
    const ep = exitProxyPayload();
    const data = await api('/api/store/export', {
      profile: storeState.profile, ids, ...(ep ? { exit_proxy: ep } : {}),
    });
    $('#store-downloads').innerHTML = data.files.map((f) => `
      <div class="dl">
        <div>
          <div class="dl-name">${esc(f.name)}</div>
          <div class="dl-size">${bytesText(f.size)} · 已选 ${ids.length} 条</div>
        </div>
        <a href="${esc(f.download)}" download="${esc(f.name)}">下载</a>
      </div>`).join('');
    toast(exitProxyToast(data, `已导出 ${ids.length} 条`));
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '导出';
  }
}

async function deleteSelected() {
  const ids = [...storeState.selected];
  if (!ids.length) return;
  if (!confirm(`从账户库删除选中的 ${ids.length} 条？token 会一起删掉。`)) return;
  try {
    const data = await api('/api/store/delete', { ids });
    storeState.selected.clear();
    await loadStore();
    toast(`已删除 ${data.removed} 条`);
  } catch (err) {
    toast(err.message, true);
  }
}

/** 2026-11-02 18:41:10 → 11-02 18:41。完整值放 title，列表里省宽度。 */
function shortTime(text) {
  const m = String(text || '').match(/^\d{4}-(\d{2}-\d{2})[ T](\d{2}:\d{2})/);
  return m ? `${m[1]} ${m[2]}` : String(text || '');
}

function shortUid(uid) {
  const text = String(uid || '');
  return text.length > 16 ? text.slice(0, 8) + '…' + text.slice(-4) : text;
}

async function saveToStore() {
  if (!state.job) {
    toast('先导入并转换一次，再保存到账户库', true);
    return;
  }
  const btn = $('#btn-save-store');
  const riskBox = $('#opt-risk');
  const risk = !!(riskBox && riskBox.checked);
  btn.disabled = true;
  try {
    const data = await api('/api/store/import', {
      job: state.job, opts: collectOpts(), risk,
      // 同上：勾选的才有份。marks 是逐行风控判断，覆盖整批的 risk
      keys: selectedKeys(),
      marks: riskPayload(),
    });
    const parts = [`新增 ${data.inserted}`, `更新 ${data.updated}`];
    if (data.skipped) parts.push(`跳过 ${data.skipped}`);
    // 风控批次要明说「进了哪」，否则用户以为存进正常区了
    if (data.risk) parts.push(`风控 ${data.risk_count || data.inserted + data.updated}`);
    if (data.marked) parts.push(`逐行标记 ${data.marked} 条`);
    toast(`已留存：${parts.join(' · ')}`);
    storeState.loaded = false;
    storeState.batch = '';
    await loadStore();
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function storeRename(id, current) {
  const next = prompt('新的名称（只影响显示，不参与去重）', current);
  if (next === null) return;
  const name = next.trim();
  if (!name || name === current) return;
  try {
    await api('/api/store/rename', { id, name });
    await loadStore();
    toast('已改名');
  } catch (err) {
    toast(err.message, true);
  }
}

async function storeDelete(id, name) {
  if (!confirm(`从账户库删除「${name}」？这条记录会连同它的 token 一起消失。`)) return;
  try {
    await api('/api/store/delete', { ids: [id] });
    await loadStore();
    toast('已删除 1 条');
  } catch (err) {
    toast(err.message, true);
  }
}

async function storeClear() {
  const total = Number($('#store-count').textContent || 0);
  if (!total) {
    toast('账户库已经是空的');
    return;
  }
  if (!confirm(`清空账户库？共 ${total} 条记录会被删除，导入历史也一并清掉。\n\n建议先点「下载备份包」留一份。`)) return;
  try {
    await api('/api/store/clear', {});
    await loadStore();
    toast('账户库已清空');
  } catch (err) {
    toast(err.message, true);
  }
}

/** 按表格当前筛选 / 勾选打包。范围由后端按同一套 SQL 复算，前端只负责传条件。 */
async function storeBackup() {
  const s = backupScope();
  if (!s.count) {
    toast('当前筛选没有可备份的账号', true);
    return;
  }
  const btn = $('#btn-store-backup');
  btn.disabled = true;
  btn.textContent = '打包中…';
  try {
    const data = await api('/api/store/backup', {
      scope: s.scope,
      filters: s.filters,
      ...(s.partial ? { ids: s.ids } : {}),
    });
    const f = data.file;
    const sc = data.scope || {};
    const count = Number(sc.count == null ? s.count : sc.count);
    const range = sc.mode === 'selected' ? `已勾选 ${count} 条`
      : sc.mode === 'filtered' ? `当前筛选 ${count} 条`
        : `整库 ${count} 条`;
    $('#backup-downloads').innerHTML = `
      <div class="dl">
        <div>
          <div class="dl-name">${esc(f.name)}</div>
          <div class="dl-size">${bytesText(f.size)} · ${range} · 含 accounts.db 与 accounts.json</div>
        </div>
        <a href="${esc(f.download)}" download="${esc(f.name)}">下载</a>
      </div>`;
    $('#backup-downloads').dataset.scopeKey = backupScopeKey(s);
    $('#backup-hint').textContent =
      `已打包 ${range}（库内共 ${sc.total == null ? storeState.total : sc.total} 条）。`
      + '包内含 token 明文，存到自己可控的位置。';
    toast(`备份包已生成（${range}）`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.textContent = '下载备份包';
    renderBackupScope();
  }
}

async function storeRestore(file) {
  if (!file) return;
  const mode = realmOf('restore-mode');
  if (mode === 'replace' && !confirm('覆盖还原会先清空当前账户库，再写入备份里的记录。继续？')) {
    return;
  }
  const btn = $('#btn-store-restore');
  btn.disabled = true;
  btn.textContent = '还原中…';
  try {
    const data64 = await toBase64(file);
    const data = await api('/api/store/restore', { name: file.name, data: data64, mode });
    await loadStore();
    $('#backup-hint').textContent =
      `已还原 ${data.restored} 条：新增 ${data.inserted} · 更新 ${data.updated} · 跳过 ${data.skipped}`;
    toast(`还原完成，新增 ${data.inserted} 条`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '选择备份文件还原';
  }
}

async function loadStoreProfiles() {
  if (!state.profiles.length) return;
  $('#store-profiles').innerHTML = state.profiles.map((p) => `
    <div class="profile" data-id="${esc(p.id)}">
      <div class="pf-top"><b>${esc(p.name)}</b><span class="tag">${esc(p.container)}</span></div>
      <small>${esc(p.project)}</small>
    </div>`).join('');

  document.querySelectorAll('#store-profiles .profile').forEach((el) => {
    el.addEventListener('click', () => {
      document.querySelectorAll('#store-profiles .profile').forEach((x) => x.classList.remove('sel'));
      el.classList.add('sel');
      storeState.profile = el.dataset.id;
      $('#btn-store-export').disabled = false;
      const p = state.profiles.find((x) => x.id === storeState.profile);
      $('#store-export-hint').textContent = `将生成：${p ? p.name : ''}`;
    });
  });
}

/**
 * 从库导出。范围规则和「导出」是同一条：
 *
 *   勾了一部分  → 只导勾选的那些（版本筛选此时不生效，用户已经用手划了范围）
 *   全勾选/全不勾 → 全部（按上面的版本选项走，支持分版本各一份）
 *
 * 全勾也算「全部」：那是「这批我都都要」，不是「只要屏幕上这几十条」。
 * 这两条规则由后端再判一次，前端算错也导不出一整库。
 */
async function storeExport() {
  if (!storeState.profile) {
    toast('先选择一种目标格式', true);
    return;
  }
  const total = storeState.rows.length;
  const picked = storeState.selected.size;
  const partial = picked > 0 && picked < total;
  const ids = partial ? [...storeState.selected] : [];

  const btn = $('#btn-store-export');
  btn.disabled = true;
  btn.textContent = '生成中…';
  try {
    const payload = {
      profile: storeState.profile,
      realm: realmOf('store-export-realm'),
      // 范围由这里明确说清：服务端不再靠「有没有 ids」猜
      scope: partial ? 'selected' : 'all',
    };
    const ep = exitProxyPayload();
    // 按每个账户自己的出口端口写 proxy，导出即存为默认配置
    if (ep) payload.exit_proxy = ep;
    if (ids.length) payload.ids = ids;
    const data = await api('/api/store/export', payload);
    const scope = partial ? `已选 ${ids.length} 条` : `全部 ${total} 条`;
    $('#store-downloads').innerHTML = data.files.map((f) => `
      <div class="dl">
        <div>
          <div class="dl-name">${esc(f.name)}</div>
          <div class="dl-size">${bytesText(f.size)} · ${scope}</div>
        </div>
        <a href="${esc(f.download)}" download="${esc(f.name)}">下载</a>
      </div>`).join('');
    toast(exitProxyToast(data, `已生成 ${data.files.length} 个文件（${scope}）`));
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '从库导出';
  }
}

/* ---------- 版本与升级 ---------- */

const updateState = { version: null, latest: null, canApply: false, checking: false };
/* 进度区的计时起点。重启要等几十秒，只写「正在重启」用户会以为卡死了。 */
const progressState = { started: 0 };

async function loadVersion() {
  const data = await apiGet('/api/version');
  const v = data.version || {};
  updateState.version = v;

  $('#ver-build').textContent = v.build && v.build !== 'dev' ? '构建 ' + v.build : '源码运行';
  $('#ver-grid').innerHTML = [
    ['版本号', 'v' + (v.version || '?')],
    ['通道', v.channel || 'stable'],
    ['构建时间', v.build || 'dev'],
    ['Python', v.python || ''],
  ].map(([k, val]) => `
    <div class="kv"><span>${esc(k)}</span><b>${esc(val)}</b></div>`).join('');

  const repo = data.update && data.update.repo ? data.update.repo : '';
  if (document.activeElement !== $('#upd-repo')) $('#upd-repo').value = repo;
  if (document.activeElement !== $('#upd-index')) $('#upd-index').value = (data.update && data.update.index_url) || '';
  if (document.activeElement !== $('#upd-proxy')) $('#upd-proxy').value = (data.update && data.update.proxy) || '';

  // 部署命令里的 {repo} 占位换成真实仓库名
  document.querySelectorAll('#view-update code').forEach((el) => {
    if (el.textContent.includes('{repo}')) {
      el.textContent = el.textContent.replace('{repo}', repo || 'owner/wk-switch');
    }
  });
}

function renderUpdate(info) {
  const dot = $('#update-dot');
  if (!info || info.ok === false) {
    $('#upd-line').textContent = '检查失败：' + ((info && info.error) || '未知原因');
    $('#upd-notes').textContent = '';
    $('#btn-apply-update').disabled = true;
    // 「只重启」不依赖检查结果，检查失败也得能点——它本身就是排障手段
    $('#btn-restart').disabled = false;
    if (dot) dot.hidden = true;
    return;
  }

  const rel = info.release || {};
  if (info.newer) {
    $('#upd-line').innerHTML = `发现新版本 <b>v${esc(info.latest)}</b>　当前 v${esc(info.current)}`;
    $('#upd-hint').textContent = rel.asset ? '包：' + rel.asset : '';
    $('#btn-apply-update').disabled = false;
    $('#btn-restart').disabled = false;
    if (dot) dot.hidden = false;
  } else {
    $('#upd-line').innerHTML = `已是最新版本 <b>v${esc(info.current)}</b>`;
    $('#upd-hint').textContent = rel.asset ? '制品库最新：v' + esc(info.latest) : '';
    $('#btn-apply-update').disabled = true;
    $('#btn-restart').disabled = false;
    if (dot) dot.hidden = true;
  }
  $('#upd-notes').textContent = rel.notes ? rel.notes.slice(0, 600) : '';
}

async function checkUpdate() {
  const btn = $('#btn-check-update');
  btn.disabled = true;
  btn.textContent = '检查中…';
  try {
    const info = await apiGet('/api/update/check');
    updateState.latest = info;
    renderUpdate(info);
    toast(info.ok === false ? info.error : (info.newer ? '有新版本 v' + info.latest : '已是最新'));
  } catch (err) {
    renderUpdate({ ok: false, error: err.message });
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '检查更新';
  }
}

/* 一键升级：一个请求里做完「下载 → 替换 → 交棒重启」，
   然后等新进程应答、自动刷新。不再有「升级完还要再点一次重启」这一步。 */
async function applyUpdate() {
  const btn = $('#btn-apply-update');
  btn.disabled = true;
  btn.textContent = '升级中…';
  showProgress('正在下载并替换代码…', '别关页面，完成后会自动重启');
  try {
    const data = await api('/api/update/upgrade', {});
    if (!data.upgraded) {
      $('#upd-line').textContent = data.message || '已经是最新版本';
      toast(data.message || '已是最新');
      hideProgress();
      return;
    }
    $('#upd-line').innerHTML = `已升级到 <b>v${esc(data.to)}</b>`;
    $('#upd-notes').textContent = `替换 ${data.files} 个文件，升级前代码已备份为 ${data.backup}`;
    if (!data.restarting) {
      failProgress('重启没交棒出去', (data.restart && data.restart.error) || '');
      return;
    }
    await waitForRestart(data.to, data.old_pid);
  } catch (err) {
    failProgress('升级失败', err.message);
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '一键升级并重启';
  }
}

async function applyLocal() {
  const input = $('#upd-file');
  const file = input.files && input.files[0];
  if (!file) {
    toast('先选一个 zip 包', true);
    return;
  }
  if (!/\.zip$/i.test(file.name)) {
    toast('只接受 .zip 升级包', true);
    return;
  }
  const btn = $('#btn-apply-local');
  btn.disabled = true;
  btn.textContent = '上传中…';
  showProgress('正在上传本地包…', file.name);
  try {
    const b64 = await new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => {
        const s = String(fr.result || '');
        resolve(s.slice(s.indexOf(',') + 1));
      };
      fr.onerror = () => reject(new Error('读取文件失败'));
      fr.readAsDataURL(file);
    });
    const data = await api('/api/update/local', { name: file.name, data: b64 });
    const verb = data.downgraded ? '回滚' : '升级';
    $('#upd-line').innerHTML = `已用本地包${verb}到 <b>v${esc(data.to)}</b>`;
    $('#upd-notes').textContent = `替换 ${data.files} 个文件，升级前代码已备份为 ${data.backup}`;
    input.value = '';
    if (!data.restarting) {
      failProgress('重启没交棒出去', (data.restart && data.restart.error) || '');
      return;
    }
    await waitForRestart(data.to, data.old_pid);
  } catch (err) {
    failProgress('本地包升级失败', err.message);
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '上传并升级';
  }
}

/* 只重启，不升级。升级流程已经自带重启，这个按钮留着当兜底。 */
async function restartServer() {
  const btn = $('#btn-restart');
  btn.disabled = true;
  btn.textContent = '重启中…';
  showProgress('正在重启服务…', '');
  let oldPid = 0;
  try {
    const data = await api('/api/update/restart', {});
    oldPid = data.old_pid || 0;
    if (!data.ok) {
      failProgress('重启没交棒出去', data.error || data.message || '');
      return;
    }
  } catch (err) {
    /* 进程正在退出，请求断掉是正常的；oldPid 拿不到就退化成只判版本 */
  } finally {
    btn.disabled = false;
    btn.textContent = '只重启服务';
  }
  await waitForRestart('', oldPid);
}

async function rollbackServer() {
  const btn = $('#btn-rollback');
  btn.disabled = true;
  showProgress('正在回滚到上一版…', '回滚同样会重启');
  try {
    const data = await api('/api/update/rollback', {});
    $('#upd-line').innerHTML = `已回滚到 <b>v${esc(data.to)}</b>`;
    $('#upd-notes').textContent = `来源 ${data.source || '备份'}；升级前代码已备份为 ${data.backup}`;
    await waitForRestart(data.to, data.old_pid);
  } catch (err) {
    failProgress('回滚失败', err.message);
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* 等新进程应答。
 *
 * 四个判据缺一不可：
 * 1. /api/ping 免鉴权 —— 会话在重启后可能还没补齐，用带鉴权的接口会一直 401；
 * 2. 版本号要对 —— 端口通了但版本还是旧的，说明起来的是别的进程；
 * 3. pid 必须换人 —— 旧进程在响应发出后还要活 0.5 秒，这期间 ping 是通的。
 *    只判「通了」会把旧进程当成新进程，页面立刻刷新，正好撞上它退出，
 *    浏览器看到的就是连接被重置——「重启出错」的观感有一半来自这里；
 * 4. 容忍连接失败 —— 重启中间那几秒服务是断的，报错是正常的，不能当失败。
 *
 * 总预算 120 秒，比后端「等端口 45s + 探活 60s」略短：真失败的话后端会先
 * 回滚，页面上看到的是「回滚后仍不可用」而不是干等。
 */
async function waitForRestart(targetVersion, oldPid) {
  const total = 120;
  let lastState = null;
  for (let i = 0; i < total; i += 1) {
    await new Promise((r) => setTimeout(r, 1000));
    updateProgressElapsed(i + 1);
    if (i % 3 === 0) {
      const st = await fetchUpdateStatus();
      if (st) {
        lastState = st;
        renderSteps(st);
        if (st.state === 'failed') {
          failProgress('重启失败', st.error || '');
          return;
        }
      }
    }
    try {
      const resp = await fetch('/api/ping', { cache: 'no-store' });
      if (!resp.ok) continue;
      const info = await resp.json();
      if (!info || !info.ok) continue;
      if (oldPid && Number(info.pid) === Number(oldPid)) {
        continue;   // 还是老进程，它还没退
      }
      if (targetVersion && String(info.version) !== String(targetVersion)) {
        continue;   // 还没换过来，继续等
      }
      finishProgress(`已重启到 v${info.version}`, '正在刷新页面…');
      await new Promise((r) => setTimeout(r, 800));
      location.reload();
      return;
    } catch (err) { /* 还没起来 */ }
  }
  failProgress('等了 120 秒服务没有回来', lastState ? (lastState.error || '') : '');
}

async function fetchUpdateStatus() {
  try {
    const resp = await fetch('/api/update/status', { cache: 'no-store' });
    if (!resp.ok) return null;
    const data = await resp.json();
    return (data && data.status) || null;
  } catch (err) {
    return null;
  }
}

/* ---------- 升级进度区 ---------- */
const STEP_TEXT = {
  preparing: '已交棒给监督进程',
  waiting: '等待旧进程退出、端口释放',
  starting: '拉起新进程',
  verifying: '等待新版本应答',
  rolledback: '新版本没起来，正在回滚',
  ok: '已就绪',
  failed: '失败',
};

function showProgress(title, sub) {
  const box = $('#upd-progress');
  if (!box) return;
  box.hidden = false;
  $('#upd-progress-title').textContent = title;
  $('#upd-progress-sub').textContent = sub || '';
  $('#upd-recover').hidden = true;
  $('#upd-log').hidden = true;
  $('#upd-steps').innerHTML = '';
  $('#upd-spinner').hidden = false;
  progressState.started = Date.now();
}

function updateProgressElapsed(seconds) {
  const sub = $('#upd-progress-sub');
  if (!sub || !progressState.started) return;
  const base = sub.textContent.split('　·　')[0];
  sub.textContent = `${base}　·　已等 ${seconds}s`;
}

function renderSteps(st) {
  const box = $('#upd-steps');
  if (!box || !st) return;
  const steps = Array.isArray(st.steps) ? st.steps.slice(-6) : [];
  box.innerHTML = steps.map((s) => {
    const t = bjTime(s.at);
    return `<li><span class="mono">${esc(t)}</span> ${esc(s.text || '')}</li>`;
  }).join('');
  if (st.state && STEP_TEXT[st.state]) {
    $('#upd-progress-title').textContent = STEP_TEXT[st.state];
  }
}

function finishProgress(title, sub) {
  $('#upd-spinner').hidden = true;
  $('#upd-progress-title').textContent = title;
  if (sub) $('#upd-progress-sub').textContent = sub;
  $('#upd-recover').hidden = true;
}

/* 失败时把服务端日志尾部摊开，并把回滚按钮亮出来——
   「重启失败」最烦的是不知道失败在哪，让人去翻 data/logs 等于没修。 */
function failProgress(title, detail) {
  $('#upd-spinner').hidden = true;
  $('#upd-progress-title').textContent = title;
  $('#upd-progress-sub').textContent = detail || '';
  $('#upd-recover').hidden = false;
  toast(title, true);
  fetchUpdateStatus().then((st) => {
    renderSteps(st);
    const tail = st && st.log_tail;
    if (tail) {
      const log = $('#upd-log');
      log.hidden = false;
      log.textContent = tail;
    }
  });
}

async function saveSource() {
  try {
    const data = await api('/api/update/config', {
      repo: $('#upd-repo').value.trim(),
      index_url: $('#upd-index').value.trim(),
      proxy: $('#upd-proxy').value.trim(),
    });
    toast('制品源已保存');
    $('#upd-hint').textContent = '仓库：' + (data.config.repo || '未设置');
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------- 封号复检 ---------- */

/* 名单状态 → 胶囊颜色。banned 用红（要处理），revived 用绿（已恢复），
   auth 用灰（不是封号，不该跟封号抢注意力）。 */
const RECHECK_PILL = { banned: 'bad', pending: 'warn', revived: 'ok', auth: 'mute' };
const RECHECK_TEXT = { banned: '封号中', pending: '待复检', revived: '已解封', auth: '凭据失效' };

/* 两档出口策略。文案要能回答「我该选哪个」，光写名字等于没说。

   「自定义」不再单列一档：指定某一行走哪个端口，改名单那一行的下拉就行，
   再单开一档全局地址表反而多一层要维护的东西。 */
const EXIT_TIPS = {
  same: '所有号从同一个端口出去：到下面点一张卡片（单选，点第二张会自动换过去）。用来验证封的是号还是 IP。',
  random: '每行在选中范围里各自分一个端口：一个都不点 = 全部端口，点了几条就只在这几条里。',
};

const recheckState = {
  cfg: null, rows: [], nodes: [], track: 'banned', q: '',
  selected: new Set(), running: false,
  //: 当前勾选的端口。只决定「分配时的范围」——真正生效的是每行的
  //: exit_port，删了这行也不影响复检走哪条线。
  picked: new Set(),
  //: 上一轮是「测过」还是「没测」，重渲染卡片时要原样传回去
  probed: false,
  //: 池子里的全部节点（含没分到端口的），卡片上的「改」用它列候选。
  //: {ok, nodes: [{name, port, delay_ms, pinned, kind}], error}
  pool: null,
};

async function loadRecheck() {
  // 下拉只认档位，不依赖设置和节点表：先发出去，别等 config / list 两个
  // 串行请求都回来才填。首屏空下拉会被读成「号没了」。
  const picker = loadRecheckPicker();
  const data = await apiGet('/api/recheck/config');
  if (!data.ok) throw new Error(data.error || '读取复检设置失败');
  recheckState.cfg = data.config;
  fillRecheckForm(data.config, data.nodes);
  renderTrackCounts(data.stats);
  await loadRecheckRows();
  await picker;
}

async function loadRecheckRows() {
  const params = new URLSearchParams();
  if (recheckState.track) params.set('track', recheckState.track);
  if (recheckState.q) params.set('q', recheckState.q);
  const data = await apiGet('/api/recheck/list?' + params.toString());
  if (!data.ok) throw new Error(data.error || '读取名单失败');
  recheckState.rows = data.rows || [];
  recheckState.selected.clear();
  renderRecheckTable();
  renderTrackCounts(data.stats);
}

function fillRecheckForm(cfg, disc) {
  $('#rc-enabled').checked = !!cfg.enabled;
  $('#rc-auto').checked = !!cfg.auto;
  const mode = EXIT_TIPS[cfg.exit_mode] ? cfg.exit_mode : 'random';
  const radio = document.querySelector(`input[name="rc-exit"][value="${mode}"]`);
  if (radio) radio.checked = true;
  applyExitMode(mode);
  recheckState.picked = new Set((cfg.exit_ports || []).map(Number).filter(Boolean));
  // 秒 → 分钟：库里存秒，界面上写分钟更好懂
  $('#rc-min-interval').value = Math.round((cfg.min_interval || 0) / 60);
  $('#rc-batch').value = cfg.batch_limit;
  $('#rc-gap').value = Math.round(cfg.gap_seconds || 3);
  $('#rc-timeout').value = Math.round(cfg.timeout || 25);
  $('#rc-auto-hour').value = cfg.auto_hour ?? 3;
  $('#rc-verify-proxy').value = cfg.verify_proxy || '';
  $('#rc-model').value = cfg.chat_model || 'deepseek-v4.1-flash';
  $('#rc-models').value = cfg.chat_models || '';
  $('#rc-paths').value = cfg.chat_paths || '';
  const mihomo = cfg.mihomo || {};
  $('#rc-host').value = mihomo.host || '';
  $('#rc-ports').value = mihomo.ports || '';
  $('#rc-api').value = mihomo.api || '';
  $('#rc-secret').value = mihomo.secret || '';
  // 这两个是 mihomo-pool 专用的：面板的 /api/v1/nodes 一次给出全部端口→
  // 节点名，比开三十条隧道反查便宜。留空也能用，后端按主机推 8006。
  $('#rc-pool').value = mihomo.pool || '';
  $('#rc-pool-token').value = mihomo.pool_token || '';
  if (disc) {
    // 打开页面就把控制器状态说清楚：只弹端口号的时候，用户第一反应是
    // 「功能没做」，而不是「地址没填对」，得先把原因摆出来。
    // 没点检测前不提控制器：那时候压根没去问（deep=false），报错是噪音。
    let line = disc.probed
      ? `检测到 ${disc.ok_count} / ${(disc.ports || []).length} 个可用出口`
      : '点「自动检测」开始测，节点名会一起读出来';
    if (disc.probed && !disc.named) {
      line += disc.api_error ? `｜${disc.api_error}` : '｜没拿到节点名，只显示端口';
    }
    $('#rc-detect-hint').textContent = line;
    $('#rc-detect-hint').classList.toggle('bad', disc.probed && !disc.named);
    renderNodes(disc.nodes || [], disc.probed, disc.pool);
  }
}

/* 切档：只显示当前这档要填的东西。三档同时摊开会让人以为都得填。 */
function applyExitMode(mode) {
  const pick = EXIT_TIPS[mode] ? mode : 'random';
  document.querySelectorAll('.rc-exit-pane').forEach((pane) => {
    pane.hidden = pane.dataset.exit !== pick;
  });
  const tip = $('#rc-exit-tip');
  if (tip) tip.textContent = EXIT_TIPS[pick];
}

function currentExitMode() {
  const on = document.querySelector('input[name="rc-exit"]:checked');
  return on ? on.value : 'random';
}

function renderNodes(nodes, probed, pool) {
  recheckState.nodes = nodes || [];
  recheckState.probed = !!probed;
  // pool 传 undefined 表示「这次没带」，保留上一次的候选表——重渲染卡片
  // 不该把已经拿到的节点列表清空。
  if (pool !== undefined) recheckState.pool = pool || null;
  const box = $('#rc-nodes');
  if (!recheckState.nodes.length) {
    box.innerHTML = '<p class="hint">没有配置端口</p>';
    return;
  }
  box.innerHTML = nodeRows(recheckState.nodes, probed);
  // 卡片是 innerHTML 重建的，选中态来自 picked；下拉要跟着补一遍
  syncExitSelects();
}

/* 点卡片 = 选/取消这个端口。
   单一出口是单选（点第二张换过去，不做取消——那档必须有一个出口）；
   随机出口是开关（再点一次取消，全不点 = 全部端口）。 */
async function togglePort(raw) {
  const port = Number(raw);
  if (!port) return;
  const mode = currentExitMode();
  const picked = recheckState.picked;
  if (mode === 'same') {
    picked.clear();
    picked.add(port);
  } else if (picked.has(port)) {
    picked.delete(port);
  } else {
    picked.add(port);
  }
  renderNodes(recheckState.nodes, recheckState.probed);
  await assignExits();
}

/* 分配请求串行化。
   每次分配都是「整份名单按当前范围重分一遍」，连点两张卡片会发两个请求。
   并发跑的话，先发的那个可能在后发的之后落库，最终状态就取决于网络顺序，
   而不是用户最后点的那个范围。排成一条链，最后一次一定是最后的状态。

   注意请求参数在**入队时**就快照下来（ports/mode 在函数开头取），
   不能在链里再读 recheckState.picked —— 那时候用户可能已经改了。 */
let assignChain = Promise.resolve();

function assignExits(ids) {
  if (!recheckState.rows.length && !(ids && ids.length)) {
    toast('复检名单还是空的，先把账号加进来', true);
    return Promise.resolve();
  }
  const mode = currentExitMode();
  const ports = [...recheckState.picked];
  const track = recheckState.track;
  const only = ids && ids.length ? ids : null;

  assignChain = assignChain.then(async () => {
    const data = await api('/api/recheck/assign', {
      mode, ports, ids: only, track,
    });
    // picked 不拿响应回写：它是本地的点击状态，服务端那份只是镜像。
    // 回写会让「点第二张时第一张的响应正好回来」把选择冲掉。
    if (data.rows) {
      recheckState.rows = data.rows;
      renderRecheckTable();
    }
    const n = data.assigned || 0;
    const scope = ports.length ? `${ports.length} 条端口` : '全部端口';
    toast(mode === 'same'
      ? `${n} 个号统一走 ${ports[0] || '第一个可用端口'}`
      : `${n} 个号已分到 ${scope}里`);
  }).catch((err) => toast(err.message, true));
  return assignChain;
}

/* 单行改出口（名单里的下拉）。0 = 直连。 */
async function setRowExit(id, port) {
  const data = await api('/api/recheck/exit', {
    ids: [Number(id)], port: Number(port) || 0, track: recheckState.track,
  });
  if (data.rows) {
    recheckState.rows = data.rows;
    renderRecheckTable();
  }
  toast(Number(port) ? `这一行改走 ${port}` : '这一行改回直连');
}

/* 出口下拉：直连 + 检测到的端口。手动改一行就是「自定义出口」。 */
function exitCell(r) {
  const cur = Number(r.exit_port || 0);
  const seen = new Set([0]);
  const opts = ['<option value="0">直连</option>'];
  // 窄屏只写端口号：那一列就那么宽，带上节点名会把号码也挤掉，而号码
  // 才是「这个号走哪条线」的答案。完整名字在 title 里。
  const narrow = window.matchMedia('(max-width: 720px)').matches;
  recheckState.nodes.forEach((n) => {
    const port = Number(n.port || 0);
    if (!port || seen.has(port)) return;
    seen.add(port);
    const name = n.name || n.listener || '';
    const text = narrow || !name ? String(port) : `${port} ${esc(name)}`;
    opts.push(`<option value="${port}"${port === cur ? ' selected' : ''}>${text}</option>`);
  });
  // 库里存着一个本轮没列出来的端口（池子重排过、或还没点检测）：也要能
  // 显示出来，否则下拉会静默跳回「直连」，看起来像分配丢了
  if (cur && !seen.has(cur)) {
    opts.push(`<option value="${cur}" selected>${cur} 未检测</option>`);
  }
  // title 放完整端口与节点名：这一列宽度有限，「17951 新加坡机房-02」会被
  // 浏览器截掉尾巴，悬停要能看全
  const curName = (recheckState.nodes.find((n) => Number(n.port) === cur) || {});
  const nodeName = curName.name || curName.listener || '';
  const label = cur ? `${cur} ${nodeName || '未检测到'}`.trim() : '直连';
  return `<select class="rc-exit-pick mono" data-id="${r.id}" title="出口：${esc(label)}">${opts.join('')}</select>`;
}

/* 卡片重建后把下拉的选中项对一遍：新版浏览器能记住 selected，但换档
   重渲染时不一定，显式设一次最稳。 */
function syncExitSelects() {
  document.querySelectorAll('#rc-table .rc-exit-pick').forEach((sel) => {
    const row = recheckState.rows.find((r) => String(r.id) === sel.dataset.id);
    if (!row) return;
    sel.value = String(row.exit_port || 0);
  });
}

/* 节点卡片：端口 + 节点名 + 类型 + 延迟。

   显示优先级是「节点名 > 端口号」，地址整串不占位。`127.0.0.1:` 这十一位
   对每一张卡片都一样，纯占地方；真正要分辨的是「哪个槽」——那五位端口
   就够了，名字有了之后连端口都只是配角。

   出口 IP 挪进 title：判断两个槽是不是同一条出口还得看它，但那是核对时
   才需要的东西，不该常驻在每张卡片上抢节点名的宽度。 */
function nodeRows(nodes, probed) {
  return nodes.map((n) => {
    const addr = String(n.url || '').replace(/^https?:\/\//, '');
    const port = String(n.port || addr.split(':').pop() || addr);
    const portNum = Number(port) || 0;
    const name = n.name || n.listener || '';
    const kind = String(n.kind || '');
    const kindCls = { '住宅': 'resi', '机房': 'dc', '代理': 'proxy' }[kind] || '';
    // 选中的卡片要说清楚「点了会发生什么」，所以提示里带上当前档位
    const hint = probed ? '' : '（点一下 = 选这个端口）';
    const tip = [name, addr, n.exit_ip ? `出口 ${n.exit_ip}` : '', hint]
      .filter(Boolean).join(' · ');

    const on = recheckState.picked.has(portNum);
    const cls = ['node'];
    if (probed) cls.push(n.ok ? 'ok' : 'bad');
    if (on) cls.push('picked');

    const portCell = `<span class="node-port mono">${esc(port)}</span>`;
    const nameCell = name
      ? `<span class="node-name" title="${esc(tip)}">${esc(name)}</span>`
      : `<span class="node-name none" title="${esc(tip)}">—</span>`;
    const kindCell = kind
      ? `<span class="node-kind ${kindCls}">${esc(kind)}</span>` : '';

    let tail;
    if (!probed) {
      tail = '<span class="node-tail hint">未检测</span>';
    } else if (n.ok) {
      tail = `<span class="node-tail">${n.latency_ms} ms</span>`;
    } else {
      tail = `<span class="node-tail" title="${esc(n.error || '')}">不可用</span>`;
    }

    // data-port 只有拿到真端口才给：点了没端口的卡片只会让分配算错
    const attr = portNum
      ? ` data-port="${portNum}" role="button" tabindex="0" aria-pressed="${on}"`
      : '';
    // 「改」只在池面板可用时才给：没有候选表，点开也是一片空白。
    // 也不能给 data-port——那会让外层的点卡片选中逻辑截走这次点击。
    const canBind = portNum && recheckState.pool && recheckState.pool.ok;
    const bindBtn = canBind
      ? `<button class="node-bind" data-bind-port="${portNum}" title="给 ${portNum} 换个节点">改</button>`
      : '';
    return `<div class="${cls.join(' ')}" title="${esc(tip)}"${attr}>${portCell}${nameCell}${kindCell}${tail}${bindBtn}</div>`;
  }).join('');
}

/* ---------- 端口换节点 ---------- */

const bindState = { port: 0, anchor: null };

/** 卡片上的「改」：列出池子里测通的节点，点一个就钉到这个端口。
 *
 *  为什么不用 <select> 直接塞卡片里：卡片是四列网格，宽度按最长的节点名
 *  算过，再塞个下拉会把端口号挤成两行。浮层还能带搜索——池子里几百个节点
 *  时，原生下拉只能滚。 */
function openBindPop(port, anchor) {
  const pop = $('#bind-pop');
  const pool = recheckState.pool;
  const node = recheckState.nodes.find((n) => Number(n.port) === port);

  bindState.port = port;
  bindState.anchor = anchor;
  $('#bind-title').textContent = `端口 ${port}` + (node && node.name ? ` · ${node.name}` : '');
  $('#bind-search').value = '';

  if (!pool || !pool.ok) {
    $('#bind-note').textContent = (pool && pool.error)
      ? `改不了：${pool.error}`
      : '改不了：没连上池面板（填上地址和 MPOOL_TOKEN，或先点「自动检测」）';
    $('#bind-list').innerHTML = '';
  } else {
    const usable = (pool.nodes || []).filter((n) => n.delay_ms != null);
    $('#bind-note').textContent =
      `池子里 ${(pool.nodes || []).length} 个节点，其中 ${usable.length} 个测通了；点一个就钉到这个端口`;
    renderBindList('');
  }

  pop.hidden = false;
  positionBindPop(anchor);
  if (pool && pool.ok) $('#bind-search').focus();
}

function positionBindPop(anchor) {
  const pop = $('#bind-pop');
  const rect = anchor.getBoundingClientRect();
  const w = pop.offsetWidth || 300;
  const h = pop.offsetHeight || 320;
  // 贴着按钮下方；下面放不下就翻到上方。左右都夹在视口里，卡片在最右列
  // 时浮层不会跑出屏幕。
  const left = Math.min(Math.max(8, rect.left), Math.max(8, window.innerWidth - w - 12));
  let top = rect.bottom + 6;
  if (top + h > window.innerHeight - 8) top = Math.max(8, rect.top - h - 6);
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}

function closeBindPop() {
  $('#bind-pop').hidden = true;
  bindState.port = 0;
  bindState.anchor = null;
}

function renderBindList(q) {
  const box = $('#bind-list');
  const pool = recheckState.pool;
  if (!pool || !pool.ok) return;

  const kw = String(q || '').trim().toLowerCase();
  const rows = (pool.nodes || [])
    // 只列测通的：没测过（delay_ms=null）的钉上去大概率不通，摆出来只会
    // 让人一条条试。要重新测就点「自动检测」。
    .filter((n) => n.delay_ms != null)
    .filter((n) => !kw
      || String(n.name || '').toLowerCase().includes(kw)
      || String(n.kind || '').toLowerCase().includes(kw))
    // 空闲的排前面：占用中的节点钉过去会被池子拒绝（端口冲突），
    // 排在后面让人先看到能用的
    .sort((a, b) => (a.port ? 1 : 0) - (b.port ? 1 : 0) || a.delay_ms - b.delay_ms);

  if (!rows.length) {
    box.innerHTML = '<p class="hint">没有匹配的可联通节点。先在池面板里测一轮，或换个关键词。</p>';
    return;
  }

  box.innerHTML = rows.slice(0, 300).map((n) => {
    const cur = Number(n.port) === bindState.port;
    const meta = [
      n.kind,
      n.port ? `占 ${n.port}` : '空闲',
      `${n.delay_ms} ms`,
    ].filter(Boolean).join(' · ');
    return `<button class="bind-item${cur ? ' cur' : ''}" data-node="${esc(n.name)}">
      <span class="bind-name">${esc(n.name)}</span>
      <span class="bind-meta">${esc(meta)}</span>
    </button>`;
  }).join('');
}

async function doBind(name) {
  const port = bindState.port;
  if (!port || !name) return;
  const cur = recheckState.nodes.find((n) => Number(n.port) === port);
  if (cur && (cur.name || '') === name) {
    toast(`端口 ${port} 本来就走 ${name}`);
    closeBindPop();
    return;
  }
  try {
    const data = await api('/api/recheck/bind', { port, node: name });
    // 绑定后池子会重排其它节点，端口映射可能整体变了，所以整张表要重画，
    // 不能只改这一张卡片。
    renderNodes(data.nodes || [], data.probed, data.pool);
    syncExitSelects();
    closeBindPop();
    // 占用者被挪走这件事必须说出来：不说的话，用户下次看到端口表里
    // 某个节点换了位置会以为是程序乱排
    if (data.moved) {
      toast(`端口 ${port} 已改走 ${name}；${data.moved.node} 挪到 ${data.moved.to}`);
    } else {
      toast(`端口 ${port} 已改走 ${name}`);
    }
  } catch (err) {
    // 池子的拒绝理由（端口被谁占了 / 节点不存在）比任何转述都准，原样显示
    $('#bind-note').textContent = err.message;
    toast(err.message, true);
  }
}

function renderTrackCounts(stats) {
  const banned = (stats && stats.banned) || 0;
  // 徽章跟当前档位走，不显示 tracked 总数：切到「已解封」却看到 3 个
  // 而表里只有 1 行，会让人以为分页丢了数据
  const badge = $('#rc-tracked');
  if (badge) badge.textContent = String(recheckState.rows.length);
  // 标签页红点只在「有封号」时亮——它是个待办信号，不是装饰
  const dot = $('#recheck-dot');
  if (dot) dot.hidden = banned === 0;
}

function renderRecheckTable() {
  const body = $('#rc-table tbody');
  if (!recheckState.rows.length) {
    body.innerHTML = '<tr class="rc-empty-row"><td colspan="10" class="empty-cell">这一档没有账号。导入带风控原因的账号会自动进名单。</td></tr>';
    $('#rc-hint').textContent = '';
    $('#rc-mobile-count').textContent = '0 条';
    syncRecheckSelection();
    return;
  }

  const sel = recheckState.selected;
  body.innerHTML = recheckState.rows.map((r) => {
    const state = r.recheck_state || '';
    const pill = RECHECK_PILL[state] || 'mute';
    const text = RECHECK_TEXT[state] || r.recheck_label || '—';
    return `
    <tr data-id="${r.id}" class="${sel.has(r.id) ? 'sel' : ''}">
      <td class="col-check"><input type="checkbox" class="rc-check" data-id="${r.id}" ${sel.has(r.id) ? 'checked' : ''}></td>
      <td title="${esc(r.name)}"><b>${esc(r.name)}</b>${r.nickname && r.nickname !== r.name ? `<br><small class="hint">${esc(r.nickname)}</small>` : ''}</td>
      <td class="col-rc-realm" data-label="版本"><span class="pill ${REALM_PILL[r.realm] || 'mute'}">${esc(r.realm_label || REALM_TEXT[r.realm] || '未知')}</span></td>
      <td class="col-rc-state" data-label="复检"><span class="pill ${pill}">${esc(text)}</span></td>
      <td class="col-credit" data-label="积分">${creditCell(r)}</td>
      <td class="col-rc-exit" data-label="出口">${exitCell(r)}</td>
      <td class="mono col-rc-count" data-label="次数">${r.recheck_count || 0}</td>
      <td class="mono col-rc-at" data-label="上次">${esc(r.recheck) || '—'}</td>
      <td class="rc-note col-rc-note" data-label="结论" title="${esc(r.recheck_note || '')}">${esc(trimNote(r.recheck_note)) || '<span class="hint">—</span>'}</td>
      <td class="row-actions"><button class="link danger" data-act="untrack">移出</button></td>
    </tr>`;
  }).join('');

  $('#rc-hint').textContent = `共 ${recheckState.rows.length} 条`;
  $('#rc-mobile-count').textContent = `共 ${recheckState.rows.length} 条`;
  syncExitSelects();
  syncRecheckSelection();
}

/* 结论原文可能很长（上游原话），表格里只留前半段，完整内容挂 title */
function trimNote(text) {
  const s = String(text || '').trim();
  return s.length > 46 ? s.slice(0, 46) + '…' : s;
}

function syncRecheckSelection() {
  const n = recheckState.selected.size;
  $('#rc-sel-count').textContent = n ? `已选 ${n} 条` : '未选择';
  // 两处全选框：桌面在表头，手机在卡片列表上方，状态必须一起走
  const total = recheckState.rows.length;
  ['#rc-check-all', '#rc-check-all-mobile'].forEach((k) => {
    const el = $(k);
    if (!el) return;
    el.checked = total > 0 && n === total;
    el.indeterminate = n > 0 && n < total;
  });
  ['#btn-rc-sel-probe', '#btn-rc-diagnose', '#btn-rc-sel-exit',
   '#btn-rc-sel-remove', '#btn-rc-sel-clear'].forEach((k) => {
    $(k).disabled = n === 0;
  });
}

async function saveRecheckCfg() {
  const cfg = {
    enabled: $('#rc-enabled').checked,
    auto: $('#rc-auto').checked,
    exit_mode: currentExitMode(),
    // 勾选端口一起存：下次打开页面要看到上次点的是哪几个。真正的出口
    // 在每行的 exit_port 上，这两个字段只是「范围」
    exit_ports: [...recheckState.picked],
    // 分钟 → 秒。后端会把小于 10 分钟的值钳回 600 秒，这里不用重复校验。
    min_interval: Math.max(10, Number($('#rc-min-interval').value) || 360) * 60,
    batch_limit: Number($('#rc-batch').value) || 20,
    gap_seconds: Number($('#rc-gap').value) || 3,
    timeout: Number($('#rc-timeout').value) || 25,
    auto_hour: Number($('#rc-auto-hour').value) || 0,
    // 固定验证代理：留空 = 按每行 exit_port 走，填了覆盖全部行
    verify_proxy: $('#rc-verify-proxy').value.trim(),
    // 裸模型名。留空时后端回默认值，不在这里硬编码第二份
    chat_model: $('#rc-model').value.trim(),
    // 留空 = 后端用内置链（含国际版 -sg 别名），不在这里复制一份默认值
    chat_models: $('#rc-models').value.trim(),
    chat_paths: $('#rc-paths').value.trim(),
    mihomo: {
      host: $('#rc-host').value.trim(),
      ports: $('#rc-ports').value.trim(),
      // 控制器地址留空时后端按 host 推 9090；secret 只在设了才发
      api: $('#rc-api').value.trim(),
      secret: $('#rc-secret').value.trim(),
      pool: $('#rc-pool').value.trim(),
      pool_token: $('#rc-pool-token').value.trim(),
    },
  };
  const data = await api('/api/recheck/config', { config: cfg });
  recheckState.cfg = data.config;
  fillRecheckForm(data.config, data.nodes);
  renderTrackCounts(data.stats);
  $('#rc-save-hint').textContent = `已保存 · ${bjTime(Date.now() / 1000)}`;
  toast('复检设置已保存');
}

async function detectNodes() {
  const btn = $('#btn-rc-detect');
  btn.disabled = true;
  // 读节点名要对着每个端口各挂一条隧道再读连接表，比单纯测延迟慢一点，
  // 按钮上给个进行中的字，免得以为没点上
  btn.textContent = '检测中…';
  $('#rc-detect-hint').textContent = '正在检测…';
  try {
    const data = await api('/api/recheck/nodes', {
      mihomo: {
        host: $('#rc-host').value.trim(),
        ports: $('#rc-ports').value.trim(),
        api: $('#rc-api').value.trim(),
        secret: $('#rc-secret').value.trim(),
        pool: $('#rc-pool').value.trim(),
        pool_token: $('#rc-pool-token').value.trim(),
      },
    });
    if (!data.ok) throw new Error(data.error || '检测失败');
    // 拿不到节点名就把控制器的具体错因贴出来：地址错、要 secret、
    // 端点不存在，三种处置完全不同，只说「未通」等于让人自己猜。
    let line = `检测到 ${data.ok_count} / ${(data.ports || []).length} 个可用出口`;
    if (!data.named) {
      line += '｜' + shortApiError(data.api_error);
    }
    // 一个都不通时补一句排查方向，但**不能覆盖**上面那行：全不通最常见
    // 的原因就是主机名在这台机器上解析不到，把控制器错因冲掉正好丢掉
    // 唯一的线索。
    if (!data.ok_count) {
      line += '｜一个都没通：确认 mihomo 在跑、端口对、本机能访问这个主机名';
    }
    $('#rc-detect-hint').textContent = line;
    $('#rc-detect-hint').classList.toggle('bad', !data.named || !data.ok_count);
    renderNodes(data.nodes || [], true, data.pool);
  } catch (err) {
    // 以前这里没有 catch：接口 500 时按钮恢复、提示停在「正在检测…」，
    // 看起来像卡死。
    $('#rc-detect-hint').textContent = err.message;
    $('#rc-detect-hint').classList.add('bad');
  } finally {
    btn.disabled = false;
    btn.textContent = '自动检测';
  }
}

/* 控制器地址在提示里是噪音：它就在上面那两栏里写着，占掉十几个字符只会
   把真正有用的后半句挤到看不见。去掉开头的 URL 再显示。 */
function shortApiError(text) {
  const raw = String(text || '').trim();
  if (!raw) return '没拿到节点名，只显示端口';
  // 以前这里会剥掉开头的 URL 省宽度。但现在提示是多条原因拼起来的
  //（9090 要 secret / 8006 Token 不对 / 反查卡在哪一步），剥掉第一段的
  // 地址就只剩「返回 401：…」，看不出是哪一步断的。换成 flex 换行后
  // 宽度不再是问题，原样显示。
  return raw;
}

/** 单号诊断：把 400 的逐次尝试和上游原话摊开，不写库。
 *  复检本身会改名单状态，排查「为什么还是 400」时不该带这个副作用。 */
async function diagnoseSelected() {
  const ids = [...recheckState.selected];
  if (!ids.length) { toast('先选一个账号', true); return; }
  const out = $('#rc-result');
  out.hidden = false;
  out.classList.remove('bad');
  out.textContent = '正在诊断：逐个试模型 / 请求体 / 路径…';
  try {
    const data = await api('/api/recheck/diagnose', { ids: [ids[0]] });
    renderDiagnose(data);
  } catch (err) {
    out.classList.add('bad');
    out.textContent = err.message;
  }
}

function renderDiagnose(data) {
  const out = $('#rc-result');
  const r = data.result || {};
  const cfg = data.config || {};
  const tries = r.attempts || [];
  const rows = tries.map((a, i) => `
    <tr><td class="mono">${i + 1}</td><td class="mono">${esc(a.model)}</td>
    <td class="mono">${esc(a.body)}</td><td class="mono">${esc(a.url)}</td>
    <td class="mono">${esc(String(a.status))}</td><td>${esc(a.reply || '')}</td></tr>`).join('');
  const verdict = r.verdict === 'revived' ? '对话已通' : (r.detail || '没拿到可用应答');
  out.innerHTML = `
    <div class="diag-head"><b>诊断 #${esc(String(data.id))}</b>：${esc(verdict)}
      <span class="hint">主模型 ${esc(cfg.chat_model || '')}｜候选 ${esc(cfg.chat_models || '内置')}｜路径 ${esc(cfg.chat_paths || '内置')}｜出口 ${esc(String(cfg.exit_port || '直连'))}｜打通于 ${esc(r.model || '—')} / ${esc(r.body || '—')}</span>
    </div>
    <div class="diag-wrap"><table class="grid diag-table">
      <thead><tr><th>#</th><th>模型</th><th>请求体</th><th>地址</th><th>状态</th><th>上游原话</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="empty-cell">没发出请求（本地就跳过了）</td></tr>'}</tbody>
    </table></div>
    <p class="hint">共 ${tries.length} 次尝试；这次诊断不写库，名单状态和次数都没变。</p>`;
  out.classList.toggle('bad', r.verdict === 'unknown');
}

async function runRecheck(force, ids) {
  if (recheckState.running) return;
  const cfg = recheckState.cfg;
  const out = $('#rc-result');
  out.hidden = false;
  out.classList.remove('bad');
  // 以前「没开总开关 / 接口报错」只弹一个两秒的 toast，页面看起来像
  // 完全没反应。所有结果都写进这块常驻区域，失败也留下原文。
  if (cfg && !cfg.enabled) {
    out.classList.add('bad');
    out.textContent = '复检没跑：总开关是关的。到上面的「复检设置」打开「启用封号复检」并保存，再点这里。';
    toast('封号复检没开启，先打开上面的总开关', true);
    return;
  }
  recheckState.running = true;
  out.textContent = force ? '正在复检（无视间隔）…' : '正在复检…';
  const buttons = ['#btn-rc-run', '#btn-rc-force', '#btn-rc-sel-probe'].map($);
  const labels = buttons.map((b) => b.textContent);
  buttons.forEach((b) => { b.disabled = true; });
  buttons[1].textContent = '复检中…';

  const payload = { force: !!force };
  if (ids && ids.length) payload.ids = ids;
  else payload.track = recheckState.track;

  try {
    const data = await api('/api/recheck/run', payload);
    const s = data.summary || {};
    const bits = [
      `解封 ${s.revived || 0}`,
      `仍封 ${s.banned || 0}`,
      `凭据失效 ${s.auth || 0}`,
      `未确认 ${s.unknown || 0}`,
    ];
    if (s.skipped) bits.push(`跳过 ${s.skipped}（未到间隔）`);
    // 出口 IP 一起报：判断「封的是号还是 IP」全靠这一行，翻进去看列表是看不出来的
    const ips = [...new Set((data.results || []).map((r) => r.proxy_ip).filter(Boolean))];
    let line = `本轮 ${data.written} 个：` + bits.join(' · ');
    if (ips.length) {
      const head = ips.slice(0, 4).join(' / ');
      line += ` ｜ 出口 ${head}${ips.length > 4 ? ` 等 ${ips.length} 个` : ''}`;
    }
    // 固定验证代理也印出来：核对「这一趟是不是真从 7891 出去的」不用翻日志
    const prox = [...new Set((data.results || []).map((r) => r.proxy).filter(Boolean))];
    if (prox.length) line += ` ｜ 代理 ${prox.slice(0, 2).join(' / ')}`;
    out.textContent = line;
    if (!s.revived && (s.banned || s.unknown)) out.classList.add('bad');
    await loadRecheckRows();
  } catch (err) {
    out.classList.add('bad');
    out.textContent = '复检失败：' + (err.message || String(err));
    toast(err.message || String(err), true);
  } finally {
    recheckState.running = false;
    buttons.forEach((b, i) => { b.textContent = labels[i]; });
    buttons[0].disabled = false;
    buttons[1].disabled = false;
    syncRecheckSelection();
  }
}

async function trackSelected(state) {
  const ids = [...recheckState.selected];
  if (!ids.length) return;
  const data = await api('/api/recheck/track', { ids, state, track: recheckState.track });
  recheckState.selected.clear();
  renderTrackCounts(data.stats);
  await loadRecheckRows();
  toast(state ? `已加入名单：${data.changed} 条` : `已移出名单：${data.changed} 条`);
}

/* 手工把某个号拖进名单：下拉里列出「可加入当前档」的账号。
   只排除已经在目标档里的号，在别的档里的号照样列（带档位标签）——
   用户把号从正常区挪进风控区之后，名单里看不见、下拉里也选不到，
   看起来就是号被删了。搜索走后端，能覆盖账户库默认 500 行之外的老号。 */
let pickerTimer = null;
let pickerSeq = 0;
async function loadRecheckPicker() {
  const seq = ++pickerSeq;
  const select = $('#rc-add-select');
  const params = new URLSearchParams();
  params.set('track', recheckState.track || 'banned');
  const q = ($('#rc-add-search') && $('#rc-add-search').value || '').trim();
  if (q) params.set('q', q);
  const zone = ($('#rc-add-zone') && $('#rc-add-zone').value || '').trim();
  if (zone) params.set('zone', zone);
  try {
    const data = await apiGet('/api/recheck/picker?' + params.toString());
    if (seq !== pickerSeq) return;   // 搜索词又变了，旧结果别盖上来
    if (!data.ok) return;
    const rows = data.rows || [];
    const keep = select.value;
    const head = rows.length
      ? '<option value="">从账户库里选一个账号…</option>'
      : `<option value="">${q ? '没搜到账号' : '这一档已包含全部账号'}</option>`;
    // 下拉按「正常账户 / 风控账户」分组。混成一串 option 靠后缀分辨太费眼，
    // 也看不出某一组是不是压根没号。zone 由后端给，前端不再自己推档位。
    const groups = [
      { key: 'normal', label: '正常账户' },
      { key: 'risk', label: '风控账户' },
    ];
    let html = head;
    groups.forEach((g) => {
      const list = rows.filter((r) => (r.zone || (r.state ? 'risk' : 'normal')) === g.key);
      if (!list.length) return;
      html += `<optgroup label="${g.label} · ${list.length} 个">`
        + list.map((r) => {
          const tag = r.state ? (r.state_label || '风控账户') : '正常账户';
          return `<option value="${r.id}">${esc(r.name)} · ${esc(r.realm_label || '')} · ${esc(tag)}</option>`;
        }).join('')
        + '</optgroup>';
    });
    select.innerHTML = html;
    if (keep) select.value = keep;
  } catch (err) {
    // 选人列表拿不到不影响主流程，静默
  }
}

/* ---------- 视图切换 ---------- */

function switchView(view) {
  document.querySelectorAll('.tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.view === view);
  });
  $('#view-convert').classList.toggle('hidden', view !== 'convert');
  $('#view-store').classList.toggle('hidden', view !== 'store');
  $('#view-activity').classList.toggle('hidden', view !== 'activity');
  $('#view-recheck').classList.toggle('hidden', view !== 'recheck');
  $('#view-update').classList.toggle('hidden', view !== 'update');
  if (view === 'store') {
    loadStore().catch((err) => toast(err.message, true));
    loadStoreProfiles();
  }
  if (view === 'activity') {
    loadActivity().catch((err) => toast(err.message, true));
  }
  if (view === 'recheck') {
    loadRecheck().catch((err) => toast(err.message, true));
  }
  if (view === 'update') {
    loadVersion().catch((err) => toast(err.message, true));
  }
}

/* ---------- 事件绑定 ---------- */

function bind() {
  const dz = $('#dropzone');
  const input = $('#fileInput');

  dz.addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    handleFiles(input.files);
    input.value = '';
  });

  ['dragenter', 'dragover'].forEach((ev) => {
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.add('over');
    });
  });
  ['dragleave', 'drop'].forEach((ev) => {
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.remove('over');
    });
  });
  dz.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files) handleFiles(e.dataTransfer.files);
  });

  // 选项变更自动重算，不用点按钮
  ['#opt-dedupe-key', '#opt-dedupe-policy', '#opt-require-token',
   '#opt-allow-envelope', '#opt-allow-expired', '#opt-only-enabled'].forEach((sel) => {
    $(sel).addEventListener('change', () => {
      if (state.job) runConvert();
    });
  });

  $('#btn-export').addEventListener('click', doExport);

  /* --- 预览表的勾选 / 风控 / 删除 --- */
  // 全选框有两个（表头和工具栏），行为一致：勾上 = 当前显示的全选
  ['#pick-all', '#pick-all-top'].forEach((sel) => {
    const box = $(sel);
    if (!box) return;
    box.addEventListener('change', () => {
      state.picked.clear();
      if (box.checked) state.rows.forEach((r) => state.picked.add(r.key));
      renderPreview(state.rows, state.total);
    });
  });

  // 行内勾选：委托，因为 tbody 每次都是重建的
  $('#preview-table').addEventListener('change', (e) => {
    const box = e.target.closest('.pick-box');
    if (!box) return;
    const tr = box.closest('tr');
    const key = tr && tr.dataset.key;
    if (!key) return;
    if (box.checked) state.picked.add(key);
    else state.picked.delete(key);
    tr.classList.toggle('picked', box.checked);
    refreshPickBar();
  });

  // 风控列：点一下循环「跟随整批 → 风控 → 正常」
  $('#preview-table').addEventListener('click', (e) => {
    const chip = e.target.closest('.risk-chip');
    if (!chip) return;
    cycleRisk(chip.dataset.riskKey);
  });

  // 批量给勾选的行打标记
  $('#btn-pick-risk').addEventListener('click', () => {
    state.picked.forEach((key) => state.marks.set(key, true));
    renderPreview(state.rows, state.total);
    toast(`已把 ${state.picked.size} 条标为风控`);
  });
  $('#btn-pick-safe').addEventListener('click', () => {
    state.picked.forEach((key) => state.marks.set(key, false));
    renderPreview(state.rows, state.total);
    toast(`已把 ${state.picked.size} 条标为正常`);
  });
  $('#btn-pick-clear').addEventListener('click', () => {
    state.picked.clear();
    renderPreview(state.rows, state.total);
  });
  $('#btn-pick-drop').addEventListener('click', dropPicked);

  /* --- 视图切换 --- */
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => switchView(tab.dataset.view));
  });

  /* --- 账户库 --- */
  $('#btn-save-store').addEventListener('click', saveToStore);
  $('#btn-store-clear').addEventListener('click', storeClear);
  $('#btn-store-backup').addEventListener('click', storeBackup);
  $('#btn-store-restore').addEventListener('click', () => $('#restoreInput').click());
  $('#restoreInput').addEventListener('change', (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    storeRestore(file);
  });
  $('#btn-store-export').addEventListener('click', storeExport);

  /* --- 封号复检 --- */
  document.querySelectorAll('input[name="rc-exit"]').forEach((el) => {
    el.addEventListener('change', () => {
      if (!el.checked) return;
      applyExitMode(el.value);
      // 切档时把范围收一下，两个档的语义本来就不一样：
      //   random：清空 = 全部端口，这是「没人点」的默认状态。留着上一档
      //           单选的那一个，会变成「随机但只有一个可选」，看着像没生效。
      //   same  ：只能有一个。多个就留第一个，一个都没有就交给后端取
      //           第一个可用端口——这一档必须有一个出口。
      const picked = recheckState.picked;
      if (el.value === 'random') {
        picked.clear();
      } else if (picked.size > 1) {
        const first = [...picked][0];
        picked.clear();
        picked.add(first);
      }
      renderNodes(recheckState.nodes, recheckState.probed);
      // 名单是空的就不发请求——那会立刻弹一句「名单里没有账号」，
      // 而用户只是在看看设置。
      if (recheckState.rows.length) {
        assignExits().catch((err) => toast(err.message, true));
      }
    });
  });
  // 点卡片选端口：事件委托，卡片是 innerHTML 重建的，逐张绑会丢
  $('#rc-nodes').addEventListener('click', (e) => {
    // 「改」按钮在卡片里面，必须拦在选中逻辑之前：它没有 data-port，
    // 但 closest('.node[data-port]') 会顺着往上找到外层卡片。
    const bind = e.target.closest('.node-bind');
    if (bind) {
      e.stopPropagation();
      openBindPop(Number(bind.dataset.bindPort), bind);
      return;
    }
    const card = e.target.closest('.node[data-port]');
    if (!card) return;
    togglePort(card.dataset.port).catch((err) => toast(err.message, true));
  });
  // 键盘也能选：卡片挂了 role=button，回车/空格要跟点击一个效果
  $('#rc-nodes').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const card = e.target.closest('.node[data-port]');
    if (!card) return;
    e.preventDefault();
    togglePort(card.dataset.port).catch((err) => toast(err.message, true));
  });
  $('#btn-rc-save').addEventListener('click', () => {
    saveRecheckCfg().catch((err) => toast(err.message, true));
  });
  $('#btn-rc-detect').addEventListener('click', () => {
    detectNodes().catch((err) => toast(err.message, true));
  });
  $('#btn-rc-run').addEventListener('click', () => {
    runRecheck(false).catch((err) => toast(err.message, true));
  });
  $('#btn-rc-force').addEventListener('click', () => {
    runRecheck(true).catch((err) => toast(err.message, true));
  });
  $('#btn-rc-sel-probe').addEventListener('click', () => {
    runRecheck(true, [...recheckState.selected]).catch((err) => toast(err.message, true));
  });
  $('#btn-rc-diagnose').addEventListener('click', () => {
    diagnoseSelected().catch((err) => toast(err.message, true));
  });
  $('#btn-rc-sel-exit').addEventListener('click', () => {
    assignExits([...recheckState.selected])
      .catch((err) => toast(err.message, true));
  });
  $('#btn-rc-sel-remove').addEventListener('click', () => {
    trackSelected('').catch((err) => toast(err.message, true));
  });
  $('#btn-rc-sel-clear').addEventListener('click', () => {
    recheckState.selected.clear();
    renderRecheckTable();
  });

  /* --- 端口换节点浮层 --- */
  $('#bind-close').addEventListener('click', closeBindPop);
  $('#bind-search').addEventListener('input', (e) => renderBindList(e.target.value));
  $('#bind-list').addEventListener('click', (e) => {
    const item = e.target.closest('.bind-item');
    if (!item) return;
    doBind(item.dataset.node);
  });
  // 点别处 / 按 Esc 收起。用捕获阶段，免得被页面里别的 stopPropagation 挡掉
  document.addEventListener('mousedown', (e) => {
    const pop = $('#bind-pop');
    if (pop.hidden) return;
    if (pop.contains(e.target)) return;
    if (bindState.anchor && bindState.anchor.contains(e.target)) return;
    closeBindPop();
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#bind-pop').hidden) closeBindPop();
  });
  // 窗口尺寸变了浮层会错位，直接收起来最省事
  window.addEventListener('resize', () => {
    if (!$('#bind-pop').hidden) closeBindPop();
  });
  $('#btn-rc-add').addEventListener('click', async () => {
    const id = Number($('#rc-add-select').value);
    if (!id) {
      toast('先选一个账号', true);
      return;
    }
    // 加入的目标 = 页面正在看的那一档。以前写死 pending，而表格默认显示
    // banned，加完号就像没生效；现在同时把表格切到目标档并清掉搜索词。
    const track = recheckState.track || 'banned';
    try {
      const data = await api('/api/recheck/track', {
        ids: [id], state: track, note: '手工加入', track,
      });
      const radio = document.querySelector(`input[name="rc-track"][value="${track}"]`);
      if (radio) radio.checked = true;
      recheckState.q = '';
      const listSearch = $('#rc-search');
      if (listSearch) listSearch.value = '';
      $('#rc-add-select').value = '';
      renderTrackCounts(data.stats);
      await loadRecheckRows();
      await loadRecheckPicker();
      toast(`已加入「${RECHECK_TEXT[track] || '复检名单'}」：${data.changed} 条`);
    } catch (err) {
      toast(err.message, true);
    }
  });

  const rcAddSearch = $('#rc-add-search');
  if (rcAddSearch) {
    rcAddSearch.addEventListener('input', () => {
      clearTimeout(pickerTimer);
      pickerTimer = setTimeout(() => loadRecheckPicker(), 200);
    });
  }
  // 搜索范围限定到某一分区：搜「一批号」时不该被另一区的同名号淹没。
  const rcAddZone = $('#rc-add-zone');
  if (rcAddZone) {
    rcAddZone.addEventListener('change', () => {
      $('#rc-add-select').value = '';
      loadRecheckPicker();
    });
  }

  // 名单档位切换与搜索：切了立刻重查
  document.querySelectorAll('input[name="rc-track"]').forEach((el) => {
    el.addEventListener('change', () => {
      if (!el.checked) return;
      recheckState.track = el.value;
      loadRecheckRows().catch((err) => toast(err.message, true));
      // 下拉按目标档过滤，切档后要重列：在「已解封」档要能选到封号中的号
      loadRecheckPicker();
    });
  });
  let rcTimer = null;
  $('#rc-search').addEventListener('input', (e) => {
    clearTimeout(rcTimer);
    rcTimer = setTimeout(() => {
      recheckState.q = e.target.value.trim();
      loadRecheckRows().catch((err) => toast(err.message, true));
    }, 220);
  });

  // 表格里勾选与行内操作
  ['#rc-check-all', '#rc-check-all-mobile'].forEach((k) => {
    $(k).addEventListener('change', (e) => {
      const on = e.target.checked;
      recheckState.selected.clear();
      if (on) recheckState.rows.forEach((r) => recheckState.selected.add(r.id));
      renderRecheckTable();
    });
  });
  $('#rc-table').addEventListener('change', (e) => {
    const pick = e.target.closest('.rc-exit-pick');
    if (pick) {
      setRowExit(pick.dataset.id, pick.value)
        .catch((err) => toast(err.message, true));
      return;
    }
    const box = e.target.closest('.rc-check');
    if (!box) return;
    const id = Number(box.dataset.id);
    if (box.checked) recheckState.selected.add(id);
    else recheckState.selected.delete(id);
    const tr = box.closest('tr');
    if (tr) tr.classList.toggle('sel', box.checked);
    syncRecheckSelection();
  });
  $('#rc-table').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act="untrack"]');
    if (!btn) return;
    const id = Number(btn.closest('tr').dataset.id);
    recheckState.selected.clear();
    recheckState.selected.add(id);
    trackSelected('').catch((err) => toast(err.message, true));
  });

  // 版本筛选与搜索：切换即重查，不用点按钮
  document.querySelectorAll('input[name="store-realm"]').forEach((el) => {
    el.addEventListener('change', () => {
      storeState.realm = el.value;
      loadStore().catch((err) => toast(err.message, true));
    });
  });

  // 正常 / 风控分区：本地过滤，点了立刻生效，不等网络。
  // 搜索词 / 版本 / 批次仍走后端，回来后按当前分区重新筛一遍。
  document.querySelectorAll('input[name="store-zone"]').forEach((el) => {
    el.addEventListener('change', () => {
      storeState.zone = el.value;
      storeState.rows = storeZoneRows(storeState.allRows);
      // 筛选外的勾选不能留在批量操作里
      const alive = new Set(storeState.rows.map((r) => r.id));
      storeState.selected = new Set([...storeState.selected].filter((id) => alive.has(id)));
      renderStoreTable(storeState.rows);
      refreshSelBar();
    });
  });

  let searchTimer = null;
  $('#store-search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    const value = e.target.value.trim();
    searchTimer = setTimeout(() => {
      storeState.q = value;
      loadStore().catch((err) => toast(err.message, true));
    }, 220);
  });

  // 批次筛选：选中某一批就只看那一批，方便对账 / 整批重打
  const batchSel = $('#store-batch');
  if (batchSel) {
    batchSel.addEventListener('change', () => {
      storeState.batch = batchSel.value;
      // 换批次等于换列表，勾选跟着清空——留着会让「导出」跨批次出错
      storeState.selected.clear();
      loadStore().catch((err) => toast(err.message, true));
    });
  }

  // 表格行内操作走事件委托：表格每次重绘，逐行绑事件会漏
  $('#store-table tbody').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const tr = btn.closest('tr');
    const id = Number(tr.dataset.id);
    const row = storeState.rows.find((r) => r.id === id);
    if (!row) return;
    if (btn.dataset.act === 'relogin') openWebLogin(row);
    if (btn.dataset.act === 'rename') storeRename(id, row.name);
    if (btn.dataset.act === 'delete') storeDelete(id, row.name);
  });

  // 勾选 + 逐行出口下拉：都走 change 委托，表格重绘不会丢事件
  $('#store-table tbody').addEventListener('change', (e) => {
    const box = e.target.closest('.row-check');
    if (box) {
      toggleSelect(Number(box.dataset.id), box.checked);
      return;
    }
    const exit = e.target.closest('.store-exit-pick');
    if (exit) setStoreRowExit(exit.dataset.id, exit.value);
  });

  document.querySelectorAll('#store-check-all, #store-check-all-top').forEach((box) => {
    box.addEventListener('change', () => selectAll(box.checked));
  });

  $('#btn-sel-probe').addEventListener('click', probeSelected);
  $('#btn-probe-all').addEventListener('click', probeAll);
  // 直接把函数交给 addEventListener 会把 MouseEvent 当 target 传进去，
  // 弹窗会误进「重新登录：未命名」。新增按钮必须显式传 null。
  $('#btn-store-login').addEventListener('click', () => openWebLogin(null));
  $('#wl-close').addEventListener('click', closeWebLogin);
  $('#wl-mask').addEventListener('click', closeWebLogin);
  $('#wl-start').addEventListener('click', startWebLogin);
  $('#wl-copy').addEventListener('click', copyWebLoginLink);
  $('#wl-target-new').addEventListener('click', () => {
    // 清掉目标并按新增模式重算默认代理；已生成的旧 state 由 start 时作废
    wlStop(true);
    openWebLogin(null).then(() => wlStatus('已切换为新增模式，点「获取登录链接」即可', ''));
  });
  $('#wl-proxy').addEventListener('input', (e) => {
    e.target.dataset.touched = '1';
    wlState.proxyReady = true;
    wlSyncProxyPick();   // 手填了新地址，下拉别还高亮着上一个端口
  });
  $('#wl-proxy-pick').addEventListener('change', wlPickProxy);
  $('#btn-wl-exits').addEventListener('click', () => loadWlExits(true));
  $('#wl-realm').addEventListener('change', () => {
    // 换了版本旧 state 就作废，直接按新版本重新申请一个链接
    if (!$('#wl-modal').hidden && wlState.state) startWebLogin();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#wl-modal').hidden) closeWebLogin();
  });
  $('#btn-store-assign').addEventListener('click', assignStoreExits);
  $('#btn-store-direct').addEventListener('click', clearStoreExits);
  // 横竖屏切换会改列数和下拉文案（窄屏只写端口号），重画一次表格
  let storeResizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(storeResizeTimer);
    storeResizeTimer = setTimeout(() => {
      if (!$('#view-store').classList.contains('hidden')) {
        renderStoreTable(storeState.rows);
      }
    }, 150);
  });
  $('#btn-store-exits').addEventListener('click', () => {
    loadStoreExits(true).then(() => {
      renderStoreTable(storeState.rows);
      toast('节点表已刷新');
    });
  });
  $('#btn-sel-export').addEventListener('click', exportSelected);
  $('#btn-sel-risk').addEventListener('click', () => setStoreRisk('banned'));
  $('#btn-sel-relogin').addEventListener('click', () => {
    const id = [...storeState.selected][0];
    const row = storeState.rows.find((r) => r.id === id);
    if (row) openWebLogin(row);
    else toast('选中的行不在当前列表里，刷新后再试', true);
  });
  $('#btn-sel-unrisk').addEventListener('click', () => setStoreRisk(''));
  $('#btn-sel-delete').addEventListener('click', deleteSelected);
  $('#btn-sel-clear').addEventListener('click', () => selectAll(false));

  /* --- 升级 --- */
  $('#btn-check-update').addEventListener('click', checkUpdate);
  $('#btn-apply-update').addEventListener('click', applyUpdate);
  $('#btn-apply-local').addEventListener('click', applyLocal);
  $('#btn-restart').addEventListener('click', restartServer);
  $('#btn-rollback').addEventListener('click', rollbackServer);
  $('#btn-save-source').addEventListener('click', saveSource);

  $('#btn-logout').addEventListener('click', async () => {
    try {
      await api('/api/logout', {});
    } catch (err) {
      /* 会话本来就过期时也会走到这里，直接跳走 */
    }
    location.replace('/login');
  });

  const sampleLink = $('#loadSample');
  if (sampleLink) {
    sampleLink.addEventListener('click', (e) => {
      e.preventDefault();
      loadSample('');
    });
  }

  // ?sample=文件名 直接进演示：分享链接时对方打开就能看到完整流程
  const params = new URLSearchParams(location.search);
  const sample = params.get('sample');
  if (sample) loadSample(sample === '1' ? '' : sample);

  // ?view=store|activity|update 直接打开对应视图（书签/深链用）
  const view = params.get('view');
  if (view === 'store' || view === 'activity' || view === 'update') switchView(view);

  // 整页拖入也接住，避免浏览器直接打开文件
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());
}

/* ---------- 每日活跃 ---------- */

const daState = {
  cfg: null,
  rows: [],
  selected: new Set(),
  pollTimer: null,
  loaded: false,
  // 账号卡的本地筛选：行数最多 2000，前端过滤比每次敲字都问后端快，
  // 也不会和 1.8s 的状态轮询抢请求。
  q: '',
  zone: '',
};

/** 轮询和手工刷新可能同时在飞，旧响应不能盖掉新状态。 */
let daLoadSeq = 0;

/** 配置 → 表单。缺字段按默认值填，不让输入框空着。 */
function daFillForm(cfg) {
  daState.cfg = cfg;
  $('#da-enabled').checked = cfg.enabled !== false;
  $('#da-web').checked = cfg.web !== false;
  $('#da-desktop').checked = cfg.desktop !== false;
  $('#da-use-exit').checked = cfg.use_exit !== false;
  $('#da-skip-done').checked = cfg.skip_done !== false;
  $('#da-model').value = cfg.chat_model || 'deepseek-v4.1-flash';
  $('#da-models').value = cfg.models || '';
  $('#da-timeout').value = cfg.timeout || 120;
  $('#da-rounds').value = cfg.rounds || 2;
  $('#da-concurrency').value = cfg.concurrency || 2;
  $('#da-daily-at').value = cfg.daily_at || '08:30';
  $('#da-interval').value = cfg.interval_minutes || 30;
  $('#da-proxy').value = cfg.proxy || '';
  $('#da-fallback').value = cfg.fallback_proxy || '';
  $('#da-prompt').value = cfg.prompt || 'Hi';
}

function daFormRead() {
  return {
    enabled: $('#da-enabled').checked,
    web: $('#da-web').checked,
    desktop: $('#da-desktop').checked,
    use_exit: $('#da-use-exit').checked,
    skip_done: $('#da-skip-done').checked,
    chat_model: $('#da-model').value.trim() || 'deepseek-v4.1-flash',
    models: $('#da-models').value.trim(),
    timeout: Number($('#da-timeout').value) || 120,
    rounds: Number($('#da-rounds').value) || 2,
    concurrency: Number($('#da-concurrency').value) || 2,
    daily_at: $('#da-daily-at').value || '08:30',
    interval_minutes: Number($('#da-interval').value) || 30,
    proxy: $('#da-proxy').value.trim(),
    fallback_proxy: $('#da-fallback').value.trim(),
    prompt: $('#da-prompt').value.trim() || 'Hi',
  };
}

async function loadActivity() {
  const data = await apiGet('/api/activity/config');
  daFillForm(data.config);
  renderDaStats(data.summary);
  daState.loaded = true;
  await Promise.all([loadDaAccounts(), loadDaLog()]);
  if (data.state && data.state.running) daStartPoll();
}

async function loadDaAccounts() {
  const seq = ++daLoadSeq;
  const data = await apiGet('/api/activity/accounts');
  if (seq !== daLoadSeq) return;
  daState.rows = data.rows || [];
  // 筛选外的勾选不能留着：批量开关、批量跑都会按 selected 全量执行，
  // 用户看不见的号被改了会莫名其妙。
  daPruneSelection();
  renderDaAccounts();
  renderDaStats(data.summary);
  daRenderState(data.state);
}

async function loadDaLog() {
  const range = $('#da-range').value || '7d';
  const result = $('#da-result').value || '';
  const data = await apiGet(
    `/api/activity/list?range=${encodeURIComponent(range)}&result=${encodeURIComponent(result)}&limit=200`);
  renderDaLog(data.rows || []);
  renderDaStats(data.summary);
}

function renderDaStats(summary) {
  if (!summary) return;
  $('#da-stats').innerHTML = `
    <div class="stat-box">
      <h3>今日成功 <b>${summary.today_ok || 0}</b></h3>
      <div class="mini-row">
        <span class="stat-mini"><i style="background:#16a34a"></i>失败 ${summary.today_failed || 0}</span>
        <span class="stat-mini" title="本地判定就不跑的：国内版、没有 access token、token 是加密信封、静态 API Key、网页腿和桌面腿都关着。具体原因见记录表的「说明」列"><i style="background:#94a3b8"></i>跳过 ${summary.today_skipped || 0}</span>
      </div>
    </div>
    <div class="stat-box">
      <h3>近 7 天成功 <b>${summary.week_ok || 0}</b></h3>
      <div class="mini-row">
        <span class="stat-mini"><i style="background:#dc2626"></i>失败 ${summary.week_failed || 0}</span>
      </div>
    </div>`;
}

function daRenderState(st) {
  if (!st) return;
  if (st.running) {
    $('#da-state').textContent =
      `运行中 ${st.done}/${st.total}（成功 ${st.ok} · 失败 ${st.failed}）`;
    return;
  }
  if (st.finished_at) {
    $('#da-state').textContent =
      `上批 ${st.total} 个：成功 ${st.ok} · 失败 ${st.failed} · 跳过 ${st.skipped}`;
    return;
  }
  $('#da-state').textContent = '未运行';
}

function daExitText(port) {
  return port ? String(port) : '直连';
}

/**
 * 一条账号能不能被当前搜索词命中。
 *
 * 状态词和真实字段拼进同一份 haystack：搜「风控」「正常」「关闭」「参与」
 * 「已完成」「失败」「跳过」都能直接定位，不必先想清楚去哪个分段找。
 * 纯前端比较，O(n·len)，n ≤ 2000，瓶颈是 join 出的临时字符串。
 */
function daMatch(r, q) {
  if (!q) return true;
  const words = [
    r.name || '', r.uid || '', r.email || '',
    r.exit_port ? String(r.exit_port) : '直连',
    r.risk ? '风控 风控账户' : '正常 正常账户',
    r.risk ? '' : (r.daily_active === false ? '关闭 关 不参与' : '参与 开'),
    r.today_ok ? '已完成 成功' : r.today_skipped ? '跳过'
      : r.today_failed ? '失败' : '未跑',
  ].join(' ').toLowerCase();
  // 空格分隔的词逐个命中，允许「风控 7890」这种组合查询。
  return q.split(/\s+/).every((word) => word && words.includes(word));
}

/** 当前分区 + 搜索词之后的行。排序沿用服务端的「参与 → 关闭 → 风控」。 */
function daVisibleRows() {
  const q = daState.q.trim().toLowerCase();
  return daState.rows.filter((r) => {
    if (daState.zone === 'risk' && !r.risk) return false;
    if (daState.zone === 'normal' && r.risk) return false;
    return daMatch(r, q);
  });
}

/** 分段按钮的计数取整批账号，不随搜索词变，避免「搜索把号搜少了」的错觉。 */
function daZoneCounts() {
  const risky = daState.rows.filter((r) => r.risk).length;
  return { all: daState.rows.length, normal: daState.rows.length - risky, risk: risky };
}

/** 只保留当前可见行的勾选；搜索 / 分区 / 轮询重绘后都调一次。 */
function daPruneSelection() {
  const visible = new Set(daVisibleRows().map((r) => r.id));
  daState.selected = new Set([...daState.selected].filter((id) => visible.has(id)));
}

/** 账号卡上那一格「活跃」：开关 + 风控说明。
 *
 *  风控账号（recheck_state=banned）不允许参与每日活跃：开关置灰、显式标「风控不
 *  参与」，点也点不动。不是把它显示成「关」——那会让人以为是自己关的，回头解封
 *  了还去找开关。这里分三种状态：参与 / 手动关 / 风控挡。
 */
function daActiveCell(r) {
  if (r.risk) {
    // 桌面写全「风控不参与」，窄屏 60px 装不下 5 个字，换「风控」两字；
    // 完整原因放 title，手机上点不出来但至少不误导成「被封了」。
    return '<span class="da-off-reason" title="风控账号不参与每日活跃；移出风控后自动恢复">'
      + '<span class="da-reason-full">风控不参与</span>'
      + '<span class="da-reason-short">风控</span></span>';
  }
  const on = r.daily_active !== false;
  return `<button class="rc-mini ${on ? 'on' : ''}" data-act="da-toggle" data-on="${on ? 1 : 0}"
    title="${on ? '点一下关闭：本程序不再跑这个号，导出的 hub 账户文件也带 tasksEnabled=false' : '点一下开启：本程序会跑这个号，导出的 hub 账户文件带 tasksEnabled=true'}">
    <span class="rc-mini-box"></span>${on ? '开' : '关'}</button>`;
}

/** 全选框跟当前可见行对齐：筛选后的「全选」不该覆盖看不见的号。 */
function daSyncCheckAll(rows) {
  const box = $('#da-check-all');
  if (!box) return;
  const picked = rows.filter((r) => daState.selected.has(r.id)).length;
  box.checked = rows.length > 0 && picked === rows.length;
  box.indeterminate = picked > 0 && picked < rows.length;
}

function renderDaAccounts() {
  const body = $('#da-accounts tbody');
  const counts = daZoneCounts();
  $('#da-zone-all').textContent = counts.all;
  $('#da-zone-normal').textContent = counts.normal;
  $('#da-zone-risk').textContent = counts.risk;

  const rows = daVisibleRows();
  if (!rows.length) {
    const emptyLib = counts.all === 0;
    body.innerHTML = '<tr><td colspan="7" class="empty-cell">' + (emptyLib
      ? '账户库里还没有国际版账号。先在「导入转换」或「官网登录」里加号。'
      : '没有符合当前搜索 / 分区的账号。切回「全部」或清空搜索词再试。') + '</td></tr>';
    $('#da-accounts-hint').textContent = emptyLib
      ? '' : `共 ${counts.all} 个国际版账号 · 当前显示 0 个`;
    daSyncCheckAll(rows);
    daSyncBulk();
    return;
  }
  body.innerHTML = rows.map((r) => {
    const on = daState.selected.has(r.id);
    // 今日状态四态。以前把「跳过」（本地判定不跑：无 token / 加密信封 / 静态 key /
    // 两条腿都关）也算成「失败」，看板上「跳过 18」在列表里对不上任何一格。
    const today = r.today_ok
      ? '<span class="pill ok">已完成</span>'
      : r.today_skipped
        ? `<span class="pill skip" title="${esc(r.last_detail || '本地判定不跑')}">跳过</span>`
        : r.today_failed
          ? '<span class="pill bad">失败</span>'
          : '<span class="pill mute">未跑</span>';
    // 窄屏靠 CSS 把 tr 变成两行网格（名称独占第一行），不在这里做结构分叉。
    return `<tr data-id="${r.id}" data-zone="${r.risk ? 'risk' : 'normal'}" class="${on ? 'sel' : ''}${r.risk ? ' risk-row' : ''}">
      <td class="col-check"><input type="checkbox" class="da-check" ${on ? 'checked' : ''}></td>
      <td title="${esc(r.uid)}${r.email ? ' · ' + esc(r.email) : ''}"><b>${esc(r.name)}</b>${zoneFlag(r)}</td>
      <td class="da-active">${daActiveCell(r)}</td>
      <td class="mono">${esc(daExitText(r.exit_port))}</td>
      <td class="da-today">${today}</td>
      <td class="mono" title="${esc(r.last_detail || '')}">${esc(r.last_at_text || '—')}</td>
      <td class="row-actions">${r.risk
    ? '<button class="link" disabled title="风控号不参与每日活跃，移出风控后才能跑">跑一次</button>'
    : '<button class="link" data-act="run-one" title="立刻跑这个号，不受「跳过今日已完成」影响">跑一次</button>'}</td>
    </tr>`;
  }).join('');
  const sel = daState.selected.size;
  const off = daState.rows.filter((r) => !r.risk && r.daily_active === false).length;
  const zoneText = daState.zone === 'risk' ? '仅风控' : daState.zone === 'normal' ? '仅正常' : '';
  $('#da-accounts-hint').textContent =
    `共 ${counts.all} 个国际版账号（参与 ${counts.all - counts.risk - off} · 关闭 ${off} · 风控 ${counts.risk}）`
    + ` · 当前显示 ${rows.length} 个${zoneText ? '（' + zoneText + '）' : ''}`
    + (daState.q ? ` · 搜索「${daState.q}」` : '')
    + (sel ? ` · 已选 ${sel} 个` : '');
  daSyncCheckAll(rows);
  daSyncBulk();
}

/** 批量开/关按钮：勾了才亮，勾的全是风控号时也不亮（改了也不生效）。 */
function daSyncBulk() {
  const ids = [...daState.selected];
  const usable = ids.filter((id) => {
    const row = daState.rows.find((r) => r.id === id);
    return row && !row.risk;
  });
  $('#btn-da-active-on').disabled = usable.length === 0;
  $('#btn-da-active-off').disabled = usable.length === 0;
}

/** 账户级开关落库。走的是账户库那条接口：写得进 DB，导出 hub 时就是 tasksEnabled。 */
async function daSetActive(ids, enabled, sourceBtn) {
  const usable = ids.filter((id) => {
    const row = daState.rows.find((r) => r.id === id);
    return row && !row.risk;
  });
  if (!usable.length) {
    toast('选中的都是风控账号，不参与每日活跃', true);
    return;
  }
  if (sourceBtn) sourceBtn.disabled = true;
  try {
    const data = await api('/api/store/daily-active', { ids: usable, enabled });
    if (!data.ok) throw new Error(data.error || '设置失败');
    const skipped = data.changed < usable.length
      ? `，${usable.length - data.changed} 个没改成` : '';
    toast(`已${enabled ? '开启' : '关闭'} ${data.changed} 个账号的每日活跃${skipped}`);
    await loadDaAccounts();
  } catch (err) {
    toast(err.message, true);
    if (sourceBtn) sourceBtn.disabled = false;
  }
}

function renderDaLog(rows) {
  const body = $('#da-log tbody');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty-cell">这个区间里还没有活跃记录。</td></tr>';
    $('#da-log-hint').textContent = '';
    return;
  }
  body.innerHTML = rows.map((r) => {
    const pill = r.skipped ? '<span class="pill mute">跳过</span>'
      : (r.ok ? '<span class="pill ok">成功</span>' : '<span class="pill bad">失败</span>');
    const mode = r.mode === 'web+desktop' ? '网页+桌面'
      : (r.mode === 'web' ? '网页' : (r.mode === 'desktop' ? '桌面' : (r.phase || '—')));
    const at = String(r.at_text || '');
    return `<tr>
      <td class="mono da-at"><span class="at-full">${esc(at)}</span><span class="at-short">${esc(at.slice(5, 16))}</span></td>
      <td title="${esc(r.uid)}"><b>${esc(r.name)}</b><span class="da-pill-inline">${pill}</span></td>
      <td class="da-result">${pill}</td>
      <td>${esc(mode)}</td>
      <td class="mono">${esc(r.model || '—')}</td>
      <td class="mono" title="${esc(r.proxy || '')}">${esc((r.proxy || '').replace(/^https?:\/\//, '') || '直连')}</td>
      <td class="mono">${r.elapsed_ms ? (r.elapsed_ms / 1000).toFixed(1) + 's' : '—'}</td>
      <td class="da-detail" title="${esc(r.detail || '')}">${esc(r.detail || '—')}</td>
    </tr>`;
  }).join('');
  $('#da-log-hint').textContent = `共 ${rows.length} 条（最新在前）`;
}

function daStartPoll() {
  if (daState.pollTimer) return;
  $('#da-dot').hidden = false;
  const tick = async () => {
    try {
      const data = await apiGet('/api/activity/state');
      daRenderState(data.state);
      if (!data.state.running) {
        clearInterval(daState.pollTimer);
        daState.pollTimer = null;
        $('#da-dot').hidden = true;
        await Promise.all([loadDaAccounts(), loadDaLog()]);
        toast('本批活跃跑完了');
        return;
      }
      await loadDaAccounts();
    } catch (err) {
      clearInterval(daState.pollTimer);
      daState.pollTimer = null;
      $('#da-dot').hidden = true;
    }
  };
  daState.pollTimer = setInterval(tick, 1800);
  tick();
}

/** 触发一批。`opts.skipDone` 能强制覆盖「跳过今日已完成」——
 *  行内「跑一次」必须传 false：那个按钮的语义是「现在就跑这一个」，
 *  被默认的 skip_done 吞掉会变成「点了没反应」，让人以为要先勾选。 */
async function daRun(scope, ids, opts) {
  const force = opts && typeof opts.skipDone === 'boolean' ? opts.skipDone : null;
  const btns = ['#btn-da-run-pending', '#btn-da-run-all'];
  btns.forEach((k) => { $(k).disabled = true; });
  try {
    const payload = {
      scope,
      skip_done: force === null ? $('#da-skip-done').checked : force,
    };
    if (ids && ids.length) payload.ids = ids;
    const data = await api('/api/activity/run', payload);
    if (!data.started) {
      toast(data.msg || '没有要跑的账号');
      return;
    }
    toast(`已开始跑 ${data.started} 个号`);
    daStartPoll();
  } catch (err) {
    toast(err.message, true);
  } finally {
    btns.forEach((k) => { $(k).disabled = false; });
  }
}

async function daSave() {
  const btn = $('#btn-da-save');
  btn.disabled = true;
  try {
    const data = await api('/api/activity/config', daFormRead());
    daFillForm(data.config);
    toast('每日活跃设置已保存');
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

function bindActivity() {
  $('#btn-da-save').addEventListener('click', daSave);
  // 勾了号就只跑勾的，没勾就跑今天还没成功的那些
  $('#btn-da-run-pending').addEventListener('click', () => {
    if (daState.selected.size) daRun('selected', [...daState.selected]);
    else daRun('pending');
  });
  $('#btn-da-run-all').addEventListener('click', () => daRun('all'));
  $('#btn-da-refresh').addEventListener('click', () => {
    loadActivity().catch((err) => toast(err.message, true));
  });
  $('#btn-da-log-refresh').addEventListener('click', () => {
    loadDaLog().catch((err) => toast(err.message, true));
  });
  $('#da-range').addEventListener('change', () => {
    loadDaLog().catch((err) => toast(err.message, true));
  });
  $('#da-result').addEventListener('change', () => {
    loadDaLog().catch((err) => toast(err.message, true));
  });

  // 账号勾选：只用来「跑选中的」，与账户库那套勾选集不共享
  $('#da-check-all').addEventListener('change', (e) => {
    // 只全选当前可见行：搜索 / 分区筛掉的号不该被悄悄勾上。
    daState.selected = new Set();
    if (e.target.checked) daVisibleRows().forEach((r) => daState.selected.add(r.id));
    renderDaAccounts();
  });

  // 账号卡本地筛选：正常 / 风控分区 + 关键字。换筛选时把落在筛选外的
  // 勾选清掉，批量开关不会改到看不见的号。
  document.querySelectorAll('input[name="da-zone"]').forEach((el) => {
    el.addEventListener('change', () => {
      daState.zone = el.value;
      daPruneSelection();
      renderDaAccounts();
    });
  });
  $('#da-account-search').addEventListener('input', (e) => {
    daState.q = e.target.value.trim();
    daPruneSelection();
    renderDaAccounts();
  });

  // 批量开 / 关活跃：走账户级开关，同一份数据导出 hub 时就是 tasksEnabled
  $('#btn-da-active-on').addEventListener('click', () =>
    daSetActive([...daState.selected], true));
  $('#btn-da-active-off').addEventListener('click', () =>
    daSetActive([...daState.selected], false));

  $('#da-accounts tbody').addEventListener('click', (e) => {
    const tr = e.target.closest('tr');
    if (!tr) return;
    const id = Number(tr.dataset.id);
    if (e.target.classList.contains('da-check')) {
      if (e.target.checked) daState.selected.add(id); else daState.selected.delete(id);
      renderDaAccounts();
      return;
    }
    const toggle = e.target.closest('button[data-act="da-toggle"]');
    if (toggle) {
      daSetActive([id], toggle.dataset.on !== '1', toggle);
      return;
    }
    // 行内「跑一次」：只跑这一行，强制忽略「跳过今日已完成」。
    // 用 closest 而不是 e.target.dataset —— 以后按钮里塞图标/文字节点就取不到。
    const runOne = e.target.closest('button[data-act="run-one"]');
    if (runOne) {
      const row = daState.rows.find((r) => r.id === id);
      if (row && row.risk) {
        toast('风控号不参与每日活跃，移出风控后再跑', true);
        return;
      }
      daRun('selected', [id], { skipDone: false });
    }
  });
}

bind();
bindActivity();
loadProfiles()
  .then(() => loadStoreProfiles())
  .catch(() => toast('格式列表加载失败', true));
loadStore({ noExits: true }).catch(() => { /* 空库不是错误，切到账户库时会再拉一次 */ });
