import { describe, expect, it } from 'vitest'

import type { components } from '../../api/schema.gen'
import {
  durationLabel,
  mapApproval,
  mapContext,
  mapError,
  collapseDuplicateErrors,
  mapMessages,
  mapPlan,
  mapSession,
  mapSessionState,
  mapStats,
  mapTurnStats,
  mapTool,
  modelLabelFor,
  timeLabel,
} from './adapters'

type Snapshot = components['schemas']['SessionSnapshot']

/** 只造用例真正读到的字段；其余按契约必填项补上（映射层不会碰它们）。 */
function snapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    schema_version: 1,
    snapshot_id: 'snap-1',
    session_id: 's-1',
    workspace: { workspace_id: 'ws-1', display_name: 'local-agent-web' },
    title: '会话标题',
    session_state: 'idle',
    created_at: '2026-09-27T10:00:00+00:00',
    messages: [],
    ...overrides,
  } as Snapshot
}

const userMessage = (id: string, text: string) => ({
  kind: 'user' as const,
  message_id: id,
  text,
  created_at: '2026-09-27T10:01:00+00:00',
})

const assistantMessage = (id: string, text: string, complete: boolean) => ({
  chunk_sequence: 0, stream_channel: 'text' as const, reasoning_truncated: false,
  kind: 'assistant' as const,
  message_id: id,
  text,
  complete,
  model_call_id: 'call-1',
  created_at: '2026-09-27T10:01:05+00:00',
})

const toolMessage = (
  status: components['schemas']['ToolUseView']['status'],
  tool: Partial<components['schemas']['ToolUseView']> = {},
) => ({
  kind: 'tool' as const,
  message_id: 't-1',
  tool: {
    tool_call_id: 'call-9',
    tool_name: 'read_file',
    status,
    redacted_arguments: { path: 'src/app.ts' },
    approval_id: null,
    output_preview: 'first line',
    output_digest: null,
    truncated: false,
    started_at: '2026-09-27T10:02:00+00:00',
    finished_at: null,
    elapsed_ms: 1500,
    ...tool,
  },
})

describe('mapSessionState', () => {
  it('does not claim a failed turn is still running', () => {
    // 视图模型只有四态；把 `failed` 显示成 running 会让界面谎称有任务在进行。
    expect(mapSessionState('failed')).toBe('idle')
  })

  it('does not claim a blocked turn is still running either', () => {
    // `blocked`：本轮已结束、在等用户补充（工具被拒、循环上限、未确认副作用）。
    // 归成 running 同样会让界面谎称有任务在进行——而"永远停在正在生成回答…"正是这次修的症状。
    expect(mapSessionState('blocked')).toBe('idle')
  })

  it('passes the states the view model does have straight through', () => {
    expect(mapSessionState('running')).toBe('running')
    expect(mapSessionState('awaiting_approval')).toBe('awaiting-approval')
    expect(mapSessionState('completed')).toBe('completed')
  })
})

