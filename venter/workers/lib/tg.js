// Telegram transport layer for the WORKERS (Node.js) — GramJS, the SAME
// library used in the browser frontend (ported from the user's TG Drive app).
// That means one StringSession format works everywhere: the frontend exports
// `client.session.save()`, the worker imports it via `new StringSession(str)`.
//
// Everything else in the workers talks to this abstraction only.

import { TelegramClient } from "telegram";
// NOTE: must import the exact file — Node.js ESM does NOT support directory
// imports ("telegram/sessions" fails with ERR_UNSUPPORTED_DIR_IMPORT on
// the runner). Bundlers like Vite tolerate it; bare Node does not.
import { StringSession } from "telegram/sessions/index.js";

// Telegram official DC IPs (port 443). The web CDN hosts
// (*.web.telegram.org) open a TCP connection from cloud runners and then
// immediately drop it — pinning the official DC IP is reliable everywhere.
const DC_IPS = {
  1: "149.154.175.53",
  2: "149.154.167.51",
  3: "149.154.175.100",
  4: "149.154.167.91",
  5: "91.108.56.130",
};

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
        // Port 443 — Telegram DCs are reachable from cloud runners on 443.
        useWSS: true,
      }
    );

    // Pin the session's DC to its official IP:443 — bypasses the web CDN
    // (*.web.telegram.org) whose connections drop immediately from Azure/
    // GitHub Actions runners.
    const dcId = this.client.session.dcId;
    const ip = DC_IPS[dcId];
    if (dcId && ip) {
      this.client.session.setDC(dcId, ip, 443);
      console.log(`Telegram DC${dcId} pinned to ${ip}:443`);
    } else {
      console.warn(`Unknown DC id ${dcId}, falling back to default resolution`);
    }

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
