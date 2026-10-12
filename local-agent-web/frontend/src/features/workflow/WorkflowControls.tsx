import type { WorkflowDraft } from './draft'
import styles from './Workflow.module.css'

export function WorkflowControls({ draft, onChange, disabled }: {
  draft: WorkflowDraft; onChange: (draft: WorkflowDraft) => void; disabled: boolean
}) {
  const field = (key: 'pattern' | 'sourcePath' | 'expectedTestCount', label: string) => (
    <label>{label}<input value={draft[key]} onChange={(event) => onChange({ ...draft, [key]: event.target.value })} /></label>
  )
  return <fieldset className={styles.controls} disabled={disabled}>
    <label>执行方式<select value={draft.name} onChange={(event) => onChange({ ...draft, name: event.target.value as WorkflowDraft['name'] })}>
      <option value="free">自由循环</option><option value="readonly_research">只读调查</option>
      <option value="code_change">代码修改与验证</option><option value="delegated_research">委派只读调查</option>
    </select></label>
    {draft.name === 'readonly_research' ? <>
      {field('pattern', '相对文件匹配（如 docs/*.md）')}
      <label>关键词（每行一个，可留空）<textarea rows={2} value={draft.keywords} onChange={(event) => onChange({ ...draft, keywords: event.target.value })} /></label>
    </> : null}
    {draft.name === 'code_change' ? <>
      {field('sourcePath', '源码相对路径')}
      <label>测试相对路径（每行一个）<textarea rows={2} value={draft.testPaths} onChange={(event) => onChange({ ...draft, testPaths: event.target.value })} /></label>
      {field('expectedTestCount', '预期测试数')}
      <p>只修改一个源码文件；使用 python -B -m unittest -v 验证，沿用当前权限与审批。</p>
    </> : null}
    {draft.name === 'delegated_research' ? <>
      {draft.tasks.map((task, index) => <div key={index} className={styles.task}>
        <label>子任务 {index + 1} 目录<input value={task.scope} onChange={(event) => onChange({ ...draft, tasks: draft.tasks.map((item, position) => position === index ? { ...item, scope: event.target.value } : item) })} /></label>
        <label>子任务 {index + 1} 问题<input value={task.question} onChange={(event) => onChange({ ...draft, tasks: draft.tasks.map((item, position) => position === index ? { ...item, question: event.target.value } : item) })} /></label>
        {draft.tasks.length > 1 ? <button type="button" onClick={() => onChange({ ...draft, tasks: draft.tasks.filter((_, position) => position !== index) })}>移除子任务 {index + 1}</button> : null}
      </div>)}
      {draft.tasks.length < 3 ? <button type="button" onClick={() => onChange({ ...draft, tasks: [...draft.tasks, { scope: '.', question: '' }] })}>添加子任务</button> : null}
      <label>同时执行数<select value={draft.concurrency} onChange={(event) => onChange({ ...draft, concurrency: Number(event.target.value) as 1 | 2 })}><option value={1}>1</option><option value={2}>2</option></select></label>
    </> : null}
  </fieldset>
}