describe('mapTool', () => {
  it('never presents a denied call as a success', () => {
    expect(mapTool(toolMessage('denied').tool).status).toBe('failed')
    expect(mapTool(toolMessage('error').tool).status).toBe('failed')
    expect(mapTool(toolMessage('ok').tool).status).toBe('succeeded')
  })

  it('keeps a queued call distinct from an executing call', () => {
    expect(mapTool(toolMessage('pending').tool).status).toBe('pending')
  })

  it('shows the command before any outcome has arrived', () => {
    const row = mapTool(toolMessage('pending', {
      tool_name: 'run_shell', redacted_arguments: { command: 'python -u example.py', cwd: 'src' },
      output_preview: null, outcome: null,
    }).tool)
    expect(row.target).toBe('python -u example.py')
    expect(row.card).toMatchObject({
      kind: 'terminal', command: 'python -u example.py', cwd: 'src', output: '', exitCode: null,
    })
  })

  it('carries the redacted arguments, the duration and the truncated output', () => {
    const row = mapTool(toolMessage('ok').tool)

    expect(row.summary).toBe('path=src/app.ts')
    expect(row.durationLabel).toBe('1.5s')
    expect(row.card).toMatchObject({ kind: 'generic', detail: 'first line' })
    expect(row.target).toBeNull() // 没有结构化结果就不猜目标路径
  })

  it('takes the delegated children from the contract instead of parsing the report', () => {
    const row = mapTool(toolMessage('ok', {
      tool_name: 'delegate_research',
      outcome: {
        kind: 'delegation', background: false,
        summary: '2 个子任务 · 1 个已完成',
        text: '已核对的子任务原文：\n# 取消（docs/cancel.md:1）',
        children: [
          {
            scope: 'docs', status: 'completed', tool_calls: 4, model_calls: 1,
            evidence_count: 6, error: null,
          },
          {
            scope: 'src', status: 'failed', tool_calls: 0, model_calls: 1,
            evidence_count: 0, error: '子任务超时',
          },
        ],
        truncated: false,
      },
    }).tool)

    expect(row.card).toMatchObject({
      kind: 'delegation',
      // 下划线 → 驼峰只在映射层发生一次；计数原样搬运，缺项按 0（契约的默认读法）。
      children: [
        { scope: 'docs', status: 'completed', toolCalls: 4, evidenceCount: 6, error: null },
        { scope: 'src', status: 'failed', toolCalls: 0, evidenceCount: 0, error: '子任务超时' },
      ],
      report: '已核对的子任务原文：\n# 取消（docs/cancel.md:1）',
    })
  })

  it('keeps the structured payload, result preview and timings for the trace view', () => {
    const row = mapTool(
      toolMessage('ok', {
        redacted_arguments: { command: 'pytest -q', cwd: '.' },
        output_preview: 'first line\nsecond line',
        truncated: true,
        started_at: '2026-09-27T10:02:00+00:00',
        finished_at: '2026-09-27T10:02:02+00:00',
      }).tool,
    )

    // 结构化参数与压平后的 `summary` 并存：轨迹检查器要键值对，不从 summary 反解。
    expect(row.parameters).toEqual([
      { name: 'command', value: 'pytest -q' },
      { name: 'cwd', value: '.' },
    ])
    expect(row.summary).toBe('command=pytest -q · cwd=.')
    // 有 outcome 时预览不再进卡片，但轨迹仍要能显示它，所以这里单独保留。
    expect(row.resultPreview).toBe('first line\nsecond line')
    expect(row.resultTruncated).toBe(true)
    expect(row.resultDigest).toBeNull()
    expect(row.toolCallId).toBe('call-9')
    expect(row.startedLabel).not.toBe('')
    expect(row.finishedLabel).not.toBe('')
  })

  it('leaves an unreported result digest as null instead of inventing one', () => {
    const row = mapTool(toolMessage('ok').tool)
    expect(row.resultDigest).toBeNull()
    expect(row.parameters).toEqual([{ name: 'path', value: 'src/app.ts' }])
  })

  it('renders a read result as a read card and surfaces the normalised target', () => {
    const row = mapTool(
      toolMessage('ok', {
        outcome: {
          kind: 'read', background: false,
          target: 'src/app.ts',
          text: 'a\nb\nc',
          total: 3,
          truncated: false,
        },
      }).tool,
    )

    expect(row.target).toBe('src/app.ts')
    expect(row.card).toMatchObject({ kind: 'read', path: 'src/app.ts', totalLines: 3, truncated: false })
    const texts = row.card.kind === 'read' ? row.card.lines.map((line) => line.text) : []
    expect(texts).toEqual(['a', 'b', 'c'])
  })

  it('numbers a windowed read from the line the server actually started at', () => {
    // v0.8 T73（§19.3）：`first_line` 是"这段正文从文件第几行开始"。少了它，页面会把
    // 从第 200 行起的窗口编号成 1 起——与 §6.7 落盘文件（`路径:行号:内容`）对不上。
    const row = mapTool(
      toolMessage('ok', {
        outcome: {
          kind: 'read', background: false,
          target: 'big.txt',
          text: 'a\nb',
          total: 1000,
          first_line: 200,
          truncated: false,
        },
      }).tool,
    )

    expect(row.card).toMatchObject({ kind: 'read', firstLine: 200 })
    const numbers = row.card.kind === 'read' ? row.card.lines.map((line) => line.number) : []
    expect(numbers).toEqual([200, 201])
  })

  it('falls back to numbering from the first line when the payload has no window start', () => {
    // 取不到就是取不到：退回从 1 起（与从前逐字相同），不编一个"看起来对"的行号。
    const row = mapTool(
      toolMessage('ok', {
        outcome: { kind: 'read', background: false, target: 'a.txt', text: 'a', total: 1, truncated: false },
      }).tool,
    )

    expect(row.card.kind === 'read' ? row.card.firstLine : 'unexpected').toBeUndefined()
    expect(row.card.kind === 'read' ? row.card.lines[0]?.number : 0).toBe(1)
  })

  it('splits stdout, stderr and the exit code instead of merging them into one blob', () => {
    const row = mapTool(
      toolMessage('ok', {
        tool_name: 'run_shell',
        redacted_arguments: { command: 'pytest -q' },
        outcome: {
          kind: 'shell', background: false,
          target: '.',
          summary: '退出码 1',
          stdout: 'out',
          stderr: 'err',
          exit_code: 1,
          timed_out: false,
          truncated: false,
        },
      }).tool,
    )

    expect(row.card).toMatchObject({
      kind: 'terminal',
      command: 'pytest -q',
      cwd: '.',
      exitCode: 1,
      stdout: 'out',
      stderr: 'err',
    })
  })

  it('carries the server-side truncation of a shell body into the card', () => {
    // §19.3：服务端把输出截断过就要在页面上说出来。此前 shell 卡不带这个事实，
    // 于是"这段 stdout 只是有界预览"在页面上**无从得知**。
    const row = mapTool(
      toolMessage('ok', {
        tool_name: 'run_shell',
        redacted_arguments: { command: 'pytest -q' },
        outcome: {
          kind: 'shell', background: false,
          target: '.',
          summary: '退出码 1',
          stdout: 'out',
          stderr: 'err',
          exit_code: 1,
          timed_out: false,
          truncated: true,
        },
      }).tool,
    )

    expect(row.card).toMatchObject({ kind: 'terminal', truncated: true })
  })

  it('gives a web result its own card instead of the generic placeholder', () => {
    // 2026-10-26：网页工具此前不在服务端的识别表里，卡片只剩"该结果格式暂无可展示的详情"。
    // 现在服务端给出搜索词、条数与有界正文，页面只照搬，不在这里重新解析 URL 列表。
    const row = mapTool(
      toolMessage('ok', {
        tool_name: 'web_search',
        outcome: {
          kind: 'web', background: false,
          target: 'MCP',
          summary: '2 条来源 · 后端 deepseek',
          text: 'MCP 文档 · modelcontextprotocol.io',
          total: 2,
          truncated: false,
        },
      }).tool,
    )

    expect(row.card).toMatchObject({
      kind: 'web',
      target: 'MCP',
      summary: '2 条来源 · 后端 deepseek',
      body: 'MCP 文档 · modelcontextprotocol.io',
    })
  })

  it('renders a mutation as a result card with the summary and the bounded body', () => {
    const row = mapTool(
      toolMessage('ok', {
        tool_name: 'edit_file',
        outcome: {
          kind: 'edit', background: false,
          target: 'src/app.ts',
          summary: '替换 1 处 · 2 行变化',
          text: '@@ -1 +1 @@',
          truncated: true,
        },
      }).tool,
    )

    expect(row.card).toEqual({
      kind: 'result',
      target: 'src/app.ts',
      summary: '替换 1 处 · 2 行变化',
      body: '@@ -1 +1 @@',
      truncated: true,
    })
  })

  it('renders a structured edit diff as a file-mutation card instead of parsing text', () => {
    const row = mapTool(
      toolMessage('ok', {
        tool_name: 'edit_file',
        outcome: {
          kind: 'edit', background: false,
          target: 'src/app.ts',
          summary: '替换 1 处 · 2 行变化',
          text: '@@ -1,2 +1,2 @@',
          truncated: false,
          diff: {
            path: 'src/app.ts',
            additions: 1,
            deletions: 1,
            hunks: [
              {
                header: '@@ -1,2 +1,2 @@',
                lines: [
                  { kind: 'del', old_line: 1, new_line: null, text: 'const a = 1' },
                  { kind: 'add', old_line: null, new_line: 1, text: 'const a = 2' },
                  { kind: 'context', old_line: 2, new_line: 2, text: 'export {}' },
                ],
              },
            ],
          },
        },
      }).tool,
    )

    // 服务端已经解析好了：适配层只搬字段（snake → camel），不重新读那段 diff 文本。
    expect(row.card).toEqual({
      kind: 'file-mutation',
      diff: {
        path: 'src/app.ts',
        additions: 1,
        deletions: 1,
        hunks: [
          {
            header: '@@ -1,2 +1,2 @@',
            lines: [
              { kind: 'del', oldLine: 1, newLine: null, text: 'const a = 1' },
              { kind: 'add', oldLine: null, newLine: 1, text: 'const a = 2' },
              { kind: 'context', oldLine: 2, newLine: 2, text: 'export {}' },
            ],
          },
        ],
      },
    })
  })

  it('carries the mechanical evidence once the reflection layer settled the call', () => {
    const row = mapTool(
      toolMessage('ok', {
        evidence: { summary: '读取结果已记录', verified: true },
      }).tool,
    )

    expect(row.evidence).toEqual({ evidenceType: '', summary: '读取结果已记录', verified: true })
  })

  it('leaves evidence null while the call is still unsettled', () => {
    expect(mapTool(toolMessage('pending').tool).evidence).toBeNull()
  })
})

