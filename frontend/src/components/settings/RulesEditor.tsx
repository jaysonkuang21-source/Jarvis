import { useCallback, useEffect, useState } from 'react'
import { Check, Loader2, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input, Label, Switch } from '@/components/ui/controls'
import { Badge } from '@/components/ui/badge'
import { api, ApiError } from '@/lib/api/client'
import type { ApiErrorCode } from '@/lib/api/errors'
import type { Policy } from '@/lib/api/types'
import { isDemoMode } from '@/lib/demo'

const CAPABILITIES = [
  {
    key: 'allow_delete',
    label: 'Allow deleting files',
    hint: 'Off by default. When on, deletes move to the trash directory and stay recoverable.',
  },
  {
    key: 'allow_download',
    label: 'Allow downloading files',
    hint: 'Off by default. Downloads land in quarantine and lose any executable suffix.',
  },
  {
    key: 'allow_shell',
    label: 'Allow shell commands',
    hint: 'Off by default, and there is no shell tool registered.',
  },
  {
    key: 'allow_email_send',
    label: 'Allow sending email',
    hint: 'Scheduled mail is held locally and sent by Jarvis; Gmail has no scheduled-send API.',
  },
  {
    key: 'allow_vault_write',
    label: 'Allow writing to the vault',
    hint: 'Writes are limited to the vault and still ask for confirmation each time.',
  },
  {
    key: 'allow_network',
    label: 'Allow network access',
    hint: 'Needed for web search and any cloud model.',
  },
] as const satisfies ReadonlyArray<{ key: keyof Policy; label: string; hint: string }>

/** Short title for a rules load failure (never raw JSON). */
function loadErrorTitle(code: ApiErrorCode | null): string {
  if (code === 'rate_limited') return 'Too many requests'
  if (code === 'backend_unreachable') return 'Backend unreachable'
  if (code === 'unauthorized') return 'Not authorized'
  return 'Could not load rules'
}

