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
import { spawnSync } from 'node:child_process';
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

const isWindows = process.platform === 'win32';
const projectRoot = process.cwd();

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

const python = findPython();

function run(label, command, commandArgs) {
  console.log('');
  console.log('=== ' + label + ' ===');
  console.log('$ ' + [command, ...commandArgs].join(' '));
  const result = spawnSync(command, commandArgs, { stdio: 'inherit' });
  if (result.error) {
    console.error('启动失败：' + result.error.message);
    return false;
  }
  return result.status === 0;
}

function step(label, command, commandArgs) {
  const ok = run(label, command, commandArgs);
  console.log(ok ? '--- ' + label + ' 完成' : '--- ' + label + ' 失败');
  return ok;
}

let testOk = true;
if (!skipTest) {
  // 上一次若被强杀，会留下孤儿 codely 进程，先清掉再开跑
  step('1/4 清理遗留 codely 进程', process.execPath, [CODELY_CLEANUP]);
  testOk = step('2/4 跑测试（project=' + project + '）', process.execPath, [MIDSCENE_BIN, '--project', project]);
} else {
  console.log('跳过测试（--skip-test）');
}

// 测试失败也继续入库：失败运行恰恰是最需要被记录和分析的
const ingestOk = step('3/4 入库', python, [resolve(projectRoot, 'server/ingest.py')]);

let cleanOk = true;
if (!noClean) {
  cleanOk = step('4/4 清理散落报告', python, [
    resolve(projectRoot, 'server/clean_runs.py'),
    '--purge-loose',
    '--yes',
  ]);
} else {
  console.log('跳过清理（--no-clean），散落报告保留在 midscene_run/report/');
}

// 正常结束时本来就会关会话，这里兜底清理异常退出留下的孤儿
if (!skipTest) {
  step('收尾 清理 codely 残留', process.execPath, [CODELY_CLEANUP]);
}

console.log('');
console.log('=== 结果 ===');
console.log('测试: ' + (skipTest ? '已跳过' : testOk ? '通过' : '失败'));
console.log('入库: ' + (ingestOk ? '成功' : '失败'));
console.log('清理: ' + (noClean ? '已跳过' : cleanOk ? '成功' : '失败'));

// 让 CI / 人一眼看出这轮到底成功没有
process.exitCode = testOk && ingestOk && cleanOk ? 0 : 1;
