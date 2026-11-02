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
      <td class="mono">${esc(r.expires) || '—'}</td>
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
    body.innerHTML = '<tr><td colspan="10" class="empty-cell">账户库是空的。先在「导入转换」里转一遍，再点「保存到账户库」。</td></tr>';
    $('#store-hint').textContent = '';
    return;
  }

  const sel = storeState.selected;
  body.innerHTML = rows.map((r) => `
    <tr data-id="${r.id}" class="${sel.has(r.id) ? 'sel' : ''}">
      <td class="col-check"><input type="checkbox" class="row-check" data-id="${r.id}" ${sel.has(r.id) ? 'checked' : ''}></td>
      <td><b>${esc(r.name)}</b>${r.nickname && r.nickname !== r.name ? `<br><small class="hint">${esc(r.nickname)}</small>` : ''}</td>
      <td><span class="pill ${REALM_PILL[r.realm] || 'mute'}">${esc(r.realm_label || REALM_TEXT[r.realm] || '未知')}</span></td>
      <td class="mono" title="${esc(r.uid)}">${esc(shortUid(r.uid))}</td>
      <td><span class="pill ${STATE_PILL[r.state] || 'mute'}">${esc(r.state_label)}</span></td>
      <td class="mono">${esc(r.expires) || '—'}</td>
      <td class="mono" title="${esc(r.at)}">${esc(r.at)}</td>
      <td>${r.has_rt ? '<span class="pill ok">有</span>' : '<span class="hint">—</span>'}</td>
      <td class="mono" title="${esc(r.updated_at)}">${esc(shortTime(r.updated_at))}</td>
      <td class="row-actions">
        <button class="link" data-act="rename">改名</button>
        <button class="link danger" data-act="delete">删除</button>
      </td>
    </tr>`).join('');

  $('#store-hint').textContent = `共 ${rows.length} 条`;
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
    });
    toast('制品源已保存');
    $('#upd-hint').textContent = '仓库：' + (data.config.repo || '未设置');
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------- 视图切换 ---------- */

function switchView(view) {
  document.querySelectorAll('.tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.view === view);
  });
  $('#view-convert').classList.toggle('hidden', view !== 'convert');
  $('#view-store').classList.toggle('hidden', view !== 'store');
  $('#view-update').classList.toggle('hidden', view !== 'update');
  if (view === 'store') {
    loadStore().catch((err) => toast(err.message, true));
    loadStoreProfiles();
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

  $('#btn-sel-export').addEventListener('click', exportSelected);
  $('#btn-sel-delete').addEventListener('click', deleteSelected);
  $('#btn-sel-clear').addEventListener('click', () => selectAll(false));

  /* --- 升级 --- */
  $('#btn-check-update').addEventListener('click', checkUpdate);
  $('#btn-apply-update').addEventListener('click', applyUpdate);
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
