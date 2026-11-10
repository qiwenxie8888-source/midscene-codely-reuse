// 打印某个渠道适配器的信息，供 Python 后端生成 game.config.ts 时使用。
// 为什么不在 Python 里重写一遍模板：渠道相关的描述必须只有一份真身，
// 否则以后接微信时会出现"TS 里改了一套、Python 里还是抖音的卡片描述"。
//
// 用法：node scripts/platform-info.mjs <渠道key> [游戏名]
// 输出：{"ok":true,"label":"抖音小游戏","cardPrompt":"..."}
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';

const [channel, gameName = ''] = process.argv.slice(2);
const root = process.cwd();

async function main() {
  if (!channel) throw new Error('缺少渠道 key');
  const file = resolve(root, 'platforms', `${channel}.ts`);
  if (!existsSync(file)) throw new Error(`渠道适配器不存在：platforms/${channel}.ts`);
  const module = await import(`file://${file.replace(/\\/g, '/')}`);
  const adapter = module[`${channel}Adapter`] ?? Object.values(module)[0];
  if (!adapter || typeof adapter.cardPrompt !== 'function') {
    throw new Error(`platforms/${channel}.ts 没有导出可用的适配器（需要 cardPrompt 函数）`);
  }
  process.stdout.write(
    JSON.stringify({
      ok: true,
      label: adapter.label ?? channel,
      cardPrompt: adapter.cardPrompt(gameName),
    }),
  );
}

main().catch((error) => {
  process.stdout.write(JSON.stringify({ ok: false, message: error.message }));
  process.exitCode = 1;
});