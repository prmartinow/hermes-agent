import fs from 'fs'
import path from 'path'

let logFilePath: string | null = null

function getLogPath(): string | null {
  if (logFilePath) return logFilePath
  const home = process.env.HERMES_HOME || (process.env.HOME ? path.join(process.env.HOME, '.hermes') : null)
  if (!home) return null
  const logsDir = path.join(home, 'logs')
  try {
    if (!fs.existsSync(logsDir)) {
      fs.mkdirSync(logsDir, { recursive: true })
    }
  } catch {
    /* ignore */
  }
  logFilePath = path.join(logsDir, 'tui-perf.log')
  return logFilePath
}

export function logForDebugging(
  message: string,
  options: {
    level?: string
  } = {}
): void {
  const target = getLogPath()
  if (!target) return

  const level = options.level || 'INFO'
  const ts = new Date().toISOString()
  const line = `${ts} [${level}] ${message}\n`
  try {
    fs.appendFileSync(target, line)
  } catch {
    /* defensive: never crash on logging failure */
  }
}
