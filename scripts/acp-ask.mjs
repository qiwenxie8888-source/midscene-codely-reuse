// ACP 问答器：从 stdin 读一批任务，复用一个 codely 会话逐条提问，结果按 NDJSON 输出。
//
// 只做"文本 + 图片 -> 文本"，工具/MCP/遥测全部关闭：归因只需要模型读证据说话，
// 一旦它去调工具就说明 prompt 有问题，直接判失败比等它绕圈快。
//
// 输入（stdin JSON）：
//   { "model": "codely-vl", "cwd": "...", "timeoutMs": 180000,
//     "tasks": [ { "id": "a", "text": "...", "images": ["C:/x.jpeg"] } ] }
// 输出（stdout 每行一个 JSON）：
//   { "id": "a", "ms": 1234, "text": "...", "stopReason": "end_turn" }
//   { "id": "a", "ms": 1234, "error": "..." }
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import path from 'node:path';

class AcpClient {
  constructor(command, args, { cwd, env, onEvent }) {
    this.pending = new Map();
    this.nextId = 1;
    this.onEvent = onEvent;
    this.child = spawn(command, args, { cwd, env, stdio: ['pipe', 'pipe', 'pipe'] });
    this.lines = createInterface({ input: this.child.stdout });
    this.lines.on('line', (line) => {
      try {
        this.receive(JSON.parse(line));
      } catch (error) {
        this.fail(error);
      }
    });
    this.child.stderr.resume();
    this.child.stdin.on('error', (error) => this.fail(error));
    this.child.on('error', (error) => this.fail(error));
    this.child.on('exit', (code, signal) =>
      this.fail(new Error(`ACP process exited: code=${code}, signal=${signal}`))
    );
  }

  fail(error) {
    this.failure ??= error;
    for (const { reject } of this.pending.values()) reject(this.failure);
    this.pending.clear();
  }

