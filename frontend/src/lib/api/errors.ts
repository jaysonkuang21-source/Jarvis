/**
 * Human-readable API / network error helpers for Settings and the rest of the UI.
 * Never surface raw JSON error bodies to users.
 */

/** Stable machine codes we classify for recoverable Settings UX. */
export type ApiErrorCode =
  | 'rate_limited'
  | 'unauthorized'
  | 'backend_unreachable'
  | 'http_error'
  | 'unknown'

/**
 * Classify a failed fetch into a stable code and a short user-facing message.
 * Prefer backend ``detail`` when it is already prose; never return raw JSON.
 */
export function describeApiFailure(
  status: number,
  body: string,
  statusText = '',
): { code: ApiErrorCode; message: string } {
  const trimmed = body.trim()
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      const parsed = JSON.parse(trimmed) as {
        error?: unknown
        detail?: unknown
      }
      const error =
        typeof parsed.error === 'string' ? parsed.error : undefined
      const detail =
        typeof parsed.detail === 'string' ? parsed.detail : undefined

      if (error === 'rate_limited' || status === 429) {
        return {
          code: 'rate_limited',
          message:
            detail && !detail.trim().startsWith('{')
              ? detail
              : 'Too many requests. Wait a moment and try again.',
        }
      }
      if (error === 'unauthorized' || status === 401) {
        return {
          code: 'unauthorized',
          message:
            detail ??
            'Not authorized. Check that the API token matches the backend.',
        }
      }
      if (detail && !detail.trim().startsWith('{')) {
        return { code: 'http_error', message: detail }
      }
      if (error) {
        return { code: 'http_error', message: humanizeErrorCode(error) }
      }
    } catch {
      // Fall through to status-based messages.
    }
  }

  if (status === 429) {
    return {
      code: 'rate_limited',
      message: 'Too many requests. Wait a moment and try again.',
    }
  }
  if (status === 401) {
    return {
      code: 'unauthorized',
      message: 'Not authorized. Check that the API token matches the backend.',
    }
  }
  if (status === 0) {
    return {
      code: 'backend_unreachable',
      message: 'Cannot reach the Jarvis backend. Is it running?',
    }
  }
  if (trimmed && !trimmed.startsWith('{')) {
    return { code: 'http_error', message: trimmed }
  }
  return {
    code: 'http_error',
    message: statusText || `Request failed (${status || 'network'})`,
  }
}

/** Turn a snake_case error code into a short sentence fragment. */
function humanizeErrorCode(code: string): string {
  return code.replace(/_/g, ' ')
}

/** True when the failure is a transient rate limit the user can retry. */
export function isRateLimitedError(err: unknown): boolean {
  return (
    typeof err === 'object' &&
    err !== null &&
    'code' in err &&
    (err as { code?: string }).code === 'rate_limited'
  )
}

/** True when the backend process is not reachable at all. */
export function isUnreachableError(err: unknown): boolean {
  return (
    typeof err === 'object' &&
    err !== null &&
    'code' in err &&
    (err as { code?: string }).code === 'backend_unreachable'
  )
}
