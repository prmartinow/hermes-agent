import { EventEmitter } from 'node:events'
import { createRequire } from 'node:module'
import { Writable } from 'node:stream'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@hermes/ink', () => import('../../packages/hermes-ink/src/entry-exports.js'))
import { Box, createRoot } from '@hermes/ink'
import instances from '../../packages/hermes-ink/src/ink/instances.js'
import {
  ModelPicker,
  modelPickerCommand,
  pickerOffersReasoning,
  reasoningPickerRowsForModel,
  initialReasoningIndexForModel,
  REASONING_PICKER_ROWS,
} from '../components/modelPicker.js'
import { DARK_THEME } from '../theme.js'
import type { GatewayClient } from '../gatewayClient.js'
import type { ModelOptionProvider } from '@hermes/shared/gateway-events'

const { Terminal } = createRequire(import.meta.url)('@xterm/xterm')
const settle = (ms = 40) => new Promise(resolve => setTimeout(resolve, ms))

class Input extends EventEmitter {
  chunks: string[] = []
  isTTY = true
  isRaw = false
  readableLength = 0
  read() { const value = this.chunks.shift() ?? null; this.readableLength = this.chunks.length; return value }
  ref() {}
  unref() {}
  setEncoding() {}
  setRawMode(value: boolean) { this.isRaw = value }
  send(text: string) { this.chunks.push(text); this.readableLength = this.chunks.length; this.emit('readable') }
}

async function fixture() {
  const term = new Terminal({ cols: 100, rows: 30, scrollback: 20000 })
  const stdin = new Input()
  const stdout = Object.assign(new Writable({ write(chunk, _encoding, done) { term.write(chunk.toString(), done) } }), { isTTY: true, columns: 100, rows: 30 }) as unknown as NodeJS.WriteStream
  const root = await createRoot({ stdout, stdin: stdin as unknown as NodeJS.ReadStream, stderr: stdout, patchConsole: false, exitOnCtrlC: false })
  instances.get(stdout)!.setInlineMouseTracking('buttons')
  const visible = () => Array.from({ length: term.rows }, (_, i) => term.buffer.active.getLine(term.buffer.active.baseY + i)?.translateToString(true) || '')
  const click = async (label: string) => {
    const lines = visible(), row = lines.findIndex(s => s.includes(label))
    expect(row, `visible target ${label}`).toBeGreaterThanOrEqual(0)
    const col = lines[row].indexOf(label) + 1
    stdin.send(`[<0;${col + 1};${row + 1}M[<0;${col + 1};${row + 1}m`)
    await settle(80)
  }
  return { term, stdin, stdout, root, visible, click, close() { root.unmount(); term.dispose(); instances.delete(stdout) } }
}

const mockProvider = (slug: string, name: string, capabilities: ModelOptionProvider['capabilities']): ModelOptionProvider => ({
  slug,
  name,
  capabilities,
})

