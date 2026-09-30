import type {
  ApprovalView,
  ConversationScenario,
  DiffHunkView,
  DiffLineView,
  ErrorNoticeView,
  MemoryRowView,
  ModelConnectionView,
  ReadLineView,
  ToolRowView,
  TurnStatsView,
} from './types'

/**
 * Build a hunk whose header counts and line numbers are derived from its rows,
 * so the fixture can never show a header that contradicts its own body.
 */
function hunk(
  oldStart: number,
  newStart: number,
  suffix: string,
  rows: ReadonlyArray<readonly [DiffLineView['kind'], string]>,
): DiffHunkView {
  let oldLine = oldStart
  let newLine = newStart
  const lines = rows.map(([kind, text]): DiffLineView => {
    if (kind === 'context') {
      const line: DiffLineView = { kind, oldLine, newLine, text }
      oldLine += 1
      newLine += 1
      return line
    }
    if (kind === 'del') {
      const line: DiffLineView = { kind, oldLine, newLine: null, text }
      oldLine += 1
      return line
    }
    const line: DiffLineView = { kind: 'add', oldLine: null, newLine, text }
    newLine += 1
    return line
  })
  const oldCount = lines.filter((line) => line.kind !== 'add').length
  const newCount = lines.filter((line) => line.kind !== 'del').length
  return {
    header: `@@ -${String(oldStart)},${String(oldCount)} +${String(newStart)},${String(newCount)} @@${suffix}`,
    lines,
  }
}

/** Long single line that must overflow horizontally rather than wrap or clip. */
const LONG_ADD_LINE =
  '    compact_long_tool_outputs_after_tokens: int = Field(default=4_000, ge=512)  # 单行很长，用来验证 diff 行内横向滚动不会换行、不会把行号挤出可视区'

function readWindow(start: number, texts: readonly string[], totalLines: number, language: string) {
  const lines: ReadLineView[] = texts.map((text, index) => ({ number: start + index, text }))
  return { kind: 'read' as const, path: 'packages/host/webserver/src/index.ts', lines, totalLines, language }
}

/**
 * Early visual checkpoint 1 — complete file diff.
 *
 * Two hunks, additions and deletions, and one deliberately long added line.
 * The reference baseline only had single-line mutations
 * (`local-agent-web/docs/ui-reference/README.md` §5), so this prototype is the
 * project's own baseline and must be confirmed before it is called done.
 */
export const diffScenario: ConversationScenario = {
  id: 'diff',
  sessionId: 's-diff',
  purpose: '早期检查点 1：双 hunk 完整文件 diff（含增删行与超长行）',
  session: {
    title: '压缩阈值重构',
    workspace: 'local-agent-web',
    status: 'completed',
    modelLabel: '模型 A · 上下文 128k',
  },
  messages: [
    { kind: 'user', id: 'u1', text: '把上下文窗口默认值和压缩配置一起改一下。', at: '10:24' },
    {
      kind: 'assistant',
      id: 'a1',
      text: '我先改默认窗口与流式开关，再补一个长输出压缩阈值。',
      at: '10:24',
    },
    {
      kind: 'tool',
      id: 't1',
      tool: {
        name: 'edit_file',
        summary: '精确串替换',
        status: 'succeeded',
        durationLabel: '0.4s',
        target: 'src/local_agent/config.py',
        expanded: true,
        card: {
          kind: 'file-mutation',
          diff: {
            path: 'src/local_agent/config.py',
            additions: 5,
            deletions: 3,
            hunks: [
              hunk(12, 12, ' from pydantic import BaseModel, Field', [
                ['context', 'from pydantic import BaseModel, Field'],
                ['context', 'from local_agent.errors import ConfigurationError'],
                ['context', ''],
                ['del', 'CONTEXT_WINDOW_DEFAULT = 65_536'],
                ['del', 'MAX_TOKENS_DEFAULT = 8_192'],
                ['add', 'CONTEXT_WINDOW_DEFAULT = 128_000'],
                ['add', 'MAX_TOKENS_DEFAULT = 8_192'],
                ['add', 'STREAMING_DEFAULT = False'],
                ['context', ''],
                ['context', 'class SecretRef(BaseModel):'],
              ]),
              hunk(148, 149, ' class CompressionConfig(_StrictModel):', [
                ['context', '    #: 用量越过该比例后压缩窗口'],
                ['context', '    trigger_ratio: float = Field(default=0.8, gt=0.0, le=1.0)'],
                ['del', '    preserve_recent_turns: int = Field(default=8, ge=1)'],
                ['add', '    preserve_recent_turns: int = Field(default=8, ge=1)'],
                ['add', LONG_ADD_LINE],
                ['context', '    preserve_recent_tool_steps: int = Field(default=3, ge=1)'],
                ['context', ''],
                ['context', '    def validate_ratio(cls) -> "CompressionConfig":'],
              ]),
            ],
          },
        },
      },
    },
    {
      kind: 'assistant',
      id: 'a2',
      text: '改动已写入，两个 hunk 共 +5 / −3。',
      at: '10:24',
    },
  ],
  plan: {
    goal: '更新上下文窗口与压缩配置',
    steps: [
      { id: 's1', title: '定位配置默认值', status: 'completed' },
      { id: 's2', title: '修改窗口与压缩阈值', status: 'completed' },
      { id: 's3', title: '跑门槛验证', status: 'running' },
    ],
  },
  stats: {
    turns: 1,
    steps: 3,
    cacheHitRatio: 0.78,
    inputTokens: 8_812,
    outputTokens: 708,
    elapsedLabel: '1 分 10 秒',
    estimated: false,
    calls: [
      {
        id: 'c1',
        purpose: '规划与工具决策',
        modelLabel: '模型 A',
        inputTokens: 8_812,
        outputTokens: 708,
        elapsedLabel: '1 分 10 秒',
      },
    ],
  },
  context: { usedTokens: 371_000, windowTokens: 1_000_000, scope: 'previous' },
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
}

