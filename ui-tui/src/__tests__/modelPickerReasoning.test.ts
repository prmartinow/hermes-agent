import type { ModelOptionProvider } from '@hermes/shared/gateway-events'
import { describe, expect, it } from 'vitest'

import { draftModelNameFromArg } from '../components/activeSessionSwitcher.js'
import {
  modelPickerCommand,
  pickerOffersReasoning,
  reasoningPickerRowsForModel,
  REASONING_PICKER_ROWS
} from '../components/modelPicker.js'

const provider = (capabilities?: ModelOptionProvider['capabilities']): ModelOptionProvider => ({
  capabilities,
  name: 'Nous Portal',
  slug: 'nous'
})

describe('ModelPicker reasoning step', () => {
  it('emits one /model request carrying provider, effort and scope', () => {
    expect(modelPickerCommand('gpt-5.6', 'nous', false, 'high')).toBe(
      'gpt-5.6 --provider nous --reasoning high --tui-session'
    )
    expect(modelPickerCommand('gpt-5.6', 'nous', true, 'none')).toBe(
      'gpt-5.6 --provider nous --reasoning none --global'
    )
    // "Keep current effort" (empty value) adds no flag at all.
    expect(modelPickerCommand('gpt-5.6', 'nous', false, '')).toBe('gpt-5.6 --provider nous --tui-session')
    expect(REASONING_PICKER_ROWS.at(-1)?.value).toBe('')
    // The new-session draft label strips the effort flag like it strips --provider.
    expect(draftModelNameFromArg(modelPickerCommand('gpt-5.6', 'nous', false, 'low'))).toBe('gpt-5.6')
  })

  it('skips the step only when the catalog says the route has no reasoning control', () => {
    expect(pickerOffersReasoning(provider({ 'gpt-5.6': { fast: false, reasoning: false } }), 'gpt-5.6')).toBe(false)
    expect(pickerOffersReasoning(provider({ 'gpt-5.6': { fast: false, reasoning: true } }), 'gpt-5.6')).toBe(true)
    expect(pickerOffersReasoning(provider(undefined), 'gpt-5.6')).toBe(true)
    expect(pickerOffersReasoning(undefined, 'gpt-5.6')).toBe(true)
  })

  it('skips reasoning step when reasoning_efforts is explicitly empty', () => {
    expect(pickerOffersReasoning(provider({ 'flash-lite': { fast: false, reasoning: true, reasoning_efforts: [] } }), 'flash-lite')).toBe(false)
  })

  it('synthesizes exact capability reasoning rows without Keep current for Cloud Code', () => {
    // 3.8 flash: exactly ['low', 'medium', 'high']
    const rows38 = reasoningPickerRowsForModel(
      provider({ 'gemini-3.8-flash': { fast: false, reasoning: true, reasoning_efforts: ['low', 'medium', 'high'], can_disable_reasoning: false } }),
      'gemini-3.8-flash'
    )
    expect(rows38.map(r => r.value)).toEqual(['low', 'medium', 'high'])

    // 3.1 pro: exactly ['low', 'high']
    const rows31 = reasoningPickerRowsForModel(
      provider({ 'gemini-3.1-pro': { fast: false, reasoning: true, reasoning_efforts: ['low', 'high'], can_disable_reasoning: false } }),
      'gemini-3.1-pro'
    )
    expect(rows31.map(r => r.value)).toEqual(['low', 'high'])

    // Generic fallback when reasoning_efforts is undefined: retains Keep current effort
    const genericRows = reasoningPickerRowsForModel(
      provider({ 'gpt-5.6': { fast: false, reasoning: true } }),
      'gpt-5.6'
    )
    expect(genericRows).toBe(REASONING_PICKER_ROWS)
    expect(genericRows.map(r => r.value)).toContain('')
  })
})