describe('Milestone 3 — ModelPicker pure-helper contracts', () => {
  it('enforces exact Cloud Code 3.8 and 3.1 Pro capability rows without none or keep-current', () => {
    const prov38 = mockProvider('gemini-oauth', 'Google Gemini (OAuth)', {
      'gemini-3.8-flash': { fast: false, reasoning: true, reasoning_efforts: ['low', 'medium', 'high'], can_disable_reasoning: false }
    })
    const rows38 = reasoningPickerRowsForModel(prov38, 'gemini-3.8-flash')
    expect(rows38.map(r => r.value)).toEqual(['low', 'medium', 'high'])

    const prov31 = mockProvider('gemini-2', 'Gemini Account 2', {
      'gemini-3.1-pro': { fast: false, reasoning: true, reasoning_efforts: ['low', 'high'], can_disable_reasoning: false }
    })
    const rows31 = reasoningPickerRowsForModel(prov31, 'gemini-3.1-pro')
    expect(rows31.map(r => r.value)).toEqual(['low', 'high'])
  })

  it('bypasses reasoning stage when reasoning_efforts is empty list (flash-lite, claude, gpt-oss)', () => {
    const provLite = mockProvider('gemini-oauth', 'Google Gemini (OAuth)', {
      'gemini-3.1-flash-lite': { fast: false, reasoning: true, reasoning_efforts: [] }
    })
    expect(pickerOffersReasoning(provLite, 'gemini-3.1-flash-lite')).toBe(false)

    const provClaude = mockProvider('gemini-oauth', 'Google Gemini (OAuth)', {
      'claude-sonnet-4-6': { fast: false, reasoning: true, reasoning_efforts: [] }
    })
    expect(pickerOffersReasoning(provClaude, 'claude-sonnet-4-6')).toBe(false)
  })

  it('retains canonical generic ladder for unknown or generic models', () => {
    const provGeneric = mockProvider('openrouter', 'OpenRouter', {
      'generic-reasoner': { fast: false, reasoning: true }
    })
    expect(pickerOffersReasoning(provGeneric, 'generic-reasoner')).toBe(true)
    const rows = reasoningPickerRowsForModel(provGeneric, 'generic-reasoner')
    expect(rows).toBe(REASONING_PICKER_ROWS)
    expect(rows.map(r => r.value)).toContain('none')
    expect(rows.map(r => r.value)).toContain('')
  })

  it('preselects authoritative effective_reasoning_effort and handles stale/disabled fallback', () => {
    const rows = [{ label: 'low', value: 'low' }, { label: 'medium', value: 'medium' }, { label: 'high', value: 'high' }]

    // 1. Authoritative medium -> index 1
    const provMed = mockProvider('gemini-oauth', 'Google Gemini (OAuth)', {
      'gemini-3.8-flash': { fast: false, reasoning: true, reasoning_efforts: ['low', 'medium', 'high'], effective_reasoning_effort: 'medium' }
    })
    expect(initialReasoningIndexForModel(provMed, 'gemini-3.8-flash', rows)).toBe(1)

    // 2. Cloud Code disabled (effective = null) -> default high (index 2)
    const provDis = mockProvider('gemini-oauth', 'Google Gemini (OAuth)', {
      'gemini-3.8-flash': { fast: false, reasoning: true, reasoning_efforts: ['low', 'medium', 'high'], effective_reasoning_effort: null }
    })
    expect(initialReasoningIndexForModel(provDis, 'gemini-3.8-flash', rows)).toBe(2)

    // 3. Stale effective effort not in supplied rows -> safely defaults to high (index 2)
    const provStale = mockProvider('gemini-oauth', 'Google Gemini (OAuth)', {
      'gemini-3.8-flash': { fast: false, reasoning: true, reasoning_efforts: ['low', 'medium', 'high'], effective_reasoning_effort: 'ultra' }
    })
    expect(initialReasoningIndexForModel(provStale, 'gemini-3.8-flash', rows)).toBe(2)
  })

  it('forwards compatible behavior when capability fields are missing or null', () => {
    const provFuture = mockProvider('future-ai', 'Future AI', {
      'future-model': { fast: false, reasoning: true, reasoning_efforts: null }
    })
    expect(pickerOffersReasoning(provFuture, 'future-model')).toBe(true)
    expect(reasoningPickerRowsForModel(provFuture, 'future-model')).toBe(REASONING_PICKER_ROWS)
  })
})

