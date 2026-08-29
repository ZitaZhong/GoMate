// lib/sse.ts
// SSE stream parser for Next.js client - serves both cross-city and local modes

export interface SSEEvent {
  event: string;
  data: unknown;
}

/**
 * Async generator that parses SSE from a fetch response.
 * Works with both cross-city (node_output/interrupt/done) and local mode
 * (room_state/theme_result/activity_candidates) events.
 */
export async function* parseSSE(
  url: string,
  options?: RequestInit
): AsyncGenerator<SSEEvent> {
  const resp = await fetch(url, options);
  if (!resp.ok) {
    throw new Error(`SSE request failed: ${resp.status}`);
  }
  if (!resp.body) return;

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split(/\r?\n\r?\n/);
    buf = parts.pop() ?? "";

    for (const chunk of parts) {
      let event = "message";
      let data = "";
      for (const line of chunk.split(/\r?\n/)) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) {
        try {
          yield { event, data: JSON.parse(data) };
        } catch {
          // Skip malformed JSON
        }
      }
    }
  }
}

/**
 * Helper: consume SSE stream and dispatch to callbacks.
 */
export async function consumeSSE(
  url: string,
  handlers: Record<string, (data: unknown) => void>,
  options?: RequestInit
): Promise<void> {
  for await (const { event, data } of parseSSE(url, options)) {
    const handler = handlers[event] ?? handlers["*"];
    if (handler) handler(data);
  }
}
