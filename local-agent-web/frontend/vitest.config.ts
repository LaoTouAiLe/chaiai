import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    restoreMocks: true,
    // Keep module class names readable in failure output; styling is verified by
    // the Playwright baselines, not by unit assertions.
    css: { modules: { classNameStrategy: 'non-scoped' } },
  },
})
