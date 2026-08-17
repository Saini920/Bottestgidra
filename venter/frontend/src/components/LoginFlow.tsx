import { useState } from "react";
import type { ReactNode } from "react";
import { Telegram } from "../lib/telegram";
import type { Settings, StoredSession } from "../types";

interface Props {
  settings: Settings;
  onSession: (s: StoredSession) => void;
}

export function LoginFlow({ settings, onSession }: Props) {
  const [tg] = useState(() => new Telegram());
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [hint, setHint] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [step, setStep] = useState<"phone" | "code" | "password">("phone");
  const [codeHash, setCodeHash] = useState<string | undefined>();

  if (!settings.apiId || !settings.apiHash) {
    return (
      <Card title="🔐 Telegram Login">
        <p className="text-sm text-zinc-400">
          Login se pehle <b>Settings</b> me apna <code>API_ID</code>, <code>API_HASH</code>{" "}
          (my.telegram.org se) aur ek <code>SESSION_KEY</code> passphrase set karo.
        </p>
      </Card>
    );
  }

  async function doSendCode() {
    setBusy(true);
    setError("");
    try {
      await tg.connect(settings.apiId, settings.apiHash);
      const res = await tg.sendCode(phone);
      setCodeHash(res.phoneCodeHash);
      setStep("code");
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function doVerifyCode() {
    setBusy(true);
    setError("");
    try {
      const res = await tg.signInWithCode(code, codeHash);
      if (res.step === "password") {
        setHint(res.passwordHint || "");
        setStep("password");
      } else if (res.step === "done") {
        await finishLogin();
      } else {
        setError(res.error || "Wrong code");
      }
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function doVerifyPassword() {
    setBusy(true);
    setError("");
    try {
      const res = await tg.signInWithPassword(password);
      if (res.step === "done") await finishLogin();
      else setError(res.error || "Wrong password");
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function finishLogin() {
    const me = await tg.getMe();
    const session = await tg.exportSession();
    const { encryptSession } = await import("../lib/crypto");
    const blob = await encryptSession(session, settings.sessionKey);
    onSession({ blob, me });
  }

  return (
    <Card title="🔐 Telegram Login">
      {step === "phone" && (
        <div className="space-y-3">
          <input
            className={inputCls}
            placeholder="+91 98765 43210"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
          />
          <button className={btnCls} disabled={busy} onClick={doSendCode}>
            {busy ? "Sending…" : "Send Code"}
          </button>
        </div>
      )}
      {step === "code" && (
        <div className="space-y-3">
          <p className="text-sm text-zinc-400">Telegram app me aaya 5-digit OTP daalo:</p>
          <input
            className={inputCls}
            placeholder="12345"
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <button className={btnCls} disabled={busy} onClick={doVerifyCode}>
            {busy ? "Verifying…" : "Verify"}
          </button>
        </div>
      )}
      {step === "password" && (
        <div className="space-y-3">
          <p className="text-sm text-zinc-400">
            2FA password chahiye{hint ? ` (hint: ${hint})` : ""}:
          </p>
          <input
            className={inputCls}
            type="password"
            placeholder="Two-step verification password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <button className={btnCls} disabled={busy} onClick={doVerifyPassword}>
            {busy ? "Checking…" : "Unlock"}
          </button>
        </div>
      )}
      {error && <p className="text-sm text-red-400">❌ {error}</p>}
      <p className="mt-4 text-xs text-zinc-500">
        Session aapke browser me AES-256 se encrypted save hogi.{" "}
        <b>2FA account use karo</b> — bina 2FA wale accounts risky hain.
      </p>
    </Card>
  );
}

export function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="max-w-md mx-auto mt-16 rounded-xl border border-zinc-800 bg-zinc-900 p-6 shadow-xl">
      <h2 className="mb-4 text-lg font-semibold">{title}</h2>
      {children}
    </div>
  );
}

export const inputCls =
  "w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-blue-500";
export const btnCls =
  "w-full rounded-lg bg-blue-600 px-3 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50";
