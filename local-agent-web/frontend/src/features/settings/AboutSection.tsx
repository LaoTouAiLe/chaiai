// 「关于」（2026-10-27）：版本与更新、卸载。
//
// 三条口径都来自**发行形态**，不是偏好设置：
//   1. **只检查、不下载**。本项目没有自升级器；替用户覆盖安装要能替换正在运行的 exe，
//      那是另一整套东西。有新版就把发布页交给系统浏览器。
//   2. **卸载交接给 Inno**。程序自己删不掉正在运行的 chaiai.exe，所以后端只写一个延迟批处理
//      （等本进程退出 → 调 unins000.exe → 按勾选删数据）并返回 202。
//   3. **删数据必须显式勾选**，默认不删（packaging/README.md 的老约定）。目录不安全时
//      后端直接 422，不会静默降级——"勾了却没删"比报错更难排查。
//
// 这里没有"安装 ID""渠道"这类字段：本项目的发行形态只有三条（安装版 / 绿色包 / 源码），
// 编造一个 ID 只会让用户以为它有用。

import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'

import { checkForUpdate, getAppInfo, uninstallApp } from '../../api/client'
import type { AppInfoDto, UpdateCheckDto } from '../../api/client'
import styles from './PreferenceSection.module.css'

const KIND_LABEL: Record<AppInfoDto['install_kind'], string> = {
  installed: '安装版',
  portable: '绿色版（解压即用）',
  source: '源码运行',
}

const SOURCE_LABEL: Record<AppInfoDto['data_root_source'], string> = {
  environment: '环境变量 CHAIAI_HOME',
  settings: '「数据位置」里的设置',
  config: 'config.yaml 的 data_root',
  default: '平台标准目录',
}

/** GitHub 给的是 ISO 时间；解析不了就原样显示，不猜。 */
function formatPublished(value: string | null | undefined): string | null {
  if (!value) return null
  const moment = new Date(value)
  return Number.isNaN(moment.getTime()) ? value : moment.toLocaleString()
}

function ReleaseLink({ url }: { url: string | null | undefined }) {
  if (!url) return null
  // 与 WebSources 的外链同一形态：桌面壳里由系统浏览器接走。
  return (
    <>
      {' '}
      <a href={url} target="_blank" rel="noreferrer noopener">
        打开发布页
      </a>
    </>
  )
}

function UpdateResult({ result }: { result: UpdateCheckDto }) {
  if (result.status === 'update_available') {
    const published = formatPublished(result.published_at)
    return (
      <p role="status" className={styles.saved}>
        发现新版本 {result.latest_version}（当前 {result.current_version}）。
        <ReleaseLink url={result.release_url} />
        {published === null ? null : ` 发布于 ${published}。`}
      </p>
    )
  }
  if (result.status === 'up_to_date') {
    return (
      <p role="status" className={styles.saved}>
        已经是最新版本（{result.current_version}）。
      </p>
    )
  }
  return (
    <p role="status" className={styles.note}>
      {result.note ?? '检查更新失败。'}
      <ReleaseLink url={result.release_url} />
    </p>
  )
}

