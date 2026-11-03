// 一条命令跑完整流程：Midscene 测试 → 入库 → 清理散落产物
//
// 之所以用 Node 脚本而不是 package.json 里用 && 串命令：
//   1. 每步都要判断成败，测试失败时仍然要入库（失败数据更要分析）
//   2. Python 解释器在 Windows / Linux 下路径不同，需要自动探测
//
// 用法：
//   node scripts/run-full.mjs --project smoke
//   node scripts/run-full.mjs --project explore
//   node scripts/run-full.mjs --project smoke --skip-test     # 只跑入库+清理
//   node scripts/run-full.mjs --project smoke --no-clean      # 保留散落报告便于排查
//   node scripts/run-full.mjs --project smoke --files a.yaml,b.yaml   # 只跑指定文件（串行）
//   node scripts/run-full.mjs --project smoke --json          # 每次运行输出一行 JSON 事件，供服务端读日志
import { spawn, spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';

const args = process.argv.slice(2);
const has = (flag) => args.includes(flag);
const valueOf = (flag, fallback) => {
  const index = args.indexOf(flag);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};

const project = valueOf('--project', 'smoke');
const skipTest = has('--skip-test');
const noClean = has('--no-clean');
const jsonMode = has('--json');
// 一条用例一个 YAML 之后，"跑哪几条"就等于"跑哪几个文件"。
// midscene-test 一次只接受一个路径，所以这里串行逐个调用。
const files = valueOf('--files', '')
  .split(',')
  .map((item) => item.trim())
  .filter(Boolean);

const isWindows = process.platform === 'win32';
const projectRoot = process.cwd();

// json 模式下 stdout 只能有 JSON 事件，人看的文字一律走 stderr
const log = (message = '') => {
  if (jsonMode) {
    console.error(message);
  } else {
    console.log(message);
  }
};

const emit = (event) => {
  if (jsonMode) process.stdout.write(`${JSON.stringify(event)}\n`);
};

// 必须用绝对路径：Windows 的 cmd 不认 .venv/Scripts/python.exe 这种正斜杠相对路径
function findPython() {
  if (process.env.PYTHON) return process.env.PYTHON;
  const candidates = isWindows
    ? ['.venv/Scripts/python.exe', '.venv/Scripts/python']
    : ['.venv/bin/python', '.venv/bin/python3'];
  for (const candidate of candidates) {
    const absolute = resolve(projectRoot, candidate);
    if (existsSync(absolute)) return absolute;
  }
  return isWindows ? 'python' : 'python3';
}

// Node 在 Windows 上不允许直接 spawn .cmd（会报 EINVAL），
// 所以不走 pnpm，直接用 node 执行 midscene-test 的 bin 入口
const MIDSCENE_BIN = resolve(projectRoot, 'node_modules/@midscene/test/bin/midscene-test');
const CODELY_CLEANUP = resolve(projectRoot, 'scripts/cleanup-codely.mjs');
// 传单个 YAML 时 CLI 会把配置搜索目录当成该文件所在目录，必须显式指定配置
const CONFIG_PATH = resolve(projectRoot, 'midscene.config.ts');

const python = findPython();

// 交互模式：保持原有行为（输出直接继承终端，进度条/颜色都不受影响）
function runSync(label, command, commandArgs) {
  log('');
  log('=== ' + label + ' ===');
  log('$ ' + [command, ...commandArgs].join(' '));
  const result = spawnSync(command, commandArgs, { stdio: 'inherit' });
  if (result.error) {
    log('启动失败：' + result.error.message);
    return false;
  }
  return result.status === 0;
}

// json 模式：逐行转发子进程输出，服务端才能实时看到日志。
// spawnSync 会等进程结束才一次性拿到输出，所以这里必须用异步 spawn。
function runStreaming(label, command, commandArgs) {
  log('');
  log('=== ' + label + ' ===');
  log('$ ' + [command, ...commandArgs].join(' '));
  emit({ type: 'cmd', label, command, args: commandArgs });

  return new Promise((resolvePromise) => {
    const child = spawn(command, commandArgs, {
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    const forward = (name, stream) => {
      let buffer = '';
      stream.setEncoding('utf8');
      stream.on('data', (chunk) => {
        buffer += chunk;
        let index = buffer.indexOf('\n');
        while (index >= 0) {
          const line = buffer.slice(0, index).replace(/\r$/, '');
          buffer = buffer.slice(index + 1);
          emit({ type: 'log', stream: name, line });
          console.error(line);
          index = buffer.indexOf('\n');
        }
      });
      stream.on('end', () => {
        if (buffer.length > 0) {
          emit({ type: 'log', stream: name, line: buffer.replace(/\r$/, '') });
          console.error(buffer);
          buffer = '';
        }
      });
    };

    forward('stdout', child.stdout);
    forward('stderr', child.stderr);

    child.on('error', (error) => {
      emit({ type: 'error', message: error.message });
      resolvePromise(false);
    });
    child.on('close', (code, signal) => {
      resolvePromise(code === 0 ? true : false);
      if (signal) emit({ type: 'signal', signal });
    });
  });
}

async function step(label, command, commandArgs) {
  emit({ type: 'step', label, status: 'running' });
  const ok = jsonMode
    ? await runStreaming(label, command, commandArgs)
    : runSync(label, command, commandArgs);
  log(ok ? '--- ' + label + ' 完成' : '--- ' + label + ' 失败');
  emit({ type: 'step', label, status: ok ? 'ok' : 'fail' });
  return ok;
}

async function main() {
  let testOk = true;
  if (!skipTest) {
    // 上一次若被强杀，会留下孤儿 codely 进程，先清掉再开跑
    await step('1/4 清理遗留 codely 进程', process.execPath, [CODELY_CLEANUP]);

    if (files.length > 0) {
      for (let index = 0; index < files.length; index += 1) {
        const file = files[index];
        const label = `2/4 跑测试 ${index + 1}/${files.length}：${file}`;
        // 一个文件失败不影响后面的文件，最后统一看结果
        const ok = await step(label, process.execPath, [
          MIDSCENE_BIN,
          file,
          '--project',
          project,
          '--config',
          CONFIG_PATH,
        ]);
        testOk = testOk && ok;
      }
    } else {
      testOk = await step('2/4 跑测试（project=' + project + '）', process.execPath, [
        MIDSCENE_BIN,
        '--project',
        project,
      ]);
    }
  } else {
    log('跳过测试（--skip-test）');
  }

  // 测试失败也继续入库：失败运行恰恰是最需要被记录和分析的
  const ingestOk = await step('3/4 入库', python, [resolve(projectRoot, 'server/ingest.py')]);

  let cleanOk = true;
  if (!noClean) {
    cleanOk = await step('4/4 清理散落报告', python, [
      resolve(projectRoot, 'server/clean_runs.py'),
      '--purge-loose',
      '--yes',
    ]);
  } else {
    log('跳过清理（--no-clean），散落报告保留在 midscene_run/report/');
  }

  // 正常结束时本来就会关会话，这里兜底清理异常退出留下的孤儿
  if (!skipTest) {
    await step('收尾 清理 codely 残留', process.execPath, [CODELY_CLEANUP]);
  }

  log('');
  log('=== 结果 ===');
  log('测试: ' + (skipTest ? '已跳过' : testOk ? '通过' : '失败'));
  log('入库: ' + (ingestOk ? '成功' : '失败'));
  log('清理: ' + (noClean ? '已跳过' : cleanOk ? '成功' : '失败'));

  const exitCode = testOk && ingestOk && cleanOk ? 0 : 1;
  emit({ type: 'result', test: skipTest ? 'skipped' : testOk, ingest: ingestOk, clean: noClean ? 'skipped' : cleanOk, exitCode });

  // 让 CI / 人一眼看出这轮到底成功没有
  process.exitCode = exitCode;
}

await main();