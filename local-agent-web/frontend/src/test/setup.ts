import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

/**
 * Testing Library's automatic cleanup only registers itself when Vitest globals
 * are enabled; this project imports test APIs explicitly, so unmount between
 * cases here instead.
 */
afterEach(() => {
  cleanup()
})
