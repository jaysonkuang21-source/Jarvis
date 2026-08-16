/**
 * Remove model think/reasoning traces so they are never spoken or shown.
 * Complete blocks are dropped; orphan tags are scrubbed; an unclosed
 * ``<think>`` truncates so hidden reasoning is held (not spoken).
 */
export function stripThinkTags(text: string): string {
  let out = text.replace(/<think>[\s\S]*?<\/think>/gi, ' ')
  out = out.replace(/<\/think>/gi, ' ')
  const open = out.toLowerCase().lastIndexOf('<think>')
  if (open >= 0) {
    out = out.slice(0, open)
  }
  return out
}

/**
 * Strip markdown-ish markup so speech synthesis reads natural prose.
 * Keeps this pure for easy unit testing without the Web Speech API.
 */
export function stripForSpeech(text: string): string {
  return stripThinkTags(text)
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/[*_~|>]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

/** Short first clip so Fish can start audio ASAP. */
const FIRST_FLUSH_CHARS = 40
/** Force a first flush at a word boundary when the model rambles. */
const HARD_FLUSH_CHARS = 72
/** After audio has started, merge follow-ups toward ~10–12s of speech. */
const FOLLOW_UP_CHARS = 200
/** Hard cap so a monster sentence still yields mid-reply for prefetch. */
const FOLLOW_UP_HARD_CHARS = 280
/** Soft clause breaks that are safe for the opening flush only. */
const CLAUSE_BREAK = /[,;:—]\s+/g

/** Options for dual-policy speech chunking (Siri-like TTFA + continuity). */
export interface DrainSpeechOptions {
  /**
   * True once the opening clip has been emitted.
   * Follow-up flushes wait for sentence ends and prefer longer phrases.
   */
  openingDone?: boolean
}

/**
 * Split cleaned prose into speakable chunks for Fish TTS.
 * First chunk stays short for fast TTFA; later chunks stay long so the
 * continuous PCM ring stays fed across fewer Fish round-trips.
 */
export function splitForSpeech(text: string): string[] {
  const clean = stripForSpeech(text)
  if (!clean) return []

  const raw =
    clean.match(/[^.!?]+(?:[.!?]+|$)/g)?.map((part) => part.trim()).filter(Boolean) ?? [
      clean,
    ]
  if (raw.length <= 1) return raw

  return coalesceSpeechChunks(raw, { openingDone: false })
}

/**
 * Pull speakable slices out of a growing reply buffer.
 * Opening: clause / ~40-char flush for low TTFA.
 * After opening: sentence boundaries only, coalesced into longer clips.
 */
export function drainCompletedSpeech(
  buffer: string,
  opts: DrainSpeechOptions = {},
): {
  ready: string[]
  rest: string
} {
  if (!buffer) return { ready: [], rest: '' }
  const openingDone = Boolean(opts.openingDone)

  // Hold an unclosed think block in ``rest`` so its contents are never spoken.
  let hold = ''
  let work = buffer.replace(/<think>[\s\S]*?<\/think>/gi, ' ')
  work = work.replace(/<\/think>/gi, ' ')
  const open = work.toLowerCase().lastIndexOf('<think>')
  if (open >= 0) {
    hold = work.slice(open)
    work = work.slice(0, open)
  }
  if (!work.trim()) return { ready: [], rest: hold || buffer }

  const ends: number[] = []
  const sentenceRe = /[.!?]+/g
  let match: RegExpExecArray | null
  while ((match = sentenceRe.exec(work)) !== null) {
    const after = match.index + match[0].length
    if (after < work.length && /\s/.test(work[after]!)) {
      ends.push(after)
    }
  }

  if (ends.length) {
    const raw: string[] = []
    let start = 0
    for (const end of ends) {
      const slice = stripForSpeech(work.slice(start, end))
      if (slice) raw.push(slice)
      start = end
    }
    // After opening, hold a short trailing sentence until more arrives so we
    // do not emit a tiny follow-up clip that starves the ring buffer.
    if (openingDone && raw.length) {
      const last = raw[raw.length - 1]!
      const heldIncomplete = stripForSpeech(work.slice(start)).length > 0
      if (!heldIncomplete && last.length < 80 && raw.length === 1) {
        return { ready: [], rest: work + hold }
      }
    }
    return {
      ready: coalesceSpeechChunks(raw, { openingDone }),
      rest: work.slice(start) + hold,
    }
  }

  if (!openingDone) {
    const earlyAt = findEarlyFlushEnd(work)
    if (earlyAt > 0) {
      const slice = stripForSpeech(work.slice(0, earlyAt))
      if (slice) {
        return { ready: [slice], rest: work.slice(earlyAt) + hold }
      }
    }
  } else {
    // Follow-up hard flush: only when the model rambles without punctuation.
    const hardAt = findFollowUpHardFlushEnd(work)
    if (hardAt > 0) {
      const slice = stripForSpeech(work.slice(0, hardAt))
      if (slice) {
        return { ready: [slice], rest: work.slice(hardAt) + hold }
      }
    }
  }

  return { ready: [], rest: work + hold }
}

/**
 * Return a cut index for the opening early-speak flush, or -1 when holding.
 */
function findEarlyFlushEnd(work: string): number {
  CLAUSE_BREAK.lastIndex = 0
  let clauseEnd = -1
  let clauseMatch: RegExpExecArray | null
  while ((clauseMatch = CLAUSE_BREAK.exec(work)) !== null) {
    const after = clauseMatch.index + clauseMatch[0].length
    const spoken = stripForSpeech(work.slice(0, after))
    if (spoken.length >= 24) {
      clauseEnd = after
      break
    }
  }
  if (clauseEnd > 0) return clauseEnd

  const trimmed = work.trimStart()
  if (trimmed.length < FIRST_FLUSH_CHARS) return -1

  const target =
    trimmed.length >= HARD_FLUSH_CHARS ? HARD_FLUSH_CHARS : FIRST_FLUSH_CHARS
  if (trimmed.length < target) return -1

  const lead = work.length - trimmed.length
  const window = trimmed.slice(0, target + 16)
  const breakAt = window.lastIndexOf(' ')
  if (breakAt < Math.floor(FIRST_FLUSH_CHARS * 0.6)) return -1
  return lead + breakAt + 1
}

/**
 * Force a follow-up flush at a word boundary when punctuation never arrives.
 */
function findFollowUpHardFlushEnd(work: string): number {
  const trimmed = work.trimStart()
  if (trimmed.length < FOLLOW_UP_HARD_CHARS) return -1
  const lead = work.length - trimmed.length
  const window = trimmed.slice(0, FOLLOW_UP_HARD_CHARS + 24)
  const breakAt = window.lastIndexOf(' ')
  if (breakAt < Math.floor(FOLLOW_UP_CHARS * 0.5)) return -1
  return lead + breakAt + 1
}

/**
 * Merge chunks: short opening for TTFA, long follow-ups for gapless playout.
 */
function coalesceSpeechChunks(
  chunks: string[],
  opts: { openingDone: boolean },
): string[] {
  if (chunks.length <= 1) return chunks

  const out: string[] = [chunks[0]!]
  for (let i = 1; i < chunks.length; i++) {
    const prev = out[out.length - 1]!
    const next = chunks[i]!
    const opening = !opts.openingDone && out.length === 1

    if (opening && prev.length >= FIRST_FLUSH_CHARS) {
      out.push(next)
      continue
    }
    const mergeTarget = opening ? FIRST_FLUSH_CHARS : FOLLOW_UP_CHARS
    if (prev.length + 1 + next.length <= mergeTarget) {
      out[out.length - 1] = `${prev} ${next}`
    } else {
      out.push(next)
    }
  }
  return out
}
