/**
 * Session-only OpenAI-compatible LLM credentials for demo BYOK.
 *
 * Held in module memory only — never localStorage/sessionStorage/URL.
 * Cleared on sign-out. Never log these values.
 */

let sessionLlmKey: string | null = null
let sessionLlmBaseUrl: string | null = null

/** Store a session LLM API key in memory (trimmed; empty clears). */
export function setSessionLlmKey(key: string | null): void {
  const trimmed = key?.trim() ?? ''
  sessionLlmKey = trimmed.length > 0 ? trimmed : null
}

/** Store an optional OpenAI-compatible base URL (e.g. OpenRouter). */
export function setSessionLlmBaseUrl(url: string | null): void {
  const trimmed = url?.trim().replace(/\/$/, '') ?? ''
  sessionLlmBaseUrl = trimmed.length > 0 ? trimmed : null
}

/** Return whether a session LLM key is currently set (does not reveal it). */
export function hasSessionLlmKey(): boolean {
  return sessionLlmKey !== null
}

/** Return the in-memory key, or null. Callers must not log the value. */
export function getSessionLlmKey(): string | null {
  return sessionLlmKey
}

/** Return the optional base URL, or null. */
export function getSessionLlmBaseUrl(): string | null {
  return sessionLlmBaseUrl
}

/** Wipe session LLM credentials (call on demo sign-out). */
export function clearSessionLlmCredentials(): void {
  sessionLlmKey = null
  sessionLlmBaseUrl = null
}

/** Headers that carry BYOK for demo chat (never log these). */
export function sessionLlmHeaders(): Record<string, string> {
  if (!sessionLlmKey) return {}
  const headers: Record<string, string> = {
    'X-Jarvis-User-LLM-Key': sessionLlmKey,
  }
  if (sessionLlmBaseUrl) {
    headers['X-Jarvis-User-LLM-Base-Url'] = sessionLlmBaseUrl
  }
  return headers
}
