import { PassThrough } from 'stream'
import { Box, renderSync, ScrollBox, type ScrollBoxHandle, Text } from '@hermes/ink'
import React, { useRef } from 'react'
import { describe, expect, it } from 'vitest'
import { useVirtualHistory } from '../hooks/useVirtualHistory.js'

interface Item {
  key: string
  text: string
}

function Harness({
  items,
  inline,
  onResult,
}: {
  items: Item[]
  inline: boolean
  onResult: (res: ReturnType<typeof useVirtualHistory>) => void
}) {
  const scrollRef = useRef<ScrollBoxHandle | null>(null)
  const vh = useVirtualHistory(scrollRef, items, 80, {
    inline,
    maxMounted: 5,
    coldStartCount: 5,
  })

  onResult(vh)

  return (
    <ScrollBox height={10} ref={scrollRef}>
      {items.slice(vh.start, vh.end).map(item => (
        <Box key={item.key}>
          <Text>{item.text}</Text>
        </Box>
      ))}
    </ScrollBox>
  )
}

describe('useVirtualHistory inline mode for native browser scrollbar', () => {
  it('mounts 100% of items from start to end with 0 spacers when inline is true', () => {
    const items: Item[] = Array.from({ length: 50 }, (_, i) => ({
      key: `item-${i}`,
      text: `Message ${i}`,
    }))

    let result!: ReturnType<typeof useVirtualHistory>

    const stdout = new PassThrough()
    Object.assign(stdout, { columns: 80, isTTY: false, rows: 20 })

    renderSync(
      <Harness
        items={items}
        inline={true}
        onResult={res => {
          result = res
        }}
      />,
      { stdout }
    )

    expect(result.start).toBe(0)
    expect(result.end).toBe(50)
    expect(result.topSpacer).toBe(0)
    expect(result.bottomSpacer).toBe(0)
  })

  it('virtualizes items into a windowed slice when inline is false', () => {
    const items: Item[] = Array.from({ length: 50 }, (_, i) => ({
      key: `item-${i}`,
      text: `Message ${i}`,
    }))

    let result!: ReturnType<typeof useVirtualHistory>

    const stdout = new PassThrough()
    Object.assign(stdout, { columns: 80, isTTY: false, rows: 20 })

    renderSync(
      <Harness
        items={items}
        inline={false}
        onResult={res => {
          result = res
        }}
      />,
      { stdout }
    )

    expect(result.end - result.start).toBeLessThan(50)
  })
})
