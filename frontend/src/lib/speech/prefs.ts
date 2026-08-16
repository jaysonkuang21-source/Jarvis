const SPEAK_REPLIES_KEY = 'jarvis.voice.speakReplies'

/** Whether assistant replies should be spoken after the stream finishes. */
export function getSpeakReplies(): boolean {
  if (typeof localStorage === 'undefined') return true
  return localStorage.getItem(SPEAK_REPLIES_KEY) !== '0'
}

/** Persist the speak-replies preference (default on). */
export function setSpeakReplies(enabled: boolean): void {
  if (typeof localStorage === 'undefined') return
  localStorage.setItem(SPEAK_REPLIES_KEY, enabled ? '1' : '0')
}
