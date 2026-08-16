/** True when the Vite build is the public NASA Hackathime demo. */
export const isDemoMode = import.meta.env.VITE_DEMO_MODE === 'true'

/** Banner copy shown above the chat shell in demo builds. */
export const DEMO_BANNER =
  'Demo: GPT-4o mini + sample vault (demo/vault). Upload docs in Settings → Ingestion; chunks index into Supabase. BYOK session key required. Max 4 users per IP.'

/** Safety copy for the session key form. */
export const DEMO_KEY_SAFETY =
  'Your API key stays in browser memory for this session only and is wiped when you clear it or close the tab. It is never stored on our servers. Always rotate the key in your provider dashboard after use.'
