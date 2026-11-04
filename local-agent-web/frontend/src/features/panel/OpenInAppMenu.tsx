import { useEffect, useRef, useState } from 'react'

import { openInApp, openInAppIconUrl } from '../../api/client'
import { useOpenInApps } from '../../api/queries'
import styles from './OpenInAppMenu.module.css'

/**
 * 「打开方式」（2026-11-03，§15.11.4）。
 *
 * 清单来自服务端对**本机**的探测（App Paths / 卸载记录 / 已知安装路径 / PATH），不是写死的
 * 固定菜单：没装的那一项不会出现。探测不到任何应用时整个入口不渲染——比给一个空菜单诚实。
 *
 * 启动成功不弹提示：用户自己的编辑器会到前台，那本身就是反馈；失败才在这里说明原因。
 */
export function OpenInAppMenu({ path, workspaceId }: {
  path: string
  /** 没有工作区时后端无法判定边界，入口不出现。 */
  workspaceId: string | null
}) {
  const apps = useOpenInApps(workspaceId !== null)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [brokenIcons, setBrokenIcons] = useState<ReadonlySet<string>>(new Set())
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const firstItemRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    firstItemRef.current?.focus()
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current?.contains(event.target as Node) !== true) setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => { document.removeEventListener('mousedown', onPointerDown) }
  }, [open])

  /**
   * Escape 只关菜单、不关面板：面板自己的 Escape 处理在 `aside` 上，事件会先经过这里——
   * 在这里 `stopPropagation()` 面板就听不到。挂在 document 上来不及（React 的根监听先跑）。
   */
  const closeOnEscape = (event: React.KeyboardEvent) => {
    if (event.key !== 'Escape') return
    event.stopPropagation()
    setOpen(false)
    triggerRef.current?.focus()
  }

  // 文件换了就把上一次的失败清掉：那是另一个文件的事。
  useEffect(() => { setFailure(null) }, [path])

  const list = apps.data?.apps ?? []
  if (apps.data?.available !== true || list.length === 0) return null

  const run = (appId: string) => {
    setBusy(appId)
    setFailure(null)
    void openInApp(appId, path, workspaceId ?? undefined)
      .then(() => { setOpen(false) })
      .catch((cause: unknown) => {
        setFailure(cause instanceof Error ? cause.message : '没能启动这个应用。')
      })
      .finally(() => { setBusy(null) })
  }

  return (
    <div className={styles.root} ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className={styles.trigger}
        aria-haspopup="menu"
        aria-expanded={open}
        title="用本机的程序打开这个文件"
        onClick={() => setOpen((value) => !value)}
        onKeyDown={closeOnEscape}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
          <path d="M14 4h6v6M20 4l-8 8M11 5H6a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-5" />
        </svg>
        <span>打开方式</span>
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      {open ? (
        <div className={styles.menu} role="menu" aria-label="打开方式" onKeyDown={closeOnEscape}>
          {list.map((app, index) => (
            <button
              key={app.id}
              ref={index === 0 ? firstItemRef : undefined}
              type="button"
              role="menuitem"
              className={styles.item}
              disabled={busy !== null}
              onClick={() => run(app.id)}
            >
              <span className={styles.iconSlot} aria-hidden="true">
                {app.icon_url !== null && !brokenIcons.has(app.id) ? (
                  <img
                    className={styles.icon}
                    src={openInAppIconUrl(app.id)}
                    alt=""
                    width={16}
                    height={16}
                    onError={() => setBrokenIcons((previous) => new Set(previous).add(app.id))}
                  />
                ) : (
                  <span className={styles.iconFallback}>{app.label.slice(0, 1).toUpperCase()}</span>
                )}
              </span>
              <span className={styles.label}>{app.label}</span>
              {index === 0 ? <span className={styles.hint}>默认</span> : null}
              {busy === app.id ? <span className={styles.hint}>正在打开…</span> : null}
            </button>
          ))}
          {failure !== null ? <p role="alert" className={styles.failure}>{failure}</p> : null}
        </div>
      ) : null}
    </div>
  )
}
