// 自定义 Node：game.enter / game.ensure
// game.enter：把"进入小游戏"的固定流程封装成 YAML 可调用的一步：
//   注入游戏上下文 → 冷启动宿主 → 深链搜索（游戏名+小游戏入口）
//   → AI 视觉定位点击游戏卡片 → 等待游戏主菜单
// game.ensure：先判断"是不是已经在主菜单"，在就直接跳过，不在才走上面那套。
//   用于"一条用例一个文件"后，每条用例开头的低成本状态复位。
// 进入链参数统一来自 games/<渠道>/<游戏>/game.config.ts，YAML 只传 gameKey 与用例集。
import { defineNode, z } from '@midscene/test';
import type { NodeExecutionContext } from '@midscene/test';
import { execSync } from 'node:child_process';
import { platformAdapters } from '../platforms';
import type { PlatformAdapter } from '../platforms/types';
import type { GameConfig, Suite } from '../games/types';
import { requireGame } from './games';
import type { ProjectContext } from './context';

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const ADB = process.env.ANDROID_DEVICE_ID
  ? `adb -s ${process.env.ANDROID_DEVICE_ID}`
  : 'adb';

type Agent = NodeExecutionContext<unknown, ProjectContext>['context']['agent'];

/**
 * 引擎只给内置节点（aiTap/aiAct/…）注册报告快照，defineNode 写的自定义节点默认
 * 什么都不上报，于是报告里这条用例会显示 "No visual evidence for this attempt"。
 * 这里补上同样两件事：把本次步骤的 executionId 记成 trace，并注册快照回调。
 * 返回的 detach 必须在 execute 退出前调用——collector 关闭后再收到 dump 更新会
 * 因 addTrace 抛错。
 */
interface ReportCapableAgent {
  addDumpUpdateListener?: (
    listener: (dump: unknown, execution?: { id?: string }) => void
  ) => (() => void) | void;
  _createReportSource?: (
    scopeId: string,
    executionIds?: readonly string[]
  ) => Promise<{ sourcePath: string; dumpPath?: string } | undefined>;
}

function attachStepReport(
  ctx: NodeExecutionContext<unknown, ProjectContext>,
  agent: Agent
): () => void {
  const capable = agent as ReportCapableAgent;
  const scopeId =
    ctx.scope === 'case' ? ctx.case.runId : ctx.document.documentRunId;
  const executionIds = new Set<string>();

  const remove = capable.addDumpUpdateListener?.((_dump, execution) => {
    const id = execution?.id;
    if (!id || executionIds.has(id)) return;
    executionIds.add(id);
    try {
      ctx.report.addTrace({ type: 'midscene-execution', executionId: id });
    } catch {
      // 步骤已结束（collector 关闭）时忽略，不影响后续调用
    }
  });

  ctx.report.onSnapshot(async (signal, traces) => {
    signal.throwIfAborted();
    const ids = traces.length
      ? traces.map((trace) => trace.executionId)
      : [...executionIds];
    // 空数组的含义是"一个都不要"，只有传 undefined 才会取整个会话的快照
    const source = await capable._createReportSource?.(
      scopeId,
      ids.length ? ids : undefined
    );
    if (!source) throw new Error('agent 没有返回报告快照');
    return source;
  });

  return () => {
    if (typeof remove === 'function') remove();
  };
}

interface ResolvedGame {
  game: GameConfig;
  adapter: PlatformAdapter;
  hostPackage: string;
}

function resolveGame(
  gameKey: string,
  profile: Suite,
  hostPackageOverride?: string
): ResolvedGame {
  const game = requireGame(gameKey);
  const adapter = platformAdapters[game.channel];
  if (!adapter) {
    throw new Error(
      `渠道 "${game.channel}" 尚未实现进入适配器，请在 platforms/index.ts 登记`
    );
  }
  const hostPackage =
    hostPackageOverride ?? game.hostPackage ?? adapter.defaultHostPackage;
  if (!hostPackage) {
    throw new Error(`游戏 "${game.key}" 未配置宿主包名`);
  }
  const aiContext = [game.aiContext, game.suiteContext?.[profile]]
    .filter(Boolean)
    .join('\n');
  return { game, adapter, hostPackage };
}

function applyAIContext(agent: Agent, game: GameConfig, profile: Suite) {
  const aiContext = [game.aiContext, game.suiteContext?.[profile]]
    .filter(Boolean)
    .join('\n');
  if (aiContext) agent.setAIContext('default', aiContext);
}

