// 「移动访问」（2026-11-03，架构 §24）：配对手机、看已授权设备、撤销。
//
// 这一版把管理界面**直接嵌进设置页**：配对状态是本机的一份文件，主服务与网关读同一份，所以
// 生成二维码 / 撤销 / 改名都走同源 API——不需要跨 origin 请求网关，也不必让主服务做反向代理。
//
// `enabled` 与 `running` 分开显示：**配了但没启动网关**是最常见的一态。合成一句"已开启"会让
// 用户对着一个连不上的地址发愁，而那正是最难自查的一种。

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'


import {
  createMobilePairingCode,
  deleteMobileDevice,
  getMobileDevices,
  getMobileInfo,
  emergencyStopMobile,
  getMobileSettings,
  renameMobileDevice,
  startMobileGateway,
  stopMobileGateway,
} from '../../api/client'
import { ApiError } from '../../api/client'
import type { MobileDeviceDto, MobilePairingCodeDto } from '../../api/client'
import { EmergencyStopDialog, RenameDeviceDialog, RevokeDeviceDialog } from './MobileDialogs'
import { MobileSettingsForm } from './MobileSettingsForm'
import shared from './PreferenceSection.module.css'
import styles from './MobileSection.module.css'

function formatMoment(value: string): string {
  const moment = new Date(value)
  return Number.isNaN(moment.getTime()) ? value : moment.toLocaleString()
}

function DeviceRow({
  device,
  onRename,
  onRevoke,
  busy,
}: {
  device: MobileDeviceDto
  onRename: (device: MobileDeviceDto) => void
  onRevoke: (device: MobileDeviceDto) => void
  busy: boolean
}): React.ReactElement {
  return (
    <li className={styles.device}>
      <div className={styles.deviceText}>
        <div className={styles.deviceName}>{device.label || '未命名设备'}</div>
        <div className={styles.deviceMeta}>
          {device.channel || '未知通道'} · 配对于 {formatMoment(device.created_at)}
        </div>
      </div>
      <div className={styles.deviceActions}>
        {/* 名字来自 UA，安卓抓到的常是型号代号（2509FPN0BC）——允许自己起名比"猜得更准"实在。 */}
        <button type="button" onClick={() => onRename(device)} disabled={busy}>
          改名
        </button>
        <button type="button" className={styles.dangerGhost} onClick={() => onRevoke(device)} disabled={busy}>
          撤销
        </button>
      </div>
    </li>
  )
}