const authFailed: ErrorNoticeView = {
  code: 'MODEL_AUTH_FAILED',
  category: 'model',
  severity: 'error',
  title: '模型连接鉴权失败',
  message: '连接“模型 A”返回鉴权失败，本轮未产生任何工具调用，也没有写入记忆。',
  hint: '到「设置 → 模型」更新该连接的 API Key 后重新发送；密钥只需要覆盖写入，不需要回读。',
  retryable: false,
  blockedReason: '需先处理配置（更新该连接的密钥），不能自动重试。',
  diagnosticId: 'err-4f3a91',
}

const rateLimited: ErrorNoticeView = {
  code: 'MODEL_RATE_LIMITED',
  category: 'model',
  severity: 'warning',
  title: '模型连接被限流',
  message: '供应商对本轮请求返回限流，应用未自动重试，等待时间由你决定。',
  hint: '稍后重新发送；若需立刻继续，可在输入区切换到另一个已测试通过的连接。',
  retryable: true,
  blockedReason: null,
  diagnosticId: 'err-90c1b7',
}

const retryExhausted: ErrorNoticeView = {
  code: 'MODEL_RETRY_EXHAUSTED',
  category: 'model',
  severity: 'error',
  title: '重试已达上限仍失败',
  message: '本轮已按策略重试 3 次，最后一次为请求超时，未执行任何工具。',
  hint: '检查该连接端点是否可达，或调小单次输出上限后重试；连续失败不会自动切换模型。',
  retryable: true,
  blockedReason: null,
  diagnosticId: 'err-2d7e40',
}

/**
 * Early visual checkpoint 2 — provider failure states.
 *
 * Three deterministic fixtures, no provider call and no API key. Every notice
 * carries an actionable hint and a safe diagnostic id: no raw provider payload,
 * no key material, no absolute private path (V0_4_DEVELOPMENT_PLAN T26).
 */
export const errorsScenario: ConversationScenario = {
  id: 'errors',
  sessionId: 's-errors',
  purpose: '早期检查点 2：供应商错误态三种（鉴权失败 / 限流 / 超时重试耗尽）',
  session: {
    title: '连接故障排查',
    workspace: 'local-agent-web',
    status: 'idle',
    modelLabel: '模型 A · 上下文 128k',
  },
  messages: [
    { kind: 'user', id: 'u1', text: '看一下 README 的结构。', at: '11:02' },
    { kind: 'error', id: 'e1', notice: authFailed },
    { kind: 'user', id: 'u2', text: '换成模型 B 再试一次。', at: '11:05' },
    { kind: 'error', id: 'e2', notice: rateLimited },
    { kind: 'user', id: 'u3', text: '再试一次。', at: '11:07' },
    { kind: 'error', id: 'e3', notice: retryExhausted },
  ],
  plan: null,
  stats: {
    turns: 3,
    steps: 0,
    cacheHitRatio: null,
    inputTokens: null,
    outputTokens: null,
    elapsedLabel: '0.9 秒',
    estimated: false,
    calls: [],
  },
  context: { usedTokens: null, windowTokens: 128_000, scope: 'unknown' },
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
}

/** Ordinary shell: conversation, plan strip, tool row and the per-turn stats line. */
export const shellScenario: ConversationScenario = {
  id: 'shell',
  sessionId: 's-shell',
  purpose: '常驻会话壳：侧栏、会话头、对话流、计划条、工具行、统计行与上下文计量器',
  session: {
    title: '给 README 补一段快速开始',
    workspace: 'local-agent-web',
    status: 'completed',
    modelLabel: '模型 A · 上下文 128k',
  },
  messages: [
    { kind: 'user', id: 'u1', text: '在 README 里补一段最小启动步骤。', at: '09:15' },
    {
      kind: 'assistant',
      id: 'a1',
      text: '我先读现有 README 的目录结构，再决定插在哪一节后面。',
      at: '09:15',
    },
    {
      kind: 'tool',
      id: 't1',
      tool: {
        name: 'read_file',
        summary: '读取文件',
        status: 'succeeded',
        durationLabel: '0.1s',
        target: 'README.md',
        expanded: false,
        card: readWindow(1, ['# local-agent-web', '', '本机学习型网页 Agent。'], 184, 'markdown'),
      },
    },
    {
      kind: 'tool',
      id: 't2',
      tool: {
        name: 'edit_file',
        summary: '精确串替换',
        status: 'succeeded',
        durationLabel: '0.3s',
        target: 'README.md',
        expanded: false,
        card: { kind: 'generic', headline: '已替换 1 处匹配', detail: 'README.md · 新增 6 行' },
      },
    },
    {
      kind: 'assistant',
      id: 'a2',
      text: '已在「快速开始」后插入三步启动说明，并保留了原有的依赖前提描述。',
      at: '09:16',
    },
  ],
  plan: {
    goal: 'README 增补最小启动步骤',
    steps: [
      { id: 's1', title: '读取 README 结构', status: 'completed' },
      { id: 's2', title: '插入快速开始', status: 'completed' },
    ],
  },
  stats: {
    turns: 1,
    steps: 2,
    cacheHitRatio: 0.62,
    inputTokens: 6_240,
    outputTokens: 412,
    elapsedLabel: '42 秒',
    estimated: false,
    calls: [
      {
        id: 'c1',
        purpose: '规划与工具决策',
        modelLabel: '模型 A',
        inputTokens: 3_120,
        outputTokens: 180,
        elapsedLabel: '21 秒',
      },
      {
        id: 'c2',
        purpose: '最终回答',
        modelLabel: '模型 A',
        inputTokens: 3_120,
        outputTokens: 232,
        elapsedLabel: '21 秒',
      },
    ],
  },
  context: { usedTokens: 84_000, windowTokens: 128_000, scope: 'current' },
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
}

const terminalOutput = [
  'Running 4 checks',
  '  ✓ typecheck                                          1.82s',
  '  ✓ lint                                               0.94s',
  '  ✓ duplication                                        2.10s',
  '  ✗ unit                                               8.41s',
  '',
  'packages/client/ui-primitives/tests/terminal-block.client.spec.tsx',
  '  FAIL  caps output at the configured line budget',
  '    expected 16 lines, received 24',
  '',
  '1 of 4 checks failed',
].join('\n')

function tool(partial: Omit<ToolRowView, 'expanded'> & { expanded?: boolean }): ToolRowView {
  return { expanded: partial.expanded ?? true, ...partial }
}

