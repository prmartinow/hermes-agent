import { closeSync, constants as fsConstants, openSync, readSync, writeSync } from 'fs'

import { DISABLE_KITTY_KEYBOARD, DISABLE_MODIFY_OTHER_KEYS } from './csi.js'
import { DBP, DFE, DISABLE_MOUSE_TRACKING, EXIT_ALT_SCREEN, SHOW_CURSOR } from './dec.js'
import { CLEAR_ITERM2_PROGRESS, CLEAR_TAB_STATUS, supportsTabStatus, wrapForMultiplexer } from './osc.js'

export function resetTerminalAfterExit(altScreenActive: boolean, drainInput: () => void): void {
  if (altScreenActive) {
    // <AlternateScreen>'s unmount effect won't run during signal-exit.
    // Exit alt screen FIRST so other cleanup sequences go to the main screen.
    writeSync(1, EXIT_ALT_SCREEN)
  }

  // Disable mouse tracking — unconditional because altScreenActive can be
  // stale if AlternateScreen's unmount (which flips the flag) raced a
  // blocked event loop + SIGINT. No-op if tracking was never enabled.
  writeSync(1, DISABLE_MOUSE_TRACKING)
  // Drain stdin so in-flight mouse events don't leak to the shell
  drainInput()
  // Disable extended key reporting (both kitty and modifyOtherKeys)
  writeSync(1, DISABLE_MODIFY_OTHER_KEYS)
  writeSync(1, DISABLE_KITTY_KEYBOARD)
  // Disable focus events (DECSET 1004)
  writeSync(1, DFE)
  // Disable bracketed paste mode
  writeSync(1, DBP)
  // Show cursor
  writeSync(1, SHOW_CURSOR)
  // Clear iTerm2 progress bar
  writeSync(1, CLEAR_ITERM2_PROGRESS)

  // Clear tab status (OSC 21337) so a stale dot doesn't linger
  if (supportsTabStatus()) {
    writeSync(1, wrapForMultiplexer(CLEAR_TAB_STATUS))
  }
}

/**
 * Discard pending stdin bytes so in-flight escape sequences (mouse tracking
 * reports, bracketed-paste markers) don't leak to the shell after exit.
 *
 * Two layers of trickiness:
 *
 * 1. setRawMode is termios, not fcntl — the stdin fd stays blocking, so
 *    readSync on it would hang forever. Node doesn't expose fcntl, so we
 *    open /dev/tty fresh with O_NONBLOCK (all fds to the controlling
 *    terminal share one line-discipline input queue).
 *
 * 2. By the time forceExit calls this, detachForShutdown has already put
 *    the TTY back in cooked (canonical) mode. Canonical mode line-buffers
 *    input until newline, so O_NONBLOCK reads return EAGAIN even when
 *    mouse bytes are sitting in the buffer. We briefly re-enter raw mode
 *    so reads return any available bytes, then restore cooked mode.
 *
 * Safe to call multiple times. Call as LATE as possible in the exit path:
 * DISABLE_MOUSE_TRACKING has terminal round-trip latency, so events can
 * arrive for a few ms after it's written.
 */

export function drainStdin(stdin: NodeJS.ReadStream = process.stdin): void {
  if (!stdin.isTTY) {
    return
  }

  // Drain Node's stream buffer (bytes libuv already pulled in). read()
  // returns null when empty — never blocks.
  try {
    while (stdin.read() !== null) {
      /* discard */
    }
  } catch {
    /* stream may be destroyed */
  }

  // No /dev/tty on Windows; CONIN$ doesn't support O_NONBLOCK semantics.
  // Windows Terminal also doesn't buffer mouse reports the same way.
  if (process.platform === 'win32') {
    return
  }

  // termios is per-device: flip stdin to raw so canonical-mode line
  // buffering doesn't hide partial input from the non-blocking read.
  // Restored in the finally block.
  const tty = stdin as NodeJS.ReadStream & {
    isRaw?: boolean
    setRawMode?: (raw: boolean) => void
  }

  const wasRaw = tty.isRaw === true
  // Drain the kernel TTY buffer via a fresh O_NONBLOCK fd. Bounded at 64
  // reads (64KB) — a real mouse burst is a few hundred bytes; the cap
  // guards against a terminal that ignores O_NONBLOCK.
  let fd = -1

  try {
    // setRawMode inside try: on revoked TTY (SIGHUP/SSH disconnect) the
    // ioctl throws EBADF — same recovery path as openSync/readSync below.
    if (!wasRaw) {
      tty.setRawMode?.(true)
    }

    fd = openSync('/dev/tty', fsConstants.O_RDONLY | fsConstants.O_NONBLOCK)
    const buf = Buffer.alloc(1024)

    for (let i = 0; i < 64; i++) {
      if (readSync(fd, buf, 0, buf.length, null) <= 0) {
        break
      }
    }
  } catch {
    // EAGAIN (buffer empty — expected), ENXIO/ENOENT (no controlling tty),
    // EBADF/EIO (TTY revoked — SIGHUP, SSH disconnect)
  } finally {
    if (fd >= 0) {
      try {
        closeSync(fd)
      } catch {
        /* ignore */
      }
    }

    if (!wasRaw) {
      try {
        tty.setRawMode?.(false)
      } catch {
        /* TTY may be gone */
      }
    }
  }
}
