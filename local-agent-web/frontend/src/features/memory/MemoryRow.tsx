import type { MemoryRowView, RetrievalSourceView, RetrievalView } from '../../fixtures/types'
import styles from './MemoryRow.module.css'

interface MemoryRowProps {
  memory: MemoryRowView
}

const OUTCOME_LABEL: Record<RetrievalView['outcome'], string> = {
  hit: '已命中',
  miss: '未命中',
  failed: '检索失败',
}

const OUTCOME_CLASS: Record<RetrievalView['outcome'], string> = {
  hit: styles.outcomeHit ?? '',
  miss: styles.outcomeMiss ?? '',
  failed: styles.outcomeFailed ?? '',
}

const KIND_LABEL: Record<RetrievalSourceView['kind'], string> = {
  'workspace-file': '工作区文件',
  'long-term-memory': '长期记忆',
  'session-history': '会话历史',
}

/**
 * Retrieval and long-term memory for one turn.
 *
 * Three outcomes, three different sentences: "looked and found nothing" is not
 * "could not look", and neither may be presented as if the answer had sources
 * behind it. References are locators (a path with a line range, a memory entry
 * id) — never the retrieved text, a vector or an internal score (plan §6.7).
 */
export function MemoryRow({ memory }: MemoryRowProps) {
  const { retrieval, write } = memory
  return (
    <section className={styles.row} aria-label="检索与记忆">
      <header className={styles.head}>
        <span className={styles.label}>检索与记忆</span>
        <span className={`${styles.outcome} ${OUTCOME_CLASS[retrieval.outcome]}`}>
          {OUTCOME_LABEL[retrieval.outcome]}
        </span>
        {retrieval.outcome === 'hit' ? (
          <span className={styles.count}>{retrieval.sources.length} 处来源</span>
        ) : null}
        {retrieval.outcome === 'failed' ? (
          <span className={styles.reason}>{retrieval.reason}</span>
        ) : null}
      </header>

      {retrieval.outcome === 'hit' ? (
        <ul className={styles.sources}>
          {retrieval.sources.map((source) => (
            <li key={`${source.kind}:${source.reference}`} className={styles.source}>
              <span className={styles.sourceKind}>{KIND_LABEL[source.kind]}</span>
              <span className={styles.sourceRef}>{source.reference}</span>
              <span className={styles.sourceUsage}>{source.usage}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {retrieval.note === '' ? null : <p className={styles.note}>{retrieval.note}</p>}

      {write === null ? null : (
        <p className={write.outcome === 'written' ? styles.writeOk : styles.writeFailed}>
          {write.outcome === 'written' ? '已写入长期记忆' : '写入长期记忆失败'} · {write.detail}
        </p>
      )}
    </section>
  )
}
