import { useEffect } from 'react'
import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getPreferences } from '../../api/client'
import { GeneralContext, generalDefaults, preferencesKey } from './preferencesContext'

export function PreferencesProvider({ children }: { children: ReactNode }) {
  const query = useQuery({ queryKey: preferencesKey, queryFn: getPreferences, staleTime: Infinity })
  const general = { ...generalDefaults, ...query.data?.values.general }
  const { theme, font_size: fontSize, animations } = general
  useEffect(() => {
    const dark = window.matchMedia?.('(prefers-color-scheme: dark)')
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    const apply = () => {
      const resolvedTheme = theme === 'system' ? (dark?.matches ? 'dark' : 'light') : theme
      const resolvedMotion = !animations || reduced?.matches ? 'reduced' : 'full'
      document.documentElement.dataset.theme = resolvedTheme
      document.documentElement.dataset.fontSize = fontSize
      document.documentElement.dataset.motion = resolvedMotion
      // 首屏防闪缓存（index.html 内联脚本读取）。这里写的是**解析后的结果**，
      // 不是用户偏好本身；偏好真源仍是服务端设置库。写入失败不影响界面。
      try {
        localStorage.setItem('chaiai.theme', resolvedTheme)
        localStorage.setItem('chaiai.motion', resolvedMotion)
      } catch {
        /* 隐私模式或存储不可用时忽略 */
      }
    }
    apply()
    dark?.addEventListener('change', apply)
    reduced?.addEventListener('change', apply)
    return () => {
      dark?.removeEventListener('change', apply)
      reduced?.removeEventListener('change', apply)
      delete document.documentElement.dataset.theme
      delete document.documentElement.dataset.fontSize
      delete document.documentElement.dataset.motion
    }
  }, [theme, fontSize, animations])
  return <GeneralContext value={general}>{children}</GeneralContext>
}
