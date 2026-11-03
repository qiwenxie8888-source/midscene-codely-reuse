# 小游戏冒烟测试（Codely 当模型）

基于 [Midscene Test](https://midscenejs.com/) 的 Android 小游戏自动化冒烟工程。

**和官方模板最大的区别：模型不走 OpenAI 兼容 API，而是用本机的 Codely CLI（ACP 协议）做推理。**
配置错一个字都跑不起来，先读下面的「模型配置」。

## 环境要求

- Node 20.19+ / 22.12+ / 24+
- 本机已安装并登录 Codely CLI（`codely --version` 能输出、`--experimental-acp` 可用）
- Android 真机已连接（`adb devices` 能看到设备）

## 模型配置（必读）

`.env`（从 `.env.example` 复制，**不要提交**）里只需三行：

```
MIDSCENE_MODEL_BASE_URL=codely://acp
MIDSCENE_MODEL_NAME=codely-vl
MIDSCENE_MODEL_FAMILY=codely-vl
```

四个坑，踩过：

1. **不要填 `MIDSCENE_MODEL_API_KEY`** —— ACP 走本机 CLI 的登录态，不需要 key。
2. **不要设 `MIDSCENE_MODEL_REASONING_ENABLED=false`** 想当然去提速：Codely 会把思考文本直接拼进正文，污染 Midscene 的 JSON 解析。宁愿慢。
3. **`testTimeout` 必须 600s**（已写在 `midscene.config.ts`）：Codely 每次冷启动的首个 AI 调用实测 70~170s，官方默认 120s 会把 `game.enter` 直接打断。
4. **会话复用阈值**：`CODELY_SESSION_MAX_PROMPTS` 默认 16，超限重建会话。一条用例一个文件、一个游戏一个会话，正常不需要改。

验证连通性（不连设备、不调模型）：

```bash
pnpm exec midscene-test model verify
```

## 常用命令

| 命令 | 作用 |
|---|---|
| `pnpm test` | 跑冒烟集（`games/**/smoke/**`） |
| `pnpm explore` | 跑探索集（`games/**/explore/**`） |
| `pnpm smoke:full` | 冒烟 + 入库 + 清理，一步到位（日常回归用这个） |
| `pnpm validate` | 离线校验用例 YAML 与节点名（不连设备、不调模型） |
| `pnpm typecheck` | TypeScript 类型检查 |
| `pnpm cleanup:codely` | 清理强杀留下的孤儿 codely 进程 |
| `pnpm dashboard` | 生成本地效果看板 |

看板服务（只读 API + 页面）：

```bash
.venv/Scripts/python -m uvicorn server.api:app --reload --port 8000
# http://127.0.0.1:8000/
```

跑完测试后需要入库才能在看板看到：

```bash
.venv/Scripts/python server/ingest.py
```

## 运行控制接口

服务起来后可以直接用 HTTP 触发运行，不必开终端：

| 接口 | 作用 |
|---|---|
| `POST /api/runs` | 起一次运行，body `{project, files: [], clean}`；`files` 为空则跑整个套件 |
| `GET /api/runs/{id}/events` | SSE 实时日志，`?from=<seq>` 可断线重连回放 |
| `GET /api/runs/{id}/log` | 轮询式取日志（SSE 的兜底） |
| `POST /api/runs/{id}/cancel` | 取消：排队中直接出队，运行中杀整棵进程树 |

```bash
curl -X POST http://127.0.0.1:8000/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"project":"smoke","files":["games/douyin/edge-space/smoke/02-inventory-open.yaml"]}'
```

单执行槽：一台真机同时只跑一个任务，最多排队 3 个，再多返回 `409`。
取消运行中的任务会连 `codely` 子进程一起杀掉（Windows 用 `taskkill /T`），不会留孤儿。

## 目录结构

```
platforms/                 # 渠道适配器：抖音 / 微信 / 官包 ……
  types.ts                 # 适配器契约（搜索关键词、深链）
  douyin.ts
games/<渠道>/<游戏>/
  game.config.ts           # 进入链参数 + AI 上下文（唯一维护点）
  smoke/NN-<feature>.yaml  # 冒烟回归，一条用例一个文件
  explore/*.yaml           # 结构化探索
scripts/
  games.ts                 # 游戏注册表（新增游戏在此登记）
  game-enter-node.ts       # game.enter / game.ensure 自定义 Node
  run-full.mjs             # 测试 → 入库 → 清理
  validate-cases.mjs       # 离线用例校验
  cleanup-codely.mjs       # 孤儿进程清理
server/                    # 本地看板（只读 FastAPI + 原生前端）
  api.py                   # HTTP 接口
  ingest.py                # summary.json → SQLite
  clean_runs.py            # 运行产物清理
  web/                     # 页面（无框架、无构建）
midscene.config.ts         # 项目/节点/超时配置
```

新增一款游戏：复制 `games/douyin/edge-space` → 改 `game.config.ts` → 在 `scripts/games.ts` 登记。
用例里统一写 `- game.enter: { gameKey: douyin/<游戏目录> }`，不需要再重复进入链。

新增用例：新建独立 YAML，命名 `NN-<feature>.yaml`，`beforeAll` 用 `game.ensure`
（已在主菜单则跳过，省掉重复进游戏）。

## 上游模板说明

If you skipped installation during creation, run `pnpm install --ignore-workspace`. The `postinstall` script automatically generates the node spec after each dependency installation. If lifecycle scripts are disabled, run `pnpm run nodes` manually.

Copy `.env.example` to `.env`, then configure the model as described above. Do not commit `.env` to your repository.

Connect an Android device and verify it with `adb devices`. Set `ANDROID_DEVICE_ID` to select a device when several are attached.

Reports are written to `midscene_run/report/`.

After changing Node registrations in `midscene.config.ts`, run `pnpm run nodes` to refresh the spec. This loads the configuration without connecting to a device or running tests. Extension factories must only acquire runtime resources inside Node execution.