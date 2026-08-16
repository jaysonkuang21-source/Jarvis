import {
  FileSearch,
  MessageSquare,
  Settings2,
  Shield,
  Timer,
} from 'lucide-react'
import { useAmbienceStore } from '@/stores/ambience'
import { useAppStore } from '@/stores/app'
import { useChatStore } from '@/stores/chat'
import { useProfileStore } from '@/stores/profile'
import { cn } from '@/lib/utils'

type AgentStatus = 'SCANNING' | 'LISTENING' | 'STABLE' | 'COORDINATING' | 'IDLE'

interface AgentCard {
  id: string
  name: string
  icon: typeof MessageSquare
  status: AgentStatus
  detail: string
  onClick: () => void
}

/** Derive agent presence cards from live app/chat/index/ambience state. */
function useAgents(): AgentCard[] {
  const isStreaming = useChatStore((s) => s.isStreaming)
  const retrieval = useChatStore((s) => s.retrieval)
  const isSpeaking = useAmbienceStore((s) => s.isSpeaking)
  const ambientArmed = useAmbienceStore((s) => s.ambientArmed)
  const connected = useAppStore((s) => s.connected)
  const timers = useAppStore((s) => s.timers)
  const setView = useAppStore((s) => s.setView)
  const setChatOpen = useAppStore((s) => s.setChatOpen)
  const openSettings = useAppStore((s) => s.openSettings)
  const indexStatus = useProfileStore((s) => s.indexStatus)
  const validation = useProfileStore((s) => s.validation)

  const chatStatus: AgentStatus = isStreaming
    ? retrieval?.active
      ? 'SCANNING'
      : 'COORDINATING'
    : isSpeaking || ambientArmed
      ? 'LISTENING'
      : 'STABLE'

  const securityErrors = (validation.issues ?? []).filter((i) => i.level === 'error').length

  return [
    {
      id: 'chat',
      name: 'Chat / Retrieval',
      icon: MessageSquare,
      status: chatStatus,
      detail: isStreaming
        ? retrieval?.label || 'Streaming'
        : 'Ready for ASK JARVIS',
      onClick: () => {
        setView('hub')
        setChatOpen(true)
      },
    },
    {
      id: 'security',
      name: 'Security / Policy',
      icon: Shield,
      status: securityErrors > 0 ? 'SCANNING' : 'STABLE',
      detail:
        securityErrors > 0
          ? `${securityErrors} profile blocker(s)`
          : 'Rules + approvals armed',
      onClick: () => openSettings('rules'),
    },
    {
      id: 'ingest',
      name: 'Ingestion',
      icon: FileSearch,
      status: indexStatus?.indexing ? 'SCANNING' : indexStatus?.ready ? 'STABLE' : 'IDLE',
      detail: indexStatus?.indexing
        ? `Indexing ${indexStatus.indexed_notes}/${indexStatus.total_notes}`
        : indexStatus
          ? `${indexStatus.total_notes} notes · ${indexStatus.stale_notes} stale`
          : 'Index status unavailable',
      onClick: () => openSettings('ingestion', 'ingestion-pipeline'),
    },
    {
      id: 'timers',
      name: 'Timers',
      icon: Timer,
      status: timers.length > 0 ? 'LISTENING' : 'IDLE',
      detail:
        timers.length > 0
          ? `${timers.length} pending`
          : connected
            ? 'Event stream live'
            : 'Stream reconnecting',
      onClick: () => setView('timers'),
    },
    {
      id: 'settings',
      name: 'Settings',
      icon: Settings2,
      status: 'STABLE',
      detail: 'Profile · models · rules',
      onClick: () => openSettings('retrieval'),
    },
  ]
}

/** Color class for an agent status pill. */
function statusClass(status: AgentStatus): string {
  switch (status) {
    case 'SCANNING':
    case 'COORDINATING':
      return 'text-warning'
    case 'LISTENING':
      return 'text-primary'
    case 'STABLE':
      return 'text-success'
    default:
      return 'text-muted-foreground'
  }
}

/** Top-right agent presence grid mapped to real Jarvis surfaces. */
export function AgentPresence() {
  const agents = useAgents()

  return (
    <section className="panel-ops flex flex-col gap-2 p-3">
      <header className="flex items-center justify-between">
        <h2 className="font-display text-[10px] text-muted-foreground">Agent Presence</h2>
        <span className="font-mono text-[9px] text-primary/70">{agents.length} NODES</span>
      </header>
      <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
        {agents.map((agent) => {
          const Icon = agent.icon
          return (
            <button
              key={agent.id}
              type="button"
              onClick={agent.onClick}
              className="flex cursor-pointer items-start gap-2 rounded border border-border/60 bg-background/40 px-2 py-2 text-left transition-colors hover:border-primary/40 hover:bg-muted/40"
            >
              <Icon className="mt-0.5 size-3.5 shrink-0 text-primary/80" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-1">
                  <span className="truncate text-[11px] font-medium">{agent.name}</span>
                  <span className={cn('font-mono text-[8px] tracking-wider', statusClass(agent.status))}>
                    {agent.status}
                  </span>
                </div>
                <p className="mt-0.5 truncate text-[10px] text-muted-foreground">{agent.detail}</p>
              </div>
            </button>
          )
        })}
      </div>
    </section>
  )
}
