// game-smoke 看板：只读，不引任何框架/构建链
const $ = (id) => document.getElementById(id);

// game_key -> 展示名（来自 /api/games，缺省退回 key）
const gameNames = {};

const esc = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (ch) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch])
  );

const gameLabel = (gameKey) => gameNames[gameKey] || gameKey;

const STATUS = {
  success: ['ok', '成功'],
  failed: ['bad', '失败'],
  'not-run': ['warn', '未跑'],
};

const statusBadge = (status) => {
  const [cls, label] = STATUS[status] || ['muted', status || '未知'];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
};

const statusDot = (status) => {
  const [cls] = STATUS[status] || ['muted'];
  return `<span class="dot ${cls}"></span>`;
};

// 失败定性只是标注：不改运行状态、不影响通过率，人工可随时改判
const CAUSE_FALLBACK = {
  env: '环境问题',
  stale: '用例过期',
  case_flaw: '用例缺陷',
  bug: '产品缺陷',
  unknown: '待定',
};
let causeLabels = { ...CAUSE_FALLBACK };

// 词表真身在 server/causes.py，这里取一次；取不到就用内置兜底
async function loadCauseCatalog() {
  try {
    const data = await getJson('/api/causes');
    for (const item of data.items) causeLabels[item.key] = item.label;
  } catch {
    // 兜底词表已经够用，不打断页面
  }
}

const causeSourceLabel = (source) =>
  source === 'manual' ? '人工' : source === 'ai' ? 'AI' : '规则';

const causeBadge = (item, runId) => {
  const key = item.cause_category || 'unknown';
  const label = causeLabels[key] || key;
  const title = `${label}（${causeSourceLabel(item.cause_source)}）${item.cause_reason || ''}`;
  return `<button class="cause-badge cause-${esc(key)} source-${esc(item.cause_source || 'rule')}"
    data-cat="${esc(key)}" data-run="${esc(runId)}" data-path="${esc(item.source_path || '')}"
    data-index="${esc(item.case_index)}" title="${esc(title)}">${esc(label)}</button>`;
};

const causeCell = (item, runId) => {
  if (item.status !== 'failed') return '<span class="cause-cell"></span>';
  return `<span class="cause-cell" data-run="${esc(runId)}" data-path="${esc(item.source_path || '')}"
    data-index="${esc(item.case_index)}">${causeBadge(item, runId)}</span>`;
};

// 改判用原生下拉：选项用完整名称，比一排缩写按钮更好认
function openCauseEditor(badge) {
  const cell = badge.closest('.cause-cell');
  if (!cell) return;
  const current = badge.dataset.cat || 'unknown';
  cell.dataset.original = badge.outerHTML;
  const options = Object.entries(causeLabels)
    .map(
      ([key, label]) =>
        `<option value="${esc(key)}"${key === current ? ' selected' : ''}>${esc(label)}</option>`
    )
    .join('');
  cell.innerHTML = `<span class="cause-edit"><select class="cause-select" aria-label="选择失败原因">${options}</select><button type="button" class="cause-cancel">取消</button></span>`;
  cell.querySelector('.cause-select')?.focus();
}

async function applyCause(cell, category) {
  if (!cell) return;
  if (!category) {
    cell.innerHTML = cell.dataset.original || '';
    return;
  }
  try {
    const response = await fetch('/api/cases/cause', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        run_id: cell.dataset.run,
        source_path: cell.dataset.path,
        case_index: Number(cell.dataset.index),
        category,
      }),
    });
    if (!response.ok) {
      throw new Error(response.status + ' ' + (await response.text()).slice(0, 120));
    }
    const updated = await response.json();
    cell.innerHTML = causeBadge(updated, updated.run_id);
  } catch (error) {
    cell.innerHTML =
      (cell.dataset.original || '') + `<span class="cause-error">改判失败：${esc(error.message)}</span>`;
  }
}

const absTime = (iso) => {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString('zh-CN', { hour12: false });
};

