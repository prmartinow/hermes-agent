import React, { type PropsWithChildren, useContext, useInsertionEffect } from 'react'

import instances from '../instances.js'
import { CURSOR_HOME, ERASE_SCREEN, ERASE_SCROLLBACK } from '../termio/csi.js'
import {
  DISABLE_MOUSE_TRACKING,
  enableMouseTrackingFor,
  ENTER_ALT_SCREEN,
  EXIT_ALT_SCREEN,
  type MouseTrackingMode
} from '../termio/dec.js'
import { TerminalWriteContext } from '../useTerminalNotification.js'

import Box from './Box.js'
import { TerminalSizeContext } from './TerminalSizeContext.js'

type Props = PropsWithChildren<{
  /**
   * Which SGR mouse-tracking preset to enable. Default 'all' — wheel +
   * click + drag + hover (1000 + 1002 + 1003 + 1006). Set to 'wheel'
   * (1000 + 1006) to silence the noisy hover events that tmux turns into
   * "No image in clipboard" spam over the prompt row, while keeping
   * scroll-wheel scrolling. 'off' disables tracking entirely.
   */
  mouseTracking?: MouseTrackingMode
  /**
   * If true, runs inline in the primary screen buffer without alt-screen
   * switching or height constraints, while asserting DEC mouse tracking
   * and notifying the Ink instance for click dispatching.
   */
  inline?: boolean
}>

/**
 * Run children in the terminal's alternate screen buffer, constrained to
 * the viewport height (or inline in primary buffer if inline=true).
 */
export function AlternateScreen(props: Props) {
  const { children, mouseTracking = 'all', inline = false } = props
  const size = useContext(TerminalSizeContext)
  const writeRaw = useContext(TerminalWriteContext)

  useInsertionEffect(() => {
    const ink = instances.get(process.stdout)
    if (!writeRaw) {
      return
    }

    const enableMouse = enableMouseTrackingFor(mouseTracking)

    if (inline) {
      writeRaw(DISABLE_MOUSE_TRACKING + enableMouse)
      ink?.setInlineMouseTracking(mouseTracking)

      return () => {
        ink?.setInlineMouseTracking('off')
        ink?.clearTextSelection()
        writeRaw(DISABLE_MOUSE_TRACKING)
      }
    }

    writeRaw(ENTER_ALT_SCREEN + ERASE_SCROLLBACK + ERASE_SCREEN + CURSOR_HOME + DISABLE_MOUSE_TRACKING + enableMouse)
    ink?.setAltScreenActive(true, mouseTracking)

    return () => {
      ink?.setAltScreenActive(false)
      ink?.clearTextSelection()
      writeRaw(DISABLE_MOUSE_TRACKING + EXIT_ALT_SCREEN)
    }
  }, [writeRaw, mouseTracking, inline])

  if (inline) {
    return <>{children}</>
  }

  const height = size?.rows ?? 24
  return (
    <Box flexDirection="column" flexShrink={0} height={height} width="100%">
      {children}
    </Box>
  )
}
