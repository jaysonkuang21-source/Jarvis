import { Fragment, useMemo } from 'react'

/**
 * Deliberately small renderer for fenced code, inline code, and paragraphs.
 *
 * A full markdown pipeline is a dependency decision, and streamed text arrives
 * with unbalanced syntax mid-token anyway, so this stays forgiving.
 */
export function Markdown({ text }: { text: string }) {
  const blocks = useMemo(() => splitFences(text), [text])

  return (
    <div className="space-y-3">
      {blocks.map((block, index) =>
        block.type === 'code' ? (
          <pre
            key={index}
            className="scrollbar-thin overflow-x-auto rounded-lg border border-border bg-muted/60 p-3 text-xs leading-relaxed"
          >
            {block.lang && (
              <div className="mb-1.5 select-none font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
                {block.lang}
              </div>
            )}
            <code className="font-mono">{block.content}</code>
          </pre>
        ) : (
          <p key={index} className="whitespace-pre-wrap break-words leading-relaxed">
            <Inline text={block.content} />
          </p>
        ),
      )}
    </div>
  )
}

/** Render inline backtick code spans within a text block. */
function Inline({ text }: { text: string }) {
  const parts = text.split(/(`[^`\n]+`)/g)
  return (
    <>
      {parts.map((part, index) =>
        part.startsWith('`') && part.endsWith('`') && part.length > 2 ? (
          <code
            key={index}
            className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]"
          >
            {part.slice(1, -1)}
          </code>
        ) : (
          <Fragment key={index}>{part}</Fragment>
        ),
      )}
    </>
  )
}

type Block = { type: 'text' | 'code'; content: string; lang?: string }

/** Split markdown into fenced code blocks and surrounding text. */
function splitFences(text: string): Block[] {
  const blocks: Block[] = []
  const pattern = /```(\w*)\n?([\s\S]*?)(?:```|$)/g
  let cursor = 0
  let match: RegExpExecArray | null

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) {
      const before = text.slice(cursor, match.index).trim()
      if (before) blocks.push({ type: 'text', content: before })
    }
    blocks.push({ type: 'code', content: match[2], lang: match[1] || undefined })
    cursor = pattern.lastIndex
  }

  const rest = text.slice(cursor).trim()
  if (rest) blocks.push({ type: 'text', content: rest })
  return blocks.length > 0 ? blocks : [{ type: 'text', content: text }]
}
