// 用例管理页：左侧渠道/游戏树 + 右侧套件列表 + 运行 / 实时进度 / 停止 / 编辑 / 新增 / 删除
// 零框架、原生 fetch，与 index.html、analytics.html 保持一致

const SUITE_LABELS = { smoke: '冒烟用例', explore: '探索用例' };
const SUITE_NOTES = {
  smoke: '每次版本必跑',
  explore: '新游戏首测时手动跑 · 一套跑完约 30-40 分钟',
};

const state = {
  items: [],
  tree: [],
  collapsedChannels: {},
  selected: new Set(),
  selectedSuite: null,
  selectedGame: null,
  collapsed: {},
  activeGame: null, // null = 全部用例视图（只读，不能运行）
  jobId: null,
  startedAt: null,
  timer: null,
  eventSource: null,
  phaseLines: new Map(),
  running: false,
};

const el = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!response.ok) {
    const error = new Error((data && data.detail) || response.statusText);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

// ------------------------------------------------------------------ 数据

async function load() {
  // 用例列表用于渲染具体文件；游戏树用于渲染左侧先道/游戏（含还没有任何用例的空游戏）
  const [cases, tree] = await Promise.all([api('/api/cases'), api('/api/game-tree')]);
  state.items = cases.items || [];
  state.tree = tree.channels || [];
  const alive = new Set(state.items.map((item) => item.path));
  for (const path of [...state.selected]) {
    if (!alive.has(path)) state.selected.delete(path);
  }
  const games = new Set(allGames().map((item) => item.game_key));
  if (state.activeGame && !games.has(state.activeGame)) state.activeGame = null;
  // 支持 ?game=douyin/xxx 直接定位到某个游戏（可分享、也方便刷新后保持选中）
  const requested = new URLSearchParams(location.search).get('game');
  if (!state.activeGame && requested && games.has(requested)) state.activeGame = requested;
  render();
}

/** 拍平游戏树，并把该游戏的用例文件挂上去（空游戏 items 为空数组）。 */
function allGames() {
  const games = [];
  for (const channel of state.tree) {
    for (const game of channel.games) {
      games.push({
        ...game,
        channel_label: channel.label,
        items: state.items.filter((item) => item.game_key === game.game_key),
      });
    }
  }
  return games.sort((a, b) => a.game_key.localeCompare(b.game_key));
}

function gamesFromItems() {
  return allGames();
}

function currentScope() {
  const games = allGames();
  return state.activeGame ? games.filter((game) => game.game_key === state.activeGame) : games;
}

// ------------------------------------------------------------------ 渲染

function render() {
  const games = allGames();
  renderSidebar(games);

  const host = el('suites');
  host.innerHTML = '';

  if (!games.length) {
    host.innerHTML =
      '<div class="suite-block" style="padding:18px"><span class="hint">还没有任何游戏。在左侧渠道名旁点「+ 新建游戏」开始。</span></div>';
    return;
  }

  if (!state.activeGame) {
    const tip = document.createElement('div');
    tip.className = 'notice';
    tip.textContent = '当前是「全部用例」视图，只用于浏览；运行前请先在左侧选择一个游戏。';
    host.appendChild(tip);
  }

  for (const game of currentScope()) {
    for (const suite of ['smoke', 'explore']) {
      const items = game.items.filter((item) => item.suite === suite);
      // 空套件也要渲染：新建的游戏没有任何用例时，用户需要从这里点「+ 新增」
      if (!items.length && !state.activeGame) continue;
      host.appendChild(suiteBlock(game, suite, items));
    }
  }
}

function renderSidebar(games) {
  const host = el('sidebar');
  host.innerHTML = '';

  const all = document.createElement('div');
  all.className = 'side-item' + (state.activeGame ? '' : ' active');
  all.dataset.game = '';
  all.innerHTML = `<span>全部用例</span><span class="count">${state.items.length}</span>`;
  host.appendChild(all);

  // 渠道分组：标题可折叠（默认展开），右侧带"+ 新建游戏"
  for (const channel of state.tree) {
    const collapsed = state.collapsedChannels[channel.channel] === true;
    const header = document.createElement('div');
    header.className = 'side-channel';
    header.innerHTML = `
      <span class="caret">${collapsed ? '▸' : '▾'}</span>
      <span class="channel-name">${channel.label}</span>
      <span class="count">${channel.games.length}</span>
      <button class="side-add" data-new-game="${channel.channel}" title="在 ${channel.label} 下新建游戏">+</button>
    `;
    header.addEventListener('click', (event) => {
      if (event.target.closest('[data-new-game]')) return;
      state.collapsedChannels[channel.channel] = !collapsed;
      render();
    });
    host.appendChild(header);

    if (collapsed) continue;
    for (const game of channel.games) {
      const node = document.createElement('div');
      node.className = 'side-item' + (state.activeGame === game.game_key ? ' active' : '');
      node.dataset.game = game.game_key;
      node.title = game.game_key;
      const count = game.smoke + game.explore;
      node.innerHTML = `<span>${game.game_name}</span><span class="count">${count}</span>`;
      host.appendChild(node);
    }
  }
}

function suiteBlock(game, suite, items) {
  const block = document.createElement('div');
  block.className = 'suite-block';
  const key = `${game.game_key}/${suite}`;
  const collapsed = suite === 'explore' ? (state.collapsed[key] ?? true) : false;
  const selectedInSuite = items.filter((item) => state.selected.has(item.path)).length;
  const allSelected = selectedInSuite === items.length && items.length > 0;
  const canRun = Boolean(state.activeGame);

  const title = state.activeGame
    ? `${SUITE_LABELS[suite]} (${items.length})`
    : `${game.channel_label} · ${game.game_name} · ${SUITE_LABELS[suite]} (${items.length})`;

  const head = document.createElement('div');
  head.className = 'suite-head';
  head.innerHTML = `
    ${suite === 'explore' ? `<button class="btn-mini" data-toggle="${key}">${collapsed ? '▸' : '▾'}</button>` : ''}
    <h3>${title}</h3>
    <span class="hint">${SUITE_NOTES[suite] || ''}</span>
    <span class="spacer"></span>
    <button class="btn-mini" data-new="${suite}" data-game="${game.game_key}">+ 新增</button>
    <button class="btn ghost" data-run-suite="${suite}" data-game="${game.game_key}" ${canRun ? '' : 'disabled title="请先在左侧选择游戏"'}>运行整个${SUITE_LABELS[suite]}</button>
  `;
  block.appendChild(head);

  if (!collapsed) {
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'case-row';
      empty.innerHTML =
        '<span class="hint" style="padding-left:4px">这个套件还没有用例，点右上角「+ 新增」创建第一条。</span>';
      block.appendChild(empty);
    }
    for (const item of items) block.appendChild(caseRow(item, game));
    const bar = document.createElement('div');
    bar.className = 'toolbar';
    bar.innerHTML = `
      <button class="btn-mini" data-select-all="${suite}" data-game="${game.game_key}" ${
        canRun && items.length ? '' : 'disabled'
      }>全选</button>
      <span class="hint">${
        allSelected
          ? '已全部选中，运行时会自动改用整包模式（复用会话，更快）'
          : `已选 ${selectedInSuite} / ${items.length}`
      }</span>
      <span class="spacer"></span>
      <button class="btn" data-run-selected="${suite}" data-game="${game.game_key}" ${
        selectedInSuite === 0 || !canRun ? 'disabled' : ''
      }>运行选中 (${selectedInSuite})</button>
    `;
    block.appendChild(bar);
  }
  return block;
}

