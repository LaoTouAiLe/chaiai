import { useEffect, useMemo, useRef, useState } from 'react'

import { FileIcon } from './fileIcon'
import styles from './FileTree.module.css'

export interface TreeEntry {
  /** 相对工作区根的完整路径；建树只用它。 */
  path: string
  kind: 'file' | 'dir'
  size?: number
}

interface Node {
  name: string
  path: string
  kind: 'file' | 'dir'
  size: number
  children: Node[]
}

/**
 * 文件树（2026-10-23 按实机参考图重做）。
 *
 * 推翻的是"逐层点进去"：用户原话是"树状图展示，不要点击进入下一层及展示"。
 * 现在一次给出整棵树、**默认全展开**，点目录只折叠/展开自己，不再换页。
 * 数据是扁平列表（后端 `?recursive=true`），建树放在这里——纯展示逻辑不该占后端。
 */
export function FileTree({
  entries,
  onSelectFile,
  selectedPath,
  emptyHint,
}: {
  entries: readonly TreeEntry[]
  onSelectFile?: (path: string) => void
  selectedPath?: string | null
  emptyHint?: string
}) {
  // **默认全部折叠**（2026-10-23 第二次反馈）：全展开在一个真实项目里有上千条，
  // 滚动条拉到天亮也找不到东西。现在先给顶层，点目录再展开。
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set())
  const initialised = useRef(false)
  useEffect(() => {
    if (initialised.current || entries.length === 0) return
    initialised.current = true
    setCollapsed(new Set(entries.filter((entry) => entry.kind === 'dir').map((entry) => entry.path)))
  }, [entries])

  const tree = useMemo(() => buildTree(entries), [entries])

  if (entries.length === 0) {
    return <p className={styles.hint}>{emptyHint ?? '这里还没有文件。'}</p>
  }

  const toggle = (path: string) => {
    setCollapsed((previous) => {
      const next = new Set(previous)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  const renderNodes = (nodes: Node[], depth: number) =>
    nodes.map((node) => {
      const isCollapsed = collapsed.has(node.path)
      if (node.kind === 'dir') {
        return (
          <li key={node.path}>
            <button
              type="button"
              className={styles.dirRow}
              style={{ paddingLeft: `${String(depth * 14 + 4)}px` }}
              aria-expanded={!isCollapsed}
              onClick={() => toggle(node.path)}
            >
              <span className={isCollapsed ? `${styles.caret} ${styles.caretClosed}` : styles.caret} aria-hidden="true">
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="m6 9 6 6 6-6" />
                </svg>
              </span>
              <span className={styles.dirName}>{node.name}</span>
            </button>
            {isCollapsed ? null : <ul className={styles.list}>{renderNodes(node.children, depth + 1)}</ul>}
          </li>
        )
      }
      return (
        <li key={node.path}>
          <button
            type="button"
            className={node.path === selectedPath ? `${styles.fileRow} ${styles.fileRowActive}` : styles.fileRow}
            style={{ paddingLeft: `${String(depth * 14 + 18)}px` }}
            onClick={() => onSelectFile?.(node.path)}
            title={node.path}
          >
            <FileIcon path={node.path} />
            <span className={styles.fileName}>{node.name}</span>
            {node.size > 0 ? <span className={styles.size}>{formatSize(node.size)}</span> : null}
          </button>
        </li>
      )
    })

  return <ul className={styles.list}>{renderNodes(tree, 0)}</ul>
}

/** 扁平路径 → 树。目录永远排在文件前面，同级按名字排序。 */
function buildTree(entries: readonly TreeEntry[]): Node[] {
  const root: Node = { name: '', path: '', kind: 'dir', size: 0, children: [] }
  const index = new Map<string, Node>([['', root]])

  const ensureDir = (path: string): Node => {
    const existing = index.get(path)
    if (existing !== undefined) return existing
    const slash = path.lastIndexOf('/')
    const parent = ensureDir(slash === -1 ? '' : path.slice(0, slash))
    const node: Node = { name: slash === -1 ? path : path.slice(slash + 1), path, kind: 'dir', size: 0, children: [] }
    parent.children.push(node)
    index.set(path, node)
    return node
  }

  for (const entry of entries) {
    if (entry.kind === 'dir') {
      ensureDir(entry.path)
      continue
    }
    const slash = entry.path.lastIndexOf('/')
    const parent = ensureDir(slash === -1 ? '' : entry.path.slice(0, slash))
    const name = slash === -1 ? entry.path : entry.path.slice(slash + 1)
    if (parent.children.some((child) => child.path === entry.path)) continue
    parent.children.push({
      name, path: entry.path, kind: 'file', size: entry.size ?? 0, children: [],
    })
  }

  const sortNodes = (nodes: Node[]): void => {
    nodes.sort((left, right) => {
      if (left.kind !== right.kind) return left.kind === 'dir' ? -1 : 1
      return left.name.localeCompare(right.name, 'zh')
    })
    nodes.forEach((node) => sortNodes(node.children))
  }
  sortNodes(root.children)
  return root.children
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${String(bytes)} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
