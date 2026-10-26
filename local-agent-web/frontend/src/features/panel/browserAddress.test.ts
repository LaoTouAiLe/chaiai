import { expect, it } from 'vitest'
import { browserAddress } from './browserAddress'

it.each([
  ['example.org:8443/preview?q=1#section', 'https://example.org:8443/preview?q=1#section'],
  ['example.org:443', 'https://example.org/'],
  ['example.org', 'https://example.org/'],
  ['  localhost:5173  ', 'http://localhost:5173/'],
  ['127.0.0.1:8080/a', 'http://127.0.0.1:8080/a'],
  ['[::1]:5173/a', 'http://[::1]:5173/a'],
  ['[2001:db8::1]:8443', 'https://[2001:db8::1]:8443/'],
  ['https://example.org:8443/a', 'https://example.org:8443/a'],
  ['http://localhost:5173', 'http://localhost:5173/'],
  ['about:blank', 'about:blank'],
])('normalizes address %s', (value, expected) => {
  expect(browserAddress(value)).toBe(expected)
})

it.each(['javascript:123', 'javascript:alert(1)', 'data:text/html,test', 'file:///C:/Windows',
  'mailto:someone@example.org', 'ftp://example.org:21', 'https://user:password@example.org',
  'example.org:99999', 'https://', ''])('rejects unsupported or invalid address %s', value => {
  expect(() => browserAddress(value)).toThrow()
})
