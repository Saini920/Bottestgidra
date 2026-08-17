// Telegram layer for the BROWSER — the only file that touches mtcute directly.
// Same isolation strategy as workers/lib/tg.js: if an mtcute signature differs
// from the installed version, fix it here.
//
// VERIFY marks: mtcute v0.31 API — client.sendCode / signIn / checkPassword,
// client.getMessages(peer, opts), client.uploadFile, client.sendMedia,
// client.downloadMedia, client.exportSession/importSession, LocalStorage.
// Confirm against the installed @mtcute/client types.

import { TelegramClient } from "@mtcute/web";

export interface LoginStep {
  step: "phone" | "code" | "password" | "done";
  phoneCodeHash?: string;
  passwordHint?: string;
  error?: string;
}

export class Telegram {
  private client: TelegramClient | null = null;
  private apiId = 0;
  private apiHash = "";

  async connect(apiId: string, apiHash: string, session?: string): Promise<void> {
    this.apiId = Number(apiId);
    this.apiHash = apiHash;
    this.client = new TelegramClient({
      apiId: this.apiId,
      apiHash: this.apiHash,
      storage: "venter:mtcute",
    });
    if (session) {
      // VERIFY: importSession accepts the string from exportSession().
      await this.client.importSession(session);
    }
    await this.client.start();
  }

  get isConnected(): boolean {
    return !!this.client;
  }

  /** Returns true when an existing session is authorized (no login needed). */
  get isAuthorized(): boolean {
    return !!this.client?.authorized;
  }

  async sendCode(phone: string): Promise<LoginStep> {
    // VERIFY: sendCode(phone) resolves to { phoneCodeHash }.
    const res = await this.client!.sendCode(phone);
    return { step: "code", phoneCodeHash: res.phoneCodeHash };
  }

  async signInWithCode(code: string, phoneCodeHash?: string): Promise<LoginStep> {
    try {
      // VERIFY: signIn({ code, phoneCodeHash }) — phoneCodeHash optional when
      // only one active code exists.
      await this.client!.signIn({ code, phoneCodeHash });
      return { step: "done" };
    } catch (e: any) {
      if (e?.type === "SESSION_PASSWORD_NEEDED") {
        // VERIFY: getPasswordHint / checkPassword for 2FA.
        let hint = "";
        try {
          hint = (await this.client!.getPasswordHint()) || "";
        } catch {
          /* no hint */
        }
        return { step: "password", passwordHint: hint };
      }
      return { step: "code", error: String(e?.message || e) };
    }
  }

  async signInWithPassword(password: string): Promise<LoginStep> {
    try {
      // VERIFY: checkPassword(password) completes 2FA login.
      await this.client!.checkPassword(password);
      return { step: "done" };
    } catch (e: any) {
      return { step: "password", error: String(e?.message || e) };
    }
  }

  /** Export the session as a string (to encrypt + store). */
  async exportSession(): Promise<string> {
    // VERIFY: exportSession() returns a string.
    return this.client!.exportSession();
  }

  /** Terminate the session server-side (blueprint Section 9.4). */
  async logout(): Promise<void> {
    try {
      await this.client!.logout();
    } finally {
      this.client = null;
    }
  }

  async getMe(): Promise<{ id: number; name: string }> {
    const me = await this.client!.getMe();
    return { id: Number(me.id), name: me.firstName || `user_${me.id}` };
  }

  /**
   * Upload a browser File to Saved Messages.
   * @returns the new message id
   */
  async uploadToSaved(file: File, onProgress?: (pct: number) => void): Promise<number> {
    // VERIFY: uploadFile accepts { fileName, file: File } and a progress cb.
    const input = await this.client!.uploadFile(
      { fileName: file.name, file },
      { progress: (up, total) => total > 0 && onProgress?.(Math.round((up / total) * 100)) }
    );
    // VERIFY: sendMedia('me', { file }) → message with .id
    const msg = await this.client!.sendMedia("me", { file: input });
    return msg.id;
  }

  /** List recent files from Saved Messages. */
  async listSaved(limit = 50): Promise<
    { messageId: number; fileName: string; size: number; date: number; caption?: string }[]
  > {
    // VERIFY: getMessages('me', { limit }) returns Message[].
    const msgs = await this.client!.getMessages("me", { limit });
    const out = [];
    for (const m of msgs) {
      const doc = m.media && (m.media as any).document;
      if (!doc) continue;
      out.push({
        messageId: m.id,
        fileName: doc.fileName || `file_${m.id}`,
        size: Number(doc.size ?? 0),
        date: m.date * 1000,
        caption: m.text || undefined,
      });
    }
    return out;
  }

  /** Download a message's document as a Blob. */
  async downloadBlob(messageId: number): Promise<{ blob: Blob; fileName: string }> {
    // VERIFY: getMessages(peer, { ids }) + downloadMedia(media, { value: 'blob' }).
    const msgs = await this.client!.getMessages("me", { ids: [messageId] });
    const m = msgs?.[0];
    if (!m?.media) throw new Error("Message not found in Saved Messages");
    const doc = (m.media as any).document;
    const blob = await this.client!.downloadMedia(m.media, { value: "blob" });
    return { blob, fileName: doc?.fileName || `file_${messageId}` };
  }

  async close(): Promise<void> {
    try {
      await this.client?.close();
    } catch {
      /* ignore */
    }
    this.client = null;
  }
}
