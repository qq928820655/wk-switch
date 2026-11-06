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

function renderPreview(rows, total) {
  const body = $('#preview-table tbody');
  body.innerHTML = rows.map((r) => `
    <tr>
      <td class="mono">${r.index}</td>
      <td class="mono" title="${esc(r.uid)}">${esc(r.uid)}</td>
      <td>${esc(r.nickname) || '<span class="hint">—</span>'}</td>
      <td>${esc(r.email) || '<span class="hint">—</span>'}</td>
      <td>${r.realm === 'cn' ? '国内' : r.realm === 'intl' ? '国际' : '<span class="hint">—</span>'}</td>
      <td><span class="pill ${STATE_PILL[r.state] || 'mute'}">${esc(r.state_label)}</span></td>
      <td class="mono col-exp">${esc(r.expires) || '—'}</td>
      <td class="mono">${esc(r.at)}</td>
      <td>${r.rt ? '<span class="pill ok">有</span>' : '<span class="hint">—</span>'}</td>
    </tr>`).join('');

  $('#preview-hint').textContent = total > rows.length
    ? `显示前 ${rows.length} 条，共 ${total} 条`
    : `共 ${total} 条`;
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
      const p = state.profiles.find((x) => x.id === state.profile);
      $('#export-hint').textContent = `将生成：${p ? p.name : ''}`;
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

const storeState = { rows: [], profile: null, realm: '', q: '', loaded: false, selected: new Set() };

const REALM_PILL = { cn: 'warn', intl: 'brand' };
const REALM_TEXT = { cn: '国内版', intl: '国际版' };

async function loadStore() {
  const params = new URLSearchParams();
  if (storeState.realm) params.set('realm', storeState.realm);
  if (storeState.q) params.set('q', storeState.q);

  const data = await apiGet('/api/store/list?' + params.toString());
  if (!data.ok) throw new Error(data.error || '账户库读取失败');

  storeState.rows = data.rows || [];
  storeState.loaded = true;

  // 库里已经删掉的 id 不能留在选择集里，否则「导出选中」会带上不存在的行
  const alive = new Set(storeState.rows.map((r) => r.id));
  storeState.selected = new Set([...storeState.selected].filter((id) => alive.has(id)));

  renderStoreStats(data.stats);
  renderStoreTable(storeState.rows);
  refreshSelBar();
}

function renderStoreStats(stats) {
  const total = stats.total || 0;
  $('#store-count').textContent = total;
  $('#store-db').textContent = stats.db_path || '';

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
}

function renderStoreTable(rows) {
  const body = $('#store-table tbody');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="11" class="empty-cell">账户库是空的。先在「导入转换」里转一遍，再点「保存到账户库」。</td></tr>';
    $('#store-hint').textContent = '';
    return;
  }

  const sel = storeState.selected;
  body.innerHTML = rows.map((r) => `
    <tr data-id="${r.id}" class="${sel.has(r.id) ? 'sel' : ''}">
      <td class="col-check"><input type="checkbox" class="row-check" data-id="${r.id}" ${sel.has(r.id) ? 'checked' : ''}></td>
      <td><b>${esc(r.name)}</b>${r.nickname && r.nickname !== r.name ? `<br><small class="hint">${esc(r.nickname)}</small>` : ''}</td>
      <td><span class="pill ${REALM_PILL[r.realm] || 'mute'}">${esc(r.realm_label || REALM_TEXT[r.realm] || '未知')}</span></td>
      <td class="mono col-uid" title="${esc(r.uid)}">${esc(shortUid(r.uid))}</td>
      <td><span class="pill ${STATE_PILL[r.state] || 'mute'}">${esc(r.state_label)}</span></td>
      <td class="col-credit">${creditCell(r)}</td>
      <td class="mono col-exp">${esc(r.expires) || '—'}</td>
      <td class="mono col-at" title="${esc(r.at)}">${esc(r.at)}</td>
      <td class="col-rt">${r.has_rt ? '<span class="pill ok">有</span>' : '<span class="hint">—</span>'}</td>
      <td class="mono col-upd" title="${esc(r.updated_at)}">${esc(shortTime(r.updated_at))}</td>
      <td class="row-actions">
        <button class="link" data-act="rename">改名</button>
        <button class="link danger" data-act="delete">删除</button>
      </td>
    </tr>`).join('');

  $('#store-hint').textContent = `共 ${rows.length} 条`;
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
  $('#btn-sel-delete').disabled = picked === 0;
  $('#btn-sel-clear').disabled = picked === 0;
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
  await runProbe('/api/probe', { ids }, $('#btn-sel-probe'), '校验选中');
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
    if (s.valid) parts.push(`有效 ${s.valid}`);
    if (s.invalid) parts.push(`失效 ${s.invalid}`);
    if (s.unknown) parts.push(`未确认 ${s.unknown}`);
    if (s.skipped) parts.push(`不可查 ${s.skipped}`);
    if (s.with_credit) parts.push(`合计剩余 ${fmtNum(s.remain_total)} 积分`);

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
    const data = await api('/api/store/export', { profile: storeState.profile, ids });
    $('#store-downloads').innerHTML = data.files.map((f) => `
      <div class="dl">
        <div>
          <div class="dl-name">${esc(f.name)}</div>
          <div class="dl-size">${bytesText(f.size)} · 已选 ${ids.length} 条</div>
        </div>
        <a href="${esc(f.download)}" download="${esc(f.name)}">下载</a>
      </div>`).join('');
    toast(`已导出 ${ids.length} 条`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '导出选中';
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
  btn.disabled = true;
  try {
    const data = await api('/api/store/import', { job: state.job, opts: collectOpts() });
    toast(`已留存：新增 ${data.inserted} · 更新 ${data.updated}${data.skipped ? ' · 跳过 ' + data.skipped : ''}`);
    storeState.loaded = false;
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

async function storeBackup() {
  const btn = $('#btn-store-backup');
  btn.disabled = true;
  btn.textContent = '打包中…';
  try {
    const data = await apiGet('/api/store/backup');
    if (!data.ok) throw new Error(data.error || '备份失败');
    const f = data.file;
    $('#backup-downloads').innerHTML = `
      <div class="dl">
        <div>
          <div class="dl-name">${esc(f.name)}</div>
          <div class="dl-size">${bytesText(f.size)} · 含 accounts.db 与 accounts.json</div>
        </div>
        <a href="${esc(f.download)}" download="${esc(f.name)}">下载</a>
      </div>`;
    $('#backup-hint').textContent = `已打包 ${data.stats.total} 条账号。包内含 token 明文，存到自己可控的位置。`;
    toast('备份包已生成');
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '下载备份包';
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

async function storeExport() {
  if (!storeState.profile) {
    toast('先选择一种目标格式', true);
    return;
  }
  const btn = $('#btn-store-export');
  btn.disabled = true;
  btn.textContent = '生成中…';
  try {
    const data = await api('/api/store/export', {
      profile: storeState.profile,
      realm: realmOf('store-export-realm'),
    });
    $('#store-downloads').innerHTML = data.files.map((f) => `
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
    btn.textContent = '从库导出';
  }
}

/* ---------- 版本与升级 ---------- */

const updateState = { version: null, latest: null, canApply: false, checking: false };

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
    $('#btn-restart').disabled = true;
    if (dot) dot.hidden = true;
    return;
  }

  const rel = info.release || {};
  if (info.newer) {
    $('#upd-line').innerHTML = `发现新版本 <b>v${esc(info.latest)}</b>　当前 v${esc(info.current)}`;
    $('#upd-hint').textContent = rel.asset ? '包：' + rel.asset : '';
    $('#btn-apply-update').disabled = false;
    if (dot) dot.hidden = false;
  } else {
    $('#upd-line').innerHTML = `已是最新版本 <b>v${esc(info.current)}</b>`;
    $('#upd-hint').textContent = rel.asset ? '制品库最新：v' + esc(info.latest) : '';
    $('#btn-apply-update').disabled = true;
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

async function applyUpdate() {
  const btn = $('#btn-apply-update');
  btn.disabled = true;
  btn.textContent = '升级中…';
  try {
    const data = await api('/api/update/apply', {});
    if (!data.upgraded) {
      $('#upd-line').textContent = data.message || '已经是最新版本';
      toast(data.message || '已是最新');
      return;
    }
    $('#upd-line').innerHTML = `已升级 <b>v${esc(data.to)}</b>，重启后生效`;
    $('#upd-notes').textContent = '升级前代码已备份为 ' + data.backup;
    $('#btn-restart').disabled = false;
    $('#upd-hint').textContent = '点「重启生效」自动拉起新进程';
    toast('已升级到 v' + data.to);
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '一键升级';
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
    $('#upd-line').innerHTML = `已用本地包${verb}到 <b>v${esc(data.to)}</b>，重启后生效`;
    $('#upd-notes').textContent = `替换 ${data.files} 个文件，升级前代码已备份为 ${data.backup}`;
    $('#btn-restart').disabled = false;
    $('#upd-hint').textContent = '点「重启生效」自动拉起新进程';
    input.value = '';
    toast(`已${verb}到 v${data.to}`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '上传并升级';
  }
}

async function restartServer() {
  const btn = $('#btn-restart');
  btn.disabled = true;
  btn.textContent = '重启中…';
  try {
    await api('/api/update/restart', {});
  } catch (err) {
    /* 进程正在退出，请求断掉是正常的 */
  }
  // 轮询直到新进程接上，再刷新页面
  for (let i = 0; i < 40; i += 1) {
    await new Promise((r) => setTimeout(r, 500));
    try {
      const resp = await fetch('/api/version', { cache: 'no-store' });
      if (resp.ok) {
        location.reload();
        return;
      }
    } catch (err) { /* 还没起来 */ }
  }
  btn.disabled = false;
  btn.textContent = '重启生效';
  toast('服务没有在 20 秒内回来，请查看控制台输出', true);
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

/* 三档出口策略。文案要能回答「我该选哪个」，光写名字等于没说。 */
const EXIT_TIPS = {
  same: '所有号从同一个出口出去。用来验证封的是号还是 IP：同一个 IP 下还有号能通，说明封在账号层。',
  random: '每个号从上面检测到的可用节点里随机挑一个。IP 不撞车，也不成规律。',
  custom: '按表里的顺序逐号轮换。手上有一套别家代理，或要指定地区时用这档。',
};

const recheckState = {
  cfg: null, rows: [], nodes: [], track: 'banned', q: '',
  selected: new Set(), running: false,
};

async function loadRecheck() {
  const data = await apiGet('/api/recheck/config');
  if (!data.ok) throw new Error(data.error || '读取复检设置失败');
  recheckState.cfg = data.config;
  fillRecheckForm(data.config, data.nodes);
  renderTrackCounts(data.stats);
  await loadRecheckRows();
  loadRecheckPicker();
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
  $('#rc-pin').value = cfg.exit_pin || '';
  $('#rc-custom').value = cfg.exit_custom || '';
  // 秒 → 分钟：库里存秒，界面上写分钟更好懂
  $('#rc-min-interval').value = Math.round((cfg.min_interval || 0) / 60);
  $('#rc-batch').value = cfg.batch_limit;
  $('#rc-gap').value = Math.round(cfg.gap_seconds || 3);
  $('#rc-timeout').value = Math.round(cfg.timeout || 25);
  $('#rc-auto-hour').value = cfg.auto_hour ?? 3;
  const mihomo = cfg.mihomo || {};
  $('#rc-host').value = mihomo.host || '';
  $('#rc-ports').value = mihomo.ports || '';
  if (disc) {
    $('#rc-detect-hint').textContent = disc.probed
      ? `检测到 ${disc.ok_count} / ${(disc.ports || []).length} 个可用出口`
      : '环境变量 WB_PROXY_DISCOVER_HOST / WB_PROXY_DISCOVER_PORTS 优先于这两栏';
    renderNodes(disc.nodes || [], disc.probed);
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

function renderNodes(nodes, probed) {
  const box = $('#rc-nodes');
  if (!nodes.length) {
    box.innerHTML = '<p class="hint">没有配置端口</p>';
    return;
  }
  box.innerHTML = nodeRows(nodes, probed);
}

function renderCustomNodes(nodes) {
  const box = $('#rc-custom-nodes');
  box.innerHTML = nodes.length
    ? nodeRows(nodes, true)
    : '<p class="hint">没有可测的地址</p>';
}

/* 节点卡片：地址 + 出口 IP + 延迟。mihomo 和自定义出口共用这一份渲染，
   两边显示不一致的话，用户会以为是两套不同的东西。 */
function nodeRows(nodes, probed) {
  return nodes.map((n) => {
    const label = n.url.replace(/^https?:\/\//, '');
    if (!probed) {
      return `<div class="node"><span class="node-url" title="${esc(label)}">${esc(label)}</span>
        <span class="node-tail hint">未检测</span></div>`;
    }
    if (n.ok) {
      return `<div class="node ok"><span class="node-url" title="${esc(label)}">${esc(label)}</span>
        <span class="node-ip mono">${esc(n.exit_ip || '')}</span>
        <span class="node-tail">${n.latency_ms} ms</span></div>`;
    }
    return `<div class="node bad"><span class="node-url" title="${esc(label)}">${esc(label)}</span>
      <span class="node-tail" title="${esc(n.error || '')}">不可用</span></div>`;
  }).join('');
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
    body.innerHTML = '<tr><td colspan="9" class="empty-cell">这一档没有账号。导入带风控原因的账号会自动进名单。</td></tr>';
    $('#rc-hint').textContent = '';
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
      <td><b>${esc(r.name)}</b>${r.nickname && r.nickname !== r.name ? `<br><small class="hint">${esc(r.nickname)}</small>` : ''}</td>
      <td class="col-rc-realm"><span class="pill ${REALM_PILL[r.realm] || 'mute'}">${esc(r.realm_label || REALM_TEXT[r.realm] || '未知')}</span></td>
      <td class="col-rc-state"><span class="pill ${pill}">${esc(text)}</span></td>
      <td class="col-credit">${creditCell(r)}</td>
      <td class="mono col-rc-count">${r.recheck_count || 0}</td>
      <td class="mono col-rc-at">${esc(r.recheck) || '—'}</td>
      <td class="rc-note" title="${esc(r.recheck_note || '')}">${esc(trimNote(r.recheck_note)) || '<span class="hint">—</span>'}</td>
      <td class="row-actions"><button class="link danger" data-act="untrack">移出</button></td>
    </tr>`;
  }).join('');

  $('#rc-hint').textContent = `共 ${recheckState.rows.length} 条`;
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
  ['#btn-rc-sel-probe', '#btn-rc-sel-remove', '#btn-rc-sel-clear'].forEach((k) => {
    $(k).disabled = n === 0;
  });
}

async function saveRecheckCfg() {
  const cfg = {
    enabled: $('#rc-enabled').checked,
    auto: $('#rc-auto').checked,
    exit_mode: currentExitMode(),
    exit_pin: $('#rc-pin').value.trim(),
    exit_custom: $('#rc-custom').value.trim(),
    // 分钟 → 秒。后端会把小于 10 分钟的值钳回 600 秒，这里不用重复校验。
    min_interval: Math.max(10, Number($('#rc-min-interval').value) || 360) * 60,
    batch_limit: Number($('#rc-batch').value) || 20,
    gap_seconds: Number($('#rc-gap').value) || 3,
    timeout: Number($('#rc-timeout').value) || 25,
    auto_hour: Number($('#rc-auto-hour').value) || 0,
    mihomo: {
      host: $('#rc-host').value.trim(),
      ports: $('#rc-ports').value.trim(),
    },
  };
  const data = await api('/api/recheck/config', { config: cfg });
  recheckState.cfg = data.config;
  fillRecheckForm(data.config, data.nodes);
  renderTrackCounts(data.stats);
  $('#rc-save-hint').textContent = `已保存 · ${new Date().toLocaleTimeString('zh-CN')}`;
  toast('复检设置已保存');
}

async function detectNodes() {
  const btn = $('#btn-rc-detect');
  btn.disabled = true;
  $('#rc-detect-hint').textContent = '正在检测…';
  try {
    const data = await api('/api/recheck/nodes', {
      mihomo: {
        host: $('#rc-host').value.trim(),
        ports: $('#rc-ports').value.trim(),
      },
    });
    if (!data.ok) throw new Error(data.error || '检测失败');
    $('#rc-detect-hint').textContent = `检测到 ${data.ok_count} / ${(data.ports || []).length} 个可用出口`;
    renderNodes(data.nodes || [], true);
    if (!data.ok_count) {
      $('#rc-detect-hint').textContent =
        '一个都没通。确认 mihomo 在跑、端口对、本机能访问这个主机名';
    }
  } finally {
    btn.disabled = false;
  }
}

async function detectCustom() {
  const btn = $('#btn-rc-test-custom');
  const urls = $('#rc-custom').value.trim();
  btn.disabled = true;
  $('#rc-custom-hint').textContent = '正在检测…';
  try {
    const data = await api('/api/recheck/nodes', { custom: urls });
    const nodes = data.custom_nodes || [];
    if (!nodes.length) {
      $('#rc-custom-hint').textContent = '没有合法地址：一行一个 http://host:port，socks5 不收';
      renderCustomNodes([]);
      return;
    }
    $('#rc-custom-hint').textContent =
      `可用 ${data.custom_ok} / ${nodes.length}｜出口 IP 见每行右侧`;
    renderCustomNodes(nodes);
  } finally {
    btn.disabled = false;
  }
}

async function runRecheck(force, ids) {
  if (recheckState.running) return;
  const cfg = recheckState.cfg;
  if (cfg && !cfg.enabled) {
    toast('封号复检没开启，先打开上面的总开关', true);
    return;
  }
  recheckState.running = true;
  const out = $('#rc-result');
  out.hidden = false;
  out.classList.remove('bad');
  out.textContent = force ? '正在复检（无视间隔）…' : '正在复检…';

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
    out.textContent = line;
    if (!s.revived && (s.banned || s.unknown)) out.classList.add('bad');
    await loadRecheckRows();
  } finally {
    recheckState.running = false;
  }
}

async function trackSelected(state) {
  const ids = [...recheckState.selected];
  if (!ids.length) return;
  const data = await api('/api/recheck/track', { ids, state });
  recheckState.selected.clear();
  renderTrackCounts(data.stats);
  await loadRecheckRows();
  toast(state ? `已加入名单：${data.changed} 条` : `已移出名单：${data.changed} 条`);
}

/* 手工把某个号拖进名单：下拉里列出全部账号，选一个加 */
async function loadRecheckPicker() {
  const select = $('#rc-add-select');
  try {
    const data = await apiGet('/api/store/list?');
    if (!data.ok) return;
    const rows = (data.rows || []).filter((r) => !r.recheck_state);
    const keep = select.value;
    select.innerHTML = '<option value="">从账户库里选一个账号…</option>'
      + rows.map((r) => `<option value="${r.id}">${esc(r.name)} · ${esc(r.realm_label || '')}</option>`).join('');
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
  $('#view-recheck').classList.toggle('hidden', view !== 'recheck');
  $('#view-update').classList.toggle('hidden', view !== 'update');
  if (view === 'store') {
    loadStore().catch((err) => toast(err.message, true));
    loadStoreProfiles();
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
      if (el.checked) applyExitMode(el.value);
    });
  });
  $('#btn-rc-test-custom').addEventListener('click', () => {
    detectCustom().catch((err) => toast(err.message, true));
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
  $('#btn-rc-sel-remove').addEventListener('click', () => {
    trackSelected('').catch((err) => toast(err.message, true));
  });
  $('#btn-rc-sel-clear').addEventListener('click', () => {
    recheckState.selected.clear();
    renderRecheckTable();
  });
  $('#btn-rc-add').addEventListener('click', () => {
    const id = Number($('#rc-add-select').value);
    if (!id) {
      toast('先选一个账号', true);
      return;
    }
    api('/api/recheck/track', { ids: [id], state: 'pending', note: '手工加入' })
      .then((data) => {
        $('#rc-add-select').value = '';
        renderTrackCounts(data.stats);
        return loadRecheckRows();
      })
      .then(() => toast('已加入复检名单'))
      .catch((err) => toast(err.message, true));
  });

  // 名单档位切换与搜索：切了立刻重查
  document.querySelectorAll('input[name="rc-track"]').forEach((el) => {
    el.addEventListener('change', () => {
      if (!el.checked) return;
      recheckState.track = el.value;
      loadRecheckRows().catch((err) => toast(err.message, true));
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
  $('#rc-check-all').addEventListener('change', (e) => {
    const on = e.target.checked;
    recheckState.selected.clear();
    if (on) recheckState.rows.forEach((r) => recheckState.selected.add(r.id));
    renderRecheckTable();
  });
  $('#rc-table').addEventListener('change', (e) => {
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

  let searchTimer = null;
  $('#store-search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    const value = e.target.value.trim();
    searchTimer = setTimeout(() => {
      storeState.q = value;
      loadStore().catch((err) => toast(err.message, true));
    }, 220);
  });

  // 表格行内操作走事件委托：表格每次重绘，逐行绑事件会漏
  $('#store-table tbody').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const tr = btn.closest('tr');
    const id = Number(tr.dataset.id);
    const row = storeState.rows.find((r) => r.id === id);
    if (!row) return;
    if (btn.dataset.act === 'rename') storeRename(id, row.name);
    if (btn.dataset.act === 'delete') storeDelete(id, row.name);
  });

  // 勾选：单行勾选框走 change 委托，表头全选同时控制两处（顶部条 + 表头）
  $('#store-table tbody').addEventListener('change', (e) => {
    const box = e.target.closest('.row-check');
    if (!box) return;
    toggleSelect(Number(box.dataset.id), box.checked);
  });

  document.querySelectorAll('#store-check-all, #store-check-all-top').forEach((box) => {
    box.addEventListener('change', () => selectAll(box.checked));
  });

  $('#btn-sel-probe').addEventListener('click', probeSelected);
  $('#btn-probe-all').addEventListener('click', probeAll);
  $('#btn-sel-export').addEventListener('click', exportSelected);
  $('#btn-sel-delete').addEventListener('click', deleteSelected);
  $('#btn-sel-clear').addEventListener('click', () => selectAll(false));

  /* --- 升级 --- */
  $('#btn-check-update').addEventListener('click', checkUpdate);
  $('#btn-apply-update').addEventListener('click', applyUpdate);
  $('#btn-apply-local').addEventListener('click', applyLocal);
  $('#btn-restart').addEventListener('click', restartServer);
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

  // ?view=store|update 直接打开对应视图（书签/深链用）
  const view = params.get('view');
  if (view === 'store' || view === 'update') switchView(view);

  // 整页拖入也接住，避免浏览器直接打开文件
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());
}

bind();
loadProfiles()
  .then(() => loadStoreProfiles())
  .catch(() => toast('格式列表加载失败', true));
loadStore().catch(() => { /* 空库不是错误，切到账户库时会再拉一次 */ });
