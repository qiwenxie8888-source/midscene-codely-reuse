import { fileURLToPath } from 'node:url';
import { config as loadEnv } from 'dotenv';
import type { NodeExecutionContext } from '@midscene/test';
import { defineProjectSetup, defineTestProject } from '@midscene/test/config';
import { createMidsceneNodes } from '@midscene/test/midscene';
import { AndroidAgent, agentFromAdbDevice } from '@midscene/android';
import { gameEnter, gameEnsure } from './scripts/game-enter-node';
import type { ProjectContext } from './scripts/context';

loadEnv({ path: fileURLToPath(new URL('.env', import.meta.url)) });

const getAgent = ({ context }: NodeExecutionContext<unknown, ProjectContext>) => {
  return context.agent;
};

const setup = defineProjectSetup<ProjectContext>({
  name: 'android',
  async setup({ env, onTeardown }) {
    const agent = await agentFromAdbDevice(env.ANDROID_DEVICE_ID || undefined);
    onTeardown(() => agent.destroy());
    return { agent };
  },
});

// 用例目录约定：games/<渠道>/<游戏>/{smoke,explore}/*.yaml
export default defineTestProject<ProjectContext>({
  projects: [
    {
      // 冒烟集：每次版本必跑（默认项目）
      name: 'smoke',
      setup,
      files: { include: ['games/**/smoke/**/*.{yaml,yml}'] },
    },
    {
      // 探索集：新游戏首测时手动跑，不进默认冒烟
      name: 'explore',
      setup,
      files: { include: ['games/**/explore/**/*.{yaml,yml}'] },
    },
  ],
  nodes: [
    ...createMidsceneNodes<ProjectContext>({ agentClass: AndroidAgent, getAgent }),
    gameEnter,
    gameEnsure,
  ],
  // Codely ACP 为每次模型调用新起一个 codely 进程，首个 AI 调用实测可达 70~170s，
  // 默认 120s 的单步超时会把 game.enter 直接打断。
  test: { testTimeout: 600_000 },
});
