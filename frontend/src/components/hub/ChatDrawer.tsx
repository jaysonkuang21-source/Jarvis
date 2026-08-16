import { useEffect } from 'react'
import { X } from 'lucide-react'
import { ChatView } from '@/components/chat/ChatView'
import { useAppStore } from '@/stores/app'
import { cn } from '@/lib/utils'

/**
 * Right-side drawer hosting the full ChatView when ASK JARVIS is pressed.
 * Preserves SSE chat, approvals, citations wiring via the existing store.
 */
export function ChatDrawer() {
  const open = useAppStore((s) => s.chatOpen)
  const setChatOpen = useAppStore((s) => s.setChatOpen)

  /** Close the Ask Jarvis drawer. */
  function close() {
    setChatOpen(false)
  }

  useEffect(() => {
    if (!open) return
    /** Escape dismisses the drawer when it is focused / open. */
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault()
        setChatOpen(false)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open, setChatOpen])

  return (
    <>
      <div
        className={cn(
          'absolute inset-0 z-40 bg-black/50 transition-opacity',
          open ? 'opacity-100' : 'pointer-events-none opacity-0',
        )}
        onClick={close}
        aria-hidden={!open}
      />
      <aside
        className={cn(
          'absolute inset-y-0 right-0 z-50 flex w-full max-w-xl flex-col border-l border-border bg-background shadow-2xl transition-transform duration-300',
          open ? 'translate-x-0' : 'pointer-events-none translate-x-full',
        )}
        aria-hidden={!open}
        // Keep slide-out panel in DOM for transform, but inert so closed state
        // cannot steal Tab focus or activate the X control.
        inert={!open ? true : undefined}
        aria-label="Ask Jarvis chat"
        role="dialog"
      >
        <div className="relative z-10 flex shrink-0 items-center justify-between border-b border-border px-3 py-2">
          <p className="font-display text-[11px] tracking-widest text-critical">Ask Jarvis</p>
          <button
            type="button"
            onClick={(event) => {
              event.preventDefault()
              event.stopPropagation()
              close()
            }}
            className="relative z-10 flex size-8 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-muted hover:text-foreground"
            aria-label="Close chat"
            tabIndex={open ? 0 : -1}
          >
            <X className="size-4 pointer-events-none" />
          </button>
        </div>
        <div className="min-h-0 flex-1">{open ? <ChatView /> : null}</div>
      </aside>
    </>
  )
}
