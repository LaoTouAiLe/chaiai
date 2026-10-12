import type { mapWorkflow } from './adapters'
import styles from './Workflow.module.css'

export function WorkflowStatus({ view }: { view: ReturnType<typeof mapWorkflow> }) {
  return <section className={styles.status} aria-label="工作流进度">
    <strong>{view.title} · {view.phase}</strong><p>{view.detail}</p>
    {view.error ? <p role="status">{view.error}</p> : null}
    {view.children.map((child) => <p key={child.id}>{child.text}{child.error ? `；${child.error}` : ''}</p>)}
  </section>
}