function caseRow(item, game) {
  const row = document.createElement('div');
  row.className = 'case-row' + (item.parse_error ? ' broken' : '');

  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.checked = state.selected.has(item.path);
  // 一次只能在一个「游戏 + 套件」里勾选：跨游戏跑会因状态污染产生假失败
  const blocked =
    (state.selectedGame && state.selectedGame !== item.game_key) ||
    (state.selectedSuite && state.selectedSuite !== item.suite) ||
    !state.activeGame;
  checkbox.disabled = Boolean(blocked);
  if (!state.activeGame) checkbox.title = '请先在左侧选择一个游戏';
  else if (blocked) checkbox.title = '一次只能在一个游戏的一个套件里勾选';
  checkbox.addEventListener('change', () => {
    if (checkbox.checked) {
      state.selectedGame = item.game_key;
      state.selectedSuite = item.suite;
      state.selected.add(item.path);
    } else {
      state.selected.delete(item.path);
      if (!state.selected.size) {
        state.selectedGame = null;
        state.selectedSuite = null;
      }
    }
    render();
  });

  const file = document.createElement('div');
  file.className = 'file';
  file.textContent = item.path.split('/').pop();

  const name = document.createElement('div');
  name.className = 'name';
  name.textContent = item.parse_error
    ? item.parse_error
    : `${item.case_name || '(未命名)'}${item.case_count > 1 ? ` 等 ${item.case_count} 条` : ''}`;

  const actions = document.createElement('div');
  actions.style.display = 'flex';
  actions.style.gap = '6px';
  actions.innerHTML = `
    ${item.parse_error ? '<span class="tag-error">解析失败</span>' : ''}
    ${item.seq != null ? `<span class="tag-seq">${String(item.seq).padStart(2, '0')}</span>` : ''}
  `;
  const edit = document.createElement('button');
  edit.className = 'btn-mini';
  edit.textContent = '编辑';
  edit.addEventListener('click', () => openEditor(item.path));
  const remove = document.createElement('button');
  remove.className = 'btn-mini danger';
  remove.textContent = '删除';
  remove.addEventListener('click', () => confirmDelete(item));
  actions.appendChild(edit);
  actions.appendChild(remove);

  row.appendChild(checkbox);
  row.appendChild(file);
  row.appendChild(name);
  row.appendChild(actions);
  return row;
}

