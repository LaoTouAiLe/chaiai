type IconName = 'chat' | 'tasks' | 'data' | 'artifacts' | 'capabilities' | 'cron' | 'settings' | 'search' | 'folder' | 'plus' | 'terminal' | 'file' | 'arrow' | 'collapse' | 'expand'

const PATHS: Record<IconName, string> = {
  chat: 'M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 4V6a2 2 0 0 1 2-2ZM7 9h10M7 13h6',
  tasks: 'M9 5h11M9 12h11M9 19h7M3 5l1 1 2-2M3 12l1 1 2-2M3 19l1 1 2-2',
  data: 'M20 6c0 2-3.6 3-8 3S4 8 4 6s3.6-3 8-3 8 1 8 3ZM4 6v6c0 2 3.6 3 8 3s8-1 8-3V6M4 12v6c0 2 3.6 3 8 3s8-1 8-3v-6',
  artifacts: 'm12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5',
  capabilities: 'm13 2-9 12h6l-1 8 11-13h-7l1-7Z',
  cron: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM12 7v5l3 2',
  settings: 'M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1 1-3ZM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  search: 'M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Zm-2 5 6 6',
  folder: 'M3 7V5h6l2 3h10v11H3V7Z',
  plus: 'M12 5v14M5 12h14',
  terminal: 'M4 5h16v14H4V5Zm4 4 3 3-3 3m5 0h3',
  file: 'M5 3h9l5 5v13H5V3Zm9 0v6h5M9 13h6M9 17h4',
  arrow: 'M5 12h14m-5-5 5 5-5 5',
  collapse: 'M4 4h16v16H4V4Zm5 0v16m7-12-4 4 4 4',
  expand: 'M4 4h16v16H4V4Zm5 0v16m3-12 4 4-4 4',
}

export function WorkbenchIcon({ name, size = 18 }: { name: IconName; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={PATHS[name]} /></svg>
}
