// Weak ownership: an unmounted terminal cannot retain queued control strings.
const pending = new WeakMap<object, string>()

export function enqueueRenderBoundary(stdout: object, content: string): void {
  pending.set(stdout, (pending.get(stdout) ?? '') + content)
}

export function takeRenderBoundary(stdout: object): string {
  const content = pending.get(stdout) ?? ''
  pending.delete(stdout)
  return content
}