// ------------------------------------------------------------------ 运行

async function startRun(payload, title) {
  if (state.running) return;
  try {
    const job = await api('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    state.jobId = job.run_id;
    state.running = true;
    state.startedAt = Date.now();
    state.phaseLines.clear();
    el('result-panel').hidden = true;
    el('run-panel').hidden = false;
    el('log-lines').innerHTML = '';
    el('run-title').textContent = '正在运行：' + title;
    el('stop').disabled = false;
    startTimer();
    watch(job.run_id);
  } catch (error) {
    alert('启动失败：' + error.message);
  }
}

function startTimer() {
  clearInterval(state.timer);
  state.timer = setInterval(() => {
    if (!state.startedAt) return;
    const seconds = Math.floor((Date.now() - state.startedAt) / 1000);
    el('run-elapsed').textContent = `已运行 ${Math.floor(seconds / 60)}分${String(seconds % 60).padStart(2, '0')}秒`;
  }, 1000);
}

function appendLine(text, className) {
  const box = el('log-lines');
  const line = document.createElement('div');
  line.className = 'log-line';
  const span = document.createElement('span');
  if (className) span.className = className;
  span.textContent = text;
  line.appendChild(span);
  const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.appendChild(line);
  if (stick) box.scrollTop = box.scrollHeight;
}

function phaseText(event) {
  const mark = event.status === 'running' ? '▸' : event.status === 'ok' ? '✓' : '✗';
  const ms = event.ms != null ? ` ${(event.ms / 1000).toFixed(1)}s` : '';
  return `${mark} ${event.phase} ${event.index}/${event.total}: ${event.node}${ms}`;
}

function watch(runId) {
  if (state.eventSource) state.eventSource.close();
  const source = new EventSource(`/api/runs/${runId}/events?from=0`);
  state.eventSource = source;

  source.addEventListener('state', (message) => {
    const data = JSON.parse(message.data);
    if (data.note) appendLine('· ' + data.note, 'run');
    if (data.status === 'queued') appendLine('· 已排队，等待前面的任务结束', 'run');
  });

  source.addEventListener('step', (message) => {
    const data = JSON.parse(message.data);
    if (data.status === 'running') appendLine(`● ${data.label}`, 'ctx');
    else if (data.status === 'ok') appendLine(`  ✓ ${data.label} 完成`, 'ok');
    else appendLine(`  ✗ ${data.label} 失败`, 'fail');
  });

  source.addEventListener('context', (message) => {
    const data = JSON.parse(message.data);
    if (data.kind === 'project') return;
    const text =
      data.kind === 'case'
        ? `[用例 ${data.index}/${data.total}] ${data.label}`
        : `[文件 ${data.index}/${data.total}] ${data.label}`;
    appendLine(text, 'ctx');
  });

  source.addEventListener('phase', (message) => {
    const data = JSON.parse(message.data);
    const key = `${data.phase}-${data.index}-${data.node}`;
    const indent = data.phase === 'step' ? 2 : 1;
    const text = '  '.repeat(indent) + phaseText(data);
    if (state.phaseLines.has(key)) {
      const node = state.phaseLines.get(key);
      node.textContent = text;
      node.className = `indent${indent} ${data.status === 'ok' ? 'ok' : data.status === 'fail' ? 'fail' : ''}`;
      return;
    }
    const box = el('log-lines');
    const line = document.createElement('div');
    line.className = 'log-line';
    const span = document.createElement('span');
    span.className = `indent${indent}`;
    span.textContent = text;
    line.appendChild(span);
    const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    box.appendChild(line);
    state.phaseLines.set(key, span);
    if (stick) box.scrollTop = box.scrollHeight;
  });

  source.addEventListener('run', (message) => {
    const data = JSON.parse(message.data);
    appendLine(`· 产生运行 ${data.run_id} status=${data.status || 'unknown'}`, 'run');
  });

  source.addEventListener('done', (message) => finish(JSON.parse(message.data)));

  source.addEventListener('error', () => {
    if (!state.running) source.close();
  });
}

async function finish(done) {
  state.running = false;
  clearInterval(state.timer);
  state.startedAt = null;
  el('stop').disabled = true;
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }

  const statusText = done.status === 'passed' ? '通过' : done.status === 'cancelled' ? '已取消' : '失败';
  const className = done.status === 'passed' ? 'ok' : done.status === 'cancelled' ? 'run' : 'fail';
  appendLine(`● 任务结束：${statusText}（exit=${done.exit_code ?? '-'}）`, className);

  const runs = done.runs || [];
  el('result-panel').hidden = false;
  el('result-title').textContent = `本次运行结果（${runs.length} 条运行记录）`;
  const rows = el('result-rows');
  rows.innerHTML = '';
  if (!runs.length) {
    rows.innerHTML = '<div class="result-row"><span class="hint">本次没有产生运行记录（可能启动前就失败了）</span></div>';
  }

  for (const run of runs) {
    const row = document.createElement('div');
    row.className = 'result-row';
    row.innerHTML =
      (run.status === 'success'
        ? '<span class="msg ok">✓ 通过</span>'
        : run.status === 'cancelled'
          ? '<span class="msg">已取消</span>'
          : '<span class="msg err">✗ 失败</span>') +
      `<span class="file">${run.run_id}</span>` +
      `<span>${run.total != null ? `总 ${run.total} / 通过 ${run.passed} / 失败 ${run.failed}` : ''}</span>` +
      '<span style="flex:1"></span><a class="btn-mini" target="_blank">查看报告</a>';
    rows.appendChild(row);
    api(`/api/runs/${run.run_id}`)
      .then((detail) => {
        const link = row.querySelector('a');
        if (detail.report_url) link.href = detail.report_url;
        else link.textContent = '无报告';
        if (detail.cases && detail.cases.length === 1 && detail.cases[0].error_code) {
          const info = document.createElement('span');
          info.className = 'msg err';
          info.textContent = `${detail.cases[0].failed_node || ''} ${detail.cases[0].error_code}`;
          row.insertBefore(info, link);
        }
      })
      .catch(() => {
        row.querySelector('a').textContent = '无报告';
      });
  }

  await load();
}