/**
 * Remaining tool cards in one flow.
 *
 * Every card shape the v0.4 conversation needs to render is here, and the last
 * row is an unknown tool so the safe generic fallback is on screen too
 * (`V0_4_DEVELOPMENT_PLAN.md` T31: "旧事件/未知工具使用安全通用渲染器").
 */
export const toolsScenario: ConversationScenario = {
  id: 'tools',
  sessionId: 's-tools',
  purpose: '其余工具卡：读取窗口 / 搜索两种形状 / 终端非零退出 / 待办 / 未知工具回退',
  session: {
    title: '定位构建失败原因',
    workspace: 'local-agent-web',
    status: 'completed',
    modelLabel: '模型 B · 上下文 200k',
  },
  messages: [
    { kind: 'user', id: 'u1', text: 'unit 检查为什么失败？', at: '14:02' },
    {
      kind: 'assistant',
      id: 'a1',
      text: '先跑一遍检查，再看相关文件与用例。',
      at: '14:02',
    },
    {
      kind: 'tool',
      id: 't1',
      tool: tool({
        name: 'run_shell',
        summary: '执行命令',
        status: 'failed',
        durationLabel: '12.6s',
        target: 'pnpm run check',
        // 轨迹视图读的字段与对话行同源：一次调用只有一份参数、一份结果预览。
        toolCallId: 'call-7f31a2',
        parameters: [
          { name: 'command', value: 'pnpm run check' },
          { name: 'cwd', value: 'local-agent-web' },
        ],
        resultPreview: terminalOutput,
        resultTruncated: false,
        resultDigest: 'sha256:4d1c9a7e3b02',
        startedLabel: '14:02',
        finishedLabel: '14:03',
        card: {
          kind: 'terminal',
          command: 'pnpm run check',
          cwd: 'local-agent-web',
          exitCode: 1,
          signal: null,
          output: terminalOutput,
          collapsedLines: 8,
        },
      }),
    },
    {
      kind: 'tool',
      id: 't2',
      tool: tool({
        name: 'grep',
        summary: '按内容搜索',
        status: 'succeeded',
        durationLabel: '0.6s',
        target: 'packages/client',
        toolCallId: 'call-18b4e0',
        parameters: [
          { name: 'pattern', value: 'DEFAULT_SEARCH_MAX_LINES' },
          { name: 'path', value: 'packages/client' },
        ],
        resultPreview:
          'packages/client/ui-primitives/src/SearchBlock.tsx:16: export const DEFAULT_SEARCH_MAX_LINES = 16',
        resultTruncated: true,
        resultDigest: 'sha256:9b27c41f0d55',
        startedLabel: '14:02',
        finishedLabel: '14:02',
        card: {
          kind: 'search',
          shape: 'matches',
          total: 42,
          truncated: true,
          groups: [
            {
              path: 'packages/client/ui-primitives/src/SearchBlock.tsx',
              matches: [
                { line: 16, text: 'export const DEFAULT_SEARCH_MAX_LINES = 16' },
                { line: 138, text: 'export function SearchBlock(props: SearchBlockProps) {' },
              ],
            },
            {
              path: 'packages/client/ui-tool/src/client/tool/models/search-card-model.ts',
              matches: [{ line: 45, text: 'export const CHAT_SEARCH_MAX_LINES = 8' }],
            },
          ],
          paths: [],
        },
      }),
    },
    {
      kind: 'tool',
      id: 't3',
      tool: tool({
        name: 'glob',
        summary: '按路径搜索',
        status: 'succeeded',
        durationLabel: '0.3s',
        target: '**/SearchBlock*',
        toolCallId: 'call-2c90da',
        parameters: [{ name: 'pattern', value: '**/SearchBlock*' }],
        resultPreview: 'packages/client/ui-primitives/src/SearchBlock.tsx',
        resultTruncated: true,
        resultDigest: 'sha256:77a0be13c9f4',
        startedLabel: '14:02',
        finishedLabel: '14:02',
        card: {
          kind: 'search',
          shape: 'paths',
          total: 23,
          truncated: true,
          groups: [],
          paths: [
            'packages/client/ui-primitives/src/SearchBlock.tsx',
            'packages/client/ui-primitives/src/SearchBlock.module.css',
            'packages/client/ui-tool/src/client/tool/toolviews/search-row.tsx',
            'packages/client/ui-tool/tests/search-card.client.spec.tsx',
            'packages/client/ui-tool/src/client/tool/models/search-card-model.ts',
          ],
        },
      }),
    },
    {
      kind: 'tool',
      id: 't4',
      tool: tool({
        name: 'read_file',
        summary: '读取片段',
        status: 'succeeded',
        durationLabel: '0.1s',
        target: 'packages/host/webserver/src/index.ts',
        toolCallId: 'call-5e07fb',
        parameters: [
          { name: 'path', value: 'packages/host/webserver/src/index.ts' },
          { name: 'offset', value: '41' },
          { name: 'limit', value: '5' },
        ],
        resultPreview: 'export interface ReadBlockProps {',
        resultTruncated: false,
        resultDigest: 'sha256:31bd8f6a24c7',
        startedLabel: '14:02',
        finishedLabel: '14:02',
        card: readWindow(
          41,
          [
            'export interface ReadBlockProps {',
            '  label?: string | undefined',
            '  lines: readonly ReadBlockLine[]',
            '  totalLines: number',
            '}',
          ],
          196,
          'ts',
        ),
      }),
    },
    {
      kind: 'tool',
      id: 't5',
      tool: tool({
        name: 'todo_write',
        summary: '更新任务清单',
        status: 'succeeded',
        durationLabel: '0.2s',
        target: null,
        toolCallId: 'call-a6e12c',
        parameters: [{ name: 'todos', value: '[4 项]' }],
        resultPreview: '任务清单已更新：1 项完成 · 2 项进行中 · 1 项待办',
        resultTruncated: false,
        resultDigest: 'sha256:c82d9014be6f',
        startedLabel: '14:02',
        finishedLabel: '14:02',
        card: {
          kind: 'todo',
          items: [
            { text: '复现 unit 失败', status: 'completed' },
            { text: '定位截断上限用例', status: 'in_progress' },
            { text: '修正高度上限', status: 'in_progress' },
            { text: '重跑门槛', status: 'pending' },
          ],
        },
      }),
    },
    {
      kind: 'tool',
      id: 't6',
      tool: tool({
        name: 'vendor_scan_tool',
        summary: '未注册工具',
        status: 'succeeded',
        durationLabel: '0.4s',
        target: null,
        // 这一行刻意**什么都不补**：参数与结果预览都没有记录。轨迹里如实显示"未知"，
        // 而不是为了好看写 0 或编一段结果。
        toolCallId: 'call-d40f58',
        parameters: [],
        resultPreview: '',
        resultTruncated: false,
        resultDigest: null,
        startedLabel: '14:03',
        finishedLabel: '14:03',
        card: {
          kind: 'generic',
          headline: '工具返回了结构化结果',
          detail: '该工具没有专用视图，按通用渲染器显示返回摘要；完整结果保存在工具事件中。',
        },
      }),
    },
    {
      kind: 'tool',
      id: 't7',
      tool: tool({
        name: 'run_shell',
        summary: '执行命令',
        status: 'succeeded',
        durationLabel: '9.4s',
        target: 'pnpm run build',
        toolCallId: 'call-9f2b6d',
        parameters: [
          { name: 'command', value: 'pnpm run build' },
          { name: 'cwd', value: 'local-agent-web' },
        ],
        resultPreview: ['> local-agent-web-frontend@0.4.0 build', '✓ built in 848ms'].join('\n'),
        resultTruncated: false,
        resultDigest: 'sha256:5a71ec09d3b8',
        startedLabel: '14:03',
        finishedLabel: '14:03',
        card: {
          kind: 'terminal',
          command: 'pnpm run build',
          cwd: 'local-agent-web',
          // Exit code 0 must render as a success, not as a failure.
          exitCode: 0,
          signal: null,
          output: ['> local-agent-web-frontend@0.4.0 build', '✓ built in 848ms'].join('\n'),
          collapsedLines: 0,
        },
      }),
    },
  ],
  plan: {
    goal: '修复 unit 检查的高度上限用例',
    steps: [
      { id: 's1', title: '复现失败', status: 'completed' },
      { id: 's2', title: '定位用例与实现', status: 'completed' },
      { id: 's3', title: '修正高度上限', status: 'completed' },
      { id: 's4', title: '重跑门槛', status: 'completed' },
    ],
  },
  stats: {
    turns: 1,
    steps: 6,
    cacheHitRatio: null,
    inputTokens: 12_400,
    outputTokens: 968,
    elapsedLabel: '3 分 04 秒',
    estimated: true,
    calls: [
      {
        id: 'c1',
        purpose: '规划',
        modelLabel: '模型 B',
        inputTokens: 4_100,
        outputTokens: 220,
        elapsedLabel: '1 分 02 秒',
      },
      {
        id: 'c2',
        purpose: '工具决策',
        modelLabel: '模型 B',
        inputTokens: 4_100,
        outputTokens: 386,
        elapsedLabel: '1 分 01 秒',
      },
      {
        id: 'c3',
        purpose: '最终回答',
        modelLabel: '模型 B',
        inputTokens: 4_200,
        outputTokens: 362,
        elapsedLabel: '1 分 01 秒',
      },
    ],
  },
  context: { usedTokens: 915_000, windowTokens: 1_000_000, scope: 'current' },
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
}