const relativeTime = (iso) => {
  if (!iso) return '—';
  const date = new Date(iso);
  const diff = Date.now() - date.getTime();
  if (Number.isNaN(diff)) return iso;
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diff < minute) return '刚刚';
  if (diff < hour) return Math.floor(diff / minute) + ' 分钟前';
  if (diff < day) return Math.floor(diff / hour) + ' 小时前';
  if (diff < 7 * day) return Math.floor(diff / day) + ' 天前';
  return date.toLocaleDateString('zh-CN');
};

const fmtDuration = (ms) => {
  if (ms == null) return '—';
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return seconds + 's';
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? minutes + 'm ' + rest + 's' : minutes + 'm';
};

const reportLink = (url, cls = 'btn ghost') =>
  url
    ? `<a class="${cls}" href="${esc(url)}" target="_blank" rel="noreferrer">报告</a>`
    : '<span class="badge muted">无报告</span>';

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error(response.status + ' ' + (await response.text()).slice(0, 160));
  return response.json();
}

async function loadStats() {
  try {
    const [health, stats] = await Promise.all([
      getJson('/api/health'),
      getJson('/api/stats?days=30'),
    ]);
    const passed = stats.items.reduce((sum, day) => sum + (day.passed || 0), 0);
    const failed = stats.items.reduce((sum, day) => sum + (day.failed || 0), 0);
    const rate = passed + failed ? Math.round((passed / (passed + failed)) * 100) : null;
    const runs = stats.items.reduce((sum, day) => sum + (day.runs || 0), 0);

    $('stats').innerHTML = [
      ['运行总数', health.runs, health.cases + ' 条用例记录'],
      ['近 30 天通过率', rate == null ? '—' : rate + '%', passed + ' 通过 / ' + failed + ' 失败'],
      ['近 30 天运行', runs, stats.items.length + ' 天有数据'],
    ]
      .map(
        ([label, value, meta]) =>
          `<div class="stat"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="meta">${esc(meta)}</div></div>`
      )
      .join('');
  } catch (error) {
    $('stats').innerHTML = `<div class="stat"><div class="label">统计加载失败</div><div class="meta">${esc(error.message)}</div></div>`;
  }
}

async function loadGames() {
  const box = $('games');
  try {
    const data = await getJson('/api/games');
    if (!data.items.length) {
      box.innerHTML =
        '<div class="game-card"><div class="name">暂无游戏数据</div><div class="key">历史运行来自旧布局（cases/…），未关联游戏</div></div>';
      return;
    }
    for (const game of data.items) gameNames[game.game_key] = game.name || game.game_key;

    box.innerHTML = data.items
      .map((game) => {
        const last = game.last_run;
        return `<div class="game-card">
          <div class="name">${esc(game.name || game.game_key)}</div>
          <div class="key">${esc(game.game_key)}</div>
          <div class="row">
            ${last ? statusBadge(last.status) : '<span class="badge muted">无运行</span>'}
            <span>${last ? esc(relativeTime(last.started_at)) + ' · ' + esc(last.passed) + '/' + esc(last.total) : '—'}</span>
          </div>
        </div>`;
      })
      .join('');

    const select = $('game');
    const current = select.value;
    select.innerHTML =
      '<option value="">全部游戏</option>' +
      data.items
        .map((game) => `<option value="${esc(game.game_key)}">${esc(game.name || game.game_key)}</option>`)
        .join('');
    select.value = current;
  } catch (error) {
    box.innerHTML = `<div class="game-card"><div class="name">游戏列表加载失败</div><div class="key">${esc(error.message)}</div></div>`;
  }
}

function renderRun(run) {
  const games = (run.games || '').split(',').filter(Boolean).map(gameLabel).join('、');
  const title = games ? esc(games) : '<span class="muted">未关联游戏</span>';
  const suite = run.suites || '—';

  return `<article class="run ${esc(run.status || '')}" data-run-id="${esc(run.run_id)}">
    <div>
      <div class="run-head">
        <span class="run-time">${esc(relativeTime(run.started_at))} <span class="abs">${esc(absTime(run.started_at))}</span></span>
        <span class="run-suite">${esc(suite)}</span>
        ${statusBadge(run.status)}
      </div>
      <div class="run-title">${title} <span class="muted">· ${esc(run.run_id)}</span></div>
      <div class="run-metrics">
        <span class="metric">总数 <b>${esc(run.total ?? '—')}</b></span>
        <span class="metric ok">通过 <b>${esc(run.passed ?? '—')}</b></span>
        <span class="metric bad">失败 <b>${esc(run.failed ?? '—')}</b></span>
        <span class="metric">未跑 <b>${esc(run.not_run ?? '—')}</b></span>
      </div>
    </div>
    <div class="run-actions">${reportLink(run.report_url)}</div>
    <div class="run-detail hidden"></div>
  </article>`;
}

