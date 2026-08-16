"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";

export default function Setup() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [apiId, setApiId] = useState("");
  const [apiHash, setApiHash] = useState("");
  const [githubToken, setGithubToken] = useState("");
  const [githubRepo, setGithubRepo] = useState("");
  const [msg, setMsg] = useState<{ type: "error" | "ok"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api("/api/setup/status")
      .then((s) => {
        if (s.setup_done) router.replace("/");
        else setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [router]);

  async function save() {
    setBusy(true);
    setMsg(null);
    try {
      const res = await api("/api/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_id: apiId,
          api_hash: apiHash,
          github_token: githubToken,
          github_repo: githubRepo,
        }),
      });
      if (res.error) throw new Error(res.error);
      setMsg({ type: "ok", text: res.message || "Saved!" });
      setTimeout(() => router.replace("/"), 1200);
    } catch (e: any) {
      setMsg({ type: "error", text: e.message });
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <div className="container muted">Checking…</div>;

  return (
    <div className="card" style={{ maxWidth: 460, margin: "40px auto" }}>
      <h1>⚙️ Setup</h1>
      <p className="muted">
        First-run setup — no .env files needed. Values are stored encrypted on the server.
      </p>

      <label className="muted">API ID <span style={{ color: "var(--err)" }}>*</span></label>
      <input type="text" placeholder="1234567" value={apiId} onChange={(e) => setApiId(e.target.value)} />

      <label className="muted">API Hash <span style={{ color: "var(--err)" }}>*</span></label>
      <input type="text" placeholder="0123456789abcdef..." value={apiHash} onChange={(e) => setApiHash(e.target.value)} />

      <label className="muted">GITHUB_TOKEN (optional — needed for RE tools)</label>
      <input type="password" placeholder="ghp_..." value={githubToken} onChange={(e) => setGithubToken(e.target.value)} />

      <label className="muted">GITHUB_REPO (optional)</label>
      <input type="text" placeholder="Saini920/Bottestgidra" value={githubRepo} onChange={(e) => setGithubRepo(e.target.value)} />

      <button onClick={save} disabled={busy || !apiId || !apiHash}>
        {busy ? "Saving…" : "Save & Continue"}
      </button>

      {msg && <div className={msg.type === "error" ? "error" : "ok"}>{msg.text}</div>}

      <p className="muted" style={{ marginTop: 16 }}>
        How to get API ID/Hash: <b>my.telegram.org</b> → API development tools.
      </p>
    </div>
  );
}
