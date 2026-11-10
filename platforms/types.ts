// 渠道适配器契约：进入链里与"渠道"相关的部分都收敛在这里。
// 新增渠道（微信 / 官包 / B站 / 快手）时实现一个 adapter 并在 index.ts 登记。
export type PlatformKey = 'douyin' | 'wechat' | 'official';

export interface PlatformAdapter {
  key: PlatformKey;
  label: string;
  /** 宿主 App 包名（抖音、微信……） */
  defaultHostPackage?: string;
  /** 由游戏名生成宿主内的搜索关键词 */
  searchKeyword(gameName: string): string;
  /** 由关键词生成直达搜索结果页的深链 */
  searchDeepLink(keyword: string): string;
  /** 由游戏名生成"点哪个入口"的视觉描述（新建游戏时写进 game.config.ts 的 enter.cardPrompt） */
  cardPrompt(gameName: string): string;
}
