import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { claimDemoSeat } from '@/lib/api/client'
import { DEMO_BANNER, DEMO_KEY_SAFETY } from '@/lib/demo'
import {
  clearSessionLlmCredentials,
  getSessionLlmBaseUrl,
  hasSessionLlmKey,
  setSessionLlmBaseUrl,
  setSessionLlmKey,
} from '@/lib/sessionLlm'

interface DemoAuthGateProps {
  /** Fired once a demo seat is leased so the shell can load profile/SSE. */
  onSeatReady?: () => void
  children: ReactNode
}

/**
 * Demo shell: banner, optional seat-limit error, BYOK key form, then chat.
 *
 * No Supabase login — anonymous browsers claim a per-IP seat (max 4).
 */
export function DemoAuthGate({ onSeatReady, children }: DemoAuthGateProps) {
  const [error, setError] = useState<string | null>(null)
  const [seatReady, setSeatReady] = useState(false)
  const [seatBusy, setSeatBusy] = useState(true)
  const [llmKeyDraft, setLlmKeyDraft] = useState('')
  const [llmBaseDraft, setLlmBaseDraft] = useState(() => getSessionLlmBaseUrl() ?? '')
  const [keyReady, setKeyReady] = useState(() => hasSessionLlmKey())

  useEffect(() => {
    let cancelled = false
    void (async () => {
      setSeatBusy(true)
      setError(null)
      try {
        await claimDemoSeat()
        if (!cancelled) {
          setSeatReady(true)
          onSeatReady?.()
        }
      } catch (err) {
        if (!cancelled) {
          setSeatReady(false)
          setError(
            err instanceof Error
              ? err.message
              : 'Demo is full for this network (4 users max). Try again later.',
          )
        }
      } finally {
        if (!cancelled) setSeatBusy(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [onSeatReady])

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

  /** Clear the session key without leaving the demo. */
  function onClearKey() {
    clearSessionLlmCredentials()
    setLlmKeyDraft('')
    setLlmBaseDraft('')
    setKeyReady(false)
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        className="shrink-0 border-b border-critical/40 bg-critical/10 px-4 py-2 text-center text-xs font-medium text-foreground"
        role="status"
      >
        {DEMO_BANNER}
      </div>

      {seatBusy ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          Reserving a demo seat…
        </div>
      ) : !seatReady ? (
        <div className="flex flex-1 items-center justify-center p-6">
          <div className="flex w-full max-w-sm flex-col gap-3 text-center">
            <h1 className="font-display text-xl font-semibold tracking-tight">
              {error?.toLowerCase().includes('network') ||
              error?.toLowerCase().includes('reach')
                ? 'Cannot reach demo API'
                : 'Demo seats full'}
            </h1>
            <p className="text-sm text-muted-foreground">
              {error ??
                'This network already has 4 active demo users. Wait for a seat to free up, then reload.'}
            </p>
            <Button type="button" onClick={() => window.location.reload()}>
              Retry
            </Button>
          </div>
        </div>
      ) : (
        <>
          <div className="flex shrink-0 items-center justify-end gap-2 border-b border-border/60 px-4 py-1.5 text-xs text-muted-foreground">
            {keyReady && (
              <Button type="button" variant="ghost" size="sm" onClick={onClearKey}>
                Clear API key
              </Button>
            )}
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
