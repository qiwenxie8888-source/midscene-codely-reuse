import { defineGame } from '../../types';

export default defineGame({
  key: "douyin/townfood",
  name: "野兽朋友们的小巷食堂",
  channel: "douyin",
  enter: {
    cardPrompt:
      "搜索结果页中名为\"野兽朋友们的小巷食堂\"的小游戏卡片（卡片上有游戏图标或\"野兽朋友们的小巷食堂\"文字，可能带\"玩一玩\"或者\"进入游戏\"之类的游戏入口按钮；整卡可点击）",
    menuPrompt: "屏幕上能够看到“店铺管理”以及“招揽顾客”等游戏内的按钮",
  },
  aiContext: `当前在测试抖音小游戏《野兽朋友们的小巷食堂》，游戏界面可能由 canvas 渲染。
遇到用户协议、隐私政策、版本公告、登录提示、更新弹窗时，一律点"同意""允许"或"关闭"继续，不要中断流程。`,
});
