import type { ReactNode } from 'react'
import { AlertTriangle, Info } from 'lucide-react'
import { Label } from '@/components/ui/controls'
import { Hint } from '@/components/ui/tooltip'
import { useProfileStore } from '@/stores/profile'

interface Props {
  name: string
  label: string
  hint?: string
  children: ReactNode
}

/**
 * Renders the issues that `validate_profile` reported for one field, so every
 * warning the backend produces shows up without being wired case by case.
 *
 * Select `validation.issues` (stable store reference), then filter in render.
 * Calling `issueFor()` inside the selector returns a fresh array every time and
 * infinite-re-renders under Zustand’s Object.is equality — blanking Settings.
 */
export function Field({ name, label, hint, children }: Props) {
  const allIssues = useProfileStore((state) => state.validation.issues)
  const issues = Array.isArray(allIssues)
    ? allIssues.filter((issue) => issue.field === name)
    : []

  return (
    <div className="grid grid-cols-[minmax(0,180px)_1fr] items-start gap-4 py-3">
      <div className="flex items-center gap-1.5 pt-1.5">
        <Label className="text-[13px]">{label}</Label>
        {hint && <Hint>{hint}</Hint>}
      </div>

      <div className="min-w-0 space-y-1.5">
        {children}
        {issues.map((issue, index) => (
          <p
            key={index}
            className={
              issue.level === 'error'
                ? 'flex items-start gap-1.5 text-xs leading-relaxed text-destructive'
                : 'flex items-start gap-1.5 text-xs leading-relaxed text-muted-foreground'
            }
          >
            {issue.level === 'error' ? (
              <AlertTriangle className="mt-0.5 size-3 shrink-0" />
            ) : (
              <Info className="mt-0.5 size-3 shrink-0" />
            )}
            {issue.message}
          </p>
        ))}
      </div>
    </div>
  )
}

/** Titled settings block with an optional description and divided children. */
export function SettingsSection({
  id,
  title,
  description,
  children,
}: {
  id?: string
  title: string
  description?: string
  children: ReactNode
}) {
  return (
    <section
      id={id}
      data-settings-focus={id}
      tabIndex={id ? -1 : undefined}
      className="border-b border-border py-5 last:border-0 outline-none focus-visible:ring-1 focus-visible:ring-primary/40"
    >
      <h3 className="text-sm font-semibold">{title}</h3>
      {description && (
        <p className="mt-0.5 max-w-prose text-xs leading-relaxed text-muted-foreground">
          {description}
        </p>
      )}
      <div className="mt-1 divide-y divide-border/60">{children}</div>
    </section>
  )
}
