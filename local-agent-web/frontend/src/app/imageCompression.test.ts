import { describe, expect, it } from 'vitest'
import { MAX_EDGE, computeTargetSize } from './imageCompression'

describe('computeTargetSize', () => {
  it('leaves an image within the edge limit at its original size', () => {
    expect(computeTargetSize(1200, 800)).toEqual({ width: 1200, height: 800 })
  })

  it('never upscales a small image', () => {
    // 放大会让文件更大、画质更差，没有任何收益。
    expect(computeTargetSize(320, 200)).toEqual({ width: 320, height: 200 })
  })

  it('scales the longest edge down and keeps the aspect ratio', () => {
    expect(computeTargetSize(8192, 4096)).toEqual({ width: MAX_EDGE, height: 2048 })
    expect(computeTargetSize(4096, 8192)).toEqual({ width: 2048, height: MAX_EDGE })
  })

  it('never returns a zero dimension for extreme ratios', () => {
    const size = computeTargetSize(20000, 3)
    expect(size.width).toBe(MAX_EDGE)
    expect(size.height).toBeGreaterThanOrEqual(1)
  })
})
