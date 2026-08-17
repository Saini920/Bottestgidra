// Telegram transport layer — the ONLY file that touches mtcute directly.
// Everything else in the workers talks to this abstraction, so if the mtcute
// API differs slightly from what's below, this single file is the fix point.
//
// NOTE: mtcute call signatures below are based on mtcute v0.31 docs
// (`session` option in the constructor, `client.exportSession()`,
// `client.getMessages(peer, { ids })`, `client.downloadMedia(media, opts)`,
// `client.uploadFile(file, opts)`, `client.sendMedia(peer, opts)`).
// Marked with VERIFY — confirm against the installed version's types.

import { TelegramClient } from "@mtcute/node";
import fs from "node:fs";

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
    // VERIFY: constructor accepts an exported string session.
    this.client = new TelegramClient({
      apiId: this.apiId,
      apiHash: this.apiHash,
      storage: "memory",
      session: this.session, // string session from client.exportSession()
    });
    await this.client.start(); // connect + validate auth
    const me = await this.client.getMe();
    return me;
  }

  async close() {
    try {
      await this.client?.close();
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

    let media;
    if (ref.messageId != null) {
      // VERIFY: getMessages signature — getMessages(peer, { ids })
      const msgs = await this.client.getMessages("me", { ids: [Number(ref.messageId)] });
      const msg = msgs?.[0];
      if (!msg?.media) throw new Error(`Message ${ref.messageId} not found in Saved Messages`);
      media = msg.media;
    } else if (ref.fileId) {
      // VERIFY: resolving a raw document by id+access_hash.
      // TODO: implement via InputDocument / getFileById once confirmed.
      throw new Error("file_id resolution not implemented yet — pass file_message_id");
    } else {
      throw new Error("no input reference (file_message_id or file_id) in payload");
    }

    // VERIFY: downloadMedia options — { filePath, progress(uploaded, total) }
    await this.client.downloadMedia(media, {
      filePath: destPath,
      progress: (uploaded, total) => {
        if (total > 0) onProgress(Math.min(100, Math.round((uploaded / total) * 100)));
      },
    });
  }

  /**
   * Upload a file to Saved Messages.
   * @returns {Promise<{messageId: number, size: number}>}
   */
  async uploadFile(filePath, caption, onProgress) {
    if (!this.client) throw new Error("Tg not started");

    // VERIFY: uploadFile(file, { progress(uploaded, total) }) → InputFile
    const input = await this.client.uploadFile(
      { fileName: fs.basename(filePath), file: fs.createReadStream(filePath) },
      {
        progress: (uploaded, total) => {
          if (total > 0) onProgress(Math.min(100, Math.round((uploaded / total) * 100)));
        },
      }
    );

    // VERIFY: sendMedia(peer, { file, caption }) → Message
    const msg = await this.client.sendMedia("me", {
      file: input,
      caption: caption || "",
    });
    return { messageId: msg.id, size: fs.statSync(filePath).size };
  }
}
