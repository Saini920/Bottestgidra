// ntfy.sh progress publisher.
// The frontend subscribes to https://ntfy.sh/{jobId} via SSE and renders these events.
// Protocol:
//   progress events -> JSON body { pct, label, bar } with Title header = label
//   final events    -> JSON body { status: "done"|"error", ... } with Title = status

export class Ntfy {
  /**
   * @param {string} jobId random topic id (also doubles as the ntfy topic)
   */
  constructor(jobId) {
    this.jobId = jobId;
    this.base = "https://ntfy.sh";
  }

  /**
   * Push a progress update.
   * @param {number} pct 0-100
   * @param {string} label current step label
   * @param {string} [bar] rendered progress bar
   */
  async progress(pct, label, bar = "") {
    await this.#post(JSON.stringify({ type: "progress", pct, label, bar }), label);
  }

  /**
   * Push a final completion / error event.
   * @param {object} data { status, message_id?, filename?, caption?, size?, error? }
   */
  async final(data) {
    const title = data.status === "done" ? "✅ Done" : "❌ Failed";
    await this.#post(JSON.stringify({ type: "final", ...data }), title);
  }

  /** Plain text push (used for raw Ghidra tail lines / diagnostics). */
  async raw(message, title = "") {
    await this.#post(message, title);
  }

  async #post(body, title) {
    if (!this.jobId) return;
    try {
      const headers = {};
      if (title) headers["Title"] = title;
      const resp = await fetch(`${this.base}/${this.jobId}`, {
        method: "POST",
        headers,
        body,
      });
      if (!resp.ok) console.warn("ntfy push failed:", resp.status);
    } catch (e) {
      // Progress pushes must never crash the worker.
      console.warn("ntfy push error:", e.message);
    }
  }
}

/** Standard 16-block progress bar (same visual as the old Python bot). */
export function progressBar(pct) {
  const val = Math.max(0, Math.min(100, Number(pct) || 0));
  const filled = Math.max(0, Math.min(16, Math.round((val * 16) / 100)));
  return "▰".repeat(filled) + "▱".repeat(16 - filled);
}