async function stopRun() {
  if (!state.jobId) return;
  el('stop').disabled = true;
  try {
    const result = await api(`/api/runs/${state.jobId}/cancel`, { method: 'POST' });
    appendLine(`· 已请求停止（原状态 ${result.was}）`, 'run');
  } catch (error) {
    alert('停止失败：' + error.message);
    el('stop').disabled = false;
  }
}

// ------------------------------------------------------------------ 弹窗

function openModal(html, narrow) {
  const mask = el('modal-mask');
  mask.hidden = false;
  mask.innerHTML = `<div class="modal${narrow ? ' narrow' : ''}">${html}</div>`;
  mask.addEventListener('click', (event) => {
    if (event.target === mask) closeModal();
  });
  return mask.querySelector('.modal');
}

function closeModal() {
  const mask = el('modal-mask');
  mask.hidden = true;
  mask.innerHTML = '';
}

function confirmBox(title, message, onConfirm, danger) {
  const modal = openModal(
    `<div class="modal-head"><h3>${title}</h3></div>
     <div class="modal-body">${message}</div>
     <div class="modal-foot"><span class="spacer"></span>
       <button class="btn ghost" data-cancel>取消</button>
       <button class="btn${danger ? ' danger' : ''}" data-ok>确定</button>
     </div>`,
    true,
  );
  modal.querySelector('[data-cancel]').addEventListener('click', closeModal);
  modal.querySelector('[data-ok]').addEventListener('click', () => {
    closeModal();
    onConfirm();
  });
}

