import type { components } from '../../api/schema.gen'

const names = { readonly_research: '只读调查', code_change: '代码修改与验证', delegated_research: '委派只读调查' }
const phases: Record<string, string> = { initialize: '准备', read: '读取原文', select: '选择文件',
  reserve_model: '预留模型额度', model: '生成受限提议', edit: '修改源码', test: '运行测试',
  reread: '核对产物', report: '核对结果', reserve: '预留子任务额度', dispatch: '执行子任务',
  waiting: '等待审批', completed: '已完成', failed: '未完成' }

export function mapWorkflow(data: components['schemas']['WorkflowView']) {
  return { title: names[data.name], phase: phases[data.phase] ?? '处理中',
    detail: `步骤 ${data.steps} · 工具 ${data.tool_calls} · 模型 ${data.model_calls} · Token 预留 ${data.model_tokens_reserved} · 证据 ${data.evidence_count}`,
    error: data.error ?? null,
    children: (data.children ?? []).map((child) => ({ id: child.child_id,
      text: `${child.scope}：${child.status === 'completed' ? '已完成' : '未完成'}，证据 ${child.evidence_count}`,
      error: child.error ?? null })) }
}