  request(method, params, timeoutMs) {
    if (this.failure) return Promise.reject(this.failure);
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`ACP ${method} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      const finish = (callback) => (value) => {
        clearTimeout(timer);
        callback(value);
      };
      this.pending.set(id, { method, resolve: finish(resolve), reject: finish(reject) });
      this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
    });
  }

  receive(message) {
    if (message.method) {
      if (message.id !== undefined) {
        // 服务端反向请求（工具/权限）：一律拒绝，归因不该用工具
        this.child.stdin.write(
          `${JSON.stringify({
            jsonrpc: '2.0',
            id: message.id,
            error: { code: -32601, message: 'Tools disabled for attribution' },
          })}\n`
        );
        this.fail(new Error(`unexpected server request: ${message.method}`));
        return;
      }
      if (message.method === 'session/update') {
        const { sessionId, update } = message.params ?? {};
        if (update?.sessionUpdate === 'agent_message_chunk' && update.content?.type === 'text') {
          this.onEvent({ sessionId, text: update.content.text });
        } else if (update?.sessionUpdate === 'tool_call') {
          this.fail(new Error('unexpected tool call: attribution runs tool-free'));
        }
      }
      return;
    }
    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    if (message.error) {
      pending.reject(new Error(`ACP ${pending.method} failed (code=${message.error.code})`));
    } else if (!Object.hasOwn(message, 'result')) {
      pending.reject(new Error(`ACP ${pending.method} response has no result`));
    } else {
      pending.resolve(message.result);
    }
  }

  async close() {
    this.fail(new Error('ACP client closed'));
    this.lines.close();
    if (!this.child.pid || this.child.exitCode !== null || this.child.signalCode !== null) return;
    const exited = once(this.child, 'exit').catch(() => {});
    this.child.stdin.end();
    this.child.kill('SIGTERM');
    const timer = setTimeout(() => this.child.kill('SIGKILL'), 2000);
    try {
      await exited;
    } finally {
      clearTimeout(timer);
    }
  }
}

const mimeOf = (file) => {
  const ext = path.extname(file).toLowerCase();
  if (ext === '.png') return 'image/png';
  if (ext === '.webp') return 'image/webp';
  return 'image/jpeg';
};

const readStdin = async () => {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf8');
};

const main = async () => {
  const input = JSON.parse(await readStdin());
  const tasks = input.tasks ?? [];
  const model = input.model ?? process.env.MIDSCENE_MODEL_NAME ?? 'codely-vl';
  const timeoutMs = input.timeoutMs ?? 180_000;
  const cwd = input.cwd ?? process.cwd();

  const args = [
    '--experimental-acp',
    '--prewarm',
    '--model',
    model,
    '--no-upm',
    '--agents=false',
    '--experimental-skills=false',
    '--no-auto-edit-memory',
    '--disable-next-speaker-check',
    '--telemetry=false',
  ];

  let currentText = null;
  let chunkCount = 0;
  let lastActivity = Date.now();
  const client = new AcpClient('codely', args, {
    cwd,
    env: {
      ...process.env,
      CODELY_ENABLE_VLM: process.env.CODELY_ENABLE_VLM ?? '1',
      CODELY_CLI_INTERNAL_DISABLE_AUTO_UPDATE: '1',
    },
    onEvent(event) {
      if (event.text && currentText) {
        currentText.push(event.text);
        chunkCount += 1;
        lastActivity = Date.now();
      }
    },
  });

  let sessionId = null;
  try {
    const handshake = await client.request(
      'initialize',
      { protocolVersion: 1, clientInfo: { name: 'midscene-failure-attribution', version: '0.1.0' }, clientCapabilities: {} },
      Math.min(timeoutMs, 20_000)
    );
    if (handshake.protocolVersion !== 1) {
      throw new Error(`unexpected ACP protocolVersion: ${handshake.protocolVersion}`);
    }
    const session = await client.request('session/new', { cwd, mcpServers: [] }, timeoutMs);
    if (!session?.sessionId) throw new Error('session/new returned no sessionId');
    sessionId = session.sessionId;

    for (const task of tasks) {
      const started = Date.now();
      currentText = [];
      chunkCount = 0;
      lastActivity = Date.now();
      try {
        const parts = [{ type: 'text', text: task.text }];
        for (const file of task.images ?? []) {
          const bytes = await readFile(file);
          parts.push({ type: 'image', mimeType: mimeOf(file), data: bytes.toString('base64') });
        }
        // 停滞检测：模型想得久不算失败，只有"开始输出后长时间没新内容"或超硬上限才判死。
        // 之前用固定总超时，把"慢"和"挂住"混为一谈，带图那条误杀率约 50%。
        const idleMs = input.idleMs ?? 150_000;
        const maxMs = input.maxMs ?? Math.max(timeoutMs, 600_000);
        let watchdogTimer = null;
        const watchdog = new Promise((_, reject) => {
          watchdogTimer = setInterval(() => {
            const now = Date.now();
            if (chunkCount > 0 && now - lastActivity > idleMs) {
              reject(new Error(`stalled: 已 ${Math.round((now - lastActivity) / 1000)}s 无新输出`));
            } else if (now - started > maxMs) {
              reject(new Error(`exceeded max ${maxMs}ms`));
            }
          }, 1000);
          watchdogTimer.unref?.();
        });
        const result = await Promise.race([
          client.request('session/prompt', { sessionId, prompt: parts }, maxMs + 5_000),
          watchdog,
        ]).finally(() => clearInterval(watchdogTimer));
        process.stdout.write(
          `${JSON.stringify({
            id: task.id,
            ms: Date.now() - started,
            text: currentText.join(''),
            stopReason: result?.stopReason,
          })}\n`
        );
      } catch (error) {
        process.stdout.write(
          `${JSON.stringify({ id: task.id, ms: Date.now() - started, error: String(error?.message ?? error) })}\n`
        );
        // 卡死/超上限后这条会话已经不可信，别再往同一会话塞后续任务
        if (/^(stalled|exceeded max)/.test(String(error?.message ?? ''))) {
          client.fail(error instanceof Error ? error : new Error(String(error)));
          break;
        }
      } finally {
        currentText = null;
      }
    }
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ id: '__fatal__', error: String(error?.message ?? error) })}\n`);
  } finally {
    await client.close();
  }
};

await main();