/**
 * Matrix 20 / 21 — 轨迹页签：工具调用表格 + 点击行展开的右侧检查器。
 *
 * 与 `tools` 是**同一个会话的另一个页签**（同 `sessionId`、同消息、同会话头）：轨迹与对话看的是
 * 同一份数据，用两个 fixture 各写一套就迟早会互相矛盾。这里只覆写两处：
 *
 * - 初始页签为 `trace`（`?state=trace` 直接落在轨迹页）；
 * - 逐次模型调用补上 `tools_schema_fingerprint`——契约里有这个字段，Schema 段要如实展示它。
 *   "最终回答"那一次没有发送工具 Schema，因此指纹为 `null`（页面显示"未知"，不编哈希）。
 */
export const traceScenario: ConversationScenario = {
  ...toolsScenario,
  id: 'trace',
  tab: 'trace',
  purpose: '轨迹视图：工具调用表格（类型 / 参数 / 结果摘要）+ 点击行展开的检查器（Summary / Payload / Result / Schema / Timing）',
  stats: toolsScenario.stats === null
    ? null
    : {
      ...toolsScenario.stats,
      calls: toolsScenario.stats.calls.map((call, index) => ({
        ...call,
        // 只有需要工具的调用才带工具 Schema：没有工具集就没有指纹，这一点不粉饰。
        schemaFingerprint: index < 2 ? 'sha256:6f1c8a2d90be' : null,
      })),
    },
}

const pendingApproval: ApprovalView = {
  approvalId: 'ap-7781',
  action: 'delete_file',
  summary: '递归删除工作区内的构建产物目录',
  target: 'dist/',
  impact: [
    '将删除 214 个文件（约 41.6 MB），操作不可撤销',
    '目标位于工作区内，其他路径不受影响',
    '本次操作会写入工具事件与审批历史',
  ],
  risk: 'high',
  state: 'pending',
  note: null,
}

const expiredApproval: ApprovalView = {
  ...pendingApproval,
  state: 'expired',
  note: '该审批已超时失效，未执行任何操作。请重新发起请求。',
}

/** Composer takeover: an approval request waiting for a decision. */
export const approvalScenario: ConversationScenario = {
  id: 'approval',
  sessionId: 's-approval',
  purpose: '审批接管：动作、参数、影响与风险，提供一次性批准或拒绝',
  session: {
    title: '清理构建产物',
    workspace: 'local-agent-web',
    status: 'awaiting-approval',
    modelLabel: '模型 A · 上下文 128k',
  },
  messages: [
    { kind: 'user', id: 'u1', text: '把 dist 目录清掉，然后重新构建。', at: '16:20' },
    {
      kind: 'assistant',
      id: 'a1',
      text: '这一步会删除 214 个文件，需要你确认后才会执行。',
      at: '16:20',
    },
  ],
  plan: {
    goal: '清理构建产物并重新构建',
    steps: [
      { id: 's1', title: '确认待删除范围', status: 'running' },
      { id: 's2', title: '删除 dist 并重建', status: 'pending' },
    ],
  },
  stats: {
    turns: 1,
    steps: 1,
    cacheHitRatio: 0.55,
    inputTokens: 2_400,
    outputTokens: 128,
    elapsedLabel: '8 秒',
    estimated: false,
    calls: [],
  },
  context: { usedTokens: 42_000, windowTokens: 128_000, scope: 'previous' },
  takeover: { kind: 'approval', approval: pendingApproval },
  settings: null,
  modelMenu: null,
}

