// 会话消息 → 轨迹表（matrix 20/21）。
//
// 轨迹页签与对话页签看的是**同一份会话数据**，差别只在呈现：对话把工具调用渲染成可展开的卡片，
// 轨迹把它压平成"类型 / 参数 / 结果摘要"的表格，右侧检查器再补 Summary / Payload / Result /
// Schema / Timing 五段。因此这里不新建数据源，只把已经映射好的工具行摊平——两个页签不可能各自
// 说一套。
//
// 与其它适配层同一条纪律：只搬运、不推断。契约里没有的（工具调用属于哪一轮、绑定的是哪次模型
// 调用、供应商没上报的 token）在视图里保持"未知"，宁可缺，不可编。

import type {
  MessageView,
  ModelCallView,
  ToolRowView,
  TraceRowView,
  TraceView,
} from '../../fixtures/types'

/**
 * 表格里"参数"与检查器 Payload 段共用的一行式写法。
 *
 * 直接沿用对话行 `summary` 的键值压法（`key=value · key=value`），不另立格式：同一份已脱敏参数
 * 在两个页签里长得一样，用户才不会以为是两批数据。
 */
export function parameterSummary(parameters: readonly { name: string; value: string }[]): string {
  return parameters.map(({ name, value }) => `${name}=${value}`).join(' · ')
}

/** 结果摘要列：预览的第一行；预览为空时返回空串，由组件决定显示"未知"。 */
export function resultSummary(resultPreview: string): string {
  const [first = ''] = resultPreview.split('\n')
  return first
}

/**
 * 一次工具调用 → 轨迹表的一行。
 *
 * `id` 用契约的 `tool_call_id`；原型 fixture 没有这个字段，就退回消息 id（同一会话内唯一）。
 */
export function toTraceRow(tool: ToolRowView, fallbackId: string): TraceRowView {
  return {
    id: tool.toolCallId ?? fallbackId,
    toolName: tool.name,
    status: tool.status,
    durationLabel: tool.durationLabel,
    target: tool.target,
    parameters: tool.parameters ?? [],
    resultPreview: tool.resultPreview ?? '',
    resultTruncated: tool.resultTruncated === true,
    resultDigest: tool.resultDigest ?? null,
    startedLabel: tool.startedLabel ?? '',
    finishedLabel: tool.finishedLabel ?? '',
    evidence: tool.evidence ?? null,
  }
}

/**
 * 会话消息 + 会话级模型调用 → 轨迹视图。
 *
 * 顺序即时间线顺序：消息流本身就是按发生次序投影的，这里排序而不重排（`ToolUseView` 有
 * `started_at`，但消息流已经表达了先后，另排一次只会在时间戳缺失时把顺序搞乱）。
 */
export function traceFromMessages(
  messages: readonly MessageView[],
  calls: readonly ModelCallView[],
): TraceView {
  return {
    rows: messages.flatMap((message) =>
      message.kind === 'tool' ? [toTraceRow(message.tool, message.id)] : [],
    ),
    calls,
  }
}