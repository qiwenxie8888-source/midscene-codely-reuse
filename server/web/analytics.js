// 结果分析页：原生 SVG 画图，不引任何图表库
const $ = (id) => document.getElementById(id);

const esc = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (ch) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch])
  );

const fmtDuration = (ms) => {
  if (ms == null) return '—';
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return seconds + 's';
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? minutes + 'm ' + rest + 's' : minutes + 'm';
};

const fmtDay = (day) => (day || '').slice(5); // 2026-10-21 -> 10-21

const statusColor = (status) => {
  if (status === 'success') return 'var(--ok)';
  if (status === 'failed') return 'var(--bad)';
  if (status === 'not-run') return 'var(--warn)';
  return 'var(--faint)';
};

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error(response.status + ' ' + (await response.text()).slice(0, 160));
  return response.json();
}

function query() {
  const params = new URLSearchParams();
  params.set('days', $('days').value);
  if ($('game').value) params.set('game', $('game').value);
  if ($('suite').value) params.set('suite', $('suite').value);
  return params;
}

/* ---------- 折线图（原生 SVG） ---------- */
function lineChart(points, { format = (v) => v, color = 'var(--accent)', yMin = 0, yMax = 1 } = {}) {
  const width = 720;
  const height = 200;
  const padLeft = 46;
  const padRight = 16;
  const padTop = 12;
  const padBottom = 26;
  const innerW = width - padLeft - padRight;
  const innerH = height - padTop - padBottom;

  if (!points.length) return '<div class="placeholder">该范围内没有数据</div>';

  const x = (index) =>
    padLeft + (points.length === 1 ? innerW / 2 : (index / (points.length - 1)) * innerW);
  const y = (value) =>
    padTop + innerH - ((value - yMin) / (yMax - yMin || 1)) * innerH;

  const line = points.map((p, i) => x(i) + ',' + y(p.value)).join(' ');
  const area = padLeft + ',' + (padTop + innerH) + ' ' + line + ' ' + x(points.length - 1) + ',' + (padTop + innerH);

  const grid = [0, 0.25, 0.5, 0.75, 1]
    .map((ratio) => {
      const value = yMin + ratio * (yMax - yMin);
      const gy = y(value);
      return (
        '<line x1="' + padLeft + '" y1="' + gy + '" x2="' + (width - padRight) + '" y2="' + gy +
        '" stroke="var(--line)" stroke-width="1" />' +
        '<text x="' + (padLeft - 8) + '" y="' + (gy + 4) + '" text-anchor="end" class="axis">' +
        esc(format(value)) + '</text>'
      );
    })
    .join('');

  const dots = points
    .map(
      (p, i) =>
        '<circle cx="' + x(i) + '" cy="' + y(p.value) + '" r="3" fill="' + color + '">' +
        '<title>' + esc(p.label + ' · ' + format(p.value)) + '</title></circle>'
    )
    .join('');

  const labels = points
    .map(
      (p, i) =>
        (points.length <= 12 || i % Math.ceil(points.length / 10) === 0
          ? '<text x="' + x(i) + '" y="' + (height - 8) + '" text-anchor="middle" class="axis">' +
            esc(p.label) + '</text>'
          : '')
    )
    .join('');

  return (
    '<svg viewBox="0 0 ' + width + ' ' + height + '" class="svg-chart" role="img">' +
    grid +
    '<polygon points="' + area + '" fill="' + color + '" opacity="0.08" />' +
    '<polyline points="' + line + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linejoin="round" />' +
    dots +
    labels +
    '</svg>'
  );
}

/* ---------- 数据加载 ---------- */
async function loadTrend() {
  const params = query();
  try {
    const data = await getJson('/api/analytics/trend?' + params.toString());
    const ratePoints = data.items
      .filter((item) => item.pass_rate != null)
      .map((item) => ({ label: fmtDay(item.day), value: item.pass_rate }));

    $('trend').innerHTML =
      lineChart(ratePoints, {
        format: (value) => Math.round(value * 100) + '%',
        color: 'var(--ok)',
      }) +
      '<div class="legend">' +
      data.items
        .map((item) => esc(fmtDay(item.day)) + '：' + esc(item.passed) + ' 通过 / ' + esc(item.failed) + ' 失败')
        .join('<span class="sep">·</span>') +
      '</div>';

    const durationPoints = data.items
      .filter((item) => item.avg_case_ms != null)
      .map((item) => ({ label: fmtDay(item.day), value: item.avg_case_ms }));
    const maxDuration = Math.max(...durationPoints.map((p) => p.value), 1000);
    $('duration').innerHTML = lineChart(
      durationPoints.map((p) => ({ ...p, value: p.value / 1000 })),
      {
        format: (value) => Math.round(value) + 's',
        color: 'var(--accent)',
        yMin: 0,
        yMax: Math.ceil(maxDuration / 1000) + 10,
      }
    );
  } catch (error) {
    $('trend').innerHTML = '<div class="placeholder">趋势加载失败：' + esc(error.message) + '</div>';
    $('duration').innerHTML = '';
  }
}

