import { Worker } from 'worker_threads'
import type { Msg } from '../types.js'

export const WORKER_POOL_SIZE = 24

interface WorkerTask {
  cols: number
  id: number
  items: Msg[]
}

interface WorkerResult {
  chunk: string
  id: number
}

// Inline worker script using pure CommonJS/ESM built-in semantics.
// Zero filesystem dependency — works seamlessly across dev and bundled entry.js.
const WORKER_SCRIPT = `
const { parentPort } = require('worker_threads');

function wrapText(text, maxCols) {
  if (!text || maxCols <= 10) return text || '';
  const lines = text.split(/\\r?\\n/);
  const out = [];

  for (const line of lines) {
    if (line.length <= maxCols) {
      out.push(line);
      continue;
    }

    let remaining = line;
    while (remaining.length > maxCols) {
      let splitAt = remaining.lastIndexOf(' ', maxCols);
      if (splitAt <= 0 || splitAt < maxCols - 20) {
        splitAt = maxCols;
      }
      out.push(remaining.slice(0, splitAt));
      remaining = remaining.slice(splitAt).trimStart();
    }
    if (remaining.length > 0) {
      out.push(remaining);
    }
  }

  return out.join(String.fromCharCode(10));
}

function formatSingleMsg(msg, cols) {
  if (!msg) return '';

  const rule = String.fromCharCode(9472).repeat(Math.max(4, Math.min(cols, 80)));
  const out = [];

  if (msg.role === 'user') {
    const wrapped = wrapText(msg.text, cols - 4);
    out.push(String.fromCharCode(27) + '[36m❯' + String.fromCharCode(27) + '[0m ' + wrapped);
  } else if (msg.role === 'assistant') {
    const text = msg.text || '';
    const wrapped = wrapText(text, cols - 2);
    out.push(wrapped);
    out.push(String.fromCharCode(27) + '[90m' + rule + String.fromCharCode(27) + '[0m');
  } else if (msg.role === 'system') {
    if (msg.text) {
      const wrapped = wrapText(msg.text, cols - 4);
      out.push(String.fromCharCode(27) + '[90m' + wrapped + String.fromCharCode(27) + '[0m');
    }
  }

  return out.join(String.fromCharCode(10));
}

if (parentPort) {
  parentPort.on('message', ({ id, items, cols }) => {
    try {
      const formatted = items.map(msg => formatSingleMsg(msg, cols)).filter(Boolean);
      const chunk = formatted.join(String.fromCharCode(10) + String.fromCharCode(10));
      parentPort.postMessage({ id, chunk });
    } catch (err) {
      parentPort.postMessage({ id, chunk: '', error: String(err) });
    }
  });
}
`

export class HistoryWorkerPool {
  private activeWorkers: Worker[] = []
  private isDisposed = false
  private taskIdCounter = 0

  constructor(private readonly size = WORKER_POOL_SIZE) {
    this.initPool()
  }

  private initPool() {
    try {
      for (let i = 0; i < this.size; i++) {
        const worker = new Worker(WORKER_SCRIPT, { eval: true })
        worker.unref?.()
        this.activeWorkers.push(worker)
      }
    } catch {
      this.activeWorkers = []
    }
  }

  async formatParallel(items: Msg[], cols: number): Promise<string> {
    if (this.isDisposed || this.activeWorkers.length === 0 || items.length < 50) {
      return this.formatSync(items, cols)
    }

    const workerCount = this.activeWorkers.length
    const chunkSize = Math.ceil(items.length / workerCount)
    const promises: Promise<string>[] = []

    for (let i = 0; i < workerCount; i++) {
      const start = i * chunkSize
      const end = Math.min(items.length, start + chunkSize)
      if (start >= items.length) break

      const slice = items.slice(start, end)
      const worker = this.activeWorkers[i]!
      const taskId = ++this.taskIdCounter

      const p = new Promise<string>((resolve) => {
        const handler = (res: WorkerResult) => {
          if (res.id === taskId) {
            worker.off('message', handler)
            resolve(res.chunk)
          }
        }
        worker.on('message', handler)
        worker.postMessage({ cols, id: taskId, items: slice } satisfies WorkerTask)
      })

      promises.push(p)
    }

    const chunks = await Promise.all(promises)
    return chunks.filter(Boolean).join('\n\n')
  }

  formatSync(items: Msg[], cols: number): string {
    const rule = '─'.repeat(Math.max(4, Math.min(cols, 80)))
    const out: string[] = []

    for (const msg of items) {
      if (!msg) continue
      if (msg.role === 'user') {
        out.push(`\x1b[36m❯\x1b[0m ${msg.text}`)
      } else if (msg.role === 'assistant') {
        out.push(msg.text)
        out.push(`\x1b[90m${rule}\x1b[0m`)
      } else if (msg.role === 'system' && msg.text) {
        out.push(`\x1b[90m${msg.text}\x1b[0m`)
      }
    }

    return out.join('\n\n')
  }

  dispose() {
    this.isDisposed = true
    for (const worker of this.activeWorkers) {
      try {
        worker.terminate()
      } catch {
        /* ignore */
      }
    }
    this.activeWorkers = []
  }
}

export const globalHistoryWorkerPool = new HistoryWorkerPool()
