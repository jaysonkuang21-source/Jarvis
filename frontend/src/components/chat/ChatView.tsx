import { useEffect, useRef } from 'react'
import { Compass, Eraser, Globe, Orbit, Sparkles, Wand2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { api } from '@/lib/api/client'
import { useAppStore } from '@/stores/app'
import { useChatStore } from '@/stores/chat'
import { useProfileStore } from '@/stores/profile'
import { cn } from '@/lib/utils'
import type { QueryMode } from '@/lib/api/types'
import { ApprovalPrompt } from './ApprovalPrompt'
import { Composer } from './Composer'
import { MessageBubble } from './MessageBubble'
import { RetrievalProgress } from './RetrievalProgress'

const STARTERS = [
  'What did I write about this week?',
  'Summarise the themes across my project notes.',
  'Which notes mention retrieval-augmented generation?',
]

const QUERY_MODES = [
  { value: 'local', label: 'Local', icon: Compass },
  { value: 'global', label: 'Global', icon: Globe },
  { value: 'drift', label: 'DRIFT', icon: Orbit },
  { value: 'auto', label: 'Auto', icon: Wand2 },
] as const

/** Main chat surface: model picker, message list, composer, and approvals. */
export function ChatView() {
  const messages = useChatStore((s) => s.messages)
  const retrieval = useChatStore((s) => s.retrieval)
  const isStreaming = useChatStore((s) => s.isStreaming)
  const pendingApproval = useChatStore((s) => s.pendingApproval)
  const send = useChatStore((s) => s.send)
  const cancel = useChatStore((s) => s.cancel)
  const clear = useChatStore((s) => s.clear)
  const selectCitation = useChatStore((s) => s.selectCitation)
  const dismissApproval = useChatStore((s) => s.dismissApproval)
  const resendLast = useChatStore((s) => s.resendLast)
  const retryLast = useChatStore((s) => s.retryLast)
  const openSettings = useAppStore((s) => s.openSettings)
  const pushToast = useAppStore((s) => s.pushToast)
  const { profile, options, validation, update, startReindex, indexStatus } = useProfileStore()

  const bottomRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)

  // Follow the stream, but stop fighting the user if they scroll up to read.
  useEffect(() => {
    if (pinned.current) bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [messages, retrieval])

  /** Track whether the user is near the bottom so auto-scroll can pause. */
  function onScroll() {
    const el = scrollRef.current
    if (!el) return
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
  }

  const blockingIssues = validation.issues.filter((issue) => issue.level === 'error')
  const chatModels = options?.chat_models ?? []

  return (
    <div className="flex h-full flex-col">
      <header className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-2.5">
        <Select
          value={profile.chat_model}
          onValueChange={(value) => {
            const model = chatModels.find((m) => m.id === value)
            update({ chat_model: value, chat_provider: model?.provider ?? 'ollama' })
          }}
        >
          <SelectTrigger className="h-8 w-[210px] text-xs">
            <SelectValue placeholder="Select a model" />
          </SelectTrigger>
          <SelectContent>
            {chatModels.length === 0 && (
              <div className="px-2 py-3 text-xs text-muted-foreground">
                No models found. Start Ollama or add an OpenAI key.
              </div>
            )}
            {chatModels.map((model) => (
              <SelectItem key={model.id} value={model.id} disabled={!model.available}>
                <span className="flex flex-col gap-0.5">
                  <span>{model.label}</span>
                  <span className="text-[10px] text-muted-foreground">
                    {model.provider}
                    {model.supports_vision && ' | vision'}
                    {!model.available && ` | ${model.unavailable_reason}`}
                  </span>
                </span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <div className="flex items-center rounded-md border border-border p-0.5">
          {QUERY_MODES.map(({ value, label, icon: Icon }) => (
            <button
              key={value}
              onClick={() => update({ query_mode: value as QueryMode })}
              className={cn(
                'inline-flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-medium transition-colors',
                profile.query_mode === value
                  ? 'bg-accent text-accent-foreground'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              <Icon className="size-3" />
              {label}
            </button>
          ))}
        </div>

        <div className="flex-1" />

        {messages.length > 0 && (
          <Button variant="ghost" size="sm" onClick={clear} title="Clear conversation">
            <Eraser className="size-3.5" />
            Clear
          </Button>
        )}
      </header>

      {blockingIssues.length > 0 && (
        <div className="shrink-0 border-b border-destructive/25 bg-destructive/8 px-4 py-2">
          {blockingIssues.map((issue) => (
            <p key={issue.field} className="text-xs text-destructive">
              <span className="font-medium">{issue.field}:</span> {issue.message}
            </p>
          ))}
        </div>
      )}

      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="scrollbar-thin flex-1 overflow-y-auto"
      >
        {messages.length === 0 ? (
          <EmptyState onPick={(text) => void send(text, profile)} />
        ) : (
          <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-6">
            {messages.map((message) => (
              <MessageBubble
                key={message.id}
                message={message}
                onCitationClick={selectCitation}
                onRetry={() => void retryLast(profile)}
                indexing={Boolean(indexStatus?.indexing)}
                onReindex={() => {
                  openSettings('ingestion', 'ingestion-index')
                  pushToast('INDEX', 'Starting vault reindex…')
                  void startReindex(Boolean(indexStatus?.indexing_stale))
                }}
              />
            ))}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      {retrieval && (
        <div className="shrink-0 pb-3">
          <RetrievalProgress retrieval={retrieval} onCancel={cancel} />
        </div>
      )}

      <div className="shrink-0">
        <Composer
          streaming={isStreaming}
          disabled={blockingIssues.length > 0}
          placeholder={
            blockingIssues.length > 0
              ? 'Fix the settings above to continue'
              : profile.query_mode === 'global'
                ? 'Ask something broad about the whole vault...'
                : profile.query_mode === 'drift'
                  ? 'Ask a synthesis question, then drill down...'
                  : profile.query_mode === 'auto'
                    ? 'Ask anything — Auto will choose Local, Global, or DRIFT...'
                    : 'Ask about your vault...'
          }
          onSend={(text) => void send(text, profile)}
          onCancel={cancel}
        />
      </div>

      <ApprovalPrompt
        approval={pendingApproval}
        onDeny={() => {
          const pending = useChatStore.getState().pendingApproval
          dismissApproval()
          if (pending) {
            void api.approve({
              request_id: pending.id,
              tool: pending.tool,
              approved: false,
            })
          }
        }}
        onApprove={(approval) => {
          // Allow once: grant only — never POST deny when the dialog closes.
          void api
            .approve({ request_id: approval.id, tool: approval.tool, approved: true })
            .then(() => {
              dismissApproval()
              return resendLast(profile, approval.id)
            })
        }}
      />
    </div>
  )
}

/** Empty conversation placeholder with starter prompts. */
function EmptyState({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="mx-auto flex h-full max-w-md flex-col items-center justify-center gap-5 px-4 text-center">
      <div className="flex size-11 items-center justify-center rounded-xl bg-accent">
        <Sparkles className="size-5 text-accent-foreground" />
      </div>
      <div>
        <h2 className="text-base font-semibold">Ask your vault</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Local finds specific notes via hybrid vector+keyword search.
          Global synthesises themes. Turn on Agentic under Settings → Retrieval
          to grade results and rewrite the query before answering.
          DRIFT starts broad then drills in. Auto picks a mode for you.
        </p>
      </div>
      <div className="flex w-full flex-col gap-1.5">
        {STARTERS.map((starter) => (
          <button
            key={starter}
            onClick={() => onPick(starter)}
            className="rounded-lg border border-border px-3 py-2 text-left text-sm transition-colors hover:border-primary/40 hover:bg-muted"
          >
            {starter}
          </button>
        ))}
      </div>
    </div>
  )
}
