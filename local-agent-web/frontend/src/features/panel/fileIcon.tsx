import styles from './fileIcon.module.css'

/**
 * 按扩展名给文件配图标与颜色（2026-10-23）。
 *
 * 没有引入 `@vscode/codicons`（hermes 用的那套）：我们只需要区分"这是代码还是图片
 * 还是表格"十来种，自带字体文件不划算。这里按**类别**上色 + 一个形状，
 * 十几像素下反而比多色细图标更容易分辨。
 */
type Category = 'code' | 'data' | 'doc' | 'image' | 'archive' | 'text' | 'other'

const BY_EXT: Record<string, Category> = {
  ts: 'code', tsx: 'code', js: 'code', jsx: 'code', py: 'code', rs: 'code', go: 'code',
  java: 'code', c: 'code', h: 'code', cpp: 'code', cs: 'code', rb: 'code', php: 'code',
  css: 'code', scss: 'code', html: 'code', vue: 'code', svelte: 'code',
  json: 'data', yaml: 'data', yml: 'data', toml: 'data', xml: 'data', csv: 'data',
  sql: 'data', db: 'data', sqlite: 'data',
  md: 'doc', docx: 'doc', doc: 'doc', pdf: 'doc', txt: 'text', log: 'text',
  xlsx: 'doc', xls: 'doc', pptx: 'doc', ppt: 'doc',
  png: 'image', jpg: 'image', jpeg: 'image', gif: 'image', webp: 'image', svg: 'image',
  ico: 'image', bmp: 'image',
  zip: 'archive', tar: 'archive', gz: 'archive', rar: 'archive', '7z': 'archive',
}

function categoryOf(path: string): Category {
  const name = path.split('/').pop() ?? path
  const dot = name.lastIndexOf('.')
  if (dot === -1) return 'other'
  return BY_EXT[name.slice(dot + 1).toLowerCase()] ?? 'other'
}

export function FileIcon({ path, size = 14 }: { path: string; size?: number }) {
  const category = categoryOf(path)
  return (
    <span className={`${styles.icon} ${styles[category]}`} aria-hidden="true">
      <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.2">
        {category === 'image' ? (
          <>
            <rect x="2" y="3" width="12" height="10" rx="1.5" />
            <circle cx="5.6" cy="6.4" r="1" />
            <path d="M2.6 12l3.4-3.4 2.4 2.4 2-2 3 3" />
          </>
        ) : category === 'archive' ? (
          <>
            <path d="M2.5 4.5h11v8h-11z" />
            <path d="M2.5 4.5L4 2h8l1.5 2.5M6.5 7.5h3" />
          </>
        ) : category === 'data' ? (
          <>
            <path d="M2.5 3.5h11v9h-11z" />
            <path d="M2.5 6.5h11M6.5 6.5v6" />
          </>
        ) : category === 'code' ? (
          <>
            <path d="M4.5 3.5h7v9h-7z" />
            <path d="M6 6.5L4.8 8 6 9.5M10 6.5 11.2 8 10 9.5" />
          </>
        ) : (
          <>
            <path d="M4 2h5l3 3v9H4z" />
            <path d="M9 2v3h3" />
          </>
        )}
      </svg>
    </span>
  )
}
