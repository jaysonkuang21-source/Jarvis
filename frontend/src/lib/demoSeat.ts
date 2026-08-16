/**
 * Anonymous demo seat id (sessionStorage) for the per-IP concurrent user cap.
 */

const STORAGE_KEY = 'jarvis.demo.seat'

let memorySeatId: string | null = null

/** Read the cached seat id from memory or sessionStorage. */
export function getDemoSeatId(): string | null {
  if (memorySeatId) return memorySeatId
  try {
    const fromStore = sessionStorage.getItem(STORAGE_KEY)
    if (fromStore && fromStore.trim()) {
      memorySeatId = fromStore.trim()
      return memorySeatId
    }
  } catch {
    // sessionStorage may be unavailable
  }
  return null
}

/** Persist a seat id for this browser tab. */
export function setDemoSeatId(seatId: string | null): void {
  const trimmed = seatId?.trim() ?? ''
  memorySeatId = trimmed.length > 0 ? trimmed : null
  try {
    if (memorySeatId) sessionStorage.setItem(STORAGE_KEY, memorySeatId)
    else sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // ignore quota / private mode
  }
}

/** Header map carrying the demo seat when one is known. */
export function demoSeatHeaders(): Record<string, string> {
  const seat = getDemoSeatId()
  if (!seat) return {}
  return { 'X-Jarvis-Demo-Seat': seat }
}
