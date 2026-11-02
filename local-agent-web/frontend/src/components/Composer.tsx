import { useEffect, useId, useLayoutEffect, useRef, useState, type ClipboardEvent } from 'react'
import { EditorDialog } from './ui/EditorDialog'
import type {
  ComposerStateView,
  ComposerTakeoverView,
  ContextMeterView,
  ModelMenuView,
  PlanView,
  SessionSummaryView,
} from '../fixtures/types'
import { ApprovalCard } from '../features/approval/ApprovalCard'
import type { ApprovalActions } from '../features/approval/ApprovalCard'
import { QuestionCard } from '../features/question/QuestionCard'
import type { QuestionActions } from '../features/question/QuestionCard'
import { PlanStrip } from '../features/plan/PlanStrip'
import { ModelMenu } from '../features/settings/ModelMenu'
import { useGeneralPreferences } from '../features/settings/preferencesContext'
import { ContextMeter, MeterPanel } from './ContextMeter'
import { quotePreview } from '../features/conversation/quote'
import type { QueuedMessage } from '../stores/outbox'
import { EMPTY_ATTACHMENT_DRAFT, useAttachmentDrafts } from '../stores/attachments'
import { Doc, ImagePlus } from './icons'
import { searchWorkspaceFiles } from '../api/client'
import styles from './Composer.module.css'

/**
 * 路径含空白时加引号（2026-10-30）。
 *
 * 后端按「反引号 → 双引号 → 单引号」的优先级解析，这里必须用**同一套顺序**：
 * 写进去能识别的形状，和发出去能识别的形状不一致，等于把用户的东西弄丢。
 * 路径里三种引号都占了就退回裸路径——那种名字本来就少见，不值得为它再造一种转义。
 */
function quoteMentionPath(path: string): string {
  if (!/\s/.test(path)) return path
  for (const quote of ['`', '"', "'"]) {
    if (!path.includes(quote)) return `${quote}${path}${quote}`
  }
  return path
}

/**
 * 两个 git 作用域（2026-10-30）：`@diff` = 工作区未提交改动，`@staged` = 暂存区。
 *
 * 它们不是文件，但必须出现在候选里——否则用户不知道这个用法存在。
 * 后端按裸词识别（见 `file_mentions.GIT_SCOPES`），插入时不需要引号。
 */
const GIT_SCOPE_ITEMS = [
  { path: 'diff', name: '工作区改动' },
  { path: 'staged', name: '暂存区改动' },
] as const

/**
 * Real wiring (T30). **Omitting it means prototype behaviour**: the input stays
 * uncontrolled and the send button stays permanently disabled, which is exactly
 * what the static baseline shows.
 */
/**
 * 一条**待发**的附件：图片（2026-10-16）或文档（2026-10-29）。
 *
 * 两者在界面上不同：图片有本地缩略图、可点开放大；文档只有一个名字与体积标签。
 * `kind` 是判据——发送时图片走 `attachments`、文档走 `files`，两条路在下游完全不同。
 */
interface PendingAttachment {
  /** 服务端的引用（相对路径）：随消息提交的就是它。 */
  id: string
  /** 本地 object URL（缩略图用）；文档没有。字节的权威在服务端，这里只是预览。 */
  previewUrl?: string
  name: string
  kind: 'image' | 'file'
  /** 文档体积（`kind === 'file'` 时有）。 */
  sizeBytes?: number
}