describe('mapPlan', () => {
  it('returns null when the session has no plan rather than an empty strip', () => {
    expect(mapPlan(null)).toBeNull()
    expect(mapPlan(undefined)).toBeNull()
  })

  it('renames the step fields and keeps the status the graph reported', () => {
    expect(
      mapPlan({
        goal: '修好构建',
        steps: [
          { step_id: 's1', title: '定位', status: 'completed' },
          { step_id: 's2', title: '修复', status: 'running' },
        ],
      }),
    ).toEqual({
      goal: '修好构建',
      steps: [
        { id: 's1', title: '定位', status: 'completed' },
        { id: 's2', title: '修复', status: 'running' },
      ],
    })
  })
})

describe('mapApproval', () => {
  const pending = {
    approval_id: 'ap-1',
    run_id: 'run-1',
    tool_call_id: 't1',
    tool_name: 'delete_file',
    redacted_arguments: { path: 'dist', recursive: 'True' },
    edited_arguments: {},
    target: 'dist',
    outside_workspace: false,
    confirmation_stage: 1,
    impact_summary: '删除 dist（递归删除）',
    risk: 'high',
    state: 'pending',
    created_at: '2026-09-27T10:00:00+00:00',
    note: null,
  } satisfies components['schemas']['ApprovalView']

  it('carries the decision into the card and shows the parameters it applies to', () => {
    expect(mapApproval(pending)).toEqual({
      approvalId: 'ap-1',
      action: 'delete_file',
      summary: '删除 dist（递归删除）',
      target: 'dist',
      outsideWorkspace: false,
      confirmationStage: 1,
      // 后端没有结构化的影响清单：留空，而不是把那句摘要拆成条目（那是前端解析自然语言）。
      impact: [],
      risk: 'high',
      state: 'pending',
      note: null,
      parameters: [
        { name: 'path', value: 'dist' },
        { name: 'recursive', value: 'True' },
      ],
    })
  })

  it('leaves an unknown target empty and keeps the refusal note verbatim', () => {
    const expired = mapApproval({
      ...pending,
      target: null,
      state: 'expired',
      note: '该审批已失效，未执行任何操作。请重新发起请求。',
    })

    // 投影层给不出目标时如实留空，不从参数或摘要里猜一个。
    expect(expired.target).toBe('')
    expect(expired.note).toBe('该审批已失效，未执行任何操作。请重新发起请求。')
  })
})

