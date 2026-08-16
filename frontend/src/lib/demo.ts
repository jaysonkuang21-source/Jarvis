/** True when the Vite build is the public NASA Hackathime demo. */
export const isDemoMode = import.meta.env.VITE_DEMO_MODE === 'true'

/** Banner copy shown above the chat shell in demo builds. */
export const DEMO_BANNER =
  'Demo: GPT-4o mini + sample vault RAG. Bring your own OpenAI-compatible API key (session only — wiped on sign-out). Always rotate the key after use. Rate limited.'

/** Safety copy for the session key form. */
export const DEMO_KEY_SAFETY =
  'Your API key stays in browser memory for this session only and is wiped when you sign out. It is never stored on our servers. Always rotate the key in your provider dashboard after use.'
