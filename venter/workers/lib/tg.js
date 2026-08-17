// Telegram transport layer for the WORKERS (Node.js) — GramJS, the SAME
// library used in the browser frontend (ported from the user's TG Drive app).
// That means one StringSession format works everywhere: the frontend exports
// `client.session.save()`, the worker imports it via `new StringSession(str)`.
//
// Everything else in the workers talks to this abstraction only.

import { TelegramClient } from "telegram";
import { StringSession } from "telegram/sessions";

export class Tg {
  /**
   * @param {{apiId: string|number, apiHash: string, session: string}} opts
   */
  constructor({ apiId, apiHash, session }) {
    this.apiId = Number(apiId);
    this.apiHash = apiHash;
    this.session = session;
    /** @type {TelegramClient|null} */
    this.client = null;
  }

  async start() {
    // GramJS auto-selects TCP transport on Node (WSS is browser-only).
    this.client = new TelegramClient(
      new StringSession(this.session || ""),
      this.apiId,
      this.apiHash,
      {
        connectionRetries: 15,
        autoReconnect: true,
        maxConcurrentDownloads: 5,
        downloadRetries: 5,
      }
    );
    await this.client.connect();
    const me = await this.client.getMe();
    return me;
  }

  async close() {
    try {
      await this.client?.disconnect();
    } catch {
      /* ignore */
    }
    this.client = null;
  }

  /**
   * Download the input file from Saved Messages.
   * @param {{messageId?: number|string, fileId?: string}} ref
   * @param {string} destPath
   * @param {(pct: number) => Promise<void>|void} onProgress
   */
  async downloadInput(ref, destPath, onProgress) {
    if (!this.client) throw new Error("Tg not started");

    let media = null;
    if (ref.messageId != null) {
      const msgs = await this.client.getMessages("me", { ids: [Number(ref.messageId)] });
      const msg = msgs?.[0];
      if (!msg?.media) throw new Error(`Message ${ref.messageId} not found in Saved Messages`);
      media = msg.media;
    } else if (ref.fileId) {
      throw new Error("file_id resolution not implemented yet — pass file_message_id");
    } else {
      throw new Error("no input reference (file_message_id or file_id) in payload");
    }

    // outputFile writes straight to disk (memory-safe for large binaries).
    await this.client.downloadMedia(media, {
      outputFile: destPath,
      progressCallback: (received, total) => {
        if (total > 0) onProgress(Math.min(100, Math.round((received / total) * 100)));
      },
    });
  }

  /**
   * Upload a file to Saved Messages.
   * @returns {Promise<{messageId: number, size: number}>}
   */
  async uploadFile(filePath, caption, onProgress) {
    if (!this.client) throw new Error("Tg not started");

    // forceDocument keeps binaries intact (no photo compression).
    const msg = await this.client.sendFile("me", {
      file: filePath,
      caption: caption || "",
      forceDocument: true,
      progressCallback: (received, total) => {
        if (total > 0) onProgress(Math.min(100, Math.round((received / total) * 100)));
      },
    });
    const size = typeof msg.media?.document?.size === "number" ? msg.media.document.size : undefined;
    return { messageId: msg.id, size: size ?? 0 };
  }
}
