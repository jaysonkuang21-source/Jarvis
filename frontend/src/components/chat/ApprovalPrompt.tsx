import { useEffect, useRef } from 'react'
import { ShieldAlert } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import type { PendingApproval } from '@/stores/chat'

/** Why the dialog is closing — Allow-once must not POST deny. */
export type ApprovalClosingReason = 'approve' | 'deny'

interface Props {
  approval: PendingApproval | null
  onApprove: (approval: PendingApproval) => void
  /** Escape, overlay click, or Deny — parent should POST deny once. */
  onDeny: () => void
}

/**
 * Approving grants exactly one call. It does not edit config/rules.md, so the
 * standing policy is never widened by clicking through a prompt.
 *
 * Escape/overlay/Deny → deny. Allow once closes without calling deny.
 */
export function ApprovalPrompt({ approval, onApprove, onDeny }: Props) {
  const closingReason = useRef<ApprovalClosingReason | null>(null)
  const denyPosted = useRef(false)

  useEffect(() => {
    if (approval !== null) {
      closingReason.current = null
      denyPosted.current = false
    }
  }, [approval?.id])

  /** POST deny at most once for Escape, overlay, or Deny button. */
  function denyOnce() {
    if (denyPosted.current) return
    denyPosted.current = true
    onDeny()
  }

  /** Allow once: mark approve so onOpenChange will not treat close as deny. */
  function handleApprove() {
    if (!approval) return
    closingReason.current = 'approve'
    onApprove(approval)
  }

  /** Explicit Deny button. */
  function handleDenyClick() {
    closingReason.current = 'deny'
    denyOnce()
  }

  return (
    <Dialog
      open={approval !== null}
      onOpenChange={(open) => {
        if (open) return
        // Controlled close after Allow once — must not POST deny.
        if (closingReason.current === 'approve') return
        closingReason.current = 'deny'
        denyOnce()
      }}
    >
      <DialogContent showClose={false}>
        <DialogHeader>
          <div className="mb-1 flex size-9 items-center justify-center rounded-lg bg-warning/15">
            <ShieldAlert className="size-4.5 text-warning" />
          </div>
          <DialogTitle>Jarvis is asking permission</DialogTitle>
          <DialogDescription>{approval?.reason}</DialogDescription>
        </DialogHeader>

        <dl className="space-y-2 rounded-lg border border-border bg-muted/40 p-3 text-xs">
          <div className="flex gap-2">
            <dt className="w-16 shrink-0 text-muted-foreground">Tool</dt>
            <dd className="font-mono break-all">{approval?.tool}</dd>
          </div>
          {Object.entries(approval?.details ?? {})
            .filter(([, value]) => value !== null && value !== undefined)
            .map(([key, value]) => (
              <div key={key} className="flex gap-2">
                <dt className="w-16 shrink-0 text-muted-foreground">{key}</dt>
                <dd className="font-mono break-all">{String(value)}</dd>
              </div>
            ))}
        </dl>

        <p className="mt-3 text-xs text-muted-foreground">
          Allowing applies to this one action only. Change the standing rules in
          Settings if you want it permanently.
        </p>

        <DialogFooter>
          <Button variant="outline" onClick={handleDenyClick}>
            Deny
          </Button>
          <Button onClick={handleApprove}>Allow once</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
