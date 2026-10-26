import { expect, it } from 'vitest'
import { filePathFromHref, sameFilePath } from './fileLinks'

it.each([
  ['E:/repo/src/app.tsx', 'E:/repo/src/app.tsx'],
  ['e:\\项目\\src\\app.py', 'E:/项目/src/app.py'],
  ['E:%5Crepo%5Ca.ts', 'E:/repo/a.ts'],
  ['/E:/repo/app.py', 'E:/repo/app.py'],
  ['file:///E:/My%20Project/app.py', 'E:/My Project/app.py'],
  ['file://localhost/E:/repo/app.py', 'E:/repo/app.py'],
  ['./src/../README.md', 'README.md'],
  ['docs/My%20Notes.md', 'docs/My Notes.md'],
  ['../outside.txt', '../outside.txt'],
  ['/tmp/app.py:12:3', '/tmp/app.py'],
  ['src/app.py#L12', 'src/app.py'],
  ['src/name%23part.ts', 'src/name#part.ts'],
  ['README', 'README'],
])('parses a file reference %s without guessing the workspace root', (href, path) => {
  expect(filePathFromHref(href)).toBe(path)
})

it.each(['https://example.org/code.py', 'http://localhost:5173', '//example.org/page',
  'mailto:someone@example.org', '#heading', 'javascript:alert(1)', 'data:text/html,test',
  'file://server/share/file.txt', '\\\\server\\share\\file.txt', 'C:relative.txt',
  'src/a%00.ts', 'javascript%3Aalert(1)', '', '.', '..', 'src/'])('does not treat %s as a file link', href => {
  // .. 是有效的上级路径，但没有文件部分时不应打开目录。
  expect(filePathFromHref(href)).toBeNull()
})

it('compares normalized paths and Windows case without conflating different absolute roots', () => {
  expect(sameFilePath('E:/Repo/a.ts', 'e:\\repo\\a.ts')).toBe(true)
  expect(sameFilePath('src/../a.ts', './a.ts')).toBe(true)
  expect(sameFilePath('E:/repo/a.ts', 'a.ts')).toBe(false)
  expect(sameFilePath('/tmp/A.ts', '/tmp/a.ts')).toBe(false)
})

it('does not parse decoded filesystem names as URLs a second time', () => {
  const hashOne = filePathFromHref('E:/repo/name%23one.ts')!
  const hashTwo = filePathFromHref('E:/repo/name%23two.ts')!
  expect(sameFilePath(hashOne, hashTwo)).toBe(false)
  expect(sameFilePath(hashOne, hashOne)).toBe(true)
  const percent = filePathFromHref('E:/repo/name%2520part.ts')!
  const space = filePathFromHref('E:/repo/name%20part.ts')!
  expect(sameFilePath(percent, space)).toBe(false)
  expect(sameFilePath(percent, percent)).toBe(true)
})