/** The same takeover after the request went stale: no buttons, only an explanation. */
export const approvalExpiredScenario: ConversationScenario = {
  id: 'approval-expired',
  // The same conversation as `approval`, later in time: one sidebar row.
  sessionId: 's-approval',
  purpose: '审批失效态：过期后不再提供按钮，只说明未执行任何操作',
  session: {
    title: '清理构建产物',
    workspace: 'local-agent-web',
    status: 'idle',
    modelLabel: '模型 A · 上下文 128k',
  },
  messages: [
    { kind: 'user', id: 'u1', text: '把 dist 目录清掉，然后重新构建。', at: '16:20' },
    {
      kind: 'assistant',
      id: 'a1',
      text: '这一步会删除 214 个文件，需要你确认后才会执行。',
      at: '16:20',
    },
  ],
  plan: {
    goal: '清理构建产物并重新构建',
    steps: [
      { id: 's1', title: '确认待删除范围', status: 'blocked' },
      { id: 's2', title: '删除 dist 并重建', status: 'pending' },
    ],
  },
  stats: {
    turns: 1,
    steps: 1,
    cacheHitRatio: null,
    inputTokens: null,
    outputTokens: null,
    elapsedLabel: '8 秒',
    estimated: false,
    calls: [],
  },
  context: { usedTokens: 42_000, windowTokens: 128_000, scope: 'previous' },
  takeover: { kind: 'approval', approval: expiredApproval },
  settings: null,
  modelMenu: null,
}

/**
 * Chat connections covering every state the settings page has to render.
 *
 * A card only ever reports what is known: an untested connection says
 * `unknown`, not "passed", and the key value is never part of the payload —
 * only whether one is configured.
 */
const connections: readonly ModelConnectionView[] = [
  {
    id: 'conn-a',
    name: '模型 A',
    baseUrl: 'https://api.example-a.com/v1',
    transport: 'https',
    modelId: 'chat-large',
    contextWindowLabel: '128k',
    isDefault: true,
    keyStatus: 'configured',
    status: 'verified',
    statusDetail: '最近一次检测通过：聊天、工具调用与流式输出均正常。',
    capabilities: [
      { name: '聊天', result: 'passed' },
      { name: '工具调用', result: 'passed' },
      { name: '流式输出', result: 'passed' },
    ],
    confirmDelete: false,
    pendingApply: false,
  },
  {
    id: 'conn-b',
    name: '模型 B',
    baseUrl: 'https://api.example-b.com/v1',
    transport: 'https',
    modelId: 'chat-pro',
    contextWindowLabel: '200k',
    isDefault: false,
    keyStatus: 'configured',
    status: 'unverified',
    // Untested is untested: neither chat nor tools may be stated as working.
    statusDetail: '聊天能力未验证；可尝试普通聊天，工具能力尚未确认。',
    capabilities: [
      { name: '聊天', result: 'unknown' },
      { name: '工具调用', result: 'unknown' },
      { name: '流式输出', result: 'unknown' },
    ],
    confirmDelete: false,
    pendingApply: true,
  },
  {
    id: 'conn-c',
    name: '本地端点',
    baseUrl: 'http://192.168.1.20:8000/v1',
    // LAN HTTP: allowed only after the user acknowledges plaintext transport.
    transport: 'plaintext-http',
    modelId: 'local-8b',
    contextWindowLabel: '32k',
    isDefault: false,
    keyStatus: 'session-only',
    status: 'testing',
    statusDetail: '正在检测聊天与工具调用能力…',
    capabilities: [
      { name: '聊天', result: 'passed' },
      { name: '工具调用', result: 'unknown' },
      { name: '流式输出', result: 'unknown' },
    ],
    confirmDelete: false,
    pendingApply: false,
  },
  {
    id: 'conn-d',
    name: '旧供应商',
    baseUrl: 'https://legacy.example.com/v1',
    transport: 'https',
    modelId: 'legacy-chat',
    contextWindowLabel: '64k',
    isDefault: false,
    keyStatus: 'configured',
    status: 'failed',
    statusDetail: '鉴权失败（HTTP 401）。请更新该连接的 API Key；本次未调用模型，也未执行任何工具。',
    capabilities: [
      { name: '聊天', result: 'failed' },
      { name: '工具调用', result: 'unknown' },
      { name: '流式输出', result: 'unknown' },
    ],
    confirmDelete: false,
    pendingApply: false,
  },
  {
    id: 'conn-e',
    name: '备用端点',
    baseUrl: 'https://api.example-c.com/v1',
    transport: 'https',
    modelId: 'chat-mini',
    contextWindowLabel: '32k',
    isDefault: false,
    keyStatus: 'configured',
    status: 'unsupported',
    statusDetail: '该模型不支持工具调用：可以普通聊天，但进入需要工具的任务前会被明确拒绝，不会静默换模型。',
    capabilities: [
      { name: '聊天', result: 'passed' },
      { name: '工具调用', result: 'failed' },
      { name: '流式输出', result: 'passed' },
    ],
    confirmDelete: false,
    pendingApply: false,
  },
  {
    id: 'conn-f',
    name: '未命名连接',
    baseUrl: 'https://api.example-d.com/v1',
    transport: 'https',
    modelId: '—',
    contextWindowLabel: '未知',
    isDefault: false,
    keyStatus: 'missing',
    status: 'unverified',
    statusDetail: '缺少 API Key，无法检测。密钥只能覆盖写入，页面不回读。',
    capabilities: [
      { name: '聊天', result: 'unknown' },
      { name: '工具调用', result: 'unknown' },
      { name: '流式输出', result: 'unknown' },
    ],
    // Deactivation is confirmed inside the card instead of a nested dialog.
    confirmDelete: true,
    pendingApply: false,
  },
]

