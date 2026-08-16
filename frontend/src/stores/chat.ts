import { create } from 'zustand'
import { streamChat } from '@/lib/api/client'
import { getSpeakReplies } from '@/lib/speech/prefs'
import { beginSpeakStream, stopSpeaking, type SpeakStream } from '@/lib/speech/tts'
import type { ChatMessage, Citation, Profile, StreamEvent } from '@/lib/api/types'

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  error?: { message: string; code: string }
  elapsedMs?: number
  streaming: boolean
  cancelled?: boolean
  createdAt: string
}

export interface RetrievalState {
  active: boolean
  label: string
  current: number
  total: number
  estimatedSeconds: number | null
  startedAt: number
}

export interface PendingApproval {
  id: string
  tool: string
  reason: string
  details: Record<string, unknown>
}

interface ChatState {
  messages: Message[]
  retrieval: RetrievalState | null
  pendingApproval: PendingApproval | null
  selectedCitation: Citation | null
  isStreaming: boolean

  send: (text: string, profile: Profile, approvalId?: string) => Promise<void>
  cancel: () => void
  clear: () => void
  selectCitation: (citation: Citation | null) => void
  dismissApproval: () => void
  /** Trim failed assistant and re-stream without duplicating the user bubble. */
  resendLast: (profile: Profile, approvalId?: string) => Promise<void>
  /** Alias for resendLast (retry UI). */
  retryLast: (profile: Profile, approvalId?: string) => Promise<void>
}

const IDLE_RETRIEVAL: RetrievalState = {
  active: true,
  label: 'Preparing',
  current: 0,
  total: 0,
  estimatedSeconds: null,
  startedAt: 0,
}

let controller: AbortController | null = null

/** Allocate a unique id for a chat message. */
function newId() {
  return crypto.randomUUID()
}

/** Index of the most recent user message, or -1 if none. */
function lastUserIndex(messages: Message[]): number {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'user') return i
  }
  return -1
}

/**
 * Run the chat SSE against existing transcript state: append only an assistant
 * placeholder, send history that matches the truncated messages preceding it.
 */
async function streamAssistantReply(opts: {
  text: string
  history: ChatMessage[]
  profile: Profile
  approvalId?: string
  set: (partial: Partial<ChatState> | ((state: ChatState) => Partial<ChatState>)) => void
  getAssistantId: () => string
}): Promise<void> {
  const assistantId = opts.getAssistantId()
  let speakStream: SpeakStream | null = null

  /** Patch the in-flight assistant message by id. */
  const patch = (fn: (message: Message) => Message) =>
    opts.set((state) => ({
      messages: state.messages.map((m) => (m.id === assistantId ? fn(m) : m)),
    }))

  const ac = new AbortController()
  controller = ac

  try {
    await streamChat({
      message: opts.text,
      history: opts.history,
      profile: opts.profile,
      approvalId: opts.approvalId,
      signal: ac.signal,
      onEvent: (event: StreamEvent) => {
        // Ignore late events from an aborted or superseded turn.
        if (controller !== ac) return
        switch (event.type) {
          case 'retrieval_start':
            opts.set({
              retrieval: {
                active: true,
                label: event.label,
                current: 0,
                total: event.estimated_calls,
                estimatedSeconds: event.estimated_seconds,
                startedAt: Date.now(),
              },
            })
            break

          case 'retrieval_progress':
            opts.set((state) => ({
              retrieval: {
                ...(state.retrieval ?? IDLE_RETRIEVAL),
                active: true,
                current: event.current,
                total: event.total,
                label: event.label || (state.retrieval?.label ?? ''),
              },
            }))
            break

          case 'citations':
            opts.set({ retrieval: null })
            patch((m) => ({ ...m, citations: event.citations }))
            break

          case 'token':
            opts.set({ retrieval: null })
            patch((m) => ({ ...m, content: m.content + event.text }))
            if (getSpeakReplies()) {
              if (!speakStream) speakStream = beginSpeakStream()
              speakStream.push(event.text)
            }
            break

          case 'approval_required':
            opts.set({
              pendingApproval: {
                id: event.id,
                tool: event.tool,
                reason: event.reason,
                details: event.details,
              },
            })
            break

          case 'error':
            stopSpeaking()
            speakStream = null
            patch((m) => ({
              ...m,
              error: { message: event.message, code: event.code },
            }))
            break

          case 'done':
            opts.set({ retrieval: null })
            patch((m) => ({
              ...m,
              streaming: false,
              elapsedMs: event.elapsed_ms,
              cancelled: event.cancelled,
            }))
            if (event.cancelled) {
              stopSpeaking()
              speakStream = null
            } else {
              speakStream?.end()
              speakStream = null
            }
            break

          default:
            break
        }
      },
    })
  } finally {
    // Only the active turn may clear streaming / controller (cancel→resend race).
    if (controller === ac) {
      controller = null
      opts.set((state) => ({
        isStreaming: false,
        retrieval: null,
        messages: state.messages.map((m) =>
          m.id === assistantId ? { ...m, streaming: false } : m,
        ),
      }))
    }
  }
}