/** Edit and save standing policy rules loaded from the backend. */
export function RulesEditor() {
  const [policy, setPolicy] = useState<Policy | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [errorCode, setErrorCode] = useState<ApiErrorCode | null>(null)

  /** Load standing policy; surfaces rate-limit vs unreachable distinctly. */
  const loadRules = useCallback(async () => {
    setLoading(true)
    setError(null)
    setErrorCode(null)
    try {
      setPolicy(await api.getRules())
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : null
      setError(apiErr?.message ?? (err instanceof Error ? err.message : 'Could not load rules'))
      setErrorCode(apiErr?.code ?? 'unknown')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadRules()
  }, [loadRules])

  /** Merge a partial policy update into local draft state. */
  function patch(update: Partial<Policy>) {
    setPolicy((current) => (current ? { ...current, ...update } : current))
    setSaved(false)
  }

  /** Persist the draft policy and mark it saved on success. */
  async function save() {
    if (!policy) return
    setSaving(true)
    setError(null)
    setErrorCode(null)
    try {
      setPolicy(await api.saveRules(policy))
      setSaved(true)
    } catch (err) {
      const apiErr = err as ApiError
      const detail = typeof apiErr.message === 'string' ? apiErr.message : ''
      if (
        apiErr instanceof ApiError &&
        apiErr.status === 403 &&
        detail.includes('confirm_elevation')
      ) {
        const confirmed = window.confirm(
          'This change elevates privileges (capabilities, tools, vault path, or sandbox roots). Confirm save?',
        )
        if (confirmed) {
          try {
            setPolicy(await api.saveRules(policy, true))
            setSaved(true)
            return
          } catch (retryErr) {
            const retryApi = retryErr instanceof ApiError ? retryErr : null
            setError(retryApi?.message ?? (retryErr as Error).message)
            setErrorCode(retryApi?.code ?? 'unknown')
            return
          }
        }
        setError('Save cancelled — elevation was not confirmed.')
        return
      }
      setError(apiErr instanceof ApiError ? apiErr.message : (err as Error).message)
      setErrorCode(apiErr instanceof ApiError ? apiErr.code : 'unknown')
    } finally {
      setSaving(false)
    }
  }

  if (error && !policy) {
    return (
      <div className="space-y-3 py-6" role="alert">
        <div>
          <p className="text-sm font-medium text-destructive">{loadErrorTitle(errorCode)}</p>
          <p className="mt-1 text-sm text-muted-foreground">{error}</p>
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={loading}
          onClick={() => void loadRules()}
        >
          {loading && <Loader2 className="size-3.5 animate-spin" />}
          Try again
        </Button>
      </div>
    )
  }

  if (loading || !policy) {
    return (
      <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        Loading policy...
      </div>
    )
  }

  return (
    <div className="space-y-6 py-5">
      <div className="flex items-start gap-2.5 rounded-lg border border-border bg-muted/40 p-3.5">
        <ShieldCheck className="mt-0.5 size-4 shrink-0 text-success" />
        <p className="text-xs leading-relaxed text-muted-foreground">
          These settings are the frontmatter of <code>config/rules.md</code> and are
          enforced in code, not in the prompt. A model that ignores its instructions
          still cannot get a denied call past the policy layer. The prose in that
          file is only there to keep the model's intent aligned with what it is
          permitted to do.
        </p>
      </div>

      <section>
        <h3 className="mb-2 text-sm font-semibold">Capabilities</h3>
        <div className="divide-y divide-border/60">
          {CAPABILITIES.map(({ key, label, hint }) => (
            <div key={key} className="flex items-start justify-between gap-6 py-3">
              <div className="min-w-0">
                <Label className="text-[13px]">{label}</Label>
                <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                  {hint}
                </p>
              </div>
              <Switch
                checked={policy[key] as boolean}
                onCheckedChange={(checked) => patch({ [key]: checked } as Partial<Policy>)}
              />
            </div>
          ))}
        </div>
      </section>

      <section
        id="rules-vault"
        data-settings-focus="rules-vault"
        tabIndex={-1}
        className="outline-none focus-visible:ring-1 focus-visible:ring-primary/40"
      >
        <h3 className="mb-2 text-sm font-semibold">Vault</h3>
        <Label className="text-[13px]">Vault path</Label>
        <Input
          className="mt-1.5 font-mono text-xs"
          value={policy.vault_path}
          placeholder="D:\Notes\MyVault"
          readOnly={isDemoMode}
          disabled={isDemoMode}
          onChange={(event) => patch({ vault_path: event.target.value })}
        />
        <p className="mt-1.5 text-xs text-muted-foreground">
          {isDemoMode
            ? 'Fixed to demo/vault in public demo. Uploads land in Inbox/; chunks index into Supabase Postgres.'
            : (
              <>
                Read and write paths resolve <code>{'${vault_path}'}</code> against
                this. Indexing reads these files directly, so it works whether or
                not Obsidian is running.
              </>
            )}
        </p>
      </section>

      <section>
        <h3 className="mb-2 text-sm font-semibold">Requires confirmation</h3>
        <div className="flex flex-wrap gap-1.5">
          {(policy.require_approval_for ?? []).map((action) => (
            <Badge key={action} variant="accent">
              {action}
            </Badge>
          ))}
        </div>
        <h3 className="mb-2 mt-4 text-sm font-semibold">Allowed tools</h3>
        <div className="flex flex-wrap gap-1.5">
          {(policy.allowed_tools ?? []).map((tool) => (
            <Badge key={tool} variant="outline">
              {tool}
            </Badge>
          ))}
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          Anything not listed is denied, including tools added in a later release.
          Edit <code>config/rules.md</code> to change these lists.
        </p>
      </section>

      <section>
        <h3 className="mb-2 text-sm font-semibold">Budgets per turn</h3>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <Label className="text-[13px]">Max file writes</Label>
            <Input
              type="number"
              min={0}
              className="mt-1.5"
              value={policy.max_file_writes_per_turn}
              onChange={(event) =>
                patch({ max_file_writes_per_turn: Number(event.target.value) })
              }
            />
          </div>
          <div>
            <Label className="text-[13px]">Max tool calls</Label>
            <Input
              type="number"
              min={1}
              className="mt-1.5"
              value={policy.max_tool_calls_per_turn}
              onChange={(event) =>
                patch({ max_tool_calls_per_turn: Number(event.target.value) })
              }
            />
          </div>
        </div>
      </section>

      {error && <p className="text-xs text-destructive">{error}</p>}

      {!isDemoMode && (
        <div className="flex items-center gap-2">
          <Button onClick={() => void save()} disabled={saving}>
            {saving && <Loader2 className="size-3.5 animate-spin" />}
            Save rules
          </Button>
          {saved && (
            <span className="flex items-center gap-1 text-xs text-success">
              <Check className="size-3.5" />
              Saved and reloaded
            </span>
          )}
        </div>
      )}
      {isDemoMode && (
        <p className="text-xs text-muted-foreground">
          Policy editing is locked in demo mode. Vault path stays <code>demo/vault</code>.
        </p>
      )}
    </div>
  )
}
