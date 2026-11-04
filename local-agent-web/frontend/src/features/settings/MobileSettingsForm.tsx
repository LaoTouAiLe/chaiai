// 移动访问的配置表单（2026-11-03）。
//
// 从 `MobileSection` 拆出来：那个组件已经 500 多行，而这张表单有完整的状态机（草稿 / 保存 /
// 服务端错误 / 生效提示），混在一起两边都不好读。

import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'

import { saveMobileSettings } from '../../api/client'
import type {
  MobileChannelSettingDto,
  MobileSettingsDto,
  MobileSettingsInput,
} from '../../api/client'
import shared from './PreferenceSection.module.css'
import styles from './MobileSection.module.css'

const CHANNEL_LABEL: Record<string, string> = {
  lan: '局域网',
  tailscale: 'Tailscale',
  cloudflared: 'Cloudflare 隧道',
}

function toInput(view: MobileSettingsDto): MobileSettingsInput {
  return {
    enabled: view.enabled,
    gateway_port: view.gateway_port,
    pairing_ttl_seconds: view.pairing_ttl_seconds,
    ws_enabled: view.ws_enabled,
    channels: view.channels.map((item) => ({ ...item })),
  }
}

/**
 * 配置表单（2026-11-03）。
 *
 * **保存不等于生效**：网关是独立进程，必须重启才读到新值。所以保存成功后明确显示"重启网关后生效"，
 * 而不是给一句"已保存"让用户对着旧行为发愁。
 */
export function MobileSettingsForm({
  value,
  onSaved,
}: {
  value: MobileSettingsDto | undefined
  onSaved: () => void
}): React.ReactElement | null {
  const [draft, setDraft] = useState<MobileSettingsInput | null>(null)
  const [saveNote, setSaveNote] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // 服务端值变了就重置草稿；否则保存完表单还停在被改过的样子，看起来像没保存。
  useEffect(() => {
    if (value) setDraft(toInput(value))
  }, [value])

  const save = useMutation({
    mutationFn: (payload: MobileSettingsInput) => saveMobileSettings(payload),
    // 文案直接用**响应里**的 restart_required：查询结果要等 invalidate 才刷新，
    // 拿旧的 value 判断会稳定显示错的那一句。
    onSuccess: (result) => {
      setSaveNote(
        result.restart_required
          ? '已保存。重启网关后生效——网关当前没有在运行，所以没有替你去拉起它。'
          : '已保存并已生效（网关已按新配置重启）。',
      )
      setError(null)
      onSaved()
    },
    onError: (exc: Error) => {
      setSaveNote(null)
      setError(exc.message)
    },
  })

  if (!value || !draft) return null

  const patch = (changes: Partial<MobileSettingsInput>): void => {
    setDraft({ ...draft, ...changes })
    setSaveNote(null)
  }
  const patchChannel = (index: number, changes: Partial<MobileChannelSettingDto>): void => {
    patch({
      channels: draft.channels.map((item, position) =>
        position === index ? { ...item, ...changes } : item,
      ),
    })
  }

  return (
    <details className={styles.config}>
      <summary className={styles.configSummary}>高级配置</summary>
      <p className={shared.note}>
        这里的值与 <code>config.yaml</code> 是同一份配置：页面上改过就以页面为准，配置文件退回出厂默认。
        <strong>保存后需要重启网关才生效</strong>——网关是独立进程，不会自动重读。
      </p>

      <label className={styles.checkRow}>
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(event) => patch({ enabled: event.target.checked })}
        />
        启用移动访问（关掉后不再启动网关）
      </label>
      <label className={styles.checkRow}>
        <input
          type="checkbox"
          checked={draft.ws_enabled}
          onChange={(event) => patch({ ws_enabled: event.target.checked })}
        />
        转发终端 WebSocket（关掉后手机上打不开终端，其余功能不受影响）
      </label>

      <label className={styles.field}>
        <span>配对码有效期（秒）</span>
        <input
          type="number"
          min={30}
          value={draft.pairing_ttl_seconds}
          onChange={(event) =>
            patch({ pairing_ttl_seconds: Number(event.target.value) || 0 })
          }
        />
      </label>
      <label className={styles.field}>
        <span>网关端口（改这里需要重启主服务）</span>
        <input
          type="number"
          value={draft.gateway_port}
          onChange={(event) => patch({ gateway_port: Number(event.target.value) || 0 })}
        />
      </label>

      {draft.channels.map((channel, index) => (
        <div key={channel.type} className={styles.channel}>
          <label className={styles.checkRow}>
            <input
              type="checkbox"
              checked={channel.enabled}
              onChange={(event) => patchChannel(index, { enabled: event.target.checked })}
            />
            {CHANNEL_LABEL[channel.type] ?? channel.type}
          </label>
          {channel.type === 'lan' ? (
            <>
              <label className={styles.field}>
                <span>端口</span>
                <input
                  type="number"
                  value={channel.port}
                  onChange={(event) =>
                    patchChannel(index, { port: Number(event.target.value) || 0 })
                  }
                />
              </label>
              <label className={styles.checkRow}>
                <input
                  type="checkbox"
                  checked={channel.tls}
                  onChange={(event) => patchChannel(index, { tls: event.target.checked })}
                />
                用 HTTPS（自签证书，手机首次要手动信任）
              </label>
            </>
          ) : null}
          {channel.type === 'tailscale' ? (
            <>
              <label className={styles.field}>
                <span>暴露方式</span>
                <select
                  value={channel.expose}
                  onChange={(event) =>
                    patchChannel(index, {
                      expose: event.target.value as MobileChannelSettingDto['expose'],
                    })
                  }
                >
                  <option value="serve">serve（仅 tailnet 内可达）</option>
                  <option value="funnel">funnel（公网可达，靠配对码挡）</option>
                </select>
              </label>
              <label className={styles.field}>
                <span>HTTPS 端口（只允许 443 / 8443 / 10000）</span>
                <input
                  type="number"
                  value={channel.https_port}
                  onChange={(event) =>
                    patchChannel(index, { https_port: Number(event.target.value) || 0 })
                  }
                />
              </label>
            </>
          ) : null}
          {channel.type === 'cloudflared' ? (
            <>
              <label className={styles.field}>
                <span>模式</span>
                <select
                  value={channel.mode}
                  onChange={(event) =>
                    patchChannel(index, {
                      mode: event.target.value as MobileChannelSettingDto['mode'],
                    })
                  }
                >
                  <option value="named">named（固定域名，需账号）</option>
                  <option value="quick">quick（随机域名，不支持 SSE）</option>
                </select>
              </label>
              <label className={styles.field}>
                <span>域名</span>
                <input
                  type="text"
                  value={channel.hostname}
                  placeholder="agent.example.com"
                  onChange={(event) => patchChannel(index, { hostname: event.target.value })}
                />
              </label>
            </>
          ) : null}
        </div>
      ))}

      <div className={shared.actions}>
        <button
          type="button"
          className={shared.primary}
          disabled={save.isPending}
          onClick={() => save.mutate(draft)}
        >
          {save.isPending ? '正在保存…' : '保存配置'}
        </button>
      </div>
      {saveNote === null ? null : (
        <p className={shared.note} role="status">
          {saveNote}
        </p>
      )}
      {error === null ? null : (
        <p className={shared.error} role="alert">
          {error}
        </p>
      )}
    </details>
  )
}

