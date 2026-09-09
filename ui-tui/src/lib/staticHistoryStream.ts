import type { Msg, SessionInfo } from '../types.js'
import { globalHistoryWorkerPool } from './historyWorkerPool.js'

function formatIntroBanner(info?: SessionInfo, cols: number = 80): string {
  const w = Math.max(20, Math.min(cols, 80))
  const line = '─'.repeat(w)
  const title = ' Nous Research · Messenger of the Digital Gods '
  const padLen = Math.max(0, Math.floor((w - title.length) / 2))
  const centeredTitle = ' '.repeat(padLen) + title

  const out: string[] = [
    `\x1b[36m${line}\x1b[0m`,
    `\x1b[1;36m${centeredTitle}\x1b[0m`,
    `\x1b[36m${line}\x1b[0m`
  ]

  if (info) {
    const model = info.model ? `model: ${info.model}` : ''
    const tools = info.tools ? `${info.tools.length} tools active` : ''
    const meta = [model, tools].filter(Boolean).join(' · ')
    if (meta) {
      out.push(`\x1b[90m${meta}\x1b[0m`)
    }
  }

  return out.join('\n')
}

export async function formatCompleteHistory(
  items: Msg[],
  cols: number,
  info?: SessionInfo
): Promise<string> {
  const introMsg = items.find(m => m.kind === 'intro')
  const sessionInfo = info ?? introMsg?.info
  const contentItems = items.filter(m => m.kind !== 'intro')

  const bannerText = formatIntroBanner(sessionInfo, cols)
  const historyText = await globalHistoryWorkerPool.formatParallel(contentItems, cols)

  if (historyText) {
    return `${bannerText}\n\n${historyText}\n`
  }

  return `${bannerText}\n`
}
