/**
 * Minimal SSE frame parser for `fetch` response bodies.
 *
 * EventSource cannot issue a POST, and the chat request carries a body, so the
 * stream is read manually. Chunk boundaries land anywhere, so partial frames
 * are buffered until a blank line completes them.
 */

export interface SseFrame {
  event: string
  data: string
}

/** Create a stateful parser that turns byte chunks into complete SSE frames. */
export function createSseParser() {
  let buffer = ''

  /** Append a chunk and return any frames completed by the new data. */
  return function push(chunk: string): SseFrame[] {
    buffer += chunk
    const frames: SseFrame[] = []

    // Servers may use \n\n or \r\n\r\n between frames.
    let boundary = findBoundary(buffer)
    while (boundary !== -1) {
      const raw = buffer.slice(0, boundary.index)
      buffer = buffer.slice(boundary.index + boundary.length)

      const frame = parseFrame(raw)
      if (frame) frames.push(frame)

      boundary = findBoundary(buffer)
    }
    return frames
  }
}

type Boundary = { index: number; length: number } | -1

/** Locate the earliest blank-line frame boundary in the buffer. */
function findBoundary(buffer: string): Boundary {
  const lf = buffer.indexOf('\n\n')
  const crlf = buffer.indexOf('\r\n\r\n')
  if (crlf !== -1 && (lf === -1 || crlf < lf)) return { index: crlf, length: 4 }
  if (lf !== -1) return { index: lf, length: 2 }
  return -1
}

/** Parse one raw SSE frame into event name and joined data lines. */
function parseFrame(raw: string): SseFrame | null {
  let event = 'message'
  const data: string[] = []

  for (const line of raw.split(/\r?\n/)) {
    // Comment frame, used for keepalives.
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '')

    if (field === 'event') event = value
    else if (field === 'data') data.push(value)
  }

  if (data.length === 0) return null
  return { event, data: data.join('\n') }
}

/** Read a fetch response body as a sequence of SSE frames. */
export async function* readSse(
  response: Response,
  signal?: AbortSignal,
): AsyncGenerator<SseFrame> {
  if (!response.body) throw new Error('Response has no body to stream')

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  const push = createSseParser()

  try {
    while (true) {
      if (signal?.aborted) return
      const { done, value } = await reader.read()
      if (done) return
      for (const frame of push(value)) yield frame
    }
  } finally {
    // Releasing the lock lets an abort tear the connection down promptly.
    reader.cancel().catch(() => {})
  }
}