describe('Milestone 3 — ModelPicker rendered Ink component contracts', () => {
  it('renders Cloud Code 3.8 with effective medium: shows medium selected and marked as current', async () => {
    const f = await fixture()
    const selected: string[] = []
    const gw = {
      request: async () => ({
        providers: [{
          slug: 'gemini-oauth',
          name: 'Google Gemini (OAuth)',
          authenticated: true,
          models: ['gemini-3.8-flash'],
          capabilities: {
            'gemini-3.8-flash': {
              fast: false,
              reasoning: true,
              reasoning_efforts: ['low', 'medium', 'high'],
              can_disable_reasoning: false,
              effective_reasoning_effort: 'medium'
            }
          }
        }]
      })
    } as unknown as GatewayClient

    try {
      f.root.render(<Box flexDirection="column"><ModelPicker gw={gw} onCancel={() => {}} onSelect={s => selected.push(s)} t={DARK_THEME} /></Box>)
      await settle(150)
      await f.click('Google Gemini (OAuth)')
      await f.click('1. gemini-3.8-flash')

      const text = f.visible().join('\n')
      expect(text).toContain('Reasoning effort (step 3/3)')
      expect(text).toContain('gemini-3.8-flash')
      expect(text).toContain('1. low')
      expect(text).toContain('2. medium  ← current')
      expect(text).toContain('3. high')
      expect(text).toContain('▸ 2. medium  ← current') // medium visually cursor-selected
      expect(text).not.toContain('none (disable reasoning)')

      // Click medium
      await f.click('2. medium')
      expect(selected).toHaveLength(1)
      expect(selected[0]).toBe('gemini-3.8-flash --provider gemini-oauth --reasoning medium --tui-session')
    } finally {
      f.close()
    }
  })

  it('renders Cloud Code disabled: shows high selected visually with no current marker and no none row', async () => {
    const f = await fixture()
    const selected: string[] = []
    const gw = {
      request: async () => ({
        providers: [{
          slug: 'gemini-oauth',
          name: 'Google Gemini (OAuth)',
          authenticated: true,
          models: ['gemini-3.8-flash'],
          capabilities: {
            'gemini-3.8-flash': {
              fast: false,
              reasoning: true,
              reasoning_efforts: ['low', 'medium', 'high'],
              can_disable_reasoning: false,
              effective_reasoning_effort: null // disabled
            }
          }
        }]
      })
    } as unknown as GatewayClient

    try {
      f.root.render(<Box flexDirection="column"><ModelPicker gw={gw} onCancel={() => {}} onSelect={s => selected.push(s)} t={DARK_THEME} /></Box>)
      await settle(150)
      await f.click('Google Gemini (OAuth)')
      await f.click('1. gemini-3.8-flash')

      const text = f.visible().join('\n')
      expect(text).toContain('Reasoning effort (step 3/3)')
      expect(text).toContain('gemini-3.8-flash')
      expect(text).toContain('1. low')
      expect(text).toContain('2. medium')
      expect(text).toContain('3. high')
      expect(text).not.toContain('← current') // No row marked current
      expect(text).not.toContain('none') // No none row
      expect(text).toContain('▸ 3. high') // High visually selected by default
    } finally {
      f.close()
    }
  })

  it('skips reasoning stage entirely when picking a known no-effort route', async () => {
    const f = await fixture()
    const selected: string[] = []
    const gw = {
      request: async () => ({
        providers: [{
          slug: 'gemini-oauth',
          name: 'Google Gemini (OAuth)',
          authenticated: true,
          models: ['gemini-3.1-flash-lite'],
          capabilities: {
            'gemini-3.1-flash-lite': {
              fast: false,
              reasoning: true,
              reasoning_efforts: [] // No selectable effort
            }
          }
        }]
      })
    } as unknown as GatewayClient

    try {
      f.root.render(<Box flexDirection="column"><ModelPicker gw={gw} onCancel={() => {}} onSelect={s => selected.push(s)} t={DARK_THEME} /></Box>)
      await settle(150)
      await f.click('Google Gemini (OAuth)')
      await f.click('1. gemini-3.1-flash-lite')

      // Selecting model immediately completes without reasoning prompt
      expect(selected).toHaveLength(1)
      expect(selected[0]).toBe('gemini-3.1-flash-lite --provider gemini-oauth --tui-session')
    } finally {
      f.close()
    }
  })
})
