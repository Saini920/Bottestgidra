// Telegram layer for the BROWSER — GramJS (same library as the user's proven
// TG Drive app). Ported from TG Drive's telegramAuth.js / Login.jsx /
// telegramStorage.js patterns: StringSession, Api.auth.SendCode/SignIn,
// computeCheck for 2FA, session.save() export, sendFile/getMessages/downloadMedia.
//
// The rest of the app only talks to this class, so the interface below is
// stable regardless of the underlying library.

import { TelegramClient } from "telegram";
import { StringSession } from "telegram/sessions";
import { Api } from "telegram";
import { computeCheck } from "telegram/Password";

export interface LoginStep {
  step: "phone" | "code" | "password" | "done";
  phoneCodeHash?: string;
  passwordHint?: string;
  error?: string;
}

function makeClient(apiId: number, apiHash: string, session?: string): TelegramClient {
  // Same options as TG Drive's getClient(): WSS in the browser + retries.
  return new TelegramClient(new StringSession(session || ""), apiId, apiHash, {
    connectionRetries: 15,
    useWSS: true,
    autoReconnect: true,
    maxConcurrentDownloads: 5,
    downloadRetries: 5,
  });
}

export class Telegram {
  private client: TelegramClient | null = null;
  private phoneNumber = "";
  private apiId = 0;
  private apiHash = "";

  async connect(apiId: string, apiHash: string, session?: string): Promise<void> {
    this.apiId = Number(apiId);
    this.apiHash = apiHash;
    this.client = makeClient(this.apiId, apiHash, session);
    await this.client.connect();
  }

  get isConnected(): boolean {
    return !!this.client?.connected;
  }

  get isAuthorized(): boolean {
    return this.isConnected;
  }

  async sendCode(phone: string): Promise<LoginStep> {
    if (!this.client) throw new Error("Telegram client not connected — settings check karo");
    this.phoneNumber = phone;
    const result = await this.client.invoke(
      new Api.auth.SendCode({
        phoneNumber: phone,
        apiId: this.apiId,
        apiHash: this.apiHash,
        settings: new Api.CodeSettings({ allowFlashcall: true, currentNumber: true, allowAppHash: true }),
      })
    );
    return { step: "code", phoneCodeHash: result.phoneCodeHash };
  }

  async signInWithCode(code: string, phoneCodeHash?: string): Promise<LoginStep> {
    if (!this.client) throw new Error("Telegram client not connected");
    try {
      await this.client.invoke(
        new Api.auth.SignIn({
          phoneNumber: this.phoneNumber,
          phoneCodeHash: phoneCodeHash ?? "",
          phoneCode: code,
        })
      );
      return { step: "done" };
    } catch (e: any) {
      if (String(e?.message || e).includes("SESSION_PASSWORD_NEEDED")) {
        // 2FA required — fetch the hint (same as TG Drive's Login.jsx).
        let hint = "";
        try {
          const pwd = await this.client.invoke(new Api.account.GetPassword());
          hint = pwd.hint || "";
        } catch {
          /* no hint */
        }
        return { step: "password", passwordHint: hint };
      }
      return { step: "code", error: String(e?.message || e) };
    }
  }

  async signInWithPassword(password: string): Promise<LoginStep> {
    if (!this.client) throw new Error("Telegram client not connected");
    try {
      const pwd = await this.client.invoke(new Api.account.GetPassword());
      if (!pwd.hasPassword) throw new Error("Account me 2FA enabled nahi hai");
      const passwordHash = await computeCheck(pwd, password);
      await this.client.invoke(new Api.auth.CheckPassword({ password: passwordHash }));
      return { step: "done" };
    } catch (e: any) {
      return { step: "password", error: String(e?.message || e) };
    }
  }

  /** Export the GramJS session as a string (to encrypt + store). */
  async exportSession(): Promise<string> {
    if (!this.client) throw new Error("Telegram client not connected");
    return this.client.session.save();
  }

  /** Terminate the session server-side, then drop the local client. */
  async logout(): Promise<void> {
    const c = this.client;
    this.client = null;
    if (!c) return;
    try {
      await c.logOut();
    } catch {
      /* session already invalid — fine */
    }
    try {
      await c.disconnect();
    } catch {
      /* ignore */
    }
  }

  async getMe(): Promise<{ id: number; name: string }> {
    if (!this.client) throw new Error("Telegram client not connected");
    const me = await this.client.getMe();
    return { id: Number(me.id), name: me.firstName || `user_${me.id}` };
  }

  /**
   * Upload a browser File to Saved Messages (forceDocument keeps binaries intact).
   * @returns the new message id
   */
  async uploadToSaved(file: File, onProgress?: (pct: number) => void): Promise<number> {
    if (!this.client) throw new Error("Telegram client not connected — login karo");
    const msg = await this.client.sendFile("me", {
      file,
      caption: "",
      forceDocument: true,
      progressCallback: (received, total) => {
        if (total > 0) onProgress?.(Math.round((received / total) * 100));
      },
    });
    return msg.id;
  }

  /** List recent files from Saved Messages. */
  async listSaved(limit = 50): Promise<
    { messageId: number; fileName: string; size: number; date: number; caption?: string }[]
  > {
    if (!this.client) throw new Error("Telegram client not connected");
    const msgs = await this.client.getMessages("me", { limit });
    const out: { messageId: number; fileName: string; size: number; date: number; caption?: string }[] = [];
    for (const m of msgs) {
      const doc = (m.media as Api.MessageMediaDocument | undefined)?.document;
      if (!doc) continue; // skip non-file messages (photos/text)
      out.push({
        messageId: m.id,
        fileName: doc.fileName || `file_${m.id}`,
        size: Number(doc.size ?? 0),
        date: m.date instanceof Date ? m.date.getTime() : Number(m.date) * 1000,
        caption: m.message || undefined,
      });
    }
    return out;
  }

  /** Download a message's document as a Blob. */
  async downloadBlob(messageId: number): Promise<{ blob: Blob; fileName: string }> {
    if (!this.client) throw new Error("Telegram client not connected");
    const msgs = await this.client.getMessages("me", { ids: [messageId] });
    const m = msgs?.[0];
    const doc = (m?.media as Api.MessageMediaDocument | undefined)?.document;
    if (!m || !doc) throw new Error("Message not found in Saved Messages");
    const buf = await this.client.downloadMedia(m, {
      progressCallback: () => {
        /* no UI progress needed here */
      },
    });
    const blob = new Blob([buf as unknown as BlobPart], {
      type: doc.mimeType || "application/octet-stream",
    });
    return { blob, fileName: doc.fileName || `file_${messageId}` };
  }

  async close(): Promise<void> {
    try {
      await this.client?.disconnect();
    } catch {
      /* ignore */
    }
    this.client = null;
  }
}
