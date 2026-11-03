// 清理 Midscene Codely provider 遗留的孤儿进程和临时工作区。
//
// 背景：每个游戏分组的 ACP 会话在 scope.close() 里主动 SIGTERM/SIGKILL，
// 但如果跑测试的 node 进程被强杀（连按两次 Ctrl+C、关终端、任务管理器结束、
// OOM），finally 不会执行，Windows 上子进程又不会随父进程退出，于是留下孤儿。
//
// 安全边界：只匹配命令行里带 --allowed-mcp-server-names=__midscene_no_mcp__
// 的 codely 进程（这是 provider 独有的参数），不会误杀日常使用的 Codely CLI。
//
// 用法：
//   node scripts/cleanup-codely.mjs            # 清理
//   node scripts/cleanup-codely.mjs --dry-run  # 只报告不杀
import { spawnSync } from 'node:child_process';
import { readdirSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const MARKER = '__midscene_no_mcp__';
const WORKSPACE_PREFIX = 'midscene-codely-';
const isWindows = process.platform === 'win32';
const dryRun = process.argv.includes('--dry-run');

function listOrphanProcesses() {
  if (isWindows) {
    const script = [
      `Get-CimInstance Win32_Process -Filter "Name='codely.exe'"`,
      `| Where-Object { $_.CommandLine -like '*${MARKER}*' }`,
      '| Select-Object -ExpandProperty ProcessId',
    ].join(' ');
    const result = spawnSync(
      'powershell',
      ['-NoProfile', '-NonInteractive', '-Command', script],
      { encoding: 'utf8' },
    );
    return (result.stdout ?? '')
      .split(/\r?\n/)
      .map((line) => Number.parseInt(line.trim(), 10))
      .filter((pid) => Number.isInteger(pid) && pid > 0);
  }
  const result = spawnSync('ps', ['-eo', 'pid=,args='], { encoding: 'utf8' });
  return (result.stdout ?? '')
    .split('\n')
    .filter((line) => line.includes(MARKER))
    .map((line) => Number.parseInt(line.trim().split(/\s+/)[0], 10))
    .filter((pid) => Number.isInteger(pid) && pid > 0);
}

function listWorkspaceDirs() {
  const root = tmpdir();
  return readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .filter((entry) => entry.name.startsWith(WORKSPACE_PREFIX))
    .map((entry) => path.join(root, entry.name));
}

function killProcess(pid) {
  if (isWindows) {
    spawnSync('taskkill', ['/PID', String(pid), '/F'], { stdio: 'ignore' });
    return;
  }
  spawnSync('kill', ['-9', String(pid)], { stdio: 'ignore' });
}

// 正在运行的会话会频繁写工作区，只清掉超过 10 分钟没动过的目录。
const STALE_WORKSPACE_MS = 10 * 60 * 1000;

const pids = listOrphanProcesses();
const workspaces = listWorkspaceDirs().filter((dir) => {
  try {
    return Date.now() - statSync(dir).mtimeMs > STALE_WORKSPACE_MS;
  } catch {
    return false;
  }
});

if (pids.length === 0 && workspaces.length === 0) {
  console.log('codely 孤儿清理：无残留');
  process.exit(0);
}

const prefix = dryRun ? '[dry-run] 将清理' : '清理';
for (const pid of pids) {
  console.log(`${prefix} 进程 PID=${pid}`);
  if (!dryRun) killProcess(pid);
}
for (const dir of workspaces) {
  console.log(`${prefix} 临时目录 ${dir}`);
  if (!dryRun) rmSync(dir, { recursive: true, force: true });
}
console.log(
  `codely 孤儿清理：进程 ${pids.length} 个，临时目录 ${workspaces.length} 个${dryRun ? '（未实际执行）' : ''}`,
);