/** Settings page: the model-connection list with every fixture state at once. */
export const settingsModelsScenario: ConversationScenario = {
  id: 'settings-models',
  sessionId: 's-shell',
  purpose:
    '设置页：连接六态（已验证 / 未验证 / 检测中 / 检测失败 / 能力不支持 / 密钥缺失）+ 删除确认 + 下一轮生效',
  session: shellScenario.session,
  messages: shellScenario.messages,
  plan: null,
  stats: shellScenario.stats,
  context: shellScenario.context,
  takeover: { kind: 'none' },
  settings: {
    connections,
    credentialStore: { kind: 'os', note: null },
    // 权限分区（T38）：停在「自定义」，带两条授权，一条只读一条读写。
    permissions: {
      mode: 'whitelist',
      defaultMode: 'whitelist',
      grants: [
        { path: 'E:/design-assets', access: 'read' },
        { path: 'D:/team/shared', access: 'read_write' },
      ],
      storeUnavailable: false,
    },
  },
  modelMenu: null,
}

/** Settings page with no connections yet, and no usable credential store. */
export const settingsEmptyScenario: ConversationScenario = {
  id: 'settings-empty',
  sessionId: 's-shell',
  purpose: '设置页空态：还没有任何连接，且系统凭据存储不可用（重启后需要重新录入）',
  // Same session as the sidebar's selected row (title and workspace included):
  // a header that disagreed with the highlighted row would contradict the shell.
  // The conversation is empty because no connection exists yet to run anything.
  session: { ...shellScenario.session, status: 'idle', modelLabel: '未配置模型' },
  messages: [],
  plan: null,
  stats: null,
  context: { usedTokens: null, windowTokens: null, scope: 'unknown' },
  takeover: { kind: 'none' },
  settings: {
    connections: [],
    credentialStore: {
      kind: 'unavailable',
      note: '当前系统凭据存储不可用：密钥只保存在本次进程内，重启后需要重新录入，也不会写入明文文件。',
    },
    // 权限同样存不下：停在最保守的档位，并如实说明重启后会回到出厂默认。
    permissions: {
      mode: 'deny',
      defaultMode: 'whitelist',
      grants: [],
      storeUnavailable: true,
    },
  },
  modelMenu: null,
}

/** Composer model menu: the choice applies to the next submit, not the running one. */
export const modelMenuScenario: ConversationScenario = {
  id: 'model-menu',
  sessionId: 's-shell',
  purpose: '输入区模型选择菜单：切换只对下一次提交生效，正在运行的一轮仍固定原连接',
  // The composer chip is the selector, so it shows the chosen connection; the
  // menu states separately that the running turn is still pinned to 模型 A.
  session: { ...shellScenario.session, status: 'running', modelLabel: '模型 B · 上下文 200k' },
  messages: shellScenario.messages,
  plan: shellScenario.plan,
  stats: shellScenario.stats,
  context: shellScenario.context,
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: {
    connections: [
      { id: 'conn-a', name: '模型 A', modelId: 'chat-large', status: 'verified' },
      { id: 'conn-b', name: '模型 B', modelId: 'chat-pro', status: 'unverified' },
      { id: 'conn-e', name: '备用端点', modelId: 'chat-mini', status: 'unsupported' },
    ],
    selectedId: 'conn-b',
    pinnedLabel: '模型 A（本轮已固定）',
    nextTurnNotice: '已选择 模型 B · 从下一轮开始生效。',
  },
}

/* ---------------------------------------------------------------------------
 * Retrieval and long-term memory (plan §6.7).
 *
 * The three outcomes are three different facts, so they are three fixtures: an
 * answer with sources behind it, an answer where retrieval found nothing, and an
 * answer where retrieval could not run at all. None of them may be shown as if
 * the others had happened.
 * ------------------------------------------------------------------------- */

const retrievalStats: TurnStatsView = {
  turns: 1,
  steps: 1,
  cacheHitRatio: 0.7,
  inputTokens: 5_120,
  outputTokens: 264,
  elapsedLabel: '14 秒',
  estimated: false,
  calls: [
    {
      id: 'c1',
      purpose: '最终回答',
      modelLabel: '模型 A',
      inputTokens: 5_120,
      outputTokens: 264,
      elapsedLabel: '14 秒',
    },
  ],
}

const memoryHit: MemoryRowView = {
  retrieval: {
    outcome: 'hit',
    sources: [
      {
        kind: 'workspace-file',
        reference: 'local-agent-web/ARCHITECTURE.md:15.6',
        usage: '沿用既有口径，未改写结论',
      },
      { kind: 'long-term-memory', reference: '记忆 m-2412 · 写入于 09-20', usage: '沿用上次定下的命名' },
      { kind: 'session-history', reference: '会话「压缩阈值重构」第 4 轮', usage: '参考同类改动的写法' },
    ],
    note: '只显示来源定位与用途：不展示正文、向量或内部评分。',
  },
  write: { outcome: 'written', detail: '1 条 · 来源：本轮对话' },
}

const memoryMiss: MemoryRowView = {
  retrieval: {
    outcome: 'miss',
    note: '本轮没有可引用的工作区文件或长期记忆。回答只基于当前对话，不会为来源留空位，也不会编造出处。',
  },
  write: { outcome: 'failed', detail: '记忆存储不可用 · 不影响本轮回答，也未写入任何内容' },
}

const memoryFailed: MemoryRowView = {
  retrieval: {
    outcome: 'failed',
    reason: '本地检索索引不可用（打开索引失败）',
    note: '本轮按无检索继续：回答中没有引用来源。恢复索引后可重试，重试前不会声称用过检索。',
  },
  write: null,
}