describe('mapMessages', () => {
  it('maps the snapshot rows in order', () => {
    const rows = mapMessages(
      snapshot({ messages: [userMessage('u-1', '你好'), assistantMessage('a-1', '你好呀', true)] }),
    )

    expect(rows.map((row) => row.kind)).toEqual(['user', 'assistant'])
    expect(rows[1]).toMatchObject({ id: 'a-1', text: '你好呀' })
  })

  it('renders the memory row from the contract instead of hiding it', () => {
    const rows = mapMessages(
      snapshot({
        messages: [
          {
            kind: 'memory',
            message_id: 'm-1',
            memory: {
              retrieval_outcome: 'hit',
              sources: [
                { kind: 'workspace_file', reference: 'src/app.ts:1-3', usage: '代码片段已注入本轮上下文' },
              ],
              retrieval_note: '只显示来源定位与用途。',
              write_outcome: 'written',
              write_detail: '1 条 · 来源：本轮对话',
            },
          },
        ] as unknown as Snapshot['messages'],
      }),
    )

    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      kind: 'memory',
      id: 'm-1',
      memory: {
        retrieval: {
          outcome: 'hit',
          sources: [{ kind: 'workspace-file', reference: 'src/app.ts:1-3' }],
        },
        write: { outcome: 'written', detail: '1 条 · 来源：本轮对话' },
      },
    })
  })

  it('keeps "could not look" apart from "found nothing"', () => {
    // 失败与未命中在契约里是两个取值：混说等于告诉用户"确实没有"，而其实没查到。
    const rows = mapMessages(
      snapshot({
        messages: [
          {
            kind: 'memory',
            message_id: 'm-1',
            memory: {
              retrieval_outcome: 'failed',
              sources: [],
              retrieval_note: '本轮没有成功检索。',
            },
          },
        ] as unknown as Snapshot['messages'],
      }),
    )

    const memory = rows[0]
    expect(memory.kind === 'memory' ? memory.memory.retrieval : null).toEqual({
      outcome: 'failed',
      reason: '本轮没有成功检索。',
      note: '',
    })
  })

  it('appends the streaming tail while the answer is still incomplete', () => {
    const rows = mapMessages(
      snapshot({ messages: [assistantMessage('a-1', '', false)] }),
      { messageId: 'a-1', modelCallId: 'call-1', text: '正在', sequence: 2 },
    )

    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ kind: 'assistant', id: 'a-1', text: '正在' })
  })

  it('drops the tail once the snapshot carries the complete message', () => {
    // 停止条件"临时 delta 与最终消息不得重复显示"：快照已是完整消息，尾部必须消失。
    const rows = mapMessages(
      snapshot({ messages: [assistantMessage('a-1', '你好呀', true)] }),
      { messageId: 'a-1', modelCallId: 'call-1', text: '你好呀', sequence: 2 },
    )

    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ text: '你好呀' })
  })

  it('does not render an empty bubble for a tail that has no text yet', () => {
    const rows = mapMessages(snapshot(), { messageId: 'a-2', modelCallId: null, text: '', sequence: 0 })

    expect(rows).toEqual([])
  })
})

