import { createContext, useContext } from 'react'
import type { GeneralPreferences } from '../../api/client'

export const preferencesKey = ['app-preferences'] as const
export const generalDefaults: Required<GeneralPreferences> = {
  theme: 'light', font_size: 'standard', animations: true,
  send_shortcut: 'enter', reasoning_expanded: false,
}
export const GeneralContext = createContext(generalDefaults)
export function useGeneralPreferences() { return useContext(GeneralContext) }
