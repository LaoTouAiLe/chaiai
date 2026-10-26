import { createContext } from 'react'

/** 主对话、侧聊和会话地图共用入口，不把面板状态耦合进 Markdown。 */
export const FileLinkContext = createContext<((path: string) => void) | null>(null)