async function loadFailures() {
  const params = query();
  params.set('limit', '10');
  try {
    const data = await getJson('/api/analytics/failures?' + params.toString());
    if (!data.items.length) {
      $('failures').innerHTML = '<div class="placeholder">该范围内没有失败用例 🎉</div>';
      return;
    }
    const max = Math.max(...data.items.map((item) => item.failures));
    $('failures').innerHTML = data.items
      .map((item) => {
        const width = Math.max(6, Math.round((item.failures / max) * 100));
        return (
          '<div class="bar-row">' +
          '<div class="bar-head">' +
          '<span class="bar-name">' + esc(item.name) + '</span>' +
          '<span class="bar-tags">' +
          (item.error_codes ? '<span class="tag">' + esc(item.error_codes.replace(/,/g, ' / ')) + '</span>' : '') +
          (item.failed_nodes ? '<span class="tag muted">' + esc(item.failed_nodes.replace(/,/g, ' / ')) + '</span>' : '') +
          '<span class="tag bad">' + esc(item.failures) + ' 次</span>' +
          '</span>' +
          '</div>' +
          '<div class="bar-track"><div class="bar-fill" style="width:' + width + '%"></div></div>' +
          (item.last_error_message
            ? '<div class="bar-note">最近错误：' + esc(item.last_error_message) + '</div>'
            : '') +
          '</div>'
        );
      })
      .join('');
  } catch (error) {
    $('failures').innerHTML = '<div class="placeholder">失败排行加载失败：' + esc(error.message) + '</div>';
  }
}

async function loadMatrix() {
  const params = new URLSearchParams({ limit: '10' });
  if ($('game').value) params.set('game', $('game').value);
  try {
    const data = await getJson('/api/analytics/matrix?' + params.toString());
    if (!data.rows.length) {
      $('matrix').innerHTML = '<div class="placeholder">没有可对比的运行</div>';
      return;
    }
    const head = data.runs
      .map((run) => '<th title="' + esc(run.started_at) + '">' + esc(String(run.started_at || '').slice(5, 10)) + '</th>')
      .join('');

    const rows = data.rows
      .map((row) => {
        const cells = row.cells
          .map((cell) => {
            const status = cell ? cell.status : null;
            const title = cell ? status + (cell.error_code ? ' · ' + cell.error_code : '') : '该次未运行';
            return '<td><span class="cell" style="background:' + statusColor(status) + '" title="' + esc(title) + '"></span></td>';
          })
          .join('');
        return (
          '<tr><th class="row-name" title="' + esc(row.name) + '">' + esc(row.name) + '</th>' + cells +
          '<td class="fail-count">' + (row.failures ? row.failures + ' 次失败' : '全部通过') + '</td></tr>'
        );
      })
      .join('');

    $('matrix').innerHTML =
      '<table class="matrix-table"><thead><tr><th></th>' + head + '<th></th></tr></thead><tbody>' + rows + '</tbody></table>';
  } catch (error) {
    $('matrix').innerHTML = '<div class="placeholder">矩阵加载失败：' + esc(error.message) + '</div>';
  }
}

async function loadDistribution() {
  try {
    const data = await getJson('/api/analytics/distribution?days=' + $('days').value);
    if (!data.items.length) {
      $('distribution').innerHTML = '<div class="placeholder">没有数据</div>';
      return;
    }
    const max = Math.max(...data.items.map((item) => (item.passed || 0) + (item.failed || 0)));
    $('distribution').innerHTML = data.items
      .map((item) => {
        const total = (item.passed || 0) + (item.failed || 0);
        const passedWidth = total ? Math.round(((item.passed || 0) / max) * 100) : 0;
        const failedWidth = total ? Math.round(((item.failed || 0) / max) * 100) : 0;
        return (
          '<div class="bar-row">' +
          '<div class="bar-head"><span class="bar-name">' + esc(item.game_key) + ' · ' + esc(item.suite) + '</span>' +
          '<span class="bar-tags"><span class="tag">' + esc(item.passed || 0) + ' 通过</span>' +
          '<span class="tag bad">' + esc(item.failed || 0) + ' 失败</span></span></div>' +
          '<div class="stack"><span class="stack-ok" style="width:' + passedWidth + '%"></span>' +
          '<span class="stack-bad" style="width:' + failedWidth + '%"></span></div>' +
          '</div>'
        );
      })
      .join('');
  } catch (error) {
    $('distribution').innerHTML = '<div class="placeholder">分布加载失败：' + esc(error.message) + '</div>';
  }
}

