import { createContext, useContext } from 'react'
import type { GeneralPreferences } from '../../api/client'

export const preferencesKey = ['app-preferences'] as const
export const generalDefaults: Required<GeneralPreferences> = {
  // 2026-10-22 起默认跟随系统（§12 决策 2）。与后端 preferences.py 的默认值保持一致。
  theme: 'system', font_size: 'standard', animations: true,
  send_shortcut: 'enter', reasoning_expanded: false,
}
export const GeneralContext = createContext(generalDefaults)
export function useGeneralPreferences() { return useContext(GeneralContext) }
