import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useProfileStore } from '@/stores/profile'
import { cn } from '@/lib/utils'

export interface Choice {
  value: string
  label: string
  hint?: string
}

interface Props {
  field: string
  value: string
  choices: Choice[]
  onChange: (value: string) => void
}

/**
 * Segmented choice where unavailable options are disabled with the backend's
 * own reason attached, rather than silently missing.
 */
export function ChoiceGroup({ field, value, choices, onChange }: Props) {
  const disabledReason = useProfileStore((state) => state.disabledReason)

  if (choices.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        No options available for {field}. Refresh models or restart the backend.
      </p>
    )
  }

  return (
    <div className="flex flex-wrap gap-1.5" role="group" aria-label={field}>
      {choices.map((choice) => {
        const reason = disabledReason(field, choice.value)
        const isDisabled = Boolean(reason) && choice.value !== value
        const selected = choice.value === value

        const button = (
          <button
            key={choice.value}
            type="button"
            disabled={isDisabled}
            onClick={() => onChange(choice.value)}
            className={cn(
              'flex min-w-[128px] flex-1 flex-col items-start gap-0.5 rounded-lg border px-3 py-2 text-left transition-colors',
              selected
                ? 'border-primary bg-accent text-accent-foreground'
                : 'border-border hover:bg-muted',
              isDisabled && 'cursor-not-allowed opacity-40 hover:bg-transparent',
            )}
          >
            <span className="text-[13px] font-medium">{choice.label}</span>
            {choice.hint && (
              <span className="text-[11px] leading-snug text-muted-foreground">
                {choice.hint}
              </span>
            )}
          </button>
        )

        if (!reason) return button

        return (
          <Tooltip key={choice.value}>
            <TooltipTrigger asChild>
              <span className="flex min-w-[128px] flex-1">{button}</span>
            </TooltipTrigger>
            <TooltipContent>{reason}</TooltipContent>
          </Tooltip>
        )
      })}
    </div>
  )
}