describe('mapError', () => {
  it('merges identical notices within a turn, preserving different errors and later turns', () => {
    const notice = mapError({ code: 'RUN_STAGE_FAILED', category: 'system', severity: 'error',
      title: '失败', message: '证据未确认', hint: '', retryable: false, blocked_reason: null,
      retry_condition: null, diagnostic_id: 'diag', occurred_at: '2026-10-07T00:00:00Z', source: 'run' })
    const error = { kind: 'error' as const, id: 'one', notice }
    const user = { kind: 'user' as const, id: 'u', text: '再问', at: '' }
    const different = { ...error, id: 'different', notice: { ...notice, message: '不同原因' } }
    expect(collapseDuplicateErrors([error, { ...error, id: 'two' }, different, user, { ...error, id: 'later' }])
      .map((row) => row.id)).toEqual(['one', 'different', 'u', 'later'])
  })
  it('renames the contract fields without reinterpreting them', () => {
    const notice = mapError({
      code: 'MODEL_AUTH_FAILED',
      category: 'model',
      severity: 'error',
      title: '模型拒绝了这个密钥',
      message: '供应商返回鉴权失败。',
      hint: '到设置里重新填一次密钥。',
      retryable: false,
      blocked_reason: '先处理配置',
      retry_condition: null,
      diagnostic_id: 'diag-1',
      occurred_at: '2026-09-27T10:00:00+00:00',
      source: 'model',
    })

    expect(notice).toMatchObject({
      code: 'MODEL_AUTH_FAILED',
      category: 'model',
      severity: 'error',
      blockedReason: '先处理配置',
      diagnosticId: 'diag-1',
    })
  })
})

describe('mapContext', () => {
  it('leaves unknown numbers unknown instead of estimating them', () => {
    expect(mapContext(snapshot())).toEqual({ usedTokens: null, windowTokens: null, scope: 'unknown' })
  })

  it('passes the contract numbers through', () => {
    const meter = mapContext(
      snapshot({ context: { scope: 'current', used_tokens: 1234, window_tokens: 65536 } }),
    )

    expect(meter).toEqual({ usedTokens: 1234, windowTokens: 65536, scope: 'current' })
  })
})

