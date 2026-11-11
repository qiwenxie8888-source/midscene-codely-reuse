import { defineGame } from '../../types';

export default defineGame({
  key: "douyin/pop2048",
  name: "泡泡2048大作战",
  channel: "douyin",
  enter: {
    cardPrompt:
      "搜索结果页中名为\"泡泡2048大作战\"的小游戏入口卡片（卡片上有游戏图标和\"泡泡2048大作战\"文字，可能带\"玩一玩\"按钮；整卡可点击）",
    menuPrompt: "屏幕上出现\"关卡x\"字样（这个x是数字，代表第x关卡）说明进入了游戏",
  },
  aiContext: `当前在测试抖音小游戏《泡泡2048大作战》，游戏界面可能由 canvas 渲染。
遇到用户协议、隐私政策、版本公告、登录提示、更新弹窗时，一律点"同意""允许"或"关闭"继续，不要中断流程。`,
});