export interface ComposerActions {
  /** Controlled draft. */
  draft: string
  onDraftChange: (text: string) => void
  /**
   * 发送。带上的附件是**引用**（`2026/10/16/<会话>/<哈希>.png`），不是文件本身——
   * 字节在上传时就落盘了（见 `onAttachFile`）。
   *
   * 返回这次提交的去向（2026-10-28 起是三态）：
   * - `sent` 服务端已接受（异步提交须等 Promise 完成）；
   * - `queued` **被接受了，但进了待发队列**（本轮还在跑）——同样要清待发图片与草稿；
   * - `rejected` 没接受（空输入、带图不允许入队、队列已满、需先处理中断）。
   * **必须先问它、再清待发图片**——否则图片会凭空消失（2026-10-20 实测）。
   */
  onSend: (
    attachmentIds: readonly string[],
    /** 文档引用 + 展示名（2026-10-29）：`name` 只用于气泡显示，服务端会先净化。 */
    files?: readonly { id: string; name: string }[],
  ) => 'sent' | 'queued' | 'rejected' | Promise<'sent' | 'queued' | 'rejected'>
  /**
   * 把一份**文档**交给服务端（2026-10-29）：走 multipart，返回引用与展示用的名字/体积。
   *
   * 与 `onAttachFile` 分开而不是加一个"类型"参数：两者上传方式不同（base64 JSON vs
   * multipart），返回值也不同（图片要预览地址，文档不要），合并只会让调用方写 if。
   */
  onAttachDocument?: (file: File) => Promise<{ id: string; name: string; sizeBytes: number }>
  /**
   * 把一张图交给服务端（2026-10-16）：成功给回引用与一个**本地预览地址**，失败**抛错**。
   *
   * 为什么预览用本地 object URL 而不是回读服务端：上传是流式的，本地那份就是同一张图，
   * 回读多一次往返、还得处理"刚传完就刷新"的时序。字节的权威仍在服务端，这里只是缩略图。
   */
  onAttachFile?: (file: File) => Promise<{ id: string; previewUrl: string }>
  /**
   * 待发的引用（2026-10-26，追问）。由宿主持有：发送时由它拼进正文，清空也在它那边。
   *
   * 这里只负责**显示与取消**：输入框上方一条单行引用，超出用省略号。省略即没有引用条
   * （原型与侧边对话不受影响）。
   */
  quote?: string | null
  onClearQuote?: () => void
  /** Whether sending is possible right now: non-empty draft and no turn in flight. */
  canSend: boolean
  /** Real shell only: the model chip opens or closes the session selector. */
  onToggleModelMenu?: () => void
  onCloseModelMenu?: () => void
  /**
   * 展开/收起上下文详情面板。省略即原型态：圆环与面板上的关闭按钮都保持惰性。
   *
   * 没有它时面板根本打不开（`meterOpen` 永远是 false），所以这一条是让
   * `ContextMeter` 那句"the ring's click target"变成真的唯一入口。
   */
  onToggleMeter?: () => void
  /** 当前会话的下一轮模式；运行、审批或切换请求中禁止改变。 */
  onSelectMode?: (mode: 'chat' | 'agent') => void
  modeDisabled?: boolean
  /**
   * 运行中的主按钮：停止这一轮（§17.4）。**省略即不渲染停止按钮**（原型态）——
   * 架构禁止渲染一个没有后端语义的停止控件，而后端语义自 v0.6 T60 起存在。
   */
  onStop?: () => void
  /** 现在可以停止：有在跑的轮次，且没在等审批。 */
  canStop?: boolean
  /** 停止请求在途：按钮锁住，避免连点产生第二次取消。 */
  stopping?: boolean
}

interface ComposerProps {
  mode?: 'chat' | 'agent'
  session: SessionSummaryView
  plan: PlanView | null
  meter: ContextMeterView
  takeover: ComposerTakeoverView
  modelMenu: ModelMenuView | null
  /** Disabled/phase overrides; omitted means enabled with no running phase. */
  state?: ComposerStateView
  /**
   * 这一轮**是被停下来的**（`final_result.state === 'cancelled'`，§17.4）：显示一个中性「已停止」
   * 标注，并说明不撤销副作用。它与「失败」必须分开——"你自己停的"不该被说成"出错了"。
   */
  stopped?: boolean
  actions?: ComposerActions
  modelMenuActions?: React.ComponentProps<typeof ModelMenu>['actions']
  /**
   * Approval decision wiring (T32); omitted in prototype mode so the static card
   * keeps its handler-free buttons.
   */
  approvalActions?: ApprovalActions
  /**
   * Question wiring (2026-10-28); omitted in prototype mode so the static card
   * keeps its handler-free buttons.
   */
  questionActions?: QuestionActions
  /**
   * 待发队列（2026-10-28）：本轮忙时草稿仍可**入队**（`canQueue` 决定回车/发送键是否被接受），
   * 队列区渲染在输入框上方，可上移/下移/删除；`queuePaused` 时给一个"继续发送"。
   *
   * 刻意**不放进 `actions`**：它不是对会话的动作，而是"我还有几条没发"的界面状态。
   */
  canQueue?: boolean
  queued?: readonly QueuedMessage[]
  queuePaused?: boolean
  onRemoveQueued?: (id: string) => void
  onMoveQueued?: (id: string, direction: 'up' | 'down') => void
  onEditQueued?: (id: string, text: string) => boolean
  onQueueEditingChange?: (editing: boolean) => void
  queueScope?: string | null
  onResumeQueue?: () => void
  /**
   * 队列相关问题的一句话（2026-10-28）：入队被拒（队列已满 / 带图）或自动发送失败。
   * 没有它时**不渲染任何占位**——正常排队时不该多一行字。
   */
  queueNotice?: string | null
  /**
   * 当前会话的工作区 id（2026-10-30）：`@` 引用按它搜文件。
   *
   * 省略即关闭引用：原型与侧边对话不传，也就不用为了一个提示去连 QueryClient。
   */
  workspaceId?: string
}

/** One copy of the running-state explanation, derived from `session.status`. */
const SEND_BLOCKED_TEXT = '任务进行中：可以先继续写草稿，但要等这一轮结束才能发送。'

/**
 * Resident input area: plan strip on top, then either the composer or whatever
 * request has taken it over.
 *
 * An approval replaces the composer **only while a decision is still required**:
 * the chat input must not stay reachable underneath a pending decision
 * (`ARCHITECTURE.md` §15.8). Once the request is no longer actionable — it
 * expired, or a decision was already taken — the composer comes back and the
 * card stays above it as a visible outcome, because a card that asks the user to
 * re-issue a request must not be the reason they cannot type one.
 */