/**
 * 当前前台应用是不是宿主包（纯 adb，不消耗模型调用）。
 * 返回 undefined 表示查不出来（命令失败/输出为空），调用方应继续走视觉判断。
 */
function hostInForeground(hostPackage: string): boolean | undefined {
  try {
    const dump = execSync(
      `${ADB} shell "dumpsys activity activities | grep -E 'mResumedActivity|topResumedActivity'"`,
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }
    );
    if (!dump.trim()) return undefined;
    return dump.includes(hostPackage);
  } catch {
    return undefined;
  }
}

interface EnterChainOptions {
  warmupMs?: number;
  pageWaitMs?: number;
  waitTimeoutMs?: number;
}

/** 完整的进入链：唤醒 → 冷启动宿主 → 深链搜索 → 点卡片 → 关订阅弹窗 → 等主菜单 */
async function runEnterChain(
  agent: Agent,
  { game, adapter, hostPackage }: ResolvedGame,
  options: EnterChainOptions = {}
) {
  const keyword = adapter.searchKeyword(game.enter.keyword ?? game.name);
  const deepLink = adapter.searchDeepLink(keyword);
  const warmupMs = options.warmupMs ?? game.enter.warmupMs ?? 10000;
  const pageWaitMs = options.pageWaitMs ?? game.enter.pageWaitMs ?? 6000;
  const waitTimeoutMs =
    options.waitTimeoutMs ?? game.enter.waitTimeoutMs ?? 150000;

  const openSearch = async () => {
    execSync(
      `${ADB} shell am start -a android.intent.action.VIEW -d "${deepLink}" ${hostPackage}`,
      { stdio: 'ignore' }
    );
    await sleep(pageWaitMs);
  };

  // 0) 唤醒并解除锁屏：熄屏时截图全黑，视觉定位必然失败
  //    注意：dismiss-keyguard 只能解无密码锁屏；设了 PIN/图案锁需另做处理
  execSync(`${ADB} shell input keyevent KEYCODE_WAKEUP`, { stdio: 'ignore' });
  execSync(`${ADB} shell wm dismiss-keyguard`, { stdio: 'ignore' });
  await sleep(1000);

  // 1) 冷启动宿主，保证起点状态确定
  execSync(`${ADB} shell am force-stop ${hostPackage}`, { stdio: 'ignore' });
  execSync(
    `${ADB} shell monkey -p ${hostPackage} -c android.intent.category.LAUNCHER 1`,
    { stdio: 'ignore' }
  );
  await sleep(warmupMs);

  // 2) 深链直达搜索结果页
  await openSearch();

  // 3) AI 视觉定位点击游戏卡片；找不到时返回并重搜一次
  try {
    await agent.aiTap(game.enter.cardPrompt);
  } catch {
    execSync(`${ADB} shell input keyevent 4`, { stdio: 'ignore' });
    await sleep(2000);
    await openSearch();
    await agent.aiTap(game.enter.cardPrompt);
  }

  // 4) 抖音首次拉起小游戏会弹订阅提醒，先关掉再等主菜单
  try {
    await agent.aiTap(
      '抖音的订阅提醒弹窗（含"当「可继续游戏」时，是否在首页视频流提醒你"字样）中的"拒绝"按钮'
    );
  } catch {
    // 没有弹窗时 aiTap 找不到目标会抛错，忽略即可
  }

  // 5) 等待游戏主菜单出现（视觉关卡）；漏网的弹窗给一次自愈机会
  try {
    await agent.aiWaitFor(game.enter.menuPrompt, {
      timeoutMs: Math.min(waitTimeoutMs, 45000),
    });
  } catch {
    await agent.aiAct(
      '如果屏幕上有弹窗遮挡，点击"拒绝""关闭"或"允许"把它关掉；没有弹窗就不要操作'
    );
    await agent.aiWaitFor(game.enter.menuPrompt, {
      timeoutMs: 60000,
    });
  }

  return {
    summary: `已进入《${game.name}》`,
    data: { gameKey: game.key, keyword, deepLink, hostPackage },
  };
}

const gameEnterInputSchema = z.strictObject({
  gameKey: z
    .string()
    .describe('game.config.ts 中的游戏 key，如 douyin/edge-space'),
  profile: z
    .enum(['smoke', 'explore'])
    .default('smoke')
    .describe('用例集类型，决定叠加哪份 AI 上下文'),
  hostPackage: z.string().optional().describe('覆盖渠道默认的宿主包名'),
  warmupMs: z.number().optional().describe('覆盖冷启动后等待开屏页的毫秒数'),
  pageWaitMs: z.number().optional().describe('覆盖深链跳转后等待渲染的毫秒数'),
  waitTimeoutMs: z.number().optional().describe('覆盖等待主菜单的超时毫秒数'),
});

