import { JsonRpcGatewayError } from '@hermes/shared/json-rpc-channel'

export type ResumeFailure = 'identity' | 'retry-same' | 'transport' | 'server'

export interface ResumeFailureDetails {
  identity?: boolean
  kind: ResumeFailure
  reason?: string
  retryable?: boolean
}

export function classifyResumeFailure(error: unknown): ResumeFailureDetails {
  if (!(error instanceof JsonRpcGatewayError)) {
    return { kind: 'transport' }
  }

  const data = error.data as { identity?: boolean; reason?: string; retryable?: boolean } | undefined
  if (data?.identity === true || data?.reason === 'session_not_found') {
    return { identity: true, kind: 'identity', reason: data?.reason }
  }
  if (data?.retryable === true || data?.reason === 'runtime_replaced' || data?.reason === 'disconnect_interrupt_settling') {
    return { kind: 'retry-same', reason: data?.reason, retryable: true }
  }

  // Backward compatibility with legacy or untagged gateways
  if (error.code === 4007 && typeof error.message === 'string' && error.message.includes('session not found')) {
    return { identity: true, kind: 'identity', reason: 'session_not_found' }
  }
  if (error.code === 4009) {
    return { kind: 'retry-same', reason: 'busy', retryable: true }
  }

  return { kind: 'server', reason: error.message }
}
