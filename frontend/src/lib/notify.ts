/**
 * Notification bridge.
 *
 * Inside Tauri this raises a real Windows toast that survives the window being
 * closed. In a plain browser it degrades to the Notification API, which only
 * works while the tab is open -- the reason the desktop shell exists at all.
 */

/** Detect whether the app is running inside the Tauri desktop shell. */
export function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}

export interface NotifyOptions {
  title: string
  body?: string
}

/** Request notification permission via Tauri or the browser Notification API. */
export async function ensurePermission(): Promise<boolean> {
  if (isTauri()) {
    try {
      const { isPermissionGranted, requestPermission } = await import(
        '@tauri-apps/plugin-notification'
      )
      if (await isPermissionGranted()) return true
      return (await requestPermission()) === 'granted'
    } catch {
      return false
    }
  }

  if (!('Notification' in window)) return false
  if (Notification.permission === 'granted') return true
  if (Notification.permission === 'denied') return false
  return (await Notification.requestPermission()) === 'granted'
}

/**
 * Play a short two-tone chime so timer fires are audible even before TTS starts.
 * Uses Web Audio only — no asset files.
 */
export function playAlertChime(): void {
  if (typeof window === 'undefined') return
  try {
    const Ctx =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext
    if (!Ctx) return
    const ctx = new Ctx()
    const now = ctx.currentTime

    /** Schedule one damped sine beep on the shared context. */
    function tone(freq: number, start: number, dur: number) {
      const osc = ctx.createOscillator()
      const gain = ctx.createGain()
      osc.type = 'sine'
      osc.frequency.value = freq
      gain.gain.setValueAtTime(0.0001, start)
      gain.gain.exponentialRampToValueAtTime(0.22, start + 0.02)
      gain.gain.exponentialRampToValueAtTime(0.0001, start + dur)
      osc.connect(gain)
      gain.connect(ctx.destination)
      osc.start(start)
      osc.stop(start + dur + 0.05)
    }

    tone(880, now, 0.12)
    tone(1175, now + 0.15, 0.2)
    void ctx.resume()
  } catch {
    /* autoplay blocked or AudioContext unavailable */
  }
}

/** Show a desktop toast (Tauri) or browser notification when permitted. */
export async function notify({ title, body }: NotifyOptions): Promise<void> {
  if (!(await ensurePermission())) return

  if (isTauri()) {
    try {
      const { sendNotification } = await import('@tauri-apps/plugin-notification')
      sendNotification({ title, body })
      return
    } catch {
      /* fall through to the browser API */
    }
  }

  try {
    new Notification(title, { body, silent: false })
  } catch {
    /* nothing else to try */
  }
}

/** Launch Jarvis on login so scheduled jobs fire without the window open. */
export async function setAutostart(enabled: boolean): Promise<boolean> {
  if (!isTauri()) return false
  try {
    const { enable, disable, isEnabled } = await import('@tauri-apps/plugin-autostart')
    if (enabled) await enable()
    else await disable()
    return await isEnabled()
  } catch {
    return false
  }
}

/** Read whether Tauri autostart is currently enabled. */
export async function isAutostartEnabled(): Promise<boolean> {
  if (!isTauri()) return false
  try {
    const { isEnabled } = await import('@tauri-apps/plugin-autostart')
    return await isEnabled()
  } catch {
    return false
  }
}