async function loadRunOptions() {
  try {
    const data = await getJson('/api/runs?limit=30');
    const options = data.items
      .map(
        (run) =>
          '<option value="' + esc(run.run_id) + '">' + esc(String(run.started_at || '').slice(0, 16).replace('T', ' ')) +
          ' · ' + esc(run.status) + ' · ' + esc(run.run_id) + '</option>'
      )
      .join('');
    $('run-a').innerHTML = '<option value="">基准（较早）</option>' + options;
    $('run-b').innerHTML = '<option value="">对比（较新）</option>' + options;
    // 默认挑"同一用例集里用例最多的那两次"：
    // 不同套件的用例没有交集，对比会全是 0；用例少的（比如只有 1 条的冒烟）也对比不出东西。
    const bySuite = {};
    for (const run of data.items) {
      const key = run.suites || '-';
      (bySuite[key] = bySuite[key] || []).push(run);
    }
    const candidates = Object.values(bySuite).filter((list) => list.length >= 2);
    candidates.sort(
      (left, right) =>
        Math.max(...right.map((run) => run.total || 0)) - Math.max(...left.map((run) => run.total || 0))
    );
    const pair = candidates[0] || data.items;
    if (pair.length >= 2) {
      $('run-a').value = pair[1].run_id;
      $('run-b').value = pair[0].run_id;
      // 选中后立即对比一次，避免用户以为按钮没反应
      doCompare();
    }
  } catch {
    // 下拉加载失败不影响其他区块
  }
}

async function loadGames() {
  try {
    const data = await getJson('/api/games');
    $('game').innerHTML =
      '<option value="">全部游戏</option>' +
      data.items
        .map((game) => '<option value="' + esc(game.game_key) + '">' + esc(game.name || game.game_key) + '</option>')
        .join('');
  } catch {
    // 忽略
  }
}

async function doCompare() {
  const a = $('run-a').value;
  const b = $('run-b').value;
  if (!a || !b) {
    $('compare-result').innerHTML = '<div class="placeholder">请选择两次运行</div>';
    return;
  }
  $('compare-result').innerHTML = '<div class="placeholder">对比中…</div>';
  try {
    const data = await getJson('/api/analytics/compare?a=' + encodeURIComponent(a) + '&b=' + encodeURIComponent(b));
    const block = (title, items, cls) =>
      '<div class="compare-block"><h4>' + esc(title) + '（' + items.length + '）</h4>' +
      (items.length
        ? '<div class="case-list">' + items
            .map(
              (item) =>
                '<div class="case"><span class="dot ' + cls + '"></span>' +
                '<span class="name">' + esc(item.name) + '</span>' +
                '<span class="suite">' + esc(item.error_code || item.status) + '</span>' +
                '<span class="file">' + esc(fmtDuration(item.duration_ms)) + '</span>' +
                '<span class="muted">' + esc(item.before || '未运行') + ' → ' + esc(item.status) + '</span></div>'
            )
            .join('') + '</div>'
        : '<div class="placeholder">无</div>') +
      '</div>';

    const counts = data.counts || {};
    const total =
      (counts.newly_failed || 0) +
      (counts.newly_passed || 0) +
      (counts.still_failed || 0) +
      (counts.not_run || 0);

    if (!total) {
      $('compare-result').innerHTML =
        '<div class="placeholder">这两次运行没有共同的用例（通常是套件不同：一次 smoke、一次 explore），无法逐条对比。<br />请选择同套件的两次运行。</div>';
      return;
    }

    $('compare-result').innerHTML =
      '<div class="compare-summary">新增失败 <b class="bad">' + (counts.newly_failed || 0) + '</b>' +
      '<span class="sep">·</span>新增通过 <b class="ok">' + (counts.newly_passed || 0) + '</b>' +
      '<span class="sep">·</span>仍然失败 <b class="warn">' + (counts.still_failed || 0) + '</b>' +
      '<span class="sep">·</span>未跑 <b>' + (counts.not_run || 0) + '</b></div>' +
      block('新增失败', data.newly_failed, 'bad') +
      block('新增通过', data.newly_passed, 'ok') +
      block('仍然失败', data.still_failed, 'warn');
  } catch (error) {
    $('compare-result').innerHTML = '<div class="placeholder">对比失败：' + esc(error.message) + '</div>';
  }
}

$('apply').addEventListener('click', () => {
  loadTrend();
  loadFailures();
  loadMatrix();
  loadDistribution();
});
$('compare').addEventListener('click', doCompare);

loadGames().then(() => {
  loadTrend();
  loadFailures();
  loadMatrix();
  loadDistribution();
  loadRunOptions();
});