type GameEnterInput = z.infer<typeof gameEnterInputSchema>;

export const gameEnter = defineNode({
  name: 'game.enter',
  description:
    '按 gameKey 查游戏配置 → 注入 AI 上下文 → 冷启动宿主 → 深链搜索 → AI 点击游戏卡片 → 等待主菜单',
  inputSchema: gameEnterInputSchema,
  async execute(ctx: NodeExecutionContext<GameEnterInput, ProjectContext>) {
    const { input, context } = ctx;
    const resolved = resolveGame(
      input.gameKey,
      input.profile,
      input.hostPackage
    );
    applyAIContext(context.agent, resolved.game, input.profile);
    const detach = attachStepReport(ctx, context.agent);
    try {
      return await runEnterChain(context.agent, resolved, {
        warmupMs: input.warmupMs,
        pageWaitMs: input.pageWaitMs,
        waitTimeoutMs: input.waitTimeoutMs,
      });
    } finally {
      detach();
    }
  },
});

const gameEnsureInputSchema = z.strictObject({
  gameKey: z
    .string()
    .describe('game.config.ts 中的游戏 key，如 douyin/edge-space'),
  profile: z
    .enum(['smoke', 'explore'])
    .default('smoke')
    .describe('用例集类型，决定叠加哪份 AI 上下文'),
  hostPackage: z.string().optional().describe('覆盖渠道默认的宿主包名'),
});

type GameEnsureInput = z.infer<typeof gameEnsureInputSchema>;

// 三条分支返回的 data 字段不完整一致，显式声明统一的可选结构
type EnsureData = {
  gameKey: string;
  keyword?: string;
  deepLink?: string;
  hostPackage?: string;
  skipped?: boolean;
  dismissed?: boolean;
};

// 判断"是否已在主菜单"的提示词：同时要求没有弹窗遮挡，避免半透明遮罩导致误判
const onMenuPrompt = (game: GameConfig) =>
  `当前是否停在《${game.name}》游戏主界面：能看到"新游戏"和"继续游戏"菜单，且没有弹窗遮挡`;

export const gameEnsure = defineNode<
  GameEnsureInput,
  EnsureData,
  ProjectContext
>({
  name: 'game.ensure',
  description:
    '状态复位：已在游戏主菜单则跳过，否则（可先关弹窗）重走完整进入链',
  inputSchema: gameEnsureInputSchema,
  async execute(ctx: NodeExecutionContext<GameEnsureInput, ProjectContext>) {
    const { input, context } = ctx;
    const resolved = resolveGame(
      input.gameKey,
      input.profile,
      input.hostPackage
    );
    const agent = context.agent;
    // 无论走哪条分支都先注入上下文，保证后续步骤提示词一致
    applyAIContext(agent, resolved.game, input.profile);

    // 三条分支都可能调模型，报告快照要覆盖整段判断过程
    const detach = attachStepReport(ctx, agent);
    try {
      // ① 前台不是宿主 → 一定不在游戏里，直接完整进入（不花模型调用）
      const foreground = hostInForeground(resolved.hostPackage);
      if (foreground === false) {
        return runEnterChain(agent, resolved);
      }

      // ② 视觉判断（一次模型调用）
      const onMenu = await agent
        .aiBoolean(onMenuPrompt(resolved.game))
        .catch(() => false);
      if (onMenu) {
        return {
          summary: `已在《${resolved.game.name}》主界面，跳过进入`,
          data: { gameKey: resolved.game.key, skipped: true },
        };
      }

      // ③ 兜底：先关掉遮挡的弹窗再判一次，仍不在才完整重进
      await agent
        .aiAct(
          '如果屏幕上有弹窗遮挡，点击"拒绝""关闭"或"允许"把它关掉；没有弹窗就不要操作'
        )
        .catch(() => {});
      const afterDismiss = await agent
        .aiBoolean(onMenuPrompt(resolved.game))
        .catch(() => false);
      if (afterDismiss) {
        return {
          summary: `关闭弹窗后回到《${resolved.game.name}》主界面`,
          data: { gameKey: resolved.game.key, skipped: true, dismissed: true },
        };
      }

      return runEnterChain(agent, resolved);
    } finally {
      detach();
    }
  },
});