describe('mapStats', () => {
  const call = (overrides: Partial<components['schemas']['ModelCallRecordView']> = {}) => ({
    model_call_id: 'run-1:model:1',
    run_id: 'run-1',
    purpose: 'chat',
    connection_id: 'mc-1',
    config_version: '1',
    provider: 'openai_compatible',
    model_id: 'deepseek-chat',
    endpoint_fingerprint: 'fp',
    status: 'ok',
    started_at: '2026-09-27T10:00:00+00:00',
    cache_metrics_reported: false,
    actual_uncached_input_tokens: 600,
    actual_cache_read_tokens: 400,
    actual_cache_write_tokens: null,
    actual_output_tokens: 20,
    model_elapsed_ms: 1200,
    ...overrides,
  }) as components['schemas']['ModelCallRecordView']

  const turn = (overrides: Partial<components['schemas']['TurnRecordView']> = {}) =>
    ({
      run_id: 'run-1',
      submitted_at: '2026-09-27T10:00:00+00:00',
      final_state: 'completed',
      usage_recorded: true,
      cache_metrics_reported: true,
      actual_uncached_input_tokens: 600,
      actual_cache_read_tokens: 400,
      actual_output_tokens: 20,
      turn_elapsed_ms: 5000,
      estimated_prompt_tokens: 900,
      ...overrides,
    }) as components['schemas']['TurnRecordView']

  it('renders no statistics line before the turn has any usage', () => {
    // 这一轮还没开始：不显示统计行，更不显示一行 0。
    expect(mapStats(snapshot())).toBeNull()
    expect(mapStats(snapshot({ usage: null }))).toBeNull()
  })

  it('carries the recorded numbers and the per-call breakdown', () => {
    const stats = mapStats(
      snapshot({
        usage: turn(),
        plan: {
          goal: '改配置',
          steps: [
            { step_id: 's1', title: '定位', status: 'completed' },
            { step_id: 's2', title: '修改', status: 'running' },
          ],
        },
        model_calls: [call(), call({ model_call_id: 'run-1:model:2', purpose: 'tool_decision' })],
      }),
    )

    expect(stats).toEqual({
      turns: 1,
      steps: 2,
      cacheHitRatio: null, // 供应商没报写缓存：命中率未知，不写 0
      inputTokens: 600,
      outputTokens: 20,
      // 整轮耗时改用人读格式（2026-10-22）：>60s 写"X 分 Y 秒"。
      elapsedLabel: '5 秒',
      // 2026-09-30 新增的四项耗时/吞吐：这一支的 `turn()` 没有给这些字段，所以标签是空串、
      // 吞吐是 `null`（缺分母就不算），而不是 0。
      modelElapsedLabel: '',
      toolElapsedLabel: '',
      firstTokenLabel: '',
      throughput: null,
      cacheReported: true,
      // 2026-10-16 新增：上下文占用与终态（轨迹页按轮显示用）。
      contextUsedTokens: null,
      contextWindowTokens: null,
      finalState: 'completed',
      estimated: false,
      calls: [
        {
          id: 'run-1:model:1',
          purpose: '闲聊回答',
          modelLabel: 'deepseek-chat',
          inputTokens: 600,
          outputTokens: 20,
          elapsedLabel: '1.2s',
          schemaFingerprint: null, // 这一支没记录工具 Schema：未知，不编指纹
          // 2026-10-16 新增的四项逐次账目：这一支的 `call()` 只给了 cache_read/uncached，
          // 其余为 `null`（未上报）或空串（没耗时），都不是 0。
          cacheReadTokens: 400,
          cacheWriteTokens: null,
          reasoningTokens: null,
          firstTokenLabel: '',
          // 2026-10-16 新增的原始时刻（时间轴用）：`call()` 只给了 `started_at`，
          // 首字与结束时刻缺席 ⇒ 空串（页面不画那一段，不拿别的时刻凑）。
          startedAt: '2026-09-27T10:00:00+00:00',
          firstTokenAt: '',
          finishedAt: '',
        },
        {
          id: 'run-1:model:2',
          purpose: '工具决策',
          modelLabel: 'deepseek-chat',
          inputTokens: 600,
          outputTokens: 20,
          elapsedLabel: '1.2s',
          schemaFingerprint: null,
          cacheReadTokens: 400,
          cacheWriteTokens: null,
          reasoningTokens: null,
          firstTokenLabel: '',
          startedAt: '2026-09-27T10:00:00+00:00',
          firstTokenAt: '',
          finishedAt: '',
        },
      ],
    })
  })

  it('derives model/tool/first-token labels and throughput from the contract numbers', () => {
    // 2026-09-30：这四项**契约里本来就有**（§15.6 的 `TurnRecordView`），此前视图模型把它们丢了。
    const stats = mapStats(
      snapshot({
        usage: turn({
          model_elapsed_ms: 2000,
          tool_elapsed_ms: 1500,
          time_to_first_token_ms: 800,
          actual_output_tokens: 500,
        }),
      }),
    )

    expect(stats?.modelElapsedLabel).toBe('2 秒')
    expect(stats?.toolElapsedLabel).toBe('1 秒')
    expect(stats?.firstTokenLabel).toBe('800ms')
    expect(stats?.throughput).toBe(250) // 500 tok ÷ 2s
  })

  it('leaves throughput unknown when the model time is missing', () => {
    // 分母缺失就不猜：算不出吞吐不是"0 tok/s"。
    const stats = mapStats(
      snapshot({ usage: turn({ model_elapsed_ms: null, actual_output_tokens: 500 }) }),
    )

    expect(stats?.throughput).toBeNull()
    expect(stats?.modelElapsedLabel).toBe('')
  })

  it('keeps unreported numbers unknown instead of zero', () => {
    const stats = mapStats(
      snapshot({
        usage: turn({ actual_uncached_input_tokens: null, actual_output_tokens: null }),
        model_calls: [call({ actual_uncached_input_tokens: null, actual_output_tokens: null })],
      }),
    )

    expect(stats?.inputTokens).toBeNull()
    expect(stats?.outputTokens).toBeNull()
    expect(stats?.calls[0].inputTokens).toBeNull()
    expect(stats?.calls[0].outputTokens).toBeNull()
  })

  it('labels the estimate when an old checkpoint has no records', () => {
    const stats = mapStats(
      snapshot({
        usage: turn({
          usage_recorded: false,
          actual_uncached_input_tokens: null,
          actual_cache_read_tokens: null,
          actual_output_tokens: null,
          estimated_prompt_tokens: 84_000,
        }),
        model_calls: [],
      }),
    )

    expect(stats?.estimated).toBe(true)
    expect(stats?.inputTokens).toBe(84_000)
    expect(stats?.outputTokens).toBeNull()
    expect(stats?.calls).toEqual([])
  })

  it('uses a public fallback for unfamiliar model purposes', () => {
    const stats = mapStats(snapshot({ usage: turn(), model_calls: [call({ purpose: 'brand_new' })] }))

    expect(stats?.calls[0].purpose).toBe('模型调用')
  })

  it('places persisted turn summaries after their own assistant messages', () => {
    const snap = snapshot({
      messages: [
        userMessage('u1', 'first'), assistantMessage('a1', 'one', true),
        userMessage('u2', 'legacy turn'), assistantMessage('a2', 'unrecorded', true),
        userMessage('u3', 'third'), assistantMessage('a3', 'three', true),
      ],
      usage: turn({ run_id: 'run-2' }),
      turn_usages: [
        { user_message_id: 'u1', usage: turn(), model_calls: [call()] },
        { user_message_id: 'u3', usage: turn({ run_id: 'run-2' }), model_calls: [call({ run_id: 'run-2' })] },
      ],
    })

    expect(mapTurnStats(snap, mapMessages(snap)).map((item) => item.afterMessageId)).toEqual(['a1', 'a3'])
  })

  it('puts the compaction row right after the turn it happened in, and the running one at the tail', () => {
    // 2026-10-16：压缩提示从输入区搬进对话流（与 Think、工具行同级）。位置有讲究：
    // 压缩发生在**这一轮的开头**（上下文到线才压），所以紧跟那轮的提问；而"正在压缩"还没有
    // Checkpoint 记录，只能放流尾。
    const snap = snapshot({
      messages: [userMessage('u1', 'first'), assistantMessage('a1', 'one', true)],
      turn_usages: [
        {
          user_message_id: 'u1',
          usage: turn(),
          model_calls: [],
          events: [
            {
              kind: 'compaction',
              event_id: 'run-1:compression:1',
              run_id: 'run-1',
              at: '2026-10-16T09:55:51Z',
              finished_at: null,
              label: '上下文压缩：折叠 151 条消息',
              status: null,
              wait_ms: null,
              folded_messages: 151,
              tokens_before: 149_231,
              tokens_after: 36_931,
            },
          ],
        },
      ],
    })

    const rows = mapMessages(snap)

    expect(rows.map((row) => row.kind)).toEqual(['user', 'compression', 'assistant'])
    const row = rows[1]
    expect(row.kind === 'compression' && row.compression.running).toBe(false)
    expect(row.kind === 'compression' && row.compression.foldedMessages).toBe(151)
  })

  it('appends the running compaction at the tail instead of guessing a turn', () => {
    const snap = snapshot({
      messages: [userMessage('u1', 'first'), assistantMessage('a1', 'one', true)],
      // 只给这一支真正会用到的字段：契约里 `scope` 是**必填**，`used_tokens`/`window_tokens` 可缺
      // （用 `...snapshot().context` 展开会在 `context` 为空时把必填项也变成可缺，`tsc` 会拦下）。
      context: {
        scope: 'current',
        used_tokens: 150_000,
        window_tokens: 200_000,
        compression: { operation_id: 'run-1:compression:2', run_id: 'run-1', state: 'running' },
      },
    })

    const rows = mapMessages(snap)

    expect(rows.map((row) => row.kind)).toEqual(['user', 'assistant', 'compression'])
    const row = rows[rows.length - 1]
    expect(row.kind === 'compression' && row.compression.running).toBe(true)
  })

  it('keeps previous turn statistics while withholding the active turn until it finishes', () => {
    const snap = snapshot({
      current_task: { run_id: 'run-2' },
      messages: [userMessage('u1', 'first'), assistantMessage('a1', 'one', true), userMessage('u2', 'second')],
      turn_usages: [
        { user_message_id: 'u1', usage: turn(), model_calls: [] },
        { user_message_id: 'u2', usage: turn({ run_id: 'run-2', final_state: 'running' }), model_calls: [] },
      ],
    })
    expect(mapTurnStats(snap, mapMessages(snap), true).map((item) => item.afterMessageId)).toEqual(['a1'])
    expect(mapTurnStats(snap, mapMessages(snap), false).map((item) => item.afterMessageId)).toEqual(['a1', 'u2'])
  })

  it('does not render an empty plan as zero of zero completed', () => {
    expect(mapPlan({ goal: 'answer directly', steps: [] })).toBeNull()
  })
})