async function loadRuns() {
  const params = new URLSearchParams();
  for (const key of ['game', 'suite', 'status']) {
    const value = $(key).value;
    if (value) params.set(key, value);
  }
  params.set('limit', $('limit').value || '20');

  const box = $('runs');
  box.innerHTML = '<div class="placeholder">加载中…</div>';
  try {
    const data = await getJson('/api/runs?' + params.toString());
    box.innerHTML = data.items.length
      ? data.items.map(renderRun).join('')
      : '<div class="placeholder">没有符合条件的运行</div>';
  } catch (error) {
    box.innerHTML = `<div class="placeholder">加载失败：${esc(error.message)}</div>`;
  }
}

async function toggleDetail(card) {
  const detail = card.querySelector('.run-detail');
  if (!detail) return;
  if (!detail.classList.contains('hidden')) {
    detail.classList.add('hidden');
    return;
  }
  detail.classList.remove('hidden');
  detail.innerHTML = '<div class="placeholder">加载中…</div>';

  try {
    const run = await getJson('/api/runs/' + encodeURIComponent(card.dataset.runId));
    const cases = run.cases || [];
    detail.innerHTML = `
      <div class="meta">
        <span>开始 ${esc(absTime(run.started_at))}</span>
        <span>耗时 ${esc(fmtDuration(run.duration_ms))}</span>
        <span>用例集 ${esc(run.suites || '—')}</span>
        <span>游戏 ${esc((run.games || '').split(',').filter(Boolean).map(gameLabel).join('、') || '—')}</span>
      </div>
      ${
        cases.length
          ? `<div class="case-list">${cases
              .map(
                (item) => `<div class="case">
                  ${statusDot(item.status)}
                  <span class="name" title="${esc(item.name || '')}">${esc(item.name || '—')}</span>
                  <span class="suite">${esc(item.suite || '—')}</span>
                  <span class="file" title="${esc(item.source_path || '')}">${esc(item.source_path || '—')}</span>
                  ${causeCell(item, run.run_id)}
                  ${statusBadge(item.status)}
                </div>`
              )
              .join('')}</div>`
          : '<div class="placeholder">这次运行没有用例记录</div>'
      }`;
  } catch (error) {
    detail.innerHTML = `<div class="placeholder">详情加载失败：${esc(error.message)}</div>`;
  }
}

$('filters').addEventListener('submit', (event) => {
  event.preventDefault();
  loadRuns();
});

$('refresh').addEventListener('click', () => {
  loadStats();
  loadGames();
  loadRuns();
});

$('runs').addEventListener('click', (event) => {
  // 改判面板优先处理，且不要把点击冒泡成"折叠详情"
  const cancel = event.target.closest('.cause-cancel');
  if (cancel) {
    event.stopPropagation();
    const cell = cancel.closest('.cause-cell');
    if (cell) cell.innerHTML = cell.dataset.original || '';
    return;
  }
  if (event.target.closest('.cause-edit')) {
    event.stopPropagation();
    return;
  }
  const badge = event.target.closest('.cause-badge');
  if (badge) {
    event.stopPropagation();
    openCauseEditor(badge);
    return;
  }
  if (event.target.closest('a')) return;
  // 详情区域内的点击（复制文件名、看错误信息）不该折叠整条运行
  if (event.target.closest('.run-detail')) return;
  const card = event.target.closest('.run');
  if (card) toggleDetail(card);
});

// 下拉选中即提交；change 也会冒泡到 #runs
$('runs').addEventListener('change', (event) => {
  const select = event.target.closest('.cause-select');
  if (!select) return;
  event.stopPropagation();
  applyCause(select.closest('.cause-cell'), select.value);
});

loadCauseCatalog();
loadStats();
loadGames();
loadRuns();