export function MobileSection() {
  const queryClient = useQueryClient()
  const info = useQuery({
    queryKey: ['mobile-info'],
    queryFn: getMobileInfo,
    refetchOnMount: 'always',
  })
  const devices = useQuery({ queryKey: ['mobile-devices'], queryFn: getMobileDevices })
  const settings = useQuery({
    queryKey: ['mobile-settings'],
    queryFn: getMobileSettings,
    refetchOnMount: 'always',
  })

  const [invite, setInvite] = useState<MobilePairingCodeDto | null>(null)
  const [left, setLeft] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // 三个确认框各自一个目标；用对话框而不是原生 prompt/confirm，见 MobileDialogs 的说明。
  const [renameTarget, setRenameTarget] = useState<MobileDeviceDto | null>(null)
  const [revokeTarget, setRevokeTarget] = useState<MobileDeviceDto | null>(null)
  const [panicOpen, setPanicOpen] = useState(false)

  // 倒计时自己走：过期就把二维码撤掉，别让用户对着一个已经作废的码反复扫。
  useEffect(() => {
    if (invite === null) return
    const deadline = Date.now() + invite.expires_in * 1000
    setLeft(invite.expires_in)
    const timer = window.setInterval(() => {
      const remain = Math.max(0, Math.round((deadline - Date.now()) / 1000))
      setLeft(remain)
      if (remain <= 0) {
        window.clearInterval(timer)
        setInvite(null)
      }
    }, 1000)
    return () => window.clearInterval(timer)
  }, [invite])

  const issue = useMutation({
    mutationFn: createMobilePairingCode,
    onSuccess: (data) => {
      setError(null)
      setInvite(data)
    },
    onError: (exc: Error) => setError(exc.message),
  })

  const revoke = useMutation({
    mutationFn: deleteMobileDevice,
    onSuccess: () => {
      setError(null)
      setRevokeTarget(null)
      setNotice('设备已撤销，那台手机需要重新扫码配对。')
      void queryClient.invalidateQueries({ queryKey: ['mobile-devices'] })
      void queryClient.invalidateQueries({ queryKey: ['mobile-info'] })
    },
    onError: (exc: Error) => setError(exc.message),
  })

  const rename = useMutation({
    mutationFn: (input: { id: string; label: string }) => renameMobileDevice(input.id, input.label),
    onSuccess: () => {
      setError(null)
      setRenameTarget(null)
      void queryClient.invalidateQueries({ queryKey: ['mobile-devices'] })
    },
    onError: (exc: Error) => setError(exc.message),
  })

  // 紧急启停（2026-11-03）：用户要"出问题之后能操控关闭"。这里除了一次确认之外不设门槛——
  // 最需要这个按钮的时刻，人多半已经慌了。
  const control = useMutation({
    mutationFn: (
      action: { kind: 'start' } | { kind: 'stop' } | { kind: 'panic'; revoke: boolean },
    ) => {
      if (action.kind === 'start') return startMobileGateway()
      if (action.kind === 'stop') return stopMobileGateway()
      return emergencyStopMobile(action.revoke)
    },
    onSuccess: (result) => {
      setError(null)
      setNotice(result.detail)
      setPanicOpen(false)
      void queryClient.invalidateQueries({ queryKey: ['mobile-info'] })
      void queryClient.invalidateQueries({ queryKey: ['mobile-devices'] })
    },
    onError: (exc: Error) => {
      setNotice(null)
      setError(exc.message)
    },
  })

  const view = info.data
  const busy = revoke.isPending || rename.isPending
  const list = devices.data?.devices ?? []

  return (
    <section className={shared.section}>
      <header className={shared.head}>
        <h3 className={shared.title}>移动访问</h3>
      </header>

      {view === undefined ? (
        <p className={styles.hint} role="status">
          {info.isError ? '读取移动访问状态失败。' : '正在读取移动访问状态…'}{' '}
          <button type="button" onClick={() => void info.refetch()}>
            重新读取
          </button>
        </p>
      ) : (
        <>
          <div className={styles.statusBar}>
            <span
              className={`${styles.badge} ${view.running ? styles.badgeOn : styles.badgeOff}`}
            >
              {view.running ? '运行中' : view.enabled ? '已停止' : '未启用'}
            </span>
            <span className={styles.statusMeta}>
              {view.enabled ? `已配对 ${view.device_count} 台设备` : '配置里没有开启'}
            </span>
            <div className={styles.statusActions}>
              {view.running ? (
                <button
                  type="button"
                  disabled={control.isPending}
                  onClick={() => control.mutate({ kind: 'stop' })}
                >
                  停止
                </button>
              ) : (
                <button
                  type="button"
                  className={shared.primary}
                  disabled={control.isPending}
                  onClick={() => control.mutate({ kind: 'start' })}
                >
                  启动
                </button>
              )}
              <button
                type="button"
                className={styles.dangerGhost}
                disabled={control.isPending}
                onClick={() => setPanicOpen(true)}
              >
                紧急关闭
              </button>
            </div>
          </div>

          {notice === null ? null : (
            <p className={styles.notice} role="status">
              {notice}
            </p>
          )}

          {!view.enabled ? (
            <p className={styles.hint}>
              移动访问没有开启。在下方「高级配置」里勾上总开关，保存后点上面的「启动」。
            </p>
          ) : (
            <>
              <div className={styles.block}>
                <h4 className={styles.blockTitle}>配对手机</h4>
                <div className={styles.blockBody}>
                  <div className={styles.blockActions}>
                    <button
                      type="button"
                      className={shared.primary}
                      onClick={() => issue.mutate()}
                      disabled={issue.isPending}
                    >
                      {issue.isPending ? '正在生成…' : '生成配对二维码'}
                    </button>
                  </div>
                  {invite === null ? null : (
                    <div className={styles.invite}>
                      <div
                        className={styles.qrBox}
                        dangerouslySetInnerHTML={{ __html: invite.qr_svg }}
                      />
                      <div className={styles.inviteMeta}>
                        <p className={styles.inviteTtl}>有效期还剩 {left} 秒 · 只能用一次</p>
                        <p className={styles.inviteLink}>
                          手机也可以直接打开：
                          <br />
                          <code>{invite.pair_url}</code>
                        </p>
                        {invite.alternates.length === 0 ? null : (
                          <p className={styles.inviteLink}>
                            扫了没反应？换这个：
                            {invite.alternates.map((item) => (
                              <span key={item.url}>
                                <br />
                                <span className={styles.altLabel}>{item.label}</span>
                                <code>{item.url}</code>
                              </span>
                            ))}
                          </p>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              </div>

              <div className={styles.block}>
                <h4 className={styles.blockTitle}>已配对设备</h4>
                <ul className={styles.devices}>
                  {devices.isError ? (
                    // 读取失败**不能**显示成"还没有设备"：那会让用户以为配对丢了，实际是请求被拒。
                    // 403 是"在手机上打开设置页"的正常结果（管理接口只服务本机），要说成人话，
                    // 而不是甩一句错误 + 一个点了也没用的"重试"。
                    <li className={styles.device}>
                      <span className={styles.deviceMeta}>
                        {devices.error instanceof ApiError && devices.error.status === 403
                          ? '配对管理只能在电脑上进行：手机上无法查看或撤销已配对的设备。'
                          : `读取设备列表失败：${devices.error.message}`}{' '}
                        {devices.error instanceof ApiError && devices.error.status === 403 ? null : (
                          <button type="button" onClick={() => void devices.refetch()}>
                            重试
                          </button>
                        )}
                      </span>
                    </li>
                  ) : list.length === 0 ? (
                    <li className={styles.device}>
                      <span className={styles.deviceMeta}>还没有配对的设备</span>
                    </li>
                  ) : (
                    list.map((device) => (
                      <DeviceRow
                        key={device.id}
                        device={device}
                        busy={busy}
                        onRename={setRenameTarget}
                        onRevoke={setRevokeTarget}
                      />
                    ))
                  )}
                </ul>
                <p className={styles.hint}>
                  撤销立即生效：那台手机的下一次请求就会被拒绝。手机丢失请立刻撤销它。
                </p>
              </div>
            </>
          )}
        </>
      )}

      <MobileSettingsForm
        value={settings.data}
        onSaved={() => {
          void queryClient.invalidateQueries({ queryKey: ['mobile-settings'] })
          void queryClient.invalidateQueries({ queryKey: ['mobile-info'] })
        }}
      />

      {error === null ? null : (
        <p className={shared.error} role="alert">
          {error}
        </p>
      )}

      {renameTarget === null ? null : (
        <RenameDeviceDialog
          device={renameTarget}
          busy={rename.isPending}
          onClose={() => setRenameTarget(null)}
          onSubmit={(label) => rename.mutate({ id: renameTarget.id, label })}
        />
      )}
      {revokeTarget === null ? null : (
        <RevokeDeviceDialog
          device={revokeTarget}
          busy={revoke.isPending}
          onClose={() => setRevokeTarget(null)}
          onConfirm={() => revoke.mutate(revokeTarget.id)}
        />
      )}
      {panicOpen ? (
        <EmergencyStopDialog
          running={view?.running ?? false}
          deviceCount={view?.device_count ?? 0}
          busy={control.isPending}
          onClose={() => setPanicOpen(false)}
          onConfirm={(revokeDevices) => control.mutate({ kind: 'panic', revoke: revokeDevices })}
        />
      ) : null}
    </section>
  )
}

export default MobileSection
