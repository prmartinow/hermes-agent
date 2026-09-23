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

describe('useVirtualHistory inline mode for full history reflow', () => {
  it('mounts 100% of items when history is within maxMounted, and bounds to live tail when history exceeds maxMounted', () => {
    // 1. Within maxMounted (5 items with maxMounted: 5)
    const smallItems: Item[] = Array.from({ length: 5 }, (_, i) => ({
      key: `item-${i}`,
      text: `Message ${i}`,
    }))

    let resultSmall!: ReturnType<typeof useVirtualHistory>
    const stdout1 = new PassThrough()
    Object.assign(stdout1, { columns: 80, isTTY: false, rows: 20 })

    renderSync(
      <Harness
        items={smallItems}
        inline={true}
        onResult={res => {
          resultSmall = res
        }}
      />,
      { stdout1 }
    )

    expect(resultSmall.start).toBe(0)
    expect(resultSmall.end).toBe(5)
    expect(resultSmall.topSpacer).toBe(0)
    expect(resultSmall.bottomSpacer).toBe(0)

    // 2. Exceeding maxMounted (50 items with maxMounted: 5 -> bounds to latest 5 items: 45..50)
    const largeItems: Item[] = Array.from({ length: 50 }, (_, i) => ({
      key: `item-${i}`,
      text: `Message ${i}`,
    }))

    let resultLarge!: ReturnType<typeof useVirtualHistory>
    const stdout2 = new PassThrough()
    Object.assign(stdout2, { columns: 80, isTTY: false, rows: 20 })

    renderSync(
      <Harness
        items={largeItems}
        inline={true}
        onResult={res => {
          resultLarge = res
        }}
      />,
      { stdout2 }
    )

    expect(resultLarge.start).toBe(45)
    expect(resultLarge.end).toBe(50)
    expect(resultLarge.topSpacer).toBe(0)
    expect(resultLarge.bottomSpacer).toBe(0)
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
