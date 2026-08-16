/**
 * Supabase browser client for demo Auth only.
 *
 * Holds the publishable anon key — never a service-role key. Session access
 * tokens are forwarded to the Render API as Bearer credentials.
 */

import { createClient, type Session, type SupabaseClient } from '@supabase/supabase-js'
import { isDemoMode } from '@/lib/demo'

let client: SupabaseClient | null = null

/** Return whether demo Supabase env vars are present. */
export function supabaseConfigured(): boolean {
  const url = import.meta.env.VITE_SUPABASE_URL
  const key = import.meta.env.VITE_SUPABASE_ANON_KEY
  return (
    isDemoMode &&
    typeof url === 'string' &&
    url.trim().length > 0 &&
    typeof key === 'string' &&
    key.trim().length > 0
  )
}

/** Lazily create the singleton Supabase client, or null when unset. */
export function getSupabase(): SupabaseClient | null {
  if (!supabaseConfigured()) return null
  if (client) return client
  const url = String(import.meta.env.VITE_SUPABASE_URL).trim()
  const key = String(import.meta.env.VITE_SUPABASE_ANON_KEY).trim()
  client = createClient(url, key, {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  })
  return client
}

/** Current access token for API Bearer headers, or null when signed out. */
export async function getAccessToken(): Promise<string | null> {
  const sb = getSupabase()
  if (!sb) return null
  const { data, error } = await sb.auth.getSession()
  if (error) return null
  return data.session?.access_token ?? null
}

/** Subscribe to auth session changes; returns an unsubscribe function. */
export function onAuthSession(
  handler: (session: Session | null) => void,
): () => void {
  const sb = getSupabase()
  if (!sb) {
    handler(null)
    return () => {}
  }
  void sb.auth.getSession().then(({ data }) => handler(data.session ?? null))
  const { data } = sb.auth.onAuthStateChange((_event, session) => {
    handler(session)
  })
  return () => data.subscription.unsubscribe()
}

/** Sign in with email + password; returns an error message or null on success. */
export async function signInWithPassword(
  email: string,
  password: string,
): Promise<string | null> {
  const sb = getSupabase()
  if (!sb) return 'Supabase is not configured for this build.'
  const { error } = await sb.auth.signInWithPassword({ email, password })
  return error?.message ?? null
}

/** Sign up with email + password; returns an error message or null on success. */
export async function signUpWithPassword(
  email: string,
  password: string,
): Promise<string | null> {
  const sb = getSupabase()
  if (!sb) return 'Supabase is not configured for this build.'
  const { error } = await sb.auth.signUp({ email, password })
  return error?.message ?? null
}

/** Clear the local Supabase session. */
export async function signOut(): Promise<void> {
  const sb = getSupabase()
  if (!sb) return
  await sb.auth.signOut()
}