// ------------------------------------------------------------------ 编辑器

async function openEditor(path) {
  const file = await api(`/api/cases/file?path=${encodeURIComponent(path)}`);
  const modal = openModal(
    `<div class="modal-head">
       <h3>${path.split('/').pop()}</h3>
       <span class="spacer"></span>
       <span class="hint">${path}</span>
       <button class="btn-mini" data-close>关闭</button>
     </div>
     <div class="modal-body">
       <div class="editor-wrap">
         <div class="gutter" id="gutter"></div>
         <textarea id="editor" spellcheck="false"></textarea>
       </div>
     </div>
     <div class="modal-foot">
       <span class="msg" id="edit-msg"></span>
       <span class="spacer"></span>
       <button class="btn ghost" data-validate>校验</button>
       <button class="btn" data-save>保存</button>
     </div>`,
  );

  const textarea = modal.querySelector('#editor');
  const gutter = modal.querySelector('#gutter');
  const msg = modal.querySelector('#edit-msg');
  let currentHash = file.hash;

  const refreshGutter = () => {
    const count = textarea.value.split('\n').length;
    gutter.innerHTML = Array.from({ length: count }, (_, i) => i + 1).join('<br />');
    gutter.scrollTop = textarea.scrollTop;
  };

  textarea.value = file.content;
  refreshGutter();
  textarea.addEventListener('input', refreshGutter);
  textarea.addEventListener('scroll', () => {
    gutter.scrollTop = textarea.scrollTop;
  });
  textarea.addEventListener('keydown', (event) => {
    if (event.key === 'Tab') {
      event.preventDefault();
      const start = textarea.selectionStart;
      textarea.value = textarea.value.slice(0, start) + '  ' + textarea.value.slice(textarea.selectionEnd);
      textarea.selectionStart = textarea.selectionEnd = start + 2;
      refreshGutter();
    }
  });

  const showMessage = (text, ok) => {
    msg.textContent = text;
    msg.className = 'msg ' + (ok ? 'ok' : 'err');
  };

  modal.querySelector('[data-close]').addEventListener('click', closeModal);

  modal.querySelector('[data-validate]').addEventListener('click', async () => {
    const result = await api('/api/cases/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: textarea.value }),
    });
    showMessage(result.message, result.ok);
  });

  modal.querySelector('[data-save]').addEventListener('click', async () => {
    try {
      const result = await api('/api/cases/file', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, content: textarea.value, expected_hash: currentHash }),
      });
      currentHash = result.hash;
      showMessage(`已保存（备份：${result.backup}）`, true);
      await load();
    } catch (error) {
      if (error.status === 409) {
        confirmBox(
          '文件已被外部修改',
          '这个文件在你编辑期间被别处改过了。<br />强制保存会覆盖外部改动，确定吗？',
          async () => {
            try {
              const forced = await api('/api/cases/file', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path, content: textarea.value }),
              });
              currentHash = forced.hash;
              showMessage(`已强制保存（备份：${forced.backup}）`, true);
              await load();
            } catch (inner) {
              showMessage(inner.message, false);
            }
          },
          true,
        );
      } else {
        showMessage(error.message, false);
      }
    }
  });
}

