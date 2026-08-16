"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  api, apiUpload, clearToken, downloadUrl, fmtSize, getToken, wsUrl,
} from "@/lib/api";
import ProgressBar from "@/components/ProgressBar";

interface Engine { id: string; premium: boolean; ext: string[]; }

export default function Tools() {
  const router = useRouter();
  const [engines, setEngines] = useState<Engine[]>([]);
  const [engine, setEngine] = useState("");
  const [jobs, setJobs] = useState<any[]>([]);
  const [progress, setProgress] = useState<{ [k: number]: number }>({});
  const [labels, setLabels] = useState<{ [k: number]: string }>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const loadJobs = useCallback(async () => {
    try {
      const r = await api("/api/jobs");
      setJobs(r.jobs || []);
    } catch {}
  }, []);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/");
      return;
    }
    api("/api/engines")
      .then((r) => {
        setEngines(r.engines || []);
        if (r.engines?.length) setEngine(r.engines[0].id);
      })
      .catch(() => {});
    api("/auth/me")
      .then((me) => setIsAdmin(!!me.is_admin))
      .catch(() => {});
    loadJobs();
  }, [router, loadJobs]);

  function watchJob(jobId: number) {
    const ws = new WebSocket(wsUrl(`job:${jobId}`));
    ws.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.pct !== undefined) {
          setProgress((m) => ({ ...m, [jobId]: d.pct }));
        }
        if (d.label) setLabels((m) => ({ ...m, [jobId]: d.label }));
        if (d.type === "done") {
          setLabels((m) => ({ ...m, [jobId]: "✅ Done" }));
          loadJobs();
          setTimeout(() => ws.close(), 500);
        }
      } catch {}
    };
    wsRef.current = ws;
  }

  async function createJob(file: File) {
    setError("");
    setBusy(true);
    const channel = `job_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const form = new FormData();
    form.append("file", file);
    form.append("engine", engine);
    form.append("channel", channel);
    try {
      const r = await apiUpload("/api/jobs", form);
      watchJob(r.job_id);
      loadJobs();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function stopJob(jobId: number) {
    try {
      await api(`/api/jobs/${jobId}/stop`, { method: "POST" });
      loadJobs();
    } catch (e: any) {
      alert(e.message);
    }
  }

  function logout() {
    api("/auth/logout", { method: "POST" }).catch(() => {});
    clearToken();
    router.replace("/");
  }

  return (
    <div className="container">
      <div className="nav">
        <Link href="/dashboard">Dashboard</Link>
        <Link href="/storage">Storage</Link>
        <Link href="/tools">RE Tools</Link>
        {isAdmin && <Link href="/settings">Settings</Link>}
        <button className="secondary" onClick={logout} style={{ marginLeft: "auto" }}>
          Logout
        </button>
      </div>

      <div className="card">
        <h1>🛠️ RE Tools</h1>
        <p className="muted">
          Upload a binary and decompile it on cloud runners (Ghidra, JADX, Apktool, Smali,
          DEX, NDK C/C++, APK build/sign, PDF→TXT). Progress is live.
        </p>
        <label className="muted">Engine</label>
        <select value={engine} onChange={(e) => setEngine(e.target.value)}>
          {engines.map((en) => (
            <option key={en.id} value={en.id}>
              {en.id} {en.premium ? "⭐Premium" : ""}
            </option>
          ))}
        </select>
        <input
          type="file"
          disabled={busy}
          onChange={(e) => e.target.files?.[0] && createJob(e.target.files[0])}
        />
        {busy && <p className="muted">Dispatching to cloud runner…</p>}
        {error && <div className="error">{error}</div>}
      </div>

      <div className="card">
        <h2>Your jobs</h2>
        {jobs.length === 0 && <p className="muted">No jobs yet.</p>}
        {jobs.map((j) => (
          <div key={j.id} className="card" style={{ padding: 14 }}>
            <div className="row spread">
              <b>{j.filename}</b>
              <span className="pill">{j.engine} · {j.status}</span>
            </div>
            <div className="muted" style={{ marginTop: 4 }}>
              #{j.id} · {new Date(j.created_at).toLocaleString()}
            </div>
            {progress[j.id] !== undefined && (
              <ProgressBar pct={progress[j.id]} label={labels[j.id] || "Processing..."} />
            )}
            <div className="row" style={{ marginTop: 8 }}>
              {j.status === "done" && j.result_path && (
                <a
                  className="ok"
                  href={`${downloadUrl(`/api/jobs/${j.id}/result`)}`}
                  style={{ fontWeight: 600 }}
                >
                  ⬇️ Download result
                </a>
              )}
              {(j.status === "queued" || j.status === "processing" || j.status === "running") && (
                <button className="danger" onClick={() => stopJob(j.id)}>🛑 Stop</button>
              )}
            </div>
            {j.error && <div className="error">{j.error}</div>}
          </div>
        ))}
      </div>
    </div>
  );
}
