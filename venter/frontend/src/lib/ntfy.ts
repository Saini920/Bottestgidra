// Live progress subscription via ntfy.sh SSE (works from the browser).

import type { ProgressEvent } from "../types";

/**
 * Subscribe to a job's progress topic.
 * @returns an unsubscribe function.
 */
export function subscribeJob(jobId: string, onEvent: (e: ProgressEvent) => void): () => void {
  const es = new EventSource(`https://ntfy.sh/${jobId}/sse`);

  es.onmessage = (ev) => {
    try {
      const raw = JSON.parse(ev.data as string); // ntfy SSE envelope
      const body: string = raw.message ?? "";
      if (!body) return;
      const parsed = JSON.parse(body) as ProgressEvent;
      onEvent(parsed);
    } catch {
      /* non-JSON heartbeat — ignore */
    }
  };
  es.onerror = () => {
    // EventSource auto-reconnects; ignore transient errors.
  };

  return () => es.close();
}
