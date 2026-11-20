export function writeAndDrain(stdout: NodeJS.WriteStream, data: string | Uint8Array): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    let settled = false
    let drained = false
    let writeCompleted = false

    const cleanup = () => {
      stdout.off('drain', onDrain)
      stdout.off('error', onError)
      stdout.off('close', onClose)
    }

    const onDrain = () => {
      drained = true
      if (writeCompleted && !settled) {
        settled = true
        cleanup()
        resolve()
      }
    }

    const onError = (err: Error) => {
      if (!settled) {
        settled = true
        cleanup()
        reject(err)
      }
    }

    const onClose = () => {
      if (!settled) {
        settled = true
        cleanup()
        reject(new Error('stdout stream closed'))
      }
    }

    stdout.once('drain', onDrain)
    stdout.once('error', onError)
    stdout.once('close', onClose)

    let ok: boolean
    try {
      ok = stdout.write(data, (err?: Error | null) => {
        if (err) {
          onError(err)
          return
        }
        writeCompleted = true
        if (drained && !settled) {
          settled = true
          cleanup()
          resolve()
        }
      })
    } catch (err) {
      onError(err as Error)
      return
    }

    if (ok) {
      drained = true
      if (writeCompleted && !settled) {
        settled = true
        cleanup()
        resolve()
      }
    }
  })
}
