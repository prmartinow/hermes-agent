import type { Frame } from './frame.js'
import { applyHyperlinkHoverHighlight } from './hyperlinkHover.js'
import { didLayoutShift } from './render-node-to-output.js'
import { applyPositionedHighlight, type MatchPosition } from './render-to-screen.js'
import type { StylePool } from './screen.js'
import { applySearchHighlight } from './searchHighlight.js'
import { applySelectionOverlay, hasSelection, type SelectionState } from './selection.js'

export interface SearchPositions {
  positions: MatchPosition[]
  rowOffset: number
  currentIdx: number
}

interface FrameOverlays {
  altScreen: boolean
  selection: SelectionState
  stylePool: StylePool
  searchQuery: string
  searchPositions: SearchPositions | null
  hoveredLink: string | undefined
  lastHoveredLink: string | undefined
  previousContaminated: boolean
}

export function applyFrameOverlays(frame: Frame, options: FrameOverlays): boolean {
  const {
    altScreen, selection, stylePool, searchQuery, searchPositions,
    hoveredLink, lastHoveredLink, previousContaminated
  } = options

  // Selection overlay: invert cell styles in the screen buffer itself,
  // so the diff picks up selection as ordinary cell changes and
  // LogUpdate remains a pure diff engine.
  //
  // Full-screen damage (PR #20120) is a correctness backstop for the
  // sibling-resize bleed: when flexbox siblings resize between frames
  // (spinner appears → bottom grows → scrollbox shrinks), the
  // cached-clear + clip-and-cull + setCellAt damage union can miss
  // transition cells at the boundary. But that only happens when layout
  // actually SHIFTS — didLayoutShift() tracks exactly this (any node's
  // cached yoga position/size differs from current, or a child was
  // removed). Steady-state frames (spinner rotate, clock tick, text
  // stream into fixed-height box) don't shift layout, so normal damage
  // bounds are correct and diffEach only compares the damaged region.
  //
  // Selection also requires full damage: overlay writes via setCellStyleId
  // which doesn't track damage, and prev-frame overlay cells need to be
  // compared when selection moves/clears. prevFrameContaminated covers
  // the frame-after-selection-clears case.
  let selActive = false
  let hlActive = false

  if (altScreen) {
    selActive = hasSelection(selection)

    if (selActive) {
      applySelectionOverlay(frame.screen, selection, stylePool)
    }

    // Scan-highlight: inverse on ALL visible matches (less/vim style).
    // Position-highlight (below) overlays CURRENT (yellow) on top.
    hlActive = applySearchHighlight(frame.screen, searchQuery, stylePool)

    // Hyperlink hover overlay: inverts every cell of the link currently
    // under the pointer. Cheap-ish (linear scan of the visible buffer),
    // only fires when hoveredHyperlink is set.
    //
    // hlActive controls full-screen damage (used by selection/search to
    // make sure the previous frame's inverted cells get re-diffed when
    // the highlight set changes). For hover, the *transition* is what
    // needs the full-damage hammer — enter / leave / change-to-other-link.
    // During steady-state hover the painted cells don't change and the
    // ordinary per-cell diff handles the no-op. Folding the steady-state
    // case into hlActive would burn full-screen diffs every frame while
    // the pointer just sits on the link.
    const hoverApplied = applyHyperlinkHoverHighlight(frame.screen, hoveredLink, stylePool)
    const hoverTransition = hoveredLink !== lastHoveredLink

    if (hoverApplied && hoverTransition) {
      hlActive = true
    }

    // Position-based CURRENT: write yellow at positions[currentIdx] +
    // rowOffset. No scanning — positions came from a prior scan when
    // the message first mounted. Message-relative + rowOffset = screen.
    if (searchPositions) {
      const sp = searchPositions

      const posApplied = applyPositionedHighlight(
        frame.screen,
        stylePool,
        sp.positions,
        sp.rowOffset,
        sp.currentIdx
      )

      hlActive = hlActive || posApplied
    }
  }

  // Full-damage backstop: applies on BOTH alt-screen and main-screen.
  // Layout shifts (spinner appears, status line resizes) can leave stale
  // cells at sibling boundaries that per-node damage tracking misses.
  // Selection/highlight overlays write via setCellStyleId which doesn't
  // track damage. prevFrameContaminated covers the cleanup frame.
  const hasOverlays = selActive || hlActive
  if (didLayoutShift() || hasOverlays || previousContaminated) {
    if (altScreen) {
      frame.screen.damage = {
        x: 0,
        y: 0,
        width: frame.screen.width,
        height: frame.screen.height
      }
    } else {
      const vpY = Math.max(0, frame.screen.height - frame.viewport.height)
      frame.screen.damage = {
        x: 0,
        y: vpY,
        width: frame.screen.width,
        height: frame.screen.height - vpY
      }
    }
  }

  return hasOverlays
}
