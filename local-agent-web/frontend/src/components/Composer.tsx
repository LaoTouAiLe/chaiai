import { useRef, useState, type ClipboardEvent } from 'react'
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
import { PlanStrip } from '../features/plan/PlanStrip'
import { ModelMenu } from '../features/settings/ModelMenu'
import { useGeneralPreferences } from '../features/settings/preferencesContext'
import { ContextMeter, MeterPanel } from './ContextMeter'
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
   * 返回 **是否真的接受了这次提交**：调用方在一轮进行中、提交在飞、待审批或需核对时
   * 会静默拒绝（不发请求、不报错）。**必须先问它、再清待发图片**——否则图片会凭空消失，
   * 而草稿还在输入框里（2026-10-20 实测）。
   */
  onSend: (attachmentIds: readonly string[]) => boolean
  /**
   * 把一张图交给服务端（2026-10-16）：成功给回引用与一个**本地预览地址**，失败**抛错**。
   *
   * 为什么预览用本地 object URL 而不是回读服务端：上传是流式的，本地那份就是同一张图，
   * 回读多一次往返、还得处理"刚传完就刷新"的时序。字节的权威仍在服务端，这里只是缩略图。
   */
  onAttachFile?: (file: File) => Promise<{ id: string; previewUrl: string }>
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
export function Composer({ mode = 'agent', session, plan, meter, takeover, modelMenu, state, stopped = false, actions, modelMenuActions, approvalActions }: ComposerProps) {
  // `animations` 不再需要：用它的是那条压缩提示，而它 2026-10-16 搬进了对话流。
  const { send_shortcut: sendShortcut } = useGeneralPreferences()
  const modelTrigger = useRef<HTMLButtonElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  // 待发图片（2026-10-16）：它是**这条草稿**的一部分，属于本地瞬态——刷新即丢（字节已在服务端）。
  const [pending, setPending] = useState<readonly PendingAttachment[]>([])
  const [attachError, setAttachError] = useState<string | null>(null)
  const [attachBusy, setAttachBusy] = useState(false)

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

  /** 发送：把待发图片的**引用**一起交出去；**只有调用方接受后才清空队列**。 */
  function send() {
    if (actions === undefined) return
    const ids = pending.map((item) => item.id)
    // 先问后清：`onSend` 命中"一轮进行中 / 提交在飞 / 待审批 / 需核对"时会返回 false
    // 且什么都不做。若这里先清队列，用户看到的就是"问题没发出去、文字还在、图片没了"。
    if (!actions.onSend(ids)) return
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
  const disabledReason = state?.disabledReason ?? null
  const phase = state?.phase ?? null
  const meterOpen = state?.meterOpen ?? false
  // A running turn blocks *sending*, not writing: the draft stays editable, the
  // input is never replaced by a stop button, and the reason is derived from the
  // run state itself rather than repeated per fixture (plan §T30, ARCHITECTURE §15.2).
  const sendBlockedReason = session.status === 'running' ? SEND_BLOCKED_TEXT : null

  return (
    <div className={styles.dock}>
      <div className={styles.stack}>
        {plan === null ? null : <PlanStrip plan={plan} />}

        {approval !== null && !awaitingDecision ? (
          <ApprovalCard key={approval.approvalId} approval={approval} actions={approvalActions} />
        ) : null}

        {modelMenu === null || awaitingDecision ? null : <ModelMenu menu={modelMenu} actions={modelMenuActions}
          onClose={actions?.onCloseModelMenu === undefined ? undefined : () => {
            actions.onCloseModelMenu?.()
            modelTrigger.current?.focus()
          }} />}

        {meterOpen && !awaitingDecision ? (
          <MeterPanel meter={meter} onClose={actions?.onToggleMeter} />
        ) : null}

        {awaitingDecision ? (
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
                        if (event.key === 'Enter' && sendKey && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229 && (actions.canSend || pending.length > 0)) {
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
                  <ContextMeter meter={meter} open={meterOpen} onToggle={actions?.onToggleMeter} />
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
                      disabled={actions === undefined || (!actions.canSend && pending.length === 0)}
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