/** Retrieval hit: every source is shown as a locator, never as content. */
export const memoryHitScenario: ConversationScenario = {
  id: 'memory-hit',
  sessionId: 's-memory',
  purpose: '检索与记忆·命中：来源类型 + 安全引用（只给定位，不给正文/向量/评分）',
  session: {
    title: '检索与记忆的三种结果',
    workspace: 'local-agent-web',
    status: 'completed',
    modelLabel: '模型 A · 上下文 128k',
  },
  messages: [
    { kind: 'user', id: 'u1', text: '把上下文计量的口径写进架构文档。', at: '09:20' },
    { kind: 'memory', id: 'm1', memory: memoryHit },
    {
      kind: 'assistant',
      id: 'a1',
      text: '已在 §15.6 补上口径说明，并保持了此前的命名方式。三处来源只用作定位，正文没有进入回答上下文之外的地方。',
      at: '09:20',
    },
  ],
  plan: null,
  stats: retrievalStats,
  context: { usedTokens: 96_000, windowTokens: 128_000, scope: 'current' },
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
}

/** Retrieval miss plus a failed memory write: both non-ideal states at once. */
export const memoryMissScenario: ConversationScenario = {
  id: 'memory-miss',
  sessionId: 's-memory',
  purpose: '检索与记忆·未命中 + 写入失败：不编造来源，写入失败不影响本轮回答',
  session: {
    title: '检索与记忆的三种结果',
    workspace: 'local-agent-web',
    status: 'completed',
    modelLabel: '模型 A · 上下文 128k',
  },
  messages: [
    { kind: 'user', id: 'u1', text: '我们之前定过这个字段的命名吗？', at: '09:24' },
    { kind: 'memory', id: 'm1', memory: memoryMiss },
    {
      kind: 'assistant',
      id: 'a1',
      text: '没有找到可引用的记录，所以我按当前对话回答：这里还没有约定，建议用 `run_id`。这条结论没有历史依据，需要你确认。',
      at: '09:24',
    },
  ],
  plan: null,
  stats: retrievalStats,
  context: { usedTokens: 92_000, windowTokens: 128_000, scope: 'current' },
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
}

/** Retrieval could not run: it must not be reported as "found nothing". */
export const memoryFailedScenario: ConversationScenario = {
  id: 'memory-failed',
  sessionId: 's-memory',
  purpose: '检索与记忆·检索失败：与"未命中"区分开，说明本轮没有检索依据',
  session: {
    title: '检索与记忆的三种结果',
    workspace: 'local-agent-web',
    status: 'completed',
    modelLabel: '模型 A · 上下文 128k',
  },
  messages: [
    { kind: 'user', id: 'u1', text: '按项目约定重构这个模块。', at: '09:31' },
    { kind: 'memory', id: 'm1', memory: memoryFailed },
    {
      kind: 'assistant',
      id: 'a1',
      text: '由于检索索引不可用，这次我按当前对话里的信息重构，没有引用任何历史约定。恢复索引后可以重跑。',
      at: '09:31',
    },
  ],
  plan: null,
  stats: retrievalStats,
  context: { usedTokens: 88_000, windowTokens: 128_000, scope: 'current' },
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
}

/* ---------------------------------------------------------------------------
 * Empty / loading / disabled shell states (matrix 01, 02, 06).
 * ------------------------------------------------------------------------- */

/**
 * No workspace selected: the composer is disabled and says why.
 *
 * `sessionId` is null because there is no session on screen — highlighting a row
 * would claim the shell had opened something.
 */
export const emptyNoWorkspaceScenario: ConversationScenario = {
  id: 'empty-no-workspace',
  sessionId: null,
  purpose: '空会话·未选工作区：输入区禁用并说明原因，侧栏不高亮任何会话',
  session: { title: '未选择工作区', workspace: null, status: 'idle', modelLabel: '模型 A · 上下文 128k' },
  messages: [],
  plan: null,
  stats: null,
  context: { usedTokens: null, windowTokens: 128_000, scope: 'unknown' },
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
  empty: {
    title: '先选择一个工作区',
    description: '从左侧工作区里选一个会话，或在某个工作区下新建会话。未选择工作区时，输入区不会向模型发送任何内容。',
  },
  composer: {
    disabledReason: '未选择工作区：发送已禁用，避免在没有工作区边界的情况下执行工具。',
    phase: null,
  },
}

/** Workspace selected, new session, nothing said yet. */
export const emptySessionScenario: ConversationScenario = {
  id: 'empty-session',
  sessionId: 's-new',
  purpose: '空会话·已选工作区：空态标题 + 可用输入区，计量器显示"尚未组装"',
  session: {
    title: '新会话',
    workspace: 'local-agent-web',
    status: 'idle',
    modelLabel: '模型 A · 上下文 128k',
  },
  messages: [],
  plan: null,
  stats: null,
  context: { usedTokens: null, windowTokens: 128_000, scope: 'unknown' },
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
  empty: {
    title: '开始一个新会话',
    description: '描述你想完成的事即可。需要工具或文件改动时，会先给出计划，涉及危险操作会先请求你的确认。',
  },
  composer: { disabledReason: null, phase: null },
}

/** Running with nothing to show yet: phase above the input, stop instead of send. */
export const runningEmptyScenario: ConversationScenario = {
  id: 'running-empty',
  sessionId: 's-running',
  purpose: '会话运行中（无内容）：阶段态 + 可写草稿但暂时禁用发送（v0.4 没有取消接口，不渲染停止按钮）',
  session: {
    title: '检查可选依赖口径',
    workspace: 'local-agent-web',
    status: 'running',
    modelLabel: '模型 A · 上下文 128k',
  },
  messages: [],
  plan: null,
  stats: null,
  context: { usedTokens: null, windowTokens: 128_000, scope: 'unknown' },
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
  // "Sending is blocked while a turn runs" is derived from `status: 'running'`,
  // so the sentence lives in one place (see `Composer.SEND_BLOCKED_TEXT`).
  composer: { disabledReason: null, phase: '正在分析你的请求…' },
}

/** Events still being read: snapshot first, then the events after it. */
export const loadingHistoryScenario: ConversationScenario = {
  id: 'loading-history',
  sessionId: 's-docs',
  purpose: '加载中：读取会话事件（快照优先），骨架只表示形状，不预填内容',
  session: {
    title: '整理 v0.4 上位文档',
    workspace: 'chaiai',
    status: 'idle',
    modelLabel: '模型 A · 上下文 128k',
  },
  messages: [],
  plan: null,
  stats: null,
  context: { usedTokens: null, windowTokens: null, scope: 'unknown' },
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
  loading: {
    label: '正在读取会话事件…',
    detail:
      '快照优先：先渲染最近快照，再补齐其后的事件。读取期间不会重放模型调用、工具执行或 Graph 副作用，也不会为了填充界面而显示占位内容。',
    rows: [92, 78, 85, 64, 88, 42],
  },
}

