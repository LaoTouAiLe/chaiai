import type { MemoryRowView, RetrievalSourceView, RetrievalView } from '../../fixtures/types'
import type { MemoryWriteView } from '../../fixtures/types'
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
 * 写入结果的三种事实，三种说法（§10.4）：写了 / 写失败 / **有候选但被跳过**。
 *
 * 第三种必须出现并带上原因——旧行为是这一行整段消失，于是"这轮没东西可记"与"想记但被跳过了"
 * 看起来一模一样，用户不知道"打开开关就能记上"。
 */
const WRITE_LABEL: Record<MemoryWriteView['outcome'], string> = {
  written: '已写入长期记忆',
  failed: '写入长期记忆失败',
  skipped: '本轮未写入长期记忆',
}

const WRITE_CLASS: Record<MemoryWriteView['outcome'], string> = {
  written: styles.writeOk ?? '',
  failed: styles.writeFailed ?? '',
  skipped: styles.writeSkipped ?? '',
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
      <details open={retrieval.outcome === 'failed'} className={styles.details}>
      <summary className={styles.head}>
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
      </summary>

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
      </details>

      {write === null ? null : (
        <p className={WRITE_CLASS[write.outcome]}>
          {WRITE_LABEL[write.outcome]} · {write.detail}
        </p>
      )}
    </section>
  )
}