// ------------------------------------------------------------------ 新增 / 删除

function openNewGame(channel) {
  const info = state.tree.find((item) => item.channel === channel);
  const label = info ? info.label : channel;
  const modal = openModal(
    `<div class="modal-head"><h3>在「${label}」下新建游戏</h3></div>
     <div class="modal-body">
       <div class="field"><label>游戏目录名（决定 games/${channel}/&lt;这里&gt;）</label>
         <input id="ng-dir" placeholder="pop-2048" />
         <span class="hint">只能用小写字母、数字、连字符，例如 pop-2048</span>
       </div>
       <div class="field"><label>游戏中文名</label>
         <input id="ng-name" placeholder="泡泡2048" />
         <span class="hint">写进 game.config.ts 的 name，同时用于左侧树显示</span>
       </div>
       <div class="field"><label>搜索关键词（留空则用中文名）</label>
         <input id="ng-keyword" placeholder="泡泡2048" />
       </div>
       <div class="field"><label>主菜单判断（menuPrompt，必填）</label>
         <textarea id="ng-menu" rows="3" placeholder="屏幕上能看到&quot;新游戏&quot;和&quot;继续游戏&quot;这两个菜单文字"></textarea>
         <span class="hint">描述“看到什么就算进入了游戏主菜单”，尽量写界面上的具体文字或按钮；跑用例时用它判断是否进入成功</span>
       </div>
       <div class="msg" id="ng-msg"></div>
     </div>
     <div class="modal-foot"><span class="spacer"></span>
       <button class="btn ghost" data-cancel>取消</button>
       <button class="btn" data-ok>创建</button>
     </div>`,
    true,
  );

  const msg = modal.querySelector('#ng-msg');
  const nameInput = modal.querySelector('#ng-name');
  const keywordInput = modal.querySelector('#ng-keyword');
  // 中文名填了就把关键词同步过去（用户改过关键词后就不再自动覆盖）
  let keywordTouched = false;
  keywordInput.addEventListener('input', () => {
    keywordTouched = Boolean(keywordInput.value.trim());
  });
  nameInput.addEventListener('input', () => {
    if (!keywordTouched) keywordInput.value = nameInput.value.trim();
  });

  modal.querySelector('[data-cancel]').addEventListener('click', closeModal);
  modal.querySelector('[data-ok]').addEventListener('click', async () => {
    const dir = modal.querySelector('#ng-dir').value.trim();
    const name = nameInput.value.trim();
    const menuPrompt = modal.querySelector('#ng-menu').value.trim();
    if (!menuPrompt) {
      msg.textContent = '主菜单判断不能为空：它是运行时判断“是否进了游戏”的依据';
      msg.className = 'msg err';
      return;
    }
    try {
      const result = await api('/api/games', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          channel,
          dir,
          name,
          keyword: keywordInput.value.trim() || null,
          menu_prompt: menuPrompt,
        }),
      });
      closeModal();
      state.activeGame = result.game_key;
      await load();
      confirmBox(
        '游戏已创建',
        `已生成：<br />· <code>${result.path}</code><br />· <code>${result.explore_case}</code><br /><br />` +
          '建议先点「运行整个探索用例」验证能否进入。<strong>第一次大概率失败</strong>，' +
          '失败说明 menuPrompt 与实际界面不符，按报错内容改 game.config.ts 里那一行再跑。',
        () => {},
      );
    } catch (error) {
      msg.textContent = error.message;
      msg.className = 'msg err';
    }
  });
}

