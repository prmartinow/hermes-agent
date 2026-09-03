import { describe, expect, it } from 'vitest'

describe('AgentsOverlay layout and rendering invariants', () => {
  describe('1. Vertical Geometry & Overhead (rowsH)', () => {
    it('ensures fixed overhead + rowsH never exceeds terminal rows on 24-row terminals', () => {
      const stdoutRows = 24
      const rowsCount = 10
      const hasGantt = rowsCount > 0
      const ganttRows = hasGantt ? Math.min(6, rowsCount) + 4 : 0
      const fixedOverhead = 2 /* paddingY */ + 2 /* header */ + ganttRows + 3 /* footer */
      const rowsH = Math.max(3, stdoutRows - fixedOverhead)

      expect(fixedOverhead).toBe(17) // 2 + 2 + 10 + 3
      expect(rowsH).toBe(7)
      expect(fixedOverhead + rowsH).toBeLessThanOrEqual(stdoutRows)
    })

    it('allocates proper rowsH on tall terminals (e.g. 40 rows)', () => {
      const stdoutRows = 40
      const rowsCount = 20
      const hasGantt = rowsCount > 0
      const ganttRows = hasGantt ? Math.min(6, rowsCount) + 4 : 0
      const fixedOverhead = 2 + 2 + ganttRows + 3
      const rowsH = Math.max(3, stdoutRows - fixedOverhead)

      expect(rowsH).toBe(23)
      expect(fixedOverhead + rowsH).toBeLessThanOrEqual(stdoutRows)
    })

    it('enforces minimum rowsH = 3 on very small terminals (e.g. 18 rows)', () => {
      const stdoutRows = 18
      const rowsCount = 4
      const hasGantt = rowsCount > 0
      const ganttRows = hasGantt ? Math.min(6, rowsCount) + 4 : 0
      const fixedOverhead = 2 + 2 + ganttRows + 3
      const rowsH = Math.max(3, stdoutRows - fixedOverhead)

      expect(rowsH).toBe(3)
    })

    it('computes zero gantt overhead when rows list is empty', () => {
      const stdoutRows = 24
      const rowsCount = 0
      const hasGantt = rowsCount > 0
      const ganttRows = hasGantt ? Math.min(6, rowsCount) + 4 : 0
      const fixedOverhead = 2 + 2 + ganttRows + 3
      const rowsH = Math.max(3, stdoutRows - fixedOverhead)

      expect(ganttRows).toBe(0)
      expect(fixedOverhead).toBe(7)
      expect(rowsH).toBe(17)
    })
  })

  describe('2. List Window Boundary Clamping (listWindowStart)', () => {
    const calcWindowStart = (rowsLength: number, rowsH: number, cursor: number) =>
      Math.max(0, Math.min(Math.max(0, rowsLength - rowsH), cursor - Math.floor(rowsH / 2)))

    it('starts at 0 when cursor is at top', () => {
      expect(calcWindowStart(20, 10, 0)).toBe(0)
    })

    it('centers window around cursor when in the middle', () => {
      expect(calcWindowStart(20, 10, 8)).toBe(3) // 8 - 5 = 3
    })

    it('clamps to exactly rows.length - rowsH when cursor is at the bottom (prevents collapsing)', () => {
      const rowsLength = 20
      const rowsH = 10
      const cursor = 19
      const start = calcWindowStart(rowsLength, rowsH, cursor)
      expect(start).toBe(10) // 20 - 10 = 10

      const visibleSlice = Array.from({ length: rowsLength }).slice(start, start + rowsH)
      expect(visibleSlice).toHaveLength(10) // exactly 10 visible rows, no dead space
    })

    it('handles short lists where rows.length < rowsH', () => {
      expect(calcWindowStart(5, 10, 4)).toBe(0)
    })

    it('handles empty list (rows.length = 0)', () => {
      expect(calcWindowStart(0, 10, 0)).toBe(0)
    })
  })

  describe('3. Horizontal Width & Dynamic Badge Allocation', () => {
    it('accounts for inner padding (cols - 2) and dynamic badge lengths', () => {
      const cols = 80
      const effectiveCols = Math.max(20, cols - 2) // 78
      expect(effectiveCols).toBe(78)

      const idWidth = 4
      const glyphWidth = 2
      const heatWidth = 1
      const toolsCount = ' ·124t'
      const kids = ' ·2↓'
      const trailing = ' · search_files'
      const badgesWidth = toolsCount.length + kids.length + trailing.length // 6 + 4 + 15 = 25
      const depth = 1

      const maxGoalWidth = Math.max(
        10,
        effectiveCols - idWidth - glyphWidth - heatWidth - badgesWidth - depth * 2 - 2
      )

      const totalEstimatedLine = 1 + 3 + 1 + depth * 2 + heatWidth + glyphWidth + maxGoalWidth + badgesWidth
      expect(totalEstimatedLine).toBeLessThanOrEqual(effectiveCols)
    })

    it('enforces minimum goal preview width of 10 chars under heavy badge pressure', () => {
      const effectiveCols = 40
      const idWidth = 4
      const glyphWidth = 2
      const heatWidth = 1
      const badgesWidth = 35 // extremely long trailing tool + badges
      const depth = 2

      const maxGoalWidth = Math.max(
        10,
        effectiveCols - idWidth - glyphWidth - heatWidth - badgesWidth - depth * 2 - 2
      )

      expect(maxGoalWidth).toBe(10)
    })
  })

  describe('4. Dynamic Gantt Gutter & Ruler Alignment', () => {
    it('uses 2-digit gutter for < 100 subagents', () => {
      const spansLength = 48
      const maxIdDigits = Math.max(2, String(spansLength).length)
      const gutterSpaces = ' '.repeat(maxIdDigits + 2)
      const idGutter = maxIdDigits + 3

      expect(maxIdDigits).toBe(2)
      expect(gutterSpaces).toBe('    ') // 4 spaces
      expect(idGutter).toBe(5)
    })

    it('dynamically expands gutter for >= 100 subagents to keep ruler aligned', () => {
      const spansLength = 150
      const maxIdDigits = Math.max(2, String(spansLength).length)
      const gutterSpaces = ' '.repeat(maxIdDigits + 2)
      const idGutter = maxIdDigits + 3

      expect(maxIdDigits).toBe(3)
      expect(gutterSpaces).toBe('     ') // 5 spaces
      expect(idGutter).toBe(6)
    })

    it('dynamically expands gutter for >= 1000 subagents (stress test)', () => {
      const spansLength = 1200
      const maxIdDigits = Math.max(2, String(spansLength).length)
      const gutterSpaces = ' '.repeat(maxIdDigits + 2)
      const idGutter = maxIdDigits + 3

      expect(maxIdDigits).toBe(4)
      expect(gutterSpaces).toBe('      ') // 6 spaces
      expect(idGutter).toBe(7)
    })
  })

  describe('5. Replay Mode Gating', () => {
    it('keeps replayMode false when historyIndex is 0 even if history is non-empty', () => {
      const historyIndex = 0
      const history = [{ subagents: [], finishedAt: Date.now(), label: 'Turn 1' }]
      const replayMode = historyIndex > 0 && history.length > 0
      const activeSnapshot = replayMode ? (history[historyIndex - 1] ?? null) : null

      expect(replayMode).toBe(false)
      expect(activeSnapshot).toBeNull()
    })

    it('activates replayMode when historyIndex > 0', () => {
      const historyIndex = 1
      const history = [{ subagents: [{ id: 'sa-1' }], finishedAt: 1234567, label: 'Turn 1' }]
      const replayMode = historyIndex > 0 && history.length > 0
      const activeSnapshot = replayMode ? (history[historyIndex - 1] ?? null) : null

      expect(replayMode).toBe(true)
      expect(activeSnapshot).toMatchObject({ label: 'Turn 1' })
    })

    it('handles out-of-range historyIndex safely', () => {
      const historyIndex = 5
      const history = [{ subagents: [{ id: 'sa-1' }], finishedAt: 1234567, label: 'Turn 1' }]
      const replayMode = historyIndex > 0 && history.length > 0
      const activeSnapshot = replayMode ? (history[historyIndex - 1] ?? null) : null

      expect(replayMode).toBe(true)
      expect(activeSnapshot).toBeNull()
    })
  })

  describe('6. Filter & Sort Invariants', () => {
    const filterPredicates = {
      all: () => true,
      leaf: (n: any) => n.children.length === 0,
      running: (n: any) => n.item.status === 'running' || n.item.status === 'queued',
      failed: (n: any) =>
        n.item.status === 'error' ||
        n.item.status === 'failed' ||
        n.item.status === 'interrupted' ||
        n.item.status === 'timeout'
    }

    it('filters running vs completed vs failed nodes accurately', () => {
      const runningNode = { item: { status: 'running' }, children: [] }
      const queuedNode = { item: { status: 'queued' }, children: [] }
      const completedNode = { item: { status: 'completed' }, children: [] }
      const failedNode = { item: { status: 'failed' }, children: [] }
      const errorNode = { item: { status: 'error' }, children: [] }
      const timeoutNode = { item: { status: 'timeout' }, children: [] }

      expect(filterPredicates.running(runningNode)).toBe(true)
      expect(filterPredicates.running(queuedNode)).toBe(true)
      expect(filterPredicates.running(completedNode)).toBe(false)

      expect(filterPredicates.failed(failedNode)).toBe(true)
      expect(filterPredicates.failed(errorNode)).toBe(true)
      expect(filterPredicates.failed(timeoutNode)).toBe(true)
      expect(filterPredicates.failed(completedNode)).toBe(false)
      expect(filterPredicates.failed(runningNode)).toBe(false)
    })

    it('identifies leaf nodes vs parent nodes', () => {
      const parentNode = { item: { status: 'completed' }, children: [{ item: { status: 'completed' } }] }
      const leafNode = { item: { status: 'completed' }, children: [] }

      expect(filterPredicates.leaf(parentNode)).toBe(false)
      expect(filterPredicates.leaf(leafNode)).toBe(true)
    })
  })
})