export const useChatStore = create<ChatState>()((set, get) => ({
  messages: [],
  retrieval: null,
  pendingApproval: null,
  selectedCitation: null,
  isStreaming: false,

  /** Clear the conversation and any selected citation. */
  clear: () => {
    stopSpeaking()
    set({ messages: [], retrieval: null, selectedCitation: null })
  },

  /** Open or close the citations side panel for a source. */
  selectCitation: (citation) => set({ selectedCitation: citation }),

  /** Dismiss the pending tool-approval dialog without approving. */
  dismissApproval: () => set({ pendingApproval: null }),

  /** Abort the in-flight chat stream and mark the assistant turn cancelled. */
  cancel: () => {
    controller?.abort()
    controller = null
    stopSpeaking()
    set((state) => ({
      isStreaming: false,
      retrieval: null,
      messages: state.messages.map((message) =>
        message.streaming
          ? { ...message, streaming: false, cancelled: true }
          : message,
      ),
    }))
  },

  /**
   * Re-run the last user turn after trimming any failed assistant reply.
   * Does not append a duplicate user bubble; API history matches the transcript.
   */
  resendLast: async (profile, approvalId) => {
    if (get().isStreaming) return
    const messages = get().messages
    const userIdx = lastUserIndex(messages)
    if (userIdx < 0) return

    stopSpeaking()

    const lastUser = messages[userIdx]
    // Keep through the last user message; drop failed/cancelled assistant after it.
    const truncated = messages.slice(0, userIdx + 1)
    // History is prior turns only — exclude the user message being resent.
    const history = truncated.slice(0, -1).map((message) => ({
      role: message.role as ChatMessage['role'],
      content: message.content,
    }))

    const assistantId = newId()
    const assistantMessage: Message = {
      id: assistantId,
      role: 'assistant',
      content: '',
      citations: [],
      streaming: true,
      createdAt: new Date().toISOString(),
    }

    set({
      messages: [...truncated, assistantMessage],
      isStreaming: true,
      retrieval: { ...IDLE_RETRIEVAL, startedAt: Date.now() },
      pendingApproval: null,
    })

    await streamAssistantReply({
      text: lastUser.content,
      history,
      profile,
      approvalId,
      set,
      getAssistantId: () => assistantId,
    })
  },

  /** UI retry / approval-retry entry point; delegates to resendLast. */
  retryLast: async (profile, approvalId) => {
    await get().resendLast(profile, approvalId)
  },

  /** Send a user message and stream the assistant response into store state. */
  send: async (text, profile, approvalId) => {
    const trimmed = text.trim()
    if (!trimmed || get().isStreaming) return

    // New turn supersedes any prior spoken reply still playing.
    stopSpeaking()

    const history: ChatMessage[] = get().messages.map((message) => ({
      role: message.role,
      content: message.content,
    }))

    const userMessage: Message = {
      id: newId(),
      role: 'user',
      content: trimmed,
      citations: [],
      streaming: false,
      createdAt: new Date().toISOString(),
    }
    const assistantId = newId()
    const assistantMessage: Message = {
      id: assistantId,
      role: 'assistant',
      content: '',
      citations: [],
      streaming: true,
      createdAt: new Date().toISOString(),
    }

    set((state) => ({
      messages: [...state.messages, userMessage, assistantMessage],
      isStreaming: true,
      retrieval: { ...IDLE_RETRIEVAL, startedAt: Date.now() },
      pendingApproval: null,
    }))

    await streamAssistantReply({
      text: trimmed,
      history,
      profile,
      approvalId,
      set,
      getAssistantId: () => assistantId,
    })
  },
}))
