import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { DEMO_BANNER, DEMO_KEY_SAFETY } from '@/lib/demo'
import {
  clearSessionLlmCredentials,
  getSessionLlmBaseUrl,
  hasSessionLlmKey,
  setSessionLlmBaseUrl,
  setSessionLlmKey,
} from '@/lib/sessionLlm'
import {
  signInWithPassword,
  signOut,
  signUpWithPassword,
} from '@/lib/supabase'

interface DemoAuthGateProps {
  /** Signed-in email to show in the signed-in chrome, if any. */
  email: string | null
  /** True while waiting for the first session probe. */
  loading: boolean
  /** True when a Supabase session exists. */
  signedIn: boolean
  children: ReactNode
}

/**
 * Gate chat behind Supabase email auth and show the locked-model demo banner.
 *
 * After sign-in, collects a session-only OpenAI-compatible API key (BYOK)
 * that is wiped on sign-out.
 */
export function DemoAuthGate({
  email,
  loading,
  signedIn,
  children,
}: DemoAuthGateProps) {
  const [mode, setMode] = useState<'signin' | 'signup'>('signin')
  const [mail, setMail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [llmKeyDraft, setLlmKeyDraft] = useState('')
  const [llmBaseDraft, setLlmBaseDraft] = useState('')
  const [keyReady, setKeyReady] = useState(() => hasSessionLlmKey())

  useEffect(() => {
    if (!signedIn) {
      clearSessionLlmCredentials()
      setKeyReady(false)
      setLlmKeyDraft('')
      setLlmBaseDraft('')
      return
    }
    setKeyReady(hasSessionLlmKey())
    setLlmBaseDraft(getSessionLlmBaseUrl() ?? '')
  }, [signedIn])

  /** Submit the active sign-in or sign-up form. */
  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    const result =
      mode === 'signin'
        ? await signInWithPassword(mail.trim(), password)
        : await signUpWithPassword(mail.trim(), password)
    setBusy(false)
    if (result) setError(result)
  }

  /** Persist the draft key into memory and unlock chat. */
  function onSaveKey(event: FormEvent) {
    event.preventDefault()
    const trimmed = llmKeyDraft.trim()
    if (!trimmed) {
      setError('Paste an OpenAI-compatible API key to continue.')
      return
    }
    setError(null)
    setSessionLlmKey(trimmed)
    setSessionLlmBaseUrl(llmBaseDraft.trim() || null)
    setLlmKeyDraft('')
    setKeyReady(true)
  }

  /** Clear the session key without signing out. */
  function onClearKey() {
    clearSessionLlmCredentials()
    setLlmKeyDraft('')
    setLlmBaseDraft('')
    setKeyReady(false)
  }

  /** Sign out and wipe session LLM credentials. */
  async function onSignOut() {
    clearSessionLlmCredentials()
    setKeyReady(false)
    await signOut()
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        className="shrink-0 border-b border-critical/40 bg-critical/10 px-4 py-2 text-center text-xs font-medium text-foreground"
        role="status"
      >
        {DEMO_BANNER}
      </div>

      {loading ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          Checking session…
        </div>
      ) : !signedIn ? (
        <div className="flex flex-1 items-center justify-center p-6">
          <form
            onSubmit={onSubmit}
            className="flex w-full max-w-sm flex-col gap-3"
          >
            <h1 className="font-display text-xl font-semibold tracking-tight">
              Jarvis demo
            </h1>
            <p className="text-sm text-muted-foreground">
              Sign in with email to try the sample knowledge base. One model
              only (GPT-4o mini). You will paste your own API key next — it
              never stays after sign-out. Rate limited.
            </p>
            <label className="flex flex-col gap-1 text-xs font-medium">
              Email
              <input
                type="email"
                required
                autoComplete="email"
                value={mail}
                onChange={(e) => setMail(e.target.value)}
                className="rounded border border-border bg-background px-3 py-2 text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs font-medium">
              Password
              <input
                type="password"
                required
                minLength={8}
                autoComplete={
                  mode === 'signin' ? 'current-password' : 'new-password'
                }
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="rounded border border-border bg-background px-3 py-2 text-sm"
              />
            </label>
            {error && (
              <p className="text-xs text-critical" role="alert">
                {error}
              </p>
            )}
            <Button type="submit" disabled={busy}>
              {busy
                ? 'Please wait…'
                : mode === 'signin'
                  ? 'Sign in'
                  : 'Create account'}
            </Button>
            <button
              type="button"
              className="text-xs text-muted-foreground underline"
              onClick={() =>
                setMode((m) => (m === 'signin' ? 'signup' : 'signin'))
              }
            >
              {mode === 'signin'
                ? 'Need an account? Sign up'
                : 'Already have an account? Sign in'}
            </button>
          </form>
        </div>
      ) : (
        <>
          <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border/60 px-4 py-1.5 text-xs text-muted-foreground">
            <span className="truncate">{email ?? 'Signed in'}</span>
            <div className="flex items-center gap-1">
              {keyReady && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={onClearKey}
                >
                  Clear API key
                </Button>
              )}
              <Button type="button" variant="ghost" size="sm" onClick={() => void onSignOut()}>
                Sign out
              </Button>
            </div>
          </div>
          {!keyReady ? (
            <div className="flex flex-1 items-center justify-center p-6">
              <form
                onSubmit={onSaveKey}
                className="flex w-full max-w-md flex-col gap-3"
              >
                <h2 className="font-display text-lg font-semibold tracking-tight">
                  Session API key
                </h2>
                <p className="text-sm text-muted-foreground">{DEMO_KEY_SAFETY}</p>
                <label className="flex flex-col gap-1 text-xs font-medium">
                  OpenAI-compatible API key
                  <input
                    type="password"
                    required
                    autoComplete="off"
                    spellCheck={false}
                    value={llmKeyDraft}
                    onChange={(e) => setLlmKeyDraft(e.target.value)}
                    placeholder="sk-…"
                    className="rounded border border-border bg-background px-3 py-2 font-mono text-sm"
                  />
                </label>
                <label className="flex flex-col gap-1 text-xs font-medium">
                  Base URL (optional)
                  <input
                    type="url"
                    autoComplete="off"
                    spellCheck={false}
                    value={llmBaseDraft}
                    onChange={(e) => setLlmBaseDraft(e.target.value)}
                    placeholder="https://api.openai.com/v1"
                    className="rounded border border-border bg-background px-3 py-2 font-mono text-sm"
                  />
                </label>
                {error && (
                  <p className="text-xs text-critical" role="alert">
                    {error}
                  </p>
                )}
                <Button type="submit">Use key for this session</Button>
              </form>
            </div>
          ) : (
            <div className="min-h-0 flex-1">{children}</div>
          )}
        </>
      )}
    </div>
  )
}