export function AboutSection() {
  const info = useQuery({
    queryKey: ['app-info'],
    queryFn: getAppInfo,
    staleTime: Infinity,
    refetchOnMount: 'always',
  })
  const [result, setResult] = useState<UpdateCheckDto | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [removeData, setRemoveData] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** 卸载已交接：进程马上会被脚本结束，界面只留一句话，不再发任何请求。 */
  const [scheduled, setScheduled] = useState<string | null>(null)

  const check = useMutation({
    mutationFn: checkForUpdate,
    onSuccess: (next) => {
      setResult(next)
      setError(null)
    },
    onError: (exc: Error) => setError(exc.message),
  })

  const uninstall = useMutation({
    mutationFn: (withData: boolean) => uninstallApp(withData),
    onSuccess: (view) => {
      setConfirming(false)
      setScheduled(view.message)
    },
    onError: (exc: Error) => setError(exc.message),
  })

  const view = info.data
  if (!view) {
    return (
      <section className={styles.section} role="status">
        {info.isError ? '读取应用信息失败。' : '正在读取应用信息…'}
        <button type="button" onClick={() => void info.refetch()}>
          重新读取
        </button>
      </section>
    )
  }

  const busy = check.isPending || uninstall.isPending

  return (
    <section className={styles.section}>
      <header className={styles.head}>
        <h3 className={styles.title}>版本与更新</h3>
        <p className={styles.note}>
          检查更新只查询 GitHub 上的最新发布版并提醒你，不会自动下载或安装。
        </p>
      </header>
      <dl className={styles.overview} aria-label="应用信息">
        <div>
          <dt>当前版本</dt>
          <dd>{view.version}</dd>
        </div>
        <div>
          <dt>安装形态</dt>
          <dd>{KIND_LABEL[view.install_kind]}</dd>
        </div>
        {view.install_dir == null ? null : (
          <div>
            <dt>安装目录</dt>
            <dd>{view.install_dir}</dd>
          </div>
        )}
        <div>
          <dt>数据目录</dt>
          <dd>
            {view.data_root}（来源：{SOURCE_LABEL[view.data_root_source]}）
          </dd>
        </div>
        {view.build == null ? null : (
          <div>
            <dt>构建</dt>
            <dd>
              {view.build.built_at} · 源树 {view.build.revision.slice(0, 12)}
              {view.build.dirty === '0' ? '' : `（含 ${view.build.dirty} 处未提交改动）`} · Python{' '}
              {view.build.python}
            </dd>
          </div>
        )}
      </dl>
      {result === null ? null : <UpdateResult result={result} />}
      <div className={styles.actions}>
        <button type="button" className={styles.primary} disabled={busy} onClick={() => check.mutate()}>
          {check.isPending ? '正在检查…' : '检查更新'}
        </button>
      </div>

      <header className={styles.head}>
        <h4>卸载</h4>
        <p className={styles.note}>
          {view.uninstall_available
            ? '卸载会关闭本程序并启动安装包的卸载程序；程序文件被删除，数据按你的选择处理。'
            : (view.uninstall_note ?? '当前形态没有可卸载的安装。')}
        </p>
      </header>
      {scheduled !== null ? (
        <p role="status" className={styles.saved}>
          {scheduled}
        </p>
      ) : (
        <>
          {confirming ? (
            <div className={styles.fields}>
              <div className={styles.field}>
                <div className={styles.fieldText}>
                  <strong>同时删除数据目录</strong>
                  <small>
                    {view.can_remove_data
                      ? `勾选后会在卸载完成后删除 ${view.data_root}，其中包含全部会话、记忆、附件与日志，删除后无法恢复。不勾选则只删程序，数据留在原处。`
                      : (view.remove_data_note ?? '这个数据目录不能自动删除。')}
                  </small>
                </div>
                <span className={styles.switch}>
                  <input
                    type="checkbox"
                    aria-label="同时删除数据目录"
                    disabled={!view.can_remove_data || uninstall.isPending}
                    checked={removeData && view.can_remove_data}
                    onChange={(event) => setRemoveData(event.target.checked)}
                  />
                  <span className={styles.switchTrack} aria-hidden="true" />
                </span>
              </div>
            </div>
          ) : null}
          <div className={styles.actions}>
            {confirming ? (
              <>
                <button
                  type="button"
                  className={styles.primary}
                  disabled={uninstall.isPending}
                  onClick={() => uninstall.mutate(removeData && view.can_remove_data)}
                >
                  {uninstall.isPending ? '正在卸载…' : '确认卸载'}
                </button>
                <button
                  type="button"
                  disabled={uninstall.isPending}
                  onClick={() => {
                    setConfirming(false)
                    setRemoveData(false)
                    setError(null)
                  }}
                >
                  取消
                </button>
              </>
            ) : (
              <button
                type="button"
                disabled={!view.uninstall_available || uninstall.isPending}
                onClick={() => {
                  setConfirming(true)
                  setError(null)
                }}
              >
                卸载 {view.install_kind === 'installed' ? 'chaiai' : ''}
              </button>
            )}
          </div>
        </>
      )}
      {error ? (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      ) : null}
      <p className={styles.note}>
        卸载不会删除数据目录，除非你在上面显式勾选。安装版也可以从开始菜单的「卸载 chaiai」
        或 Windows 的「应用」里卸载。
      </p>
    </section>
  )
}
