import type { components } from '../../api/schema.gen'
import { toModelMenuItem } from '../../api/viewModels'
import type { ModelMenuView } from '../../fixtures/types'

type Snapshot = components['schemas']['SessionSnapshot']
type Connection = components['schemas']['ModelConnectionView']

/** 仅用服务端快照和连接列表构造菜单；运行中所用的连接并未出现在快照，不能猜。 */
export function toModelMenu(snapshot: Snapshot, connections: readonly Connection[]): ModelMenuView {
  const followsDefault = snapshot.model_selection == null
  const selectedId = followsDefault
    ? (connections.find((item) => item.is_default)?.connection_id ?? null)
    : snapshot.model_selection?.connection_id ?? null
  const selected = connections.find((item) => item.connection_id === selectedId)

  return {
    connections: connections.map(toModelMenuItem),
    selectedId,
    followsDefault,
    pinnedLabel: snapshot.session_state === 'running' ? '连接版本已固定，本次切换不会影响它' : null,
    nextTurnNotice: selected === undefined
      ? '下一轮尚无可用模型；请先在设置中添加或选择连接。'
      : followsDefault
        ? `当前跟随默认模型：${selected.display_name}。默认模型变更后，本会话下一轮随之更新。`
        : `已选择 ${selected.display_name} · 从下一轮开始生效。`,
  }
}

/** 版本只来自服务端的十进制字符串；无效或不安全的数值不得作为乐观校验发送。 */
export function parseSelectionVersion(value: string): number {
  if (!/^(0|[1-9]\d*)$/.test(value)) throw new Error('模型连接版本无效，请刷新后重试。')
  const version = Number(value)
  if (!Number.isSafeInteger(version)) throw new Error('模型连接版本无效，请刷新后重试。')
  return version
}

export function selectionExpectation(snapshot: Snapshot): components['schemas']['SelectionExpectation'] {
  const selected = snapshot.model_selection
  return selected == null
    ? { connection_id: null, config_version: null }
    : { connection_id: selected.connection_id, config_version: parseSelectionVersion(selected.config_version) }
}