describe('labels', () => {
  it('leaves an unparseable time empty rather than inventing one', () => {
    expect(timeLabel('not-a-date')).toBe('')
    expect(timeLabel(null)).toBe('')
  })

  it('formats milliseconds and seconds distinctly', () => {
    expect(durationLabel(240)).toBe('240ms')
    expect(durationLabel(1500)).toBe('1.5s')
    expect(durationLabel(null)).toBe('')
  })

  it('reports no model label when the session has no selection', () => {
    expect(modelLabelFor(snapshot(), [])).toBe('')
  })

  it('resolves the selected connection to its model id', () => {
    const snap = snapshot({ model_selection: { connection_id: 'mc-1', config_version: '2' } })

    expect(modelLabelFor(snap, [{ connection_id: 'mc-1', model_id: 'mid-model' }])).toBe('mid-model')
  })

  it('uses the default connection only while the session follows the default', () => {
    const connections = [
      { connection_id: 'mc-1', model_id: 'default-model', is_default: true },
      { connection_id: 'mc-2', model_id: 'chosen-model', is_default: false },
    ]
    expect(modelLabelFor(snapshot(), connections)).toBe('default-model')
    expect(modelLabelFor(snapshot({ model_selection: { connection_id: 'mc-2', config_version: '1' } }), connections))
      .toBe('chosen-model')
    // A stale/retired explicit choice must not silently appear to be the default.
    expect(modelLabelFor(snapshot({ model_selection: { connection_id: 'missing', config_version: '1' } }), connections))
      .toBe('')
  })

  it('carries title, workspace and status into the session summary', () => {
    expect(mapSession(snapshot({ session_state: 'completed' }), 'mid-model')).toEqual({
      title: '会话标题',
      workspace: 'local-agent-web',
      status: 'completed',
      modelLabel: 'mid-model',
    })
  })

  it('carries the server word on streaming into the session summary', () => {
    // §19.4：页面判"在不在流式"只读这一个字段，所以它必须**原样**搬过来——
    // 不推断（"有增量"不算）、不替服务端决定（缺字段就是缺，不给默认值）。
    expect(mapSession(snapshot({ answer_streaming: true }), 'mid-model').answerStreaming).toBe(true)
    expect(mapSession(snapshot({ answer_streaming: false }), 'mid-model').answerStreaming).toBe(false)
  })
})
