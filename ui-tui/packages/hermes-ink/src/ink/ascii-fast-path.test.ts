import { describe, it, expect } from 'vitest'
import { tokenize, styledCharsFromTokens } from '@alcalzone/ansi-tokenize'
import { flushBuffer } from './output.js'
import { stringWidth } from './stringWidth.js'
import { StylePool, extractHyperlinkFromStyles, filterOutHyperlinkStyles, OSC8_PREFIX } from './screen.js'
import { getGraphemeSegmenter } from '../utils/intl.js'

function referenceFlushBuffer(buffer: string, styles: any[], stylePool: StylePool, out: any[]): void {
  const hyperlink = extractHyperlinkFromStyles(styles) ?? undefined
  const hasOsc8Styles =
    hyperlink !== undefined || styles.some((s: any) => s.code.length >= OSC8_PREFIX.length && s.code.startsWith(OSC8_PREFIX))
  const filteredStyles = hasOsc8Styles ? filterOutHyperlinkStyles(styles) : styles
  const styleId = stylePool.intern(filteredStyles)

  for (const { segment: grapheme } of getGraphemeSegmenter().segment(buffer)) {
    out.push({
      value: grapheme,
      width: stringWidth(grapheme),
      styleId,
      hyperlink
    })
  }
}

describe('printable-ASCII fast path in flushBuffer and singlechar stringWidth', () => {
  it('singlechar stringWidth early returns correctly for ASCII and non-ASCII', () => {
    for (let i = 0; i <= 127; i++) {
      const ch = String.fromCharCode(i)
      const w = stringWidth(ch)
      if (i >= 0x20 && i <= 0x7e) {
        expect(w).toBe(1)
      } else {
        expect(w).toBe(0)
      }
    }
    expect(stringWidth('中')).toBe(2)
    expect(stringWidth('🚀')).toBe(2)
    expect(stringWidth('\u0300')).toBe(0)
    expect(stringWidth('é')).toBe(1)
    expect(stringWidth('')).toBe(0)
  })

  it('preserves exact equality for styles and OSC 8 hyperlinks', () => {
    const stylePool = new StylePool()
    const testStrings = [
      '\x1b[31mRed text\x1b[0m',
      '\x1b[1;32;44mBold Green on Blue\x1b[0m',
      '\x1b]8;;https://example.com/docs\x07Click Here\x1b]8;;\x07',
      '\x1b[33m\x1b]8;;https://example.com\x07Yellow Hyperlink\x1b]8;;\x07\x1b[0m',
      'Normal text with \x1b[36mCyan\x1b[39m and \x1b]8;;https://hermes.ai\x07Doc\x1b]8;;\x07 link'
    ]
    for (const raw of testStrings) {
      const chars = styledCharsFromTokens(tokenize(raw))
      const outOpt: any[] = []
      const outRef: any[] = []
      let bufferChars: string[] = []
      let bufferStyles: any[] = chars[0]?.styles ?? []

      for (let i = 0; i < chars.length; i++) {
        const c = chars[i]!
        if (bufferChars.length > 0 && JSON.stringify(c.styles) !== JSON.stringify(bufferStyles)) {
          const buf = bufferChars.join('')
          flushBuffer(buf, bufferStyles, stylePool, outOpt)
          referenceFlushBuffer(buf, bufferStyles, stylePool, outRef)
          bufferChars.length = 0
        }
        bufferChars.push(c.value)
        bufferStyles = c.styles
      }
      if (bufferChars.length > 0) {
        const buf = bufferChars.join('')
        flushBuffer(buf, bufferStyles, stylePool, outOpt)
        referenceFlushBuffer(buf, bufferStyles, stylePool, outRef)
      }
      expect(outOpt).toEqual(outRef)
    }
  })

  it('preserves exact equality for ASCII DEL and control characters', () => {
    const stylePool = new StylePool()
    const buffers = [
      'hello\x7fworld',
      '\x7f',
      'abc\x7f',
      '\x7fdef',
      'line\twith\ttabs',
      'line\nwith\nnewlines',
      'carriage\rreturn',
      'null\x00byte',
      'bell\x07char',
      'escape\x1bchar',
      'ctrl\x1funit',
      '\x01\x02\x03\x04\x05'
    ]
    for (const buf of buffers) {
      const outOpt: any[] = []
      const outRef: any[] = []
      flushBuffer(buf, [], stylePool, outOpt)
      referenceFlushBuffer(buf, [], stylePool, outRef)
      expect(outOpt).toEqual(outRef)
    }
  })

  it('preserves exact equality for non-ASCII combining, CJK, emoji, and surrogate mixed buffers', () => {
    const stylePool = new StylePool()
    const buffers = [
      'e\u0301 accent',
      'test \u0300\u0301\u0302 marks',
      '\u0915\u094d\u0937 Devanagari ksha ligature',
      '中文测试',
      'Japanese: 日本語 カタカナ ひらがな',
      'Korean: 한국어',
      'Mixed: Hello 世界! 123',
      '🚀 Rocket',
      '👨‍👩‍👧‍👦 Family emoji sequence',
      '👍🏽 Thumbs up with Fitzpatrick skin tone',
      'Flag: 🇺🇸 US flag regional indicators',
      'Keycap: 1️⃣ digit keycap',
      '\uD83D\uDE80',
      'Musical: \uD834\uDD1E G-clef',
      'Mathematical: \uD835\uDC00 bold A',
      'Mixed buffer: prefix 🚀 中文 \u0301 suffix \uD834\uDD1E end',
      'Simple ASCII text 12345!@#$%^&*()_+~',
      'Short',
      'a',
      ' '
    ]
    for (const buf of buffers) {
      const outOpt: any[] = []
      const outRef: any[] = []
      flushBuffer(buf, [], stylePool, outOpt)
      referenceFlushBuffer(buf, [], stylePool, outRef)
      expect(outOpt).toEqual(outRef)
    }
  })
})
