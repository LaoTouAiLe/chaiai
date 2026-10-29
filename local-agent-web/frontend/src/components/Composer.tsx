import { useEffect, useLayoutEffect, useRef, useState, type ClipboardEvent } from 'react'
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
import { ImagePlus } from './icons'
import styles from './Composer.module.css'

/**
 * Real wiring (T30). **Omitting it means prototype behaviour**: the input stays
 * uncontrolled and the send button stays permanently disabled, which is exactly
 * what the static baseline shows.
 */
/** 一张**待发**的图片（2026-10-16）：服务端的引用 + 本地预览地址。 */
interface PendingAttachment {
  /** 服务端的引用（相对路径）：随消息提交的就是它。 */
  id: string
  /** 本地 object URL（缩略图用）；字节的权威在服务端，这里只是预览。 */
  previewUrl: string
  name: string
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
   * - `sent` 已发出去；
   * - `queued` **被接受了，但进了待发队列**（本轮还在跑）——同样要清待发图片与草稿；
   * - `rejected` 没接受（空输入、带图不允许入队、队列已满、需先处理中断）。
   * **必须先问它、再清待发图片**——否则图片会凭空消失（2026-10-20 实测）。
   */
  onSend: (attachmentIds: readonly string[]) => 'sent' | 'queued' | 'rejected'
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
export function Composer({ mode = 'agent', session, plan, meter, takeover, modelMenu, state, stopped = false, actions, modelMenuActions, approvalActions, questionActions, canQueue = false, queued = [], queuePaused = false, onRemoveQueued, onMoveQueued, onEditQueued, onQueueEditingChange, queueScope, onResumeQueue, queueNotice = null }: ComposerProps) {
  // `animations` 不再需要：用它的是那条压缩提示，而它 2026-10-16 搬进了对话流。
  const { send_shortcut: sendShortcut } = useGeneralPreferences()
  const modelTrigger = useRef<HTMLButtonElement>(null)
  const meterTrigger = useRef<HTMLButtonElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  // 待发图片（2026-10-16）：它是**这条草稿**的一部分，属于本地瞬态——刷新即丢（字节已在服务端）。
  const [pending, setPending] = useState<readonly PendingAttachment[]>([])
  const [attachError, setAttachError] = useState<string | null>(null)
  const [attachBusy, setAttachBusy] = useState(false)
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

  /** 粘贴与选图走**同一条路**：两者都只是"拿到若干 File"，处理方式必须一样。 */
  async function attachFiles(files: readonly File[]) {
    const images = files.filter((file) => file.type.startsWith('image/'))
    if (images.length === 0 || actions?.onAttachFile === undefined) return
    setAttachError(null)
    setAttachBusy(true)
    try {
      for (const file of images) {
        const attached = await actions.onAttachFile(file)
        setPending((current) => [...current, { ...attached, name: file.name }])
      }
    } catch (error) {
      // 服务端的拒绝理由是可执行的（格式/大小/当天张数），原样给用户看，不改写。
      setAttachError(error instanceof Error ? error.message : '这张图没能上传')
    } finally {
      setAttachBusy(false)
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
    const ids = pending.map((item) => item.id)
    // 先问后清：`rejected` 命中"空输入 / 带图不允许入队 / 队列已满 / 需先处理中断"时
    // 什么都不做。若这里先清，用户看到的就是"问题没发出去、文字还在、图片没了"。
    if (actions.onSend(ids) === 'rejected') return
    setPending([])
    setAttachError(null)
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
              <ul className={styles.attachments} aria-label="待发送的图片">
                {pending.map((item) => (
                  <li key={item.id} className={styles.attachment}>
                    <img className={styles.attachmentImage} src={item.previewUrl} alt="" />
                    <button
                      type="button"
                      className={styles.attachmentRemove}
                      aria-label={`移除图片 ${item.name}`}
                      onClick={() =>
                        setPending((current) => current.filter((row) => row.id !== item.id))
                      }
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}
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
                        actions.onDraftChange(event.target.value)
                      }
                  }
                  onPaste={actions === undefined ? undefined : pasteImages}
                  onKeyDown={
                    actions === undefined
                      ? undefined
                      : (event) => {
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
                <p className={styles.attachError} role="status">
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
                    disabled={actions?.onAttachFile === undefined || attachBusy}
                    onClick={() => fileInput.current?.click()}
                    aria-label={attachBusy ? '正在上传图片' : '加图片'}
                    title={attachBusy ? '正在上传图片…' : '加图片（也可以直接把图片粘贴到输入框）'}
                  >
                    {attachBusy ? <span className={styles.attachSpinner} aria-hidden="true" /> : <ImagePlus />}
                  </button>
                  <input
                    ref={fileInput}
                    className="visually-hidden"
                    type="file"
                    accept="image/png,image/jpeg,image/gif,image/webp"
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
                      disabled={actions === undefined || (!actions.canSend && !canQueue && pending.length === 0)}
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
            <div className={styles.helper} aria-hidden="true"><span>{sendShortcut === 'ctrl_enter' ? 'Ctrl / ⌘ + Enter 发送' : 'Enter 发送'}</span><span>Shift + Enter 换行</span></div>
          </>
        )}
      </div>
    </div>
  )
}
