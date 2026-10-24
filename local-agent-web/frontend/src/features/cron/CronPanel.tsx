import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { ApiError, createCronJob, deleteCronJob, runCronJobNow, updateCronJob } from '../../api/client'
import { useCronJobs, useCronRuns } from '../../api/queries'
import { Badge } from '../../components/ui/Badge'
import { Button } from '../../components/ui/Button'
import { EmptyState } from '../../components/ui/EmptyState'
import { describeCron } from './cronDescribe'
import styles from './CronPanel.module.css'

/** 预设频率。第一版不给"自然语言转 cron"——那要模型调用，属另一层能力（§21.5）。 */
const PRESETS: { label: string; schedule: string }[] = [
  { label: '每天 9:00', schedule: '0 9 * * *' },
  { label: '每天 18:00', schedule: '0 18 * * *' },
  { label: '每小时', schedule: '0 * * * *' },
  { label: '每 30 分钟', schedule: '*/30 * * * *' },
  { label: '每周一 9:00', schedule: '0 9 * * 1' },
]

const STATUS_TONE = {
  succeeded: 'success',
  failed: 'danger',
  blocked: 'warn',
  running: 'accent',
} as const

function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

/**
 * 定时任务（§21.5，2026-10-22 J2）。
 *
 * 两条必须让用户看到的事实：
 * 1. 调度线程没跑时，任务**永远不会触发**——页面顶部直接显示；
 * 2. 运行记录能点进那次运行的会话——否则用户只能看到"失败"却不知道发生了什么。
 */
