"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, setToken } from "@/lib/api";

type Step = "phone" | "otp" | "password";

export default function LoginWizard() {
  const router = useRouter();
  const [step, setStep] = useState<Step>("phone");
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [msg, setMsg] = useState<{ type: "error" | "ok"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function sendCode() {
    setBusy(true);
    setMsg(null);
    try {
      const res = await api("/auth/send-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone }),
      });
      if (res.error) throw new Error(res.error);
      setMsg({ type: "ok", text: "Code sent to your Telegram! 📩" });
      setStep("otp");
    } catch (e: any) {
      setMsg({ type: "error", text: e.message });
    } finally {
      setBusy(false);
    }
  }

  async function verifyCode() {
    setBusy(true);
    setMsg(null);
    try {
      const res = await api("/auth/verify-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone, code }),
      });
      if (res.error) throw new Error(res.error);
      if (res.password_required) {
        setMsg({ type: "ok", text: "2FA password required 🔒" });
        setStep("password");
        return;
      }
      setToken(res.token);
      router.push("/dashboard");
    } catch (e: any) {
      setMsg({ type: "error", text: e.message });
    } finally {
      setBusy(false);
    }
  }

  async function checkPassword() {
    setBusy(true);
    setMsg(null);
    try {
      const res = await api("/auth/check-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone, password }),
      });
      if (res.error) throw new Error(res.error);
      setToken(res.token);
      router.push("/dashboard");
    } catch (e: any) {
      setMsg({ type: "error", text: e.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ maxWidth: 420, margin: "40px auto" }}>
      <h1>Telegram Web</h1>
      <p className="muted">Login with your Telegram account</p>

      {step === "phone" && (
        <>
          <label className="muted">Phone number (with country code)</label>
          <input
            type="text"
            placeholder="+91 98XXXXXXXX"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
          />
          <button onClick={sendCode} disabled={busy || !phone}>
            {busy ? "Sending..." : "Send Code"}
          </button>
        </>
      )}

      {step === "otp" && (
        <>
          <label className="muted">Enter the code from Telegram</label>
          <input
            type="text"
            placeholder="12345"
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <button onClick={verifyCode} disabled={busy || !code}>
            {busy ? "Verifying..." : "Verify"}
          </button>
          <div className="row">
            <button className="secondary" onClick={() => setStep("phone")}>
              Back
            </button>
            <button className="secondary" onClick={sendCode} disabled={busy}>
              Resend code
            </button>
          </div>
        </>
      )}

      {step === "password" && (
        <>
          <div className="pill premium">🔒 2FA enabled</div>
          <label className="muted">Enter your 2FA password</label>
          <input
            type="password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <button onClick={checkPassword} disabled={busy || !password}>
            {busy ? "Unlocking..." : "Unlock"}
          </button>
          <div className="row">
            <button className="secondary" onClick={() => setStep("phone")}>
              Back
            </button>
          </div>
        </>
      )}

      {msg && <div className={msg.type === "error" ? "error" : "ok"}>{msg.text}</div>}
    </div>
  );
}
