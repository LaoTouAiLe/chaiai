import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MessageImages } from './MessageImages'

describe('message image presentation', () => {
  it.each([
    [1200, 600, 240, 120, 'center'],
    [600, 1200, 120, 240, 'center'],
    [200, 2400, 60, 240, 'center top'],
    [64, 32, 64, 32, 'center'],
  ])('keeps a %s×%s image readable without upscaling', (width, height, boxWidth, boxHeight, position) => {
    render(<MessageImages images={['/sample.png']} />)
    const image = screen.getByRole('img')
    Object.defineProperties(image, { naturalWidth: { value: width }, naturalHeight: { value: height } })
    fireEvent.load(image)
    expect(screen.getByRole('button')).toHaveStyle({ width: `${boxWidth}px`, aspectRatio: `${boxWidth} / ${boxHeight}` })
    expect(image).toHaveStyle({ objectPosition: position })
  })
  it('uses gallery tiles for multiple images and offers a retry for a failed image', () => {
    render(<MessageImages images={['/first.png', '/second.png']} />)
    expect(screen.getAllByRole('button').every(button => button.dataset.imageVariant === 'tile')).toBe(true)
    fireEvent.error(screen.getAllByRole('img')[0]!)
    fireEvent.click(screen.getByRole('button', { name: '图片加载失败，点击重试' }))
    expect(screen.getAllByRole('img')).toHaveLength(2)
    expect(screen.queryByText('图片加载失败')).not.toBeInTheDocument()
  })
})