function openNewCase(suite, gameKey) {
  const games = gamesFromItems();
  const options = games
    .map(
      (game) =>
        `<option value="${game.game_key}"${game.game_key === gameKey ? ' selected' : ''}>${game.channel_label} · ${game.game_name}</option>`,
    )
    .join('');
  const modal = openModal(
    `<div class="modal-head"><h3>新增用例</h3></div>
     <div class="modal-body">
       <div class="field"><label>所属游戏</label><select id="new-game">${options}</select></div>
       <div class="field"><label>所属套件</label>
         <select id="new-suite">
           <option value="smoke"${suite === 'smoke' ? ' selected' : ''}>smoke（冒烟）</option>
           <option value="explore"${suite === 'explore' ? ' selected' : ''}>explore（探索）</option>
         </select>
       </div>
       <div class="field"><label>文件名（不含 .yaml）</label>
         <input id="new-name" placeholder="05-newbie-guide" />
         <span class="hint">只能用小写字母、数字、连字符；建议「序号-功能」</span>
       </div>
       <div class="field"><label>用例名（name 字段）</label>
         <input id="new-case-name" placeholder="新手引导-首次进入可完整走完引导" />
         <span class="hint">建议「功能-可验证的现象」</span>
       </div>
       <div class="msg" id="new-msg"></div>
     </div>
     <div class="modal-foot"><span class="spacer"></span>
       <button class="btn ghost" data-cancel>取消</button>
       <button class="btn" data-ok>创建</button>
     </div>`,
    true,
  );
  const msg = modal.querySelector('#new-msg');
  modal.querySelector('[data-cancel]').addEventListener('click', closeModal);
  modal.querySelector('[data-ok]').addEventListener('click', async () => {
    try {
      const result = await api('/api/cases', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          game_key: modal.querySelector('#new-game').value,
          suite: modal.querySelector('#new-suite').value,
          name: modal.querySelector('#new-name').value.trim(),
          case_name: modal.querySelector('#new-case-name').value.trim() || '新用例-待填写',
        }),
      });
      closeModal();
      if (result.warnings && result.warnings.length) {
        alert('已创建：' + result.path + '\n\n提示：\n' + result.warnings.join('\n'));
      }
      await load();
      openEditor(result.path);
    } catch (error) {
      msg.textContent = error.message;
      msg.className = 'msg err';
    }
  });
}

function confirmDelete(item) {
  confirmBox(
    '删除用例',
    `确定删除 <strong>${item.path.split('/').pop()}</strong> 吗？<br /><br />` +
      '文件会先备份到 <code>.backup/cases/deleted/</code>，然后从磁盘删除。',
    async () => {
      try {
        const result = await api(`/api/cases/file?path=${encodeURIComponent(item.path)}`, {
          method: 'DELETE',
        });
        state.selected.delete(item.path);
        if (!state.selected.size) {
          state.selectedSuite = null;
          state.selectedGame = null;
        }
        await load();
        alert('已删除，备份在：' + result.backup);
      } catch (error) {
        alert('删除失败：' + error.message);
      }
    },
    true,
  );
}

