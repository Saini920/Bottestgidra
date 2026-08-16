"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, clearToken, getToken } from "@/lib/api";

interface SettingInfo {
  set: boolean;
  masked: string;
  hint: string;
}

export default function Settings() {
  const router = useRouter();
  const [settings, setSettings] = useState<Record<string, SettingInfo> | null>(null);
  const [isAdmin, setIsAdmin] = useState<boolean | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<{ type: "error" | "ok"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/");
      return;
    }
    api("/auth/me")
      .then((me) => {
        setIsAdmin(!!me.is_admin);
        if (!me.is_admin) return;
        return api("/api/settings").then((r) => setSettings(r.settings));
      })
      .catch(() => router.replace("/"));
  }, [router]);

  async function save(key: string) {
    setBusy(true);
    setMsg(null);
    try {
      await api("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, value: values[key] || "" }),
      });
      const r = await api("/api/settings");
      setSettings(r.settings);
      setMsg({ type: "ok", text: `${key} saved ✅` });
    } catch (e: any) {
      setMsg({ type: "error", text: e.message });
    } finally {
      setBusy(false);
    }
  }

  function logout() {
    api("/auth/logout", { method: "POST" }).catch(() => {});
    clearToken();
    router.replace("/");
  }

  if (isAdmin === null) return <div className="container muted">Loading…</div>;

  if (isAdmin === false) {
    return (
      <div className="container">
        <div className="nav">
          <Link href="/dashboard">Dashboard</Link>
          <Link href="/storage">Storage</Link>
          <Link href="/tools">RE Tools</Link>
        </div>
        <div className="card">
          <h1>🔒 Admins only</h1>
          <p className="muted">You need admin rights to view settings.</p>
        </div>
      </div>
    );
  }

  const order = [
    "API_ID", "API_HASH", "GITHUB_TOKEN", "GITHUB_REPO", "PUBLIC_URL",
    "WEBHOOK_TOKEN", "TELEGRAM_BOT_TOKEN", "ADMIN_IDS",
  ];

  return (
    <div className="container">
      <div className="nav">
        <Link href="/dashboard">Dashboard</Link>
        <Link href="/storage">Storage</Link>
        <Link href="/tools">RE Tools</Link>
        <Link href="/settings">Settings</Link>
        <button className="secondary" onClick={logout} style={{ marginLeft: "auto" }}>
          Logout
        </button>
      </div>

      <div className="card">
        <h1>⚙️ Server Settings</h1>
        <p className="muted">
          No .env needed — values are encrypted at rest and apply live.
          Leave a field empty + save to clear it.
        </p>
        {msg && <div className={msg.type === "error" ? "error" : "ok"}>{msg.text}</div>}
      </div>

      {!settings ? (
        <div className="card muted">Loading…</div>
      ) : (
        order.map((key) => {
          const info = settings[key];
          return (
            <div className="card" key={key} style={{ padding: 14 }}>
              <div className="row spread">
                <div>
                  <b>{key}</b>
                  <div className="muted">{info.hint}</div>
                  {info.set ? (
                    <div className="ok" style={{ fontSize: 12 }}>Configured: {info.masked}</div>
                  ) : (
                    <div className="muted" style={{ fontSize: 12 }}>Not configured</div>
                  )}
                </div>
                <div style={{ flex: 1, maxWidth: 340 }}>
                  <input
                    type={key.includes("TOKEN") || key.includes("HASH") || key === "API_HASH" ? "password" : "text"}
                    placeholder={info.set ? "Leave empty to keep current" : "Enter value"}
                    value={values[key] || ""}
                    onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.value }))}
                  />
                  <button className="secondary" onClick={() => save(key)} disabled={busy}>
                    Save
                  </button>
                </div>
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}
