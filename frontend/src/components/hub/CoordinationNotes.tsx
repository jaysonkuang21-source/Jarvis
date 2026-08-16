import { useProfileStore } from '@/stores/profile'
import { useAppStore } from '@/stores/app'
import { useAmbienceStore } from '@/stores/ambience'

/** Short coordination notes bridging modules (live + placeholder mix). */
export function CoordinationNotes() {
  const indexStatus = useProfileStore((s) => s.indexStatus)
  const profile = useProfileStore((s) => s.profile)
  const timers = useAppStore((s) => s.timers)
  const isSpeaking = useAmbienceStore((s) => s.isSpeaking)

  const notes = [
    indexStatus?.ready
      ? `Retrieval → Chat — index ready (${indexStatus.communities} communities).`
      : 'Retrieval → Chat — awaiting index readiness.',
    `Ingest → Embed — ${profile.chunker} · effort ${profile.ingest_effort}.`,
    timers.length > 0
      ? `Timers → Notify — ${timers.length} job(s) coordinating wake signals.`
      : 'Timers → Notify — idle queue.',
    isSpeaking
      ? 'Ambience → Hub — speaking filler active (TTS hook pending).'
      : 'Ambience → Hub — standby; WAKE arms filler speech.',
    // TODO: replace with real policy↔retrieval coordination events when audited.
    `Policy → Tools — approvals gated on require_approval_for.`,
  ]

  return (
    <section className="panel-ops space-y-2 p-3">
      <h2 className="font-display text-[10px] text-muted-foreground">Coordination</h2>
      <ul className="space-y-1.5">
        {notes.map((note) => (
          <li key={note} className="flex gap-2 text-[11px] leading-snug text-foreground/85">
            <span className="mt-1.5 size-1 shrink-0 rounded-full bg-primary/70" />
            <span>{note}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}
