// 此文件由 `pnpm sync-games` 根据 games/*/*/game.config.ts 自动生成，请勿手改。
// 新增游戏后跑一次 `pnpm sync-games` 即可更新注册表。
import edgeSpace from '../games/douyin/edge-space/game.config';
import type { GameConfig } from '../games/types';

export const games: Readonly<Record<string, GameConfig>> = Object.freeze({
  [edgeSpace.key]: edgeSpace,
});

export const gameKeys: readonly string[] = Object.keys(games);

export function requireGame(gameKey: string): GameConfig {
  const game = games[gameKey];
  if (!game) {
    throw new Error(
      `未注册的游戏 "${gameKey}"；可用：${gameKeys.join(", ") || "（空）"}`
    );
  }
  return game;
}
