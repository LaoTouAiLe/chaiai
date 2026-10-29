import { useEffect, useState } from 'react'

const COMPACT_QUERY = '(max-width: 1024px)'

export function useCompactNavigation() {
  const [compact, setCompact] = useState(() => typeof window.matchMedia === 'function' && window.matchMedia(COMPACT_QUERY).matches)
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const query = window.matchMedia(COMPACT_QUERY)
    const update = (event: MediaQueryListEvent) => setCompact(event.matches)
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])
  return compact
}
