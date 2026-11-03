# 协作规范

## 分支

从 `main` 切出，命名用「类型/简短描述」：

| 前缀 | 用途 | 示例 |
|---|---|---|
| `feat/` | 新功能、新增游戏或渠道 | `feat/wechat-adapter` |
| `fix/` | 修 bug | `fix/enter-retry` |
| `chore/` | 工程配置、依赖、CI | `chore/ci-setup` |
| `docs/` | 文档 | `docs/exploration-playbook` |

推荐走 PR（便于回溯和对比运行结果），个人紧急修复可直接提交 `main`。
若开启分支保护，**只勾 status checks，不要勾 Require approvals**——单人仓库会永远合不进去。

## 提交信息

格式：`类型: 说明`，与分支前缀一致，中文描述即可。

```
feat: 增加微信渠道进入适配器
fix: 修复 aiWaitFor 超时被步骤级 120s 提前掐断
chore: 补充 CI 工作流
```

一次提交只做一件事，不要把"改配置 + 修 bug + 调格式"混在一起。

## 提交前自检

```bash
pnpm typecheck    # TypeScript 类型
pnpm validate     # 用例 YAML 与节点名（不连设备、不调模型）
```

改前端时本地起服务看一眼：

```bash
.venv/Scripts/python -m uvicorn server.api:app --reload --port 8000
```

## 新增用例

**一条用例一个 YAML**，命名 `NN-<feature>.yaml`。原因有两个：CLI 不支持按用例名过滤（只能按文件跑），
拆开后"选文件 = 选用例"；同时一条用例失败不会挡住同文件里的其他用例。

```yaml
beforeAll:
  - game.ensure:
      gameKey: douyin/edge-space   # 已在主菜单则跳过，省掉重复进游戏

cases:
  - name: <功能>-<可验证的现象>
    steps:
      - ...
```

`game.ensure` 用于绝大多数用例；只有「存档持久化」这类必须真冷启动的用例才用 `game.enter`。

## 新增游戏 / 渠道

1. 复制 `games/<渠道>/<现有游戏>` 为新目录
2. 改 `game.config.ts`：`key`、`name`、`enter.cardPrompt`、`enter.menuPrompt`
3. 在 `scripts/games.ts` 登记
4. 用例里统一写 `- game.enter: { gameKey: <key> }`
5. 新增渠道时实现 `platforms/<渠道>.ts` 并在 `platforms/index.ts` 登记

## 模型配置

本项目用 Codely CLI（ACP）当模型，不是 OpenAI 兼容 API。环境变量与已知坑见 README 的「模型配置」。
改 `midscene.config.ts` 时不要动 `test: { testTimeout: 600_000 }`，否则长链路用例必超时。

## 不要提交的内容

`.env`（含密钥）、`midscene_run/`、`.midscene/`、`server/data/`、`.venv/`、`midscene-node-spec.*.md`、
根目录的临时截图（`.codely-*.png`、`tmp-*.png`），均已在 `.gitignore` 中。

截图和报告走 PR 描述里的链接，不放进仓库。
