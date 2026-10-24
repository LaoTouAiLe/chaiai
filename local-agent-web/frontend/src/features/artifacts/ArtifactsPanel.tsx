import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import type { ArtifactKind } from '../../api/client'
import { useArtifacts, useArtifactsDetail } from '../../api/queries'
import { getArtifacts } from '../../api/client'
import { Badge } from '../../components/ui/Badge'
import { Button } from '../../components/ui/Button'
import { EmptyState } from '../../components/ui/EmptyState'
import styles from './ArtifactsPanel.module.css'

const KIND_LABEL: Record<ArtifactKind, string> = { image: '图片', file: '文件', link: '链接' }
const PAGE_SIZE = 50

function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

/** 链接直接新窗口打开；不是链接就交给右栏/外部处理。 */
function isLink(location: string): boolean {
  return /^https?:\/\//i.test(location)
}

/**
 * 产物（§5.5，2026-10-22 从数据中台第六个 tab 提为独立页）。
 *
 * 这一页的价值全在"点开之后"：链接外开、图片看缩略图、文件交给右栏预览。
 * 只有列表而没有打开能力的产物页是没有意义的——所以每一行都必须有可执行的动作。
 */
export function ArtifactsPanel({
  onClose,
  onOpenFile,
  onOpenSession,
}: {
  /** 返回会话。整页必须有一个明确的出口，否则只能靠侧栏再点一次。 */
  onClose?: () => void
  /** 交给右栏/宿主打开工作区内的文件（相对路径）。 */
  onOpenFile?: (path: string) => void
  onOpenSession?: (sessionId: string) => void
}) {
  const [kind, setKind] = useState<ArtifactKind | 'all'>('all')
  const [days, setDays] = useState(30)
  const [page, setPage] = useState(0)
  // 预览要带上条目所属的工作区：产物跨多个工作区，不带就会按"启动工作区"解析而读不到。
  const [preview, setPreview] = useState<{ path: string; workspaceId?: string; scope?: 'workspace' | 'attachments' } | null>(null)

  const queryClient = useQueryClient()
  const [refreshing, setRefreshing] = useState(false)
  const filters = {
    kind: kind === 'all' ? undefined : kind,
    days,
    offset: page * PAGE_SIZE,
    limit: PAGE_SIZE,
  }
  const query = useArtifacts(filters, true)
  const detail = useArtifactsDetail(preview?.path ?? null, preview?.workspaceId, preview?.scope)

  /**
   * 刷新：**必须绕过服务端 60 秒缓存**。普通 `refetch` 只会再打一次同一份缓存结果，
   * 用户看到"点了刷新还是旧数据"，会以为产物没生成。
   */
  const onRefresh = async () => {
    setRefreshing(true)
    try {
      const fresh = await getArtifacts(filters, true)
      // key 与 `useArtifacts` 里的一致；React Query 对对象 key 做稳定序列化，同形对象可命中。
      queryClient.setQueryData(['data-center', 'artifacts', filters], fresh)
    } finally {
      setRefreshing(false)
    }
  }

  const data = query.data
  const entries = data?.entries ?? []
  const totalPages = data === undefined ? 1 : Math.max(1, Math.ceil(data.total / PAGE_SIZE))

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <span className={styles.eyebrow}>回顾</span>
          <h1 className={styles.title}>产物</h1>
          <p className={styles.subtitle}>
            历史会话里生成过的图片、写出的文件、引用过的链接。点一条看内容，或回到它出自哪一轮。
          </p>
        </div>
        <div className={styles.actions}>
          <Button size="sm" onClick={() => void onRefresh()} disabled={refreshing}>
            {refreshing ? '刷新中…' : '刷新'}
          </Button>
          {onClose ? <Button size="sm" variant="ghost" onClick={onClose}>返回会话</Button> : null}
        </div>
      </header>

      <div className={styles.filters}>
        <label>
          类型
          <select value={kind} onChange={(event) => { setKind(event.target.value as ArtifactKind | 'all'); setPage(0) }}>
            <option value="all">全部</option>
            <option value="image">图片</option>
            <option value="file">文件</option>
            <option value="link">链接</option>
          </select>
        </label>
        <label>
          时间范围
          <select value={days} onChange={(event) => { setDays(Number(event.target.value)); setPage(0) }}>
            <option value={7}>最近 7 天</option>
            <option value={30}>最近 30 天</option>
            <option value={90}>最近 90 天</option>
          </select>
        </label>
        <span className={styles.count}>
          {data?.incomplete ? '已扫描范围内' : '共'} {data?.total ?? 0} 项 · 扫描 {data?.scanned_sessions ?? 0} 个会话
        </span>
      </div>

      {data?.notices?.map((note) => <p key={note} role="status" className={styles.notice}>{note}</p>)}

      {query.isError ? <p role="alert" className={styles.notice}>产物读取失败，请点刷新重试。</p> : null}

      {!query.isPending && entries.length === 0 ? (
        <EmptyState
          compact
          title="这段时间没有产物"
          description="Agent 生成的图片、写出的文件与引用过的链接会自动出现在这里，不需要额外配置。"
        />
      ) : null}

      <ul className={styles.list}>
        {entries.map((entry) => (
          <li key={entry.artifact_id} className={styles.row}>
            {entry.kind === 'image' && isLink(entry.location) ? (
              // 外链图片直接用 img 预览；本地附件没有内容端点，退化成文件名按钮。
              <img className={styles.thumb} src={entry.location} alt="" loading="lazy" />
            ) : (
              <span className={styles.kindIcon} aria-hidden="true">{entry.kind === 'image' ? '▣' : entry.kind === 'link' ? '↗' : '▤'}</span>
            )}
            <div className={styles.body}>
              <div className={styles.titleRow}>
                <Badge tone={entry.kind === 'image' ? 'accent' : 'neutral'}>{KIND_LABEL[entry.kind]}</Badge>
                <button
                  type="button"
                  className={styles.name}
                  onClick={() => {
                    if (isLink(entry.location)) { window.open(entry.location, '_blank', 'noopener'); return }
                    if (entry.kind === 'file' && onOpenFile) { onOpenFile(entry.location); return }
                    setPreview({ path: entry.location, workspaceId: entry.workspace_id ?? undefined, scope: entry.scope })
                  }}
                >
                  {entry.title}
                </button>
                {entry.external ? <Badge tone="warn">工作区外</Badge> : null}
              </div>
              <code className={styles.location}>{entry.location}</code>
              <div className={styles.meta}>
                <span>{entry.session_title}</span>
                <span>{formatTime(entry.created_at)}</span>
                {onOpenSession ? (
                  <button type="button" className={styles.link} onClick={() => onOpenSession(entry.session_id)}>
                    回到来源会话
                  </button>
                ) : null}
              </div>
            </div>
          </li>
        ))}
      </ul>

      {totalPages > 1 ? (
        <div className={styles.pager}>
          <Button size="sm" variant="ghost" disabled={page === 0} onClick={() => setPage((value) => Math.max(0, value - 1))}>
            上一页
          </Button>
          <span>{page + 1} / {totalPages}</span>
          <Button size="sm" variant="ghost" disabled={page + 1 >= totalPages} onClick={() => setPage((value) => value + 1)}>
            下一页
          </Button>
        </div>
      ) : null}

      {preview !== null ? (
        <div className={styles.previewOverlay} role="presentation" onMouseDown={() => setPreview(null)}>
          <div className={styles.preview} role="dialog" aria-modal="true" aria-label="产物内容" onMouseDown={(event) => event.stopPropagation()}>
            <header className={styles.previewHead}>
              <code>{preview.path}</code>
              <Button size="sm" variant="ghost" onClick={() => setPreview(null)}>关闭</Button>
            </header>
            {detail.isPending ? <p className={styles.previewBody}>正在读取…</p> : null}
            {detail.isError ? <p className={styles.previewBody}>这个位置读不到内容（可能是工作区外的路径或已被删除）。</p> : null}
            {detail.data?.kind === 'image' ? (
              <img className={styles.previewImage} src={detail.data.url ?? undefined} alt="" />
            ) : null}
            {detail.data?.kind === 'text' ? <pre className={styles.previewBody}>{detail.data.text}</pre> : null}
            {/* 以前这里对非 text/image 的 kind 什么都不渲染，弹窗是空白的（2026-10-22 实测）。 */}
            {detail.data?.kind === 'too_large' ? (
              <p className={styles.previewBody}>
                这个文件 {Math.round(detail.data.size_bytes / 1024)} KB，超过预览上限（文本 256 KB / 图片 2 MB）。
              </p>
            ) : null}
            {detail.data?.kind === 'unreadable' ? (
              <p className={styles.previewBody}>这个文件读不出来。</p>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  )
}