/* ---------------------------------------------------------------------------
 * T26 收尾 B: matrix 22/23 (context meter) and 24 (collapsed sidebar).
 * ------------------------------------------------------------------------- */

/** Matrix 23: the meter's 口径 pinned open instead of shown on hover. */
export const meterOpenScenario: ConversationScenario = {
  id: 'meter-open',
  sessionId: 's-shell',
  purpose: '计量器展开：精确读数 + 三态口径 + 窗口必须被确认、缓存只在同一连接版本下比较',
  session: shellScenario.session,
  messages: shellScenario.messages,
  plan: shellScenario.plan,
  stats: shellScenario.stats,
  context: shellScenario.context,
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
  composer: { disabledReason: null, phase: null, meterOpen: true },
}

/**
 * Matrix 24: the resident shell in its narrow state.
 *
 * The tree is not rendered while collapsed — nothing is hidden-but-selected, so
 * there is no selection for the header to contradict.
 */
export const sidebarCollapsedScenario: ConversationScenario = {
  id: 'sidebar-collapsed',
  sessionId: 's-shell',
  purpose: '侧栏收起：窄轨保留品牌、新建、展开与设置，不渲染会话树',
  session: shellScenario.session,
  messages: shellScenario.messages,
  plan: shellScenario.plan,
  stats: shellScenario.stats,
  context: shellScenario.context,
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
  sidebarCollapsed: true,
}

const sessionBusy: ErrorNoticeView = {
  code: 'SESSION_BUSY',
  category: 'system',
  severity: 'warning',
  title: '该会话已有正在运行的一轮',
  message: '本次提交未被接受：同一会话同一时刻只允许一轮运行，请求不会排队。',
  // No "stop it" step: v0.4 exposes no cancel interface, so offering one would
  // promise a control that does not exist (ARCHITECTURE §15.2).
  hint: '等待当前一轮结束后再提交这条内容；v0.4 不公开取消接口，因此没有可以中止它的按钮。',
  retryable: false,
  blockedReason: '当前一轮结束前不能重发，也不会进入队列，且无法中止。',
  diagnosticId: 'err-7b21c4',
}

const idempotencyConflict: ErrorNoticeView = {
  code: 'IDEMPOTENCY_CONFLICT',
  category: 'system',
  severity: 'error',
  title: '同一请求标识的内容不一致',
  message: '这个请求标识此前已用于另一次提交，且内容不同，因此本次提交被拒绝，未执行任何工具。',
  hint: '先在历史里确认上一次提交的结果，再把它当作新的请求发送；不要改写后沿用同一个请求标识重发。',
  retryable: false,
  blockedReason: '内容不一致，必须先确认上一次的结果，不能直接重发。',
  diagnosticId: 'err-c40f19',
}

const recoveryRequired: ErrorNoticeView = {
  code: 'RECOVERY_REQUIRED',
  category: 'recovery',
  severity: 'warning',
  title: '需要先完成上一次运行的恢复',
  message: '上一个运行在中断后留下了未收尾的记录。无法证明它的工具副作用与终态，因此不会自动重提同一输入。',
  // 人工核对，不是"自动补齐"：无法证明终态时绝不重提输入或再次执行工具
  // （ARCHITECTURE §15.2 RECOVERY_REQUIRED）。
  hint: '人工核对上一次运行的记录：确认没有未收尾的副作用后，再把它作为新的请求重新提交。应用不会自动补执行未完成的步骤。',
  retryable: false,
  blockedReason: '需要人工核对上一次运行的记录；不会自动重提或再次执行工具。',
  diagnosticId: 'err-1a8d63',
}

/**
 * Server-side conflict codes (plan §6.4, matrix §5).
 *
 * Three codes, three different next steps. None of them may fall back to one
 * shared sentence — a busy session, a conflicting replay and an unfinished run
 * are not the same problem, and only the last one is a "配置" issue at all.
 */
export const errorCodesScenario: ConversationScenario = {
  id: 'error-codes',
  sessionId: 's-errors',
  purpose: '服务端冲突码三种：SESSION_BUSY / IDEMPOTENCY_CONFLICT / RECOVERY_REQUIRED 的可执行性各不相同',
  session: {
    title: '连接故障排查',
    workspace: 'local-agent-web',
    status: 'idle',
    modelLabel: '模型 A · 上下文 128k',
  },
  messages: [
    { kind: 'user', id: 'u1', text: '继续上一轮，把 dist 清掉。', at: '11:20' },
    { kind: 'error', id: 'e1', notice: sessionBusy },
    { kind: 'user', id: 'u2', text: '那我改一下再发一次。', at: '11:21' },
    { kind: 'error', id: 'e2', notice: idempotencyConflict },
    { kind: 'user', id: 'u3', text: '重新开始吧。', at: '11:23' },
    { kind: 'error', id: 'e3', notice: recoveryRequired },
  ],
  plan: null,
  stats: null,
  context: { usedTokens: null, windowTokens: 128_000, scope: 'unknown' },
  takeover: { kind: 'none' },
  settings: null,
  modelMenu: null,
}

export const scenarios: readonly ConversationScenario[] = [
  shellScenario,
  diffScenario,
  errorsScenario,
  toolsScenario,
  traceScenario,
  approvalScenario,
  approvalExpiredScenario,
  settingsModelsScenario,
  settingsEmptyScenario,
  modelMenuScenario,
  memoryHitScenario,
  memoryMissScenario,
  memoryFailedScenario,
  emptyNoWorkspaceScenario,
  emptySessionScenario,
  runningEmptyScenario,
  loadingHistoryScenario,
  meterOpenScenario,
  sidebarCollapsedScenario,
  errorCodesScenario,
]

export function findScenario(id: string | null): ConversationScenario {
  return scenarios.find((scenario) => scenario.id === id) ?? diffScenario
}
