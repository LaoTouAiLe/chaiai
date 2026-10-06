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
      document.documentElement.dataset.theme = theme === 'system' ? (dark?.matches ? 'dark' : 'light') : theme
      document.documentElement.dataset.fontSize = fontSize
      document.documentElement.dataset.motion = !animations || reduced?.matches ? 'reduced' : 'full'
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
