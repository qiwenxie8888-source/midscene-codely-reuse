import type { PlatformAdapter } from './types';

// 抖音：snssdk1128 搜索深链 + 结果页小游戏入口卡片。
// 关键词必须带"小游戏入口"，只搜游戏名会被攻略帖压过卡片。
export const douyinAdapter: PlatformAdapter = {
  key: 'douyin',
  label: '抖音小游戏',
  defaultHostPackage: 'com.ss.android.ugc.aweme',
  searchKeyword: (gameName) => `${gameName}小游戏入口`,
  searchDeepLink: (keyword) =>
    `snssdk1128://search?keyword=${encodeURIComponent(keyword)}`,
  // 抖音的入口形态是"搜索结果页里的卡片"；别的渠道形态不同，所以由各自适配器提供
  cardPrompt: (gameName) =>
    `搜索结果页中名为"${gameName}"的小游戏卡片（卡片上有游戏图标或"${gameName}"文字，可能带"玩一玩"或者"进入游戏"之类的游戏入口按钮；整卡可点击）`,
};