export function Composer({ mode = 'agent', session, plan, meter, takeover, modelMenu, state, stopped = false, actions, modelMenuActions, approvalActions, questionActions, canQueue = false, queued = [], queuePaused = false, onRemoveQueued, onMoveQueued, onEditQueued, onQueueEditingChange, queueScope, onResumeQueue, queueNotice = null, workspaceId }: ComposerProps) {
  // `animations` 不再需要：用它的是那条压缩提示，而它 2026-10-16 搬进了对话流。
  const { send_shortcut: sendShortcut } = useGeneralPreferences()
  const modelTrigger = useRef<HTMLButtonElement>(null)
  const meterTrigger = useRef<HTMLButtonElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  /**
   * `@` 引用（2026-10-30）：光标前那个还没写完的 `@路径`。
   *
   * 只记 `start`（`@` 的位置）与 `query`（已输入的部分）：插入时要靠它们把这段替换掉，
   * 而不是在整段正文里做字符串替换——正文里可能还有另一个同名文件。
   */
  const [mention, setMention] = useState<{ start: number; query: string } | null>(null)
  const [mentionItems, setMentionItems] = useState<readonly { path: string; name: string }[]>([])
  const [mentionIndex, setMentionIndex] = useState(0)

  /**
   * 候选文件的搜索（2026-10-30）：停止输入 150ms 再发，避免每个字符一次请求。
   *
   * 不用 `useSearchWorkspaceFiles`：那是 react-query hook，原型页没有 Provider，
   * 无条件调用会直接崩在渲染阶段。这里手写 debounce——`workspaceId` 没有时一次请求都不发。
   */
  useEffect(() => {
    if (mention === null || workspaceId === undefined || mention.query.trim() === '') {
      setMentionItems([])
      return
    }
    let cancelled = false
    const timer = setTimeout(() => {
      void searchWorkspaceFiles(mention.query, workspaceId)
        .then((view) => {
          if (cancelled) return
          // `path` 在契约里可空（目录项只需要名字），而插入正文的必须是可用路径：
          // 用 flatMap 顺手收窄，比先 filter 再断言安全。
          setMentionItems((view.entries ?? [])
            .flatMap((entry) => (entry.kind === 'file' && typeof entry.path === 'string' && entry.path !== ''
              ? [{ path: entry.path, name: entry.name }]
              : []))
            .slice(0, 6))
          setMentionIndex(0)
        })
        .catch(() => { if (!cancelled) setMentionItems([]) })
    }, 150)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [mention, workspaceId])

  /** 把正在输入的那段 `@query` 换成 `@路径 `，并把光标放到它后面。 */
  const applyMention = (path: string) => {
    if (mention === null || actions === undefined) return
    const caret = inputRef.current?.selectionStart ?? mention.start + mention.query.length + 1
    const inserted = `@${quoteMentionPath(path)} `
    actions.onDraftChange(`${actions.draft.slice(0, mention.start)}${inserted}${actions.draft.slice(caret)}`)
    setMention(null)
    setMentionItems([])
    const at = mention.start + inserted.length
    requestAnimationFrame(() => {
      inputRef.current?.focus()
      inputRef.current?.setSelectionRange(at, at)
    })
  }

  /**
   * 候选 = 两个 git 作用域 + 文件搜索结果（2026-10-30）。
   *
   * 刚打出 `@`、搜索词还空的时候只有前两条——正好把「还能这么用」提示出来；
   * 输入 `@d` 时也只剩 `@diff`，不会和文件结果抢位置。
   */
  const mentionOptions = mention === null ? [] : [
    ...GIT_SCOPE_ITEMS.filter((item) => item.path.startsWith(mention.query)),
    ...mentionItems,
  ]
  // 待发图片（2026-10-16）：它是**这条草稿**的一部分，属于本地瞬态——刷新即丢（字节已在服务端）。
  const localAttachmentScope = useId()
  const attachmentScope = queueScope ?? localAttachmentScope
  const attachmentDraft = useAttachmentDrafts(state => state.drafts[attachmentScope]) ?? EMPTY_ATTACHMENT_DRAFT
  const updateAttachments = useAttachmentDrafts(state => state.update)
  const { items: pending, error: attachError, uploading, sending: sendBusy } = attachmentDraft
  const attachBusy = uploading !== null
  // 待发引用（2026-10-26）：状态在宿主（它负责拼进正文），这里只用来显示与取消。
  const quote = actions?.quote ?? ''
  const [queueEditor, setQueueEditor] = useState<{ id: string; text: string } | null>(null)
  const [editError, setEditError] = useState<string | null>(null)
  const editingChange = useRef(onQueueEditingChange)
  useLayoutEffect(() => { editingChange.current = onQueueEditingChange }, [onQueueEditingChange])
  useEffect(() => {
    setQueueEditor(null)
    setEditError(null)
    editingChange.current?.(false)
    return () => editingChange.current?.(false)
  }, [queueScope])
  const closeQueueEditor = () => {
    setQueueEditor(null)
    setEditError(null)
    editingChange.current?.(false)
  }

  /** 粘贴与选文件走**同一条路**：两者都只是"拿到若干 File"，处理方式必须一样。 */
  async function attachFiles(files: readonly File[]) {
    const current = useAttachmentDrafts.getState().drafts[attachmentScope] ?? EMPTY_ATTACHMENT_DRAFT
    if (current.uploading !== null || current.sending) return
    const images = files.filter((file) => file.type.startsWith('image/'))
    // 非图片一律当文档交给服务端判（它按魔数认格式，认不出会给出可执行的拒绝理由）。
    const documents = files.filter((file) => !file.type.startsWith('image/'))
    if (current.items.filter(item => item.kind === 'file').length + documents.length > 5) {
      updateAttachments(attachmentScope, draft => ({ ...draft, error: '每条消息最多附带 5 份文档，请移除多余文件后再选择。' }))
      return
    }
    if (documents.some(file => file.size > 50 * 1024 * 1024)) {
      updateAttachments(attachmentScope, draft => ({ ...draft, error: '单份文档不能超过 50 MB，请先拆分或压缩。' }))
      return
    }
    if (images.length === 0 && documents.length === 0) return
    // 先取成局部常量：`actions?.x === undefined` 拦不住后面的 `actions.x`（TS 不跨语句收窄）。
    const uploadImage = actions?.onAttachFile
    const uploadDocument = actions?.onAttachDocument
    if (images.length > 0 && uploadImage === undefined) return
    if (documents.length > 0 && uploadDocument === undefined) return
    updateAttachments(attachmentScope, draft => ({ ...draft, error: null, uploading: files[0]?.name ?? '附件' }))
    const append = (item: PendingAttachment) => updateAttachments(attachmentScope, draft => ({
      ...draft, items: [...draft.items, item],
    }))
    try {
      if (uploadImage !== undefined) {
        for (const file of images) {
          updateAttachments(attachmentScope, draft => ({ ...draft, uploading: file.name }))
          const attached = await uploadImage(file)
          append({ ...attached, name: file.name, kind: 'image' })
        }
      }
      if (uploadDocument !== undefined) {
        for (const file of documents) {
          updateAttachments(attachmentScope, draft => ({ ...draft, uploading: file.name }))
          const attached = await uploadDocument(file)
          append({ ...attached, kind: 'file' })
        }
      }
    } catch (error) {
      // 服务端的拒绝理由是可执行的（格式/大小/当天张数），原样给用户看，不改写。
      updateAttachments(attachmentScope, draft => ({ ...draft, error: error instanceof Error ? error.message : '这个附件没能上传' }))
    } finally {
      updateAttachments(attachmentScope, draft => ({ ...draft, uploading: null }))
    }
  }

  /** 从剪贴板取图。**只在真有图时**拦默认行为——否则连正常粘贴文字都会被吃掉。 */
  function pasteImages(event: ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(event.clipboardData?.files ?? [])
    if (!files.some((file) => file.type.startsWith('image/'))) return
    event.preventDefault()
    void attachFiles(files)
  }

  /** 发送：把待发图片的**引用**一起交出去；**只有调用方接受（sent / queued）后才清空**。 */
  function send() {
    if (actions === undefined) return
    const current = useAttachmentDrafts.getState().drafts[attachmentScope] ?? EMPTY_ATTACHMENT_DRAFT
    if (current.uploading !== null || current.sending) return
    // 图片与文档分成两批交出去：下游是两条完全不同的路（data URL 内容块 vs 路径提示）。
    const ids = pending.filter((item) => item.kind === 'image').map((item) => item.id)
    const fileIds = pending
      .filter((item) => item.kind === 'file')
      .map((item) => ({ id: item.id, name: item.name }))
    const clear = () => updateAttachments(attachmentScope, draft => ({ ...draft, items: [], error: null }))
    const failed = () => updateAttachments(attachmentScope, draft => ({ ...draft, error: '消息未能发送，附件已保留，请核对状态后重试。' }))
    try {
      const result = actions.onSend(ids, fileIds)
      if (typeof result === 'string') {
        // 同步路径：`rejected` 命中"空输入 / 带附件不允许入队 / 队列已满 / 需先处理中断"，
        // 那时什么都不清——否则用户看到的是"问题没发出去、文字还在、附件没了"。
        if (result !== 'rejected') clear()
        return
      }
      if (!result || typeof result.then !== 'function') return
      // 带附件走异步：**先清空、失败再放回**（2026-10-30 实机反馈）。
      // 原先等 Promise 落地才清，而那个 Promise 要等 `/messages` **整轮跑完** + 快照重取才
      // resolve——结果是附件在输入框上方挂了几分钟，看起来像没发出去。
      // 快照那份留到失败回滚用：被拒或网络错误时原样放回，用户不必重新选文件。
      const snapshot = current.items
      clear()
      const restore = () => updateAttachments(attachmentScope, draft => ({ ...draft, items: snapshot }))
      updateAttachments(attachmentScope, draft => ({ ...draft, sending: true }))
      void result
        .then((status) => {
          if (status === 'rejected') restore()
        })
        .catch(() => {
          restore()
          failed()
        })
        .finally(() => {
          updateAttachments(attachmentScope, draft => ({ ...draft, sending: false }))
        })
    } catch {
      failed()
    }
  }
  // 模式切换改成**两个常驻分段**，没有弹出菜单了：不再需要开关状态、外部点击与 Escape 处理（那些
  // 都是给下拉写的）。原型态（没有 `actions`）把两段都设为禁用——控件照实画出来，但不假装能点。
  const modeDisabled = actions === undefined || actions.modeDisabled === true
  // 停止按钮只在**真的有在跑的轮次**时出现（由调用方按会话状态给 `canStop`）。
  const stopVisible = actions?.onStop !== undefined && actions.canStop === true
  const approval = takeover.kind === 'approval' ? takeover.approval : null
  const awaitingDecision = approval !== null && approval.state === 'pending'
  // 提问（2026-10-28）：与审批互斥——后端保证同一时刻最多只有一个非 `none`（架构 §4.2）。
  const question = takeover.kind === 'question' ? takeover.question : null
  const awaitingAnswer = question !== null && question.state === 'pending'
  const disabledReason = state?.disabledReason ?? null
  const phase = state?.phase ?? null
  const meterOpen = state?.meterOpen ?? false
  useLayoutEffect(() => {
    const resize = () => {
      const input = inputRef.current
      if (input === null) return
      input.style.height = 'auto'
      input.style.height = `${Math.min(input.scrollHeight, 200)}px`
    }
    resize()
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [actions?.draft, awaitingDecision, awaitingAnswer])
  // A running turn blocks *sending*, not writing: the draft stays editable, the
  // input is never replaced by a stop button, and the reason is derived from the
  // run state itself rather than repeated per fixture (plan §T30, ARCHITECTURE §15.2).
  const sendBlockedReason = session.status === 'running' ? SEND_BLOCKED_TEXT : null

  return (
    <div className={styles.dock} data-composer="true" data-composer-mode={awaitingDecision ? 'approval' : awaitingAnswer ? 'question' : 'input'}>
      <div className={styles.stack}>
        {plan === null ? null : <PlanStrip plan={plan} />}

        {/* 队列相关的一句话（2026-10-28）：入队被拒或自动发送失败时才出现。 */}
        {queueNotice === null || queueNotice === undefined ? null : (
          <p className={styles.queueNotice} role="status">{queueNotice}</p>
        )}

        {/* 待发队列（2026-10-28）：本轮还没结束时排队；顺序可调、可单条取消。 */}
        {queued.length === 0 ? null : (
          <section className={styles.queue} aria-label="待发送的消息">
            <div className={styles.queueHead}>
              <span className={styles.queueTitle}>
                待发送 {queued.length} 条 · {queuePaused ? '已暂停' : '本轮结束后自动发送'}
              </span>
              {queuePaused && onResumeQueue !== undefined ? (
                <button type="button" className={styles.queueResume} onClick={onResumeQueue}>
                  继续发送
                </button>
              ) : null}
            </div>
            <ol className={styles.queueList}>
              {queued.map((item, index) => (
                <li key={item.id} className={styles.queueItem}>
                  <span className={styles.queueIndex}>{index + 1}</span>
                  <span className={styles.queueText}>{item.text}</span>
                  <span className={styles.queueActions}>
                    <button type="button" className={styles.queueButton} aria-label={`编辑第 ${index + 1} 条`}
                      disabled={onEditQueued === undefined} onClick={() => {
                        setQueueEditor({ id: item.id, text: item.text })
                        setEditError(null)
                        editingChange.current?.(true)
                      }}>
                      <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="m10 3 3 3M3 10l7-7a2.1 2.1 0 0 1 3 3l-7 7-4 1z" fill="none" stroke="currentColor" strokeWidth="1.4" /></svg>
                    </button>
                    <button
                      type="button"
                      className={styles.queueButton}
                      aria-label={`上移第 ${index + 1} 条`}
                      disabled={index === 0 || onMoveQueued === undefined}
                      onClick={() => onMoveQueued?.(item.id, 'up')}
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      className={styles.queueButton}
                      aria-label={`下移第 ${index + 1} 条`}
                      disabled={index === queued.length - 1 || onMoveQueued === undefined}
                      onClick={() => onMoveQueued?.(item.id, 'down')}
                    >
                      ↓
                    </button>
                    <button
                      type="button"
                      className={styles.queueButton}
                      aria-label={`删除第 ${index + 1} 条`}
                      disabled={onRemoveQueued === undefined}
                      onClick={() => onRemoveQueued?.(item.id)}
                    >
                      ×
                    </button>
                  </span>
                </li>
              ))}
            </ol>
          </section>
        )}
        {queueEditor !== null && queued.some(item => item.id === queueEditor.id) ? (
          <EditorDialog title="编辑待发消息" description="保存后保留原来的排队位置；编辑期间暂缓自动发送。" onClose={closeQueueEditor}>
            <form className={styles.queueEditor} onSubmit={event => {
              event.preventDefault()
              if (!queueEditor.text.trim()) return
              if (onEditQueued?.(queueEditor.id, queueEditor.text)) closeQueueEditor()
              else setEditError('这条消息已不在队列中，请关闭窗口并检查会话。')
            }}>
              <label>消息内容<textarea aria-label="待发消息内容" value={queueEditor.text} rows={7} maxLength={100000}
                onChange={event => setQueueEditor({ ...queueEditor, text: event.target.value })} /></label>
              {editError ? <p role="alert">{editError}</p> : null}
              <div><button type="button" onClick={closeQueueEditor}>取消</button>
                <button type="submit" disabled={!queueEditor.text.trim()}>保存修改</button></div>
            </form>
          </EditorDialog>
        ) : null}

        {approval !== null && !awaitingDecision ? (
          <ApprovalCard key={approval.approvalId} approval={approval} actions={approvalActions} />
        ) : null}

        {question !== null && !awaitingAnswer ? (
          <QuestionCard key={question.questionId} question={question} actions={questionActions} />
        ) : null}

        {modelMenu === null || awaitingDecision || awaitingAnswer ? null : <ModelMenu menu={modelMenu} actions={modelMenuActions}
          anchorRef={modelTrigger} onClose={actions?.onCloseModelMenu} />}

        {meterOpen && !awaitingDecision && !awaitingAnswer ? (
          <MeterPanel meter={meter} anchorRef={meterTrigger} onClose={actions?.onToggleMeter} />
        ) : null}

        {awaitingAnswer ? (
          <QuestionCard key={question.questionId} question={question} actions={questionActions} />
        ) : awaitingDecision ? (
          <ApprovalCard key={approval.approvalId} approval={approval} actions={approvalActions} />
        ) : (
          <>
            {/* 2026-10-16：压缩提示**不在这里**。它是流程的一步（与 Think、工具行同级），
                现在渲染在对话流里（`features/conversation/CompressionRow`）——挂在输入框上方时，
                读者会以为那是"输入状态"，而它回答的是"这轮上下文为什么变小了"。 */}
            {/* 待发图片（2026-10-16）：粘贴或选图之后先在这里排队，发送时随消息一起交给模型。 */}
            {pending.length === 0 ? null : (
              <ul className={styles.attachments} aria-label="待发送的附件">
                {pending.map((item, index) => (
                  <li
                    key={`${item.id}:${index}`}
                    className={item.previewUrl === undefined
                      ? `${styles.attachment} ${styles.attachmentDoc}`
                      : styles.attachment}
                  >
                    {item.previewUrl === undefined ? (
                      // 文件名可截断，扩展名与大小保留，便于发送前核对。
                      <span
                        className={styles.attachmentFile}
                        title={`${item.name} · 上传后保存在本机，助手可直接读取；不需要就点右侧 × 移除`}
                      ><Doc /><span className={styles.attachmentName}>{item.name.replace(/\.[^.]+$/, '')}</span><span>{item.name.match(/\.[^.]+$/)?.[0]}</span><span className={styles.attachmentSize}>{item.sizeBytes === undefined ? '' : item.sizeBytes >= 1024 * 1024 ? `${(item.sizeBytes / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(item.sizeBytes / 1024)} KB`}</span></span>
                    ) : (
                      <img className={styles.attachmentImage} src={item.previewUrl} alt="" />
                    )}
                    <button
                      type="button"
                      className={styles.attachmentRemove}
                      aria-label={`移除${item.kind === 'file' ? '附件' : '图片'} ${item.name}`}
                      disabled={sendBusy}
                      onClick={() =>
                        updateAttachments(attachmentScope, draft => ({ ...draft, items: draft.items.filter((_, rowIndex) => rowIndex !== index) }))
                      }
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {uploading === null ? null : <p className={styles.uploadStatus} role="status">正在上传 {uploading}…</p>}
            {/* 待发引用（2026-10-26，追问）：只留一条，再点追问会替换掉上一条（引用是"当前
                在问的那段"，不是收件箱，排队反而让用户分不清自己在问哪段）。 */}
            {quote === '' ? null : (
              <div className={styles.quote} role="note" aria-label="引用内容">
                <span className={styles.quoteMark} aria-hidden="true">“</span>
                <span className={styles.quoteText}>{quotePreview(quote)}</span>
                <button
                  type="button"
                  className={styles.quoteRemove}
                  aria-label="取消引用"
                  title="取消引用"
                  onClick={actions?.onClearQuote}
                >
                  ×
                </button>
              </div>
            )}
            {phase === null ? null : (
              <div className={styles.phase} role="status">
                {phase}
              </div>
            )}
            <div className={styles.composer}>
              {/* `@` 引用候选（2026-10-30）：贴在卡片上沿。用 mousedown 而不是 click——
                  输入框先失焦会把弹层收掉，click 就再也收不到了。 */}
              {mentionOptions.length === 0 ? null : (
                <div className={styles.mentionMenu} role="listbox" aria-label="引用工作区文件或改动">
                  {mentionOptions.map((item, index) => (
                    <button key={item.path} type="button" role="option"
                      aria-selected={index === mentionIndex}
                      className={index === mentionIndex ? `${styles.mentionItem} ${styles.mentionActive}` : styles.mentionItem}
                      onMouseDown={(event) => { event.preventDefault(); applyMention(item.path) }}>
                      <span className={styles.mentionName}>{item.name}</span>
                      <span className={styles.mentionPath}>{item.path}</span>
                    </button>
                  ))}
                </div>
              )}
              <label className={styles.inputLabel}>
                <span className="visually-hidden">给智能体发消息</span>
                {/* A disabled input states why it is disabled, and points at that
                    sentence for assistive tech rather than relying on the greying. */}
                <textarea
                  ref={inputRef}
                  className={styles.input}
                  placeholder="给智能体发消息"
                  rows={1}
                  disabled={disabledReason !== null}
                  aria-describedby={disabledReason === null ? undefined : 'composer-disabled-reason'}
                  // Prototype mode stays uncontrolled (untouched); real mode is driven by the draft,
                  // which lives in the UI store so a turn in flight never eats what the user typed.
                  value={actions === undefined ? undefined : actions.draft}
                  onChange={
                    actions === undefined
                      ? undefined
                      : (event) => {
                        const next = event.target.value
                        actions.onDraftChange(next)
                        if (workspaceId === undefined) return
                        // 只看光标**之前**：`@` 出现在后面不该影响当前正在打的这段。
                        const caret = event.target.selectionStart ?? next.length
                        const match = /(?:^|\s)@([^\s@]*)$/.exec(next.slice(0, caret))
                        if (match === null) {
                          setMention(null)
                          setMentionItems([])
                          return
                        }
                        const query = match[1] ?? ''
                        setMention({ start: caret - query.length - 1, query })
                      }
                  }
                  onPaste={actions === undefined ? undefined : pasteImages}
                  onKeyDown={
                    actions === undefined
                      ? undefined
                      : (event) => {
                        // `@` 候选优先吃掉方向键 / Enter / Tab / Esc（2026-10-30）：
                        // 否则回车会一边插入路径、一边把消息发出去。
                        if (mentionOptions.length > 0) {
                          if (event.key === 'ArrowDown') {
                            event.preventDefault()
                            setMentionIndex((current) => (current + 1) % mentionOptions.length)
                            return
                          }
                          if (event.key === 'ArrowUp') {
                            event.preventDefault()
                            setMentionIndex((current) => (current - 1 + mentionOptions.length) % mentionOptions.length)
                            return
                          }
                          if ((event.key === 'Enter' || event.key === 'Tab') && !event.shiftKey) {
                            event.preventDefault()
                            applyMention(mentionOptions[mentionIndex]?.path ?? mentionOptions[0]!.path)
                            return
                          }
                          if (event.key === 'Escape') {
                            event.preventDefault()
                            setMention(null)
                            setMentionItems([])
                            return
                          }
                        }
                        // Enter sends, Shift+Enter keeps the newline (chat convention).
                        const sendKey = sendShortcut === 'ctrl_enter' ? event.ctrlKey || event.metaKey : !event.ctrlKey && !event.metaKey
                        // 有待发图片时也允许发出（2026-10-16）：只贴一张图、不写字是合法提交。
                        // `canQueue`（2026-10-28）：本轮忙时回车也算"被接受"——进待发队列。
                        if (event.key === 'Enter' && sendKey && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229 && (actions.canSend || canQueue || pending.length > 0)) {
                          event.preventDefault()
                          send()
                        }
                      }
                  }
                />
              </label>
              {disabledReason === null ? null : (
                <p className={styles.disabledNote} id="composer-disabled-reason">
                  {disabledReason}
                </p>
              )}
              {sendBlockedReason === null ? null : (
                <p className={styles.disabledNote} id="composer-send-blocked">
                  {sendBlockedReason}
                </p>
              )}
              {attachError === null ? null : (
                <p className={styles.attachError} role="alert">
                  {attachError}
                </p>
              )}
              <div className={styles.controls}>
                <div
                  className={styles.modeSelector}
                  role="group"
                  aria-label="选择会话模式"
                  aria-describedby="composer-mode-hint"
                >
                  {/* 两段同时可见、一点即切（2026-10-13 用户反馈：原来那个下拉又丑又要两步）。
                      操作说明（"下一轮生效、历史保留"）留在 DOM 里给读屏软件，视觉上不再多一行小字。 */}
                  <p className="visually-hidden" id="composer-mode-hint">
                    切换当前会话的模式，从下一轮生效，历史记录保留。
                  </p>
                  <button
                    type="button"
                    className={styles.modeOption}
                    aria-pressed={mode === 'chat'}
                    disabled={modeDisabled}
                    title="闲聊：可读取文件，不修改。"
                    onClick={() => { if (mode !== 'chat') actions?.onSelectMode?.('chat') }}
                  >
                    闲聊
                  </button>
                  <button
                    type="button"
                    className={styles.modeOption}
                    aria-pressed={mode === 'agent'}
                    disabled={modeDisabled}
                    title="Agent：可执行受控工具。"
                    onClick={() => { if (mode !== 'agent') actions?.onSelectMode?.('agent') }}
                  >
                    Agent
                  </button>
                </div>
                <button
                  type="button"
                  className={styles.chip}
                  ref={modelTrigger}
                  aria-haspopup="dialog"
                  onClick={actions?.onToggleModelMenu}
                  aria-expanded={actions?.onToggleModelMenu === undefined ? undefined : modelMenu !== null}
                  aria-label={actions?.onToggleModelMenu === undefined ? undefined : '选择会话模型'}
                >
                  {actions !== undefined && session.modelLabel === '' ? '选择模型' : session.modelLabel}
                </button>
                {/* v0.8 T74（§19.4）：关闭流式时**明说**"未流式"——否则用户只会看到一段长时间的
                    什么都没发生，然后再一次出现整段回答。未知（原型夹具）不显示任何东西。 */}
                {session.answerStreaming === false ? (
                  <span
                    className={styles.streamNote}
                    title="未启用流式：回答在完成时一次性出现，不是逐字出现。可在设置里为该模型连接打开流式。"
                  >
                    未流式
                  </span>
                ) : null}
                {/* §17.4：这一轮是被停下来的。**中性标注**，与"未流式"同一套写法——它不是告警，
                    也不说"失败"：用户自己停的不该被说成出错。 */}
                {stopped ? (
                  <span
                    className={styles.streamNote}
                    title="这一轮已被停止：已经执行的工具不会回滚。要接着做，直接发下一条即可。"
                  >
                    已停止
                  </span>
                ) : null}
                <div className={styles.spacer} />
                {/* 计量器与发送是一个整体：窄窗整组折到第二行，而不是把发送挤出视口。 */}
                <div className={styles.right}>
                  {/* 加图片（2026-10-19 用户反馈）：与粘贴**同一套处理**（都只是拿到 File），
                      入口从输入框上方单独一行文字按钮，收到这里——与计量器、发送同一条基线，
                      不再为一颗次要按钮单占一行。图片的字节在服务端，这里只负责把 File 交出去。 */}
                  <button
                    type="button"
                    className={styles.attach}
                    disabled={(actions?.onAttachFile === undefined && actions?.onAttachDocument === undefined) || attachBusy || sendBusy}
                    onClick={() => fileInput.current?.click()}
                    aria-label={attachBusy ? '正在上传附件' : '添加附件'}
                    title={attachBusy ? `正在上传 ${uploading}…` : '添加图片或文档（每条最多 5 份文档，单份不超过 50 MB；图片也可直接粘贴）'}
                  >
                    {attachBusy ? <span className={styles.attachSpinner} aria-hidden="true" /> : <ImagePlus />}
                  </button>
                  <input
                    ref={fileInput}
                    className="visually-hidden"
                    type="file"
                    accept="image/png,image/jpeg,image/gif,image/webp,.pdf,.docx,.xlsx,.pptx,.doc,.xls,.ppt"
                    multiple
                    onChange={(event) => {
                      void attachFiles(Array.from(event.target.files ?? []))
                      // 清空 value：否则连续选同一张图不会再触发 change（浏览器行为）。
                      event.target.value = ''
                    }}
                  />
                  <ContextMeter meter={meter} triggerRef={meterTrigger} open={meterOpen} onToggle={actions?.onToggleMeter} />
                  {/* v0.4 exposes no `cancel_task()`, so the primary action is never a
                      stop button: sending is simply unavailable until the turn ends
                      (ARCHITECTURE §15.2, plan §T30). */}
                  {/* 提示词里那条"v0.4 不做停止按钮"已作废：后端自 v0.6 T60 起有取消语义，
                      且 2026-10-13 起**正在飞的模型调用真的能在流式块之间被打断**（§11.7），
                      所以这个按钮现在只在**有在跑的轮次**时替换掉发送（§17.4）。
                      停止是"等它收敛"：不撤销已发生的副作用，收敛完即可发下一条。 */}
                  {stopVisible ? (
                    <button
                      type="button"
                      className={styles.stop}
                      disabled={actions?.stopping === true}
                      onClick={() => actions?.onStop?.()}
                      aria-label="停止生成"
                      title="停止这一轮：已经执行的工具不会回滚；本轮收敛后即可发送下一条。"
                    >
                      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" focusable="false">
                        <rect x="1.5" y="1.5" width="9" height="9" rx="1.5" fill="currentColor" />
                      </svg>
                    </button>
                  ) : (
                    <button
                      type="button"
                      className={styles.send}
                      // Prototype mode keeps it permanently disabled (the fixture snapshot);
                      // real mode enables it once there is something to send.
                      // 有待发图片时也可以点（2026-10-16）：`canSend` 只管"草稿非空"。
                      disabled={actions === undefined || attachBusy || sendBusy || (!actions.canSend && !canQueue && pending.length === 0)}
                      onClick={actions === undefined ? undefined : () => send()}
                      aria-label="发送消息"
                      aria-describedby={sendBlockedReason === null ? undefined : 'composer-send-blocked'}
                    >
                      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" focusable="false">
                        <path
                          d="M7 11.5V3M7 3 3.4 6.6M7 3l3.6 3.6"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.7"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </button>
                  )}
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
