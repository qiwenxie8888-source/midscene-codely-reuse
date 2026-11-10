// 由磁盘目录重新生成 scripts/games.ts（游戏注册表）。
//
// 为什么要生成而不是手写：
//   新建游戏时如果忘了登记，跑用例才会报"未注册的游戏"，而那时已经很难定位。
//   整体重算保证"目录里有什么，注册表里就有什么"，不会插错位置、也不会漏。
//
// 用法：
//   pnpm sync-games          # 重新生成（内容没变则不写盘）
//   pnpm sync-games --check  # 只检查是否最新，不写盘（供 CI 用）
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

const projectRoot = process.cwd();
const gamesRoot = resolve(projectRoot, 'games');
const target = resolve(projectRoot, 'scripts/games.ts');
const backupRoot = resolve(projectRoot, '.backup/games-ts');
const checkOnly = process.argv.includes('--check');

const timestamp = new Date()
  .toISOString()
  .replace(/[-:]/g, '')
  .replace(/\..+/, '')
  .replace('T', '-');

/** pop-2048 → pop2048；edge-space → edgeSpace */
function toIdentifier(channel, dir) {
  const camel = dir
    .split('-')
    .filter(Boolean)
    .map((part, index) => (index === 0 ? part : part.charAt(0).toUpperCase() + part.slice(1)))
    .join('');
  const safe = /^[0-9]/.test(camel) ? `${channel}${camel}` : camel;
  return safe;
}

function discoverGames() {
  if (!existsSync(gamesRoot)) return [];
  const games = [];
  for (const channel of readdirSync(gamesRoot, { withFileTypes: true })) {
    if (!channel.isDirectory()) continue;
    const channelDir = join(gamesRoot, channel.name);
    for (const game of readdirSync(channelDir, { withFileTypes: true })) {
      if (!game.isDirectory()) continue;
      const config = join(channelDir, game.name, 'game.config.ts');
      if (!existsSync(config)) continue;
      games.push({ channel: channel.name, dir: game.name, key: `${channel.name}/${game.name}` });
    }
  }
  return games.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
}

function render(games) {
  const used = new Map();
  const imports = [];
  const entries = [];

  for (const game of games) {
    let identifier = toIdentifier(game.channel, game.dir);
    // 不同渠道下的同名目录会撞标识符，加渠道前缀区分
    if (used.has(identifier)) identifier = `${game.channel}${identifier.charAt(0).toUpperCase()}${identifier.slice(1)}`;
    used.set(identifier, game.key);
    imports.push(`import ${identifier} from '../games/${game.channel}/${game.dir}/game.config';`);
    entries.push(`  [${identifier}.key]: ${identifier},`);
  }

  return [
    '// 此文件由 `pnpm sync-games` 根据 games/*/*/game.config.ts 自动生成，请勿手改。',
    '// 新增游戏后跑一次 `pnpm sync-games` 即可更新注册表。',
    ...(imports.length ? imports : []),
    "import type { GameConfig } from '../games/types';",
    '',
    'export const games: Readonly<Record<string, GameConfig>> = Object.freeze({',
    ...entries,
    '});',
    '',
    'export const gameKeys: readonly string[] = Object.keys(games);',
    '',
    'export function requireGame(gameKey: string): GameConfig {',
    '  const game = games[gameKey];',
    '  if (!game) {',
    '    throw new Error(',
    '      `未注册的游戏 "${gameKey}"；可用：${gameKeys.join(", ") || "（空）"}`',
    '    );',
    '  }',
    '  return game;',
    '}',
    '',
  ].join('\n');
}

const games = discoverGames();
const content = render(games);
const current = existsSync(target) ? readFileSync(target, 'utf8') : null;

if (current === content) {
  console.log(`已是最新，无需改动（共 ${games.length} 个游戏）`);
  process.exit(0);
}

if (checkOnly) {
  console.error(`scripts/games.ts 与 games/ 目录不一致，请运行 pnpm sync-games`);
  process.exit(1);
}

if (current !== null) {
  const backupDir = join(backupRoot, timestamp);
  mkdirSync(backupDir, { recursive: true });
  writeFileSync(join(backupDir, 'games.ts'), current, 'utf8');
}

writeFileSync(target, content, 'utf8');
console.log(`已更新 scripts/games.ts（共 ${games.length} 个游戏：${games.map((g) => g.key).join(', ') || '无'}）`);