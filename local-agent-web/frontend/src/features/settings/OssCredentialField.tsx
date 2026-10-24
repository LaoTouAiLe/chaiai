// 对象存储凭据（2026-10-20）：「设置 → 图片与附件」里的密钥与"测试上传"那一块。
//
// 三条口径与模型连接一致：
//   1. **只写不回读**：接口不返回密钥，页面只报"已配置 / 未配置"；
//   2. **保存与删除分开**：保存空白不是删除；
//   3. **测试用已保存的配置**：它读的是设置库里的值，不是上面还没保存的草稿——
//      所以文案要求"先保存，再测试"，避免测出一个不存在的组合。

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { clearOssCredential, getOssCredential, setOssCredential, testOssStorage } from '../../api/client'
import type { components } from '../../api/schema.gen'
import styles from './PreferenceSection.module.css'

export const ossCredentialQueryKey = ['oss-credential'] as const

type CredentialView = components['schemas']['OssCredentialView']
type TestResult = components['schemas']['OssTestResult']

const STORE_LABEL: Record<string, string> = {
  local: '已存入本地设置库（重启后仍在）',
  os: '已存入系统凭据存储',
  memory: '只在本进程内有效（重启后要重新录入）',
}

export function OssCredentialField() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ossCredentialQueryKey,
    queryFn: getOssCredential,
    staleTime: Infinity,
    refetchOnMount: 'always',
  })
  const [accessKey, setAccessKey] = useState('')
  const [secretKey, setSecretKey] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [testResult, setTestResult] = useState<TestResult | null>(null)
  // 已配置时先显示两个"已配置"的只读格（接口不给密钥、连掩码都不给），
  // 点任一格转成输入态；Key 与 Secret 必须成对重填，所以两格同时切换。
  const [editing, setEditing] = useState(false)

  const update = (view: CredentialView) => {
    queryClient.setQueryData(ossCredentialQueryKey, view)
    setAccessKey('')
    setSecretKey('')
    setEditing(false)
    setError(null)
    setSaved(true)
    setTestResult(null)
  }
  const save = useMutation({
    mutationFn: () => setOssCredential(accessKey, secretKey),
    onSuccess: update,
    onError: (caught: unknown) => {
      setSaved(false)
      setError(caught instanceof Error ? caught.message : '密钥保存失败')
    },
  })
  const clear = useMutation({
    mutationFn: clearOssCredential,
    onSuccess: update,
    onError: (caught: unknown) => {
      setSaved(false)
      setError(caught instanceof Error ? caught.message : '密钥删除失败')
    },
  })
  const test = useMutation({
    mutationFn: testOssStorage,
    onSuccess: (result) => { setTestResult(result); setError(null) },
    onError: (caught: unknown) => {
      setTestResult(null)
      setError(caught instanceof Error ? caught.message : '测试失败')
    },
  })

  const view = query.data
  const busy = save.isPending || clear.isPending || test.isPending
  const masked = view !== undefined && view.configured && !editing
  return (
    <section className={styles.section} aria-label="对象存储凭据">
      <h4>对象存储凭据</h4>
      <p className={styles.note}>
        密钥只写入不回读，页面只显示"有没有"。Key 与 Secret 成对保存——换一个就两个一起重填。
        桶需要允许公开读：模型和页面都直接取图片链接。
      </p>
      {!view ? (
        <p role="status">
          {query.isError ? '密钥状态读取失败。' : '正在读取密钥状态…'}
          <button type="button" onClick={() => void query.refetch()}>重新读取</button>
        </p>
      ) : (
        <>
          <dl className={styles.overview} aria-label="对象存储密钥状态">
            <div><dt>当前状态</dt><dd>{view.configured ? '已配置' : '未配置'}</dd></div>
            <div><dt>存放位置</dt><dd>{view.store ? (STORE_LABEL[view.store] ?? view.store) : '没有可用的凭据存储'}</dd></div>
          </dl>
          {view.note === null ? null : <p role="alert" className={styles.error}>{view.note}</p>}
          {masked ? (
            <>
              <span className={styles.field}>
                <span className={styles.fieldText}>
                  <strong>Access Key</strong>
                  <small>已配置，只写入不回读。点右边任一格可以整对重填（Key 与 Secret 必须成对）。</small>
                </span>
                <span className={styles.control}>
                  <input
                    type="text"
                    readOnly
                    aria-label="Access Key（已配置，点击修改）"
                    className={styles.maskedInput}
                    value="••••••••"
                    onClick={() => setEditing(true)}
                    onFocus={() => setEditing(true)}
                  />
                </span>
              </span>
              <span className={styles.field}>
                <span className={styles.fieldText}>
                  <strong>Secret Key</strong>
                  <small>只写入不回读。</small>
                </span>
                <span className={styles.control}>
                  <input
                    type="text"
                    readOnly
                    aria-label="Secret Key（已配置，点击修改）"
                    className={styles.maskedInput}
                    value="••••••••"
                    onClick={() => setEditing(true)}
                    onFocus={() => setEditing(true)}
                  />
                </span>
              </span>
            </>
          ) : (
            <>
              <label className={styles.field}>
                <span className={styles.fieldText}>
                  <strong>Access Key</strong>
                  <small>对象存储的访问密钥 ID；保存后从下一张上传的图片开始生效。</small>
                </span>
                <span className={styles.control}>
                  <input
                    type="text"
                    aria-label="Access Key"
                    autoComplete="off"
                    disabled={busy}
                    value={accessKey}
                    onChange={event => { setAccessKey(event.target.value); setSaved(false) }}
                  />
                </span>
              </label>
              <label className={styles.field}>
                <span className={styles.fieldText}>
                  <strong>Secret Key</strong>
                  <small>只写入不回读；不确定就整对重填。</small>
                </span>
                <span className={styles.control}>
                  <input
                    type="password"
                    aria-label="Secret Key"
                    autoComplete="off"
                    disabled={busy}
                    value={secretKey}
                    onChange={event => { setSecretKey(event.target.value); setSaved(false) }}
                  />
                </span>
              </label>
            </>
          )}
          <div className={styles.actions}>
            {masked ? null : (
              <button
                type="button"
                className={styles.primary}
                disabled={busy || accessKey.trim() === '' || secretKey.trim() === ''}
                onClick={() => save.mutate()}
              >
                {save.isPending ? '正在保存…' : '保存密钥'}
              </button>
            )}
            <button type="button" disabled={busy || !view.configured} onClick={() => clear.mutate()}>
              {clear.isPending ? '正在删除…' : '删除已保存密钥'}
            </button>
            <button type="button" disabled={busy} onClick={() => test.mutate()}>
              {test.isPending ? '正在测试…' : '测试上传'}
            </button>
          </div>
          <p className={styles.note}>
            「测试上传」用的是**已保存**的配置与密钥：先保存上面的存储设置和这里的密钥，再点测试。
            它会传一张 1×1 测试图并检查公开地址，不写入任何用户数据。
          </p>
          {error ? <p role="alert" className={styles.error}>{error}</p> : null}
          {saved ? <p role="status" className={styles.saved}>已保存，下一张上传的图片生效</p> : null}
          {testResult === null ? null : (
            <p role={testResult.ok ? 'status' : 'alert'} className={testResult.ok ? styles.saved : styles.error}>
              {testResult.detail}
            </p>
          )}
        </>
      )}
    </section>
  )
}
