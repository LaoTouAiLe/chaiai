import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { previewArtifact } from '../../api/client'
import styles from './PreviewPane.module.css'

function columnName(index: number): string {
  let name = ''
  for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26)) name = String.fromCharCode(65 + (n - 1) % 26) + name
  return name
}

export function SpreadsheetView({ path, workspaceId, scope }: {
  path: string; workspaceId: string | null; scope: 'workspace' | 'attachments'
}) {
  const [sheet, setSheet] = useState<string>()
  const [offset, setOffset] = useState(0)
  useEffect(() => { setSheet(undefined); setOffset(0) }, [path, workspaceId, scope])
  const query = useQuery({ queryKey: ['spreadsheet', path, workspaceId, scope, sheet, offset],
    queryFn: () => previewArtifact(path, workspaceId ?? undefined, scope, offset, 1, sheet, 'formatted') })
  const data = query.data
  const columns = Math.max(1, ...data?.rows?.map(row => row.length) ?? [])
  return <div className={styles.document}>
    <div className={styles.sheetViewport}>
      {query.isPending ? <p role="status" className={styles.hint}>正在读取工作表…</p> : null}
      {query.isError ? <div role="alert" className={styles.state}>工作表读取失败。<button onClick={() => void query.refetch()}>重试</button></div> : null}
      {data ? <table className={styles.sheetGrid} aria-label={data.sheet ?? '工作表'}>
        <thead><tr><th aria-label="行号" />{Array.from({ length: columns }, (_, i) => <th key={i}>{columnName(i)}</th>)}</tr></thead>
        <tbody>{data.rows?.map((row, index) => <tr key={offset + index}><th scope="row">{offset + index + 1}</th>{row.map((cell, i) => <td key={i} title={cell}>{cell}</td>)}</tr>)}</tbody>
      </table> : null}
    </div>
    <div className={styles.sheetTabs} role="tablist" aria-label="工作表">
      {data?.sheets?.map(name => <button role="tab" aria-selected={data.sheet === name} key={name} onClick={() => { setSheet(name); setOffset(0) }}>{name}</button>)}
    </div>
    <div className={styles.readerTools}>
      <button disabled={offset === 0 || query.isFetching} onClick={() => setOffset(Math.max(0, offset - 200))}>上一组</button>
      <span>第 {offset + 1}–{offset + (data?.rows?.length ?? 0)} 行{data?.beyond_limit ? ' · 仅显示前 200 列' : ''}</span>
      <button disabled={!data?.has_more || query.isFetching} onClick={() => setOffset(data?.next_offset ?? offset)}>下一组</button>
    </div>
  </div>
}