// ------------------------------------------------------------------ 事件绑定

document.addEventListener('click', async (event) => {
  const target = event.target.closest('button, .side-item');
  if (!target) return;

  if (target.id === 'refresh') {
    await load();
    return;
  }

  // 渠道行右侧的"+ 新建游戏"
  const newGameChannel = target.dataset.newGame;
  if (newGameChannel) {
    openNewGame(newGameChannel);
    return;
  }
  if (target.id === 'stop') {
    confirmBox(
      '确认停止这次运行？',
      `当前已运行 ${el('run-elapsed').textContent || ''}。<br /><br />` +
        '停止会连 codely 子进程一起杀掉，已跑完的用例仍会保留记录（标记为"已取消"）。',
      stopRun,
      true,
    );
    return;
  }

  // 左侧树：切换当前游戏
  if (target.classList.contains('side-item')) {
    state.activeGame = target.dataset.game || null;
    state.selected.clear();
    state.selectedSuite = null;
    state.selectedGame = null;
    render();
    return;
  }

  const toggle = target.dataset.toggle;
  if (toggle) {
    state.collapsed[toggle] = !(state.collapsed[toggle] ?? true);
    render();
    return;
  }

  const newSuite = target.dataset.new;
  if (newSuite) {
    openNewCase(newSuite, target.dataset.game);
    return;
  }

  const game = target.dataset.game;
  const selectAll = target.dataset.selectAll;
  if (selectAll) {
    const items = state.items.filter((item) => item.suite === selectAll && item.game_key === game);
    const allSelected = items.every((item) => state.selected.has(item.path));
    state.selected.clear();
    if (allSelected) {
      state.selectedSuite = null;
      state.selectedGame = null;
    } else {
      state.selectedSuite = selectAll;
      state.selectedGame = game;
      for (const item of items) state.selected.add(item.path);
    }
    render();
    return;
  }

  const runSuite = target.dataset.runSuite;
  if (runSuite) {
    const items = state.items.filter((item) => item.suite === runSuite && item.game_key === game);
    if (!items.length || !state.activeGame) return;
    const gameInfo = gamesFromItems().find((entry) => entry.game_key === game);
    confirmBox(
      '运行整个' + SUITE_LABELS[runSuite],
      `将运行「${gameInfo ? gameInfo.game_name : game}」的 ${items.length} 条用例，` +
        '一个进程跑完（会话可复用）。' +
        (runSuite === 'explore' ? '<br /><br />探索集耗时约 30-40 分钟，确定开始吗？' : ''),
      () =>
        startRun(
          { project: runSuite, files: [], clean: true, game_key: game },
          `${gameInfo ? gameInfo.game_name : game} · 整个${SUITE_LABELS[runSuite]}`,
        ),
    );
    return;
  }

  const runSelected = target.dataset.runSelected;
  if (runSelected) {
    const files = [...state.selected];
    if (!files.length) return;
    const total = state.items.filter(
      (item) => item.suite === runSelected && item.game_key === state.selectedGame,
    ).length;
    const gameInfo = gamesFromItems().find((entry) => entry.game_key === state.selectedGame);
    const title =
      files.length === total
        ? `${gameInfo ? gameInfo.game_name : ''} · 整个${SUITE_LABELS[runSelected]}（全选）`
        : `选中的 ${files.length} 条用例`;
    confirmBox('确认运行', `将运行 ${files.length} 条用例：<br />${files.map((f) => '· ' + f.split('/').pop()).join('<br />')}`, () =>
      startRun({ project: runSelected, files, clean: true, game_key: state.selectedGame }, title),
    );
  }
});

load().catch((error) => {
  el('suites').innerHTML = `<div class="suite-block" style="padding:18px"><span class="msg err">加载失败：${error.message}</span></div>`;
});