export function CronPanel({ onOpenSession }: { onOpenSession?: (sessionId: string) => void }) {
  const queryClient = useQueryClient()
  const jobs = useCronJobs()
  const [creating, setCreating] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState({ name: '', prompt: '', schedule: PRESETS[0].schedule })

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['cron'] })
  const fail = (cause: unknown) =>
    setError(cause instanceof ApiError ? cause.message : '操作失败，请重试。')

  const create = useMutation({
    // 第一版固定 agent 模式：定时任务基本都要动手，纯聊天模式在这里没有意义。
    mutationFn: () => createCronJob({
      name: form.name, prompt: form.prompt, schedule: form.schedule, mode: 'agent' as const,
    }),
    onSuccess: async () => {
      setCreating(false)
      setForm({ name: '', prompt: '', schedule: PRESETS[0].schedule })
      setError(null)
      await refresh()
    },
    onError: fail,
  })

  const toggle = useMutation({
    mutationFn: (input: { jobId: string; enabled: boolean }) =>
      updateCronJob(input.jobId, { enabled: input.enabled }),
    onSuccess: async () => {
      setError(null)
      await refresh()
    },
    onError: fail,
  })

  const trigger = useMutation({
    mutationFn: (jobId: string) => runCronJobNow(jobId),
    onSuccess: async () => {
      setError(null)
      await refresh()
    },
    onError: fail,
  })

  const remove = useMutation({
    mutationFn: (jobId: string) => deleteCronJob(jobId),
    onSuccess: async () => {
      setError(null)
      await refresh()
    },
    onError: fail,
  })

  const data = jobs.data
  const list = data?.jobs ?? []
  // 表单里的实时解释：合法就把 cron 翻成中文，看不懂就说明原因——但**不阻止创建**，
  // 后端 croniter 才是权威（它认识一些这里不解释的扩展写法）。
  const description = describeCron(form.schedule)
  const matchedPreset = PRESETS.some((preset) => preset.schedule === form.schedule) ? form.schedule : ''

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <span className={styles.eyebrow}>自动化</span>
          <h1 className={styles.title}>定时任务</h1>
          <p className={styles.subtitle}>
            按 cron 排期自动跑一轮。会话在后台创建，不抢当前会话；遇到需要审批的动作会被直接拒绝并记为失败。
          </p>
        </div>
        <Button variant="primary" size="sm" onClick={() => setCreating((open) => !open)}>
          {creating ? '取消' : '新建任务'}
        </Button>
      </header>

      {data?.scheduler_running === false ? (
        <p role="alert" className={styles.notice}>
          调度器未运行：任务会保存但**不会自动触发**。请从源码方式启动服务（`local-agent` 命令）。
        </p>
      ) : null}

      {error !== null ? <p role="alert" className={styles.notice}>{error}</p> : null}

      {creating ? (
        <form
          className={styles.form}
          onSubmit={(event) => {
            event.preventDefault()
            create.mutate()
          }}
        >
          <label className={styles.field}>
            <span>任务名</span>
            <input
              value={form.name}
              maxLength={80}
              placeholder="例如：每日提交汇总"
              onChange={(event) => setForm({ ...form, name: event.target.value })}
            />
          </label>
          <label className={styles.field}>
            <span>提示词</span>
            <textarea
              value={form.prompt}
              maxLength={4000}
              rows={3}
              placeholder="到点后要它做什么，就像在输入框里说一句话"
              onChange={(event) => setForm({ ...form, prompt: event.target.value })}
            />
          </label>
          <label className={styles.field}>
            <span>频率</span>
            <div className={styles.scheduleRow}>
              <select
                aria-label="常用频率"
                value={matchedPreset}
                onChange={(event) => {
                  // 选"自定义"时保留输入框现有内容，方便在预设基础上改。
                  if (event.target.value !== '') setForm({ ...form, schedule: event.target.value })
                }}
              >
                <option value="">自定义</option>
                {PRESETS.map((preset) => (
                  <option key={preset.schedule} value={preset.schedule}>
                    {preset.label}（{preset.schedule}）
                  </option>
                ))}
              </select>
              <input
                aria-label="cron 表达式"
                aria-describedby="cron-description"
                value={form.schedule}
                spellCheck={false}
                placeholder="分 时 日 月 周，例如 0 9 * * *"
                onChange={(event) => setForm({ ...form, schedule: event.target.value })}
              />
            </div>
            <span
              id="cron-description"
              role="status"
              className={description.ok ? styles.hint : styles.lastError}
            >
              {description.ok
                ? `= ${description.text}`
                : `${description.reason}；仍可创建，最终由服务端校验`}
            </span>
          </label>
          <div className={styles.formActions}>
            <Button type="submit" variant="primary" disabled={create.isPending || !form.name.trim() || !form.prompt.trim()}>
              {create.isPending ? '创建中…' : '创建'}
            </Button>
            <span className={styles.hint}>投递结果只落本地运行记录，不推送外部渠道。</span>
          </div>
        </form>
      ) : null}

      {list.length === 0 && !jobs.isPending ? (
        <EmptyState
          compact
          title="还没有定时任务"
          description="适合「每天汇总一次提交」「每周清理构建产物」这类不需要人盯着的活。"
        />
      ) : null}

      <ul className={styles.list}>
        {list.map((job) => {
          const schedule = describeCron(job.schedule)
          return (
          <li key={job.job_id} className={styles.row}>
            <div className={styles.rowMain}>
              <strong className={styles.name}>{job.name}</strong>
              <code className={styles.schedule}>{job.schedule}</code>
              {schedule.ok ? <span className={styles.time}>{schedule.text}</span> : null}
              {job.last_status === null ? null : (
                <Badge tone={STATUS_TONE[job.last_status as keyof typeof STATUS_TONE] ?? 'neutral'}>
                  {job.last_status === 'succeeded' ? '上次成功'
                    : job.last_status === 'failed' ? '上次失败'
                      : job.last_status === 'blocked' ? '被审批拦下'
                        : '运行中'}
                </Badge>
              )}
              {job.enabled ? null : <Badge tone="neutral">已暂停</Badge>}
              <span className={styles.time}>下次 {formatTime(job.next_run_at)}</span>
            </div>
            <div className={styles.rowActions}>
              <Button size="sm" variant="ghost" onClick={() => trigger.mutate(job.job_id)} disabled={trigger.isPending}>
                立即运行
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => toggle.mutate({ jobId: job.job_id, enabled: !job.enabled })}
                disabled={toggle.isPending}
              >
                {job.enabled ? '暂停' : '恢复'}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setExpanded(expanded === job.job_id ? null : job.job_id)}>
                {expanded === job.job_id ? '收起记录' : '运行记录'}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  if (window.confirm(`删除任务「${job.name}」？运行记录会一并删除。`)) remove.mutate(job.job_id)
                }}
              >
                删除
              </Button>
            </div>
            {job.last_error ? <p className={styles.lastError}>{job.last_error}</p> : null}
            {expanded === job.job_id ? <RunList jobId={job.job_id} onOpenSession={onOpenSession} /> : null}
          </li>
          )
        })}
      </ul>
    </div>
  )
}

function RunList({ jobId, onOpenSession }: { jobId: string; onOpenSession?: (sessionId: string) => void }) {
  const runs = useCronRuns(jobId)
  if (runs.isPending) return <p className={styles.hint}>正在读取运行记录…</p>
  if (runs.isError) return <p className={styles.hint}>运行记录读取失败。</p>
  const rows = runs.data?.runs ?? []
  if (rows.length === 0) return <p className={styles.hint}>还没有运行记录。</p>
  return (
    <ul className={styles.runs}>
      {rows.map((run) => (
        <li key={run.run_id}>
          <Badge tone={STATUS_TONE[run.status as keyof typeof STATUS_TONE] ?? 'neutral'}>{run.status}</Badge>
          <span className={styles.time}>{formatTime(run.started_at)}</span>
          {run.summary ? <span className={styles.summary}>{run.summary}</span> : null}
          {run.session_id && onOpenSession ? (
            <button type="button" className={styles.link} onClick={() => onOpenSession(run.session_id!)}>
              打开那次会话
            </button>
          ) : null}
        </li>
      ))}
    </ul>
  )
}
