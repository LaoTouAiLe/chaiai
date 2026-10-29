import type { EmptyStateView } from '../../fixtures/types'
import { WorkbenchIcon } from '../../components/WorkbenchIcon'
import styles from './WelcomePanel.module.css'

const STARTERS = [
  { title: '了解项目', icon: 'folder' as const, prompt: '请阅读当前工作区，解释项目结构、主要入口和运行方式。先阅读，不修改文件。' },
  { title: '完成修改', icon: 'file' as const, prompt: '我想修改这个项目。请先了解相关代码，和我确认需求与修改计划，再开始实施。' },
  { title: '检查与验证', icon: 'terminal' as const, prompt: '请检查当前项目的构建和测试方式，说明如何验证项目。执行前先说明计划，需要审批的操作先请求确认。' },
]

/** 首屏使用现有空态事实；快捷入口只准备草稿，由用户决定是否发送。 */
export function WelcomePanel({ empty, onUsePrompt }: { empty: EmptyStateView; onUsePrompt?: (prompt: string) => void }) {
  return <section className={styles.welcome} aria-label="开始工作">
    <div className={styles.intro}>
      <div className={styles.brand}><img src="/chaiai.svg" width="34" height="34" alt="" /><span>你的本地工作空间</span></div>
      <h2>{empty.title}</h2>
      <p className={styles.description}>{empty.description}</p>
    </div>
    {empty.highlightsTitle === undefined || empty.highlights === undefined ? null : <section aria-label={empty.highlightsTitle} className={styles.starters}>
      <ol>{empty.highlights.map((item, index) => {
        const starter = STARTERS[index]
        return <li key={item}><button type="button" disabled={onUsePrompt === undefined} onClick={() => onUsePrompt?.(starter?.prompt ?? item)}>
          <div className={styles.cardTop}><span className={styles.cardIcon}><WorkbenchIcon name={starter?.icon ?? 'chat'} size={19} /></span><WorkbenchIcon name="arrow" size={15} /></div>
          <strong>{starter?.title ?? '开始一个任务'}</strong><span className={styles.detail}>{item}</span>
        </button></li>
      })}</ol>
    </section>}
    {empty.hint === undefined ? null : <p className={styles.hint}>{empty.hint}</p>}
  </section>
}
