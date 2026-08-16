"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  api, apiUpload, clearToken, downloadUrl, fmtSize, getToken, wsUrl,
} from "@/lib/api";
import ProgressBar from "@/components/ProgressBar";

interface UploadState {
  filename: string;
  pct: number;
  done?: boolean;
  error?: string;
}

export default function Storage() {
  const router = useRouter();
  const [files, setFiles] = useState<any[]>([]);
  const [upload, setUpload] = useState<UploadState | null>(null);
  const [downloading, setDownloading] = useState<{ [k: number]: number }>({});
  const [isAdmin, setIsAdmin] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api("/storage/files");
      setFiles(r.files || []);
    } catch (e: any) {
      if (String(e.message).includes("401") || String(e.message).includes("token")) {
        router.replace("/");
      }
    }
  }, [router]);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/");
      return;
    }
    api("/auth/me")
      .then((me) => setIsAdmin(!!me.is_admin))
      .catch(() => {});
    load();
  }, [load, router]);

  function openWs(channel: string, onMsg: (d: any) => void) {
    if (wsRef.current) wsRef.current.close();
    const ws = new WebSocket(wsUrl(channel));
    ws.onmessage = (ev) => {
      try {
        onMsg(JSON.parse(ev.data));
      } catch {}
    };
    wsRef.current = ws;
  }

  async function onUpload(file: File) {
    const channel = `up_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    setUpload({ filename: file.name, pct: 0 });
    openWs(channel, (d) => {
      if (d.type === "progress") {
        setUpload((u) => (u ? { ...u, pct: d.pct } : u));
      } else if (d.type === "done") {
        setUpload((u) => (u ? { ...u, pct: 100, done: true } : u));
        load();
        setTimeout(() => setUpload(null), 1200);
      }
    });
    const form = new FormData();
    form.append("file", file);
    form.append("channel", channel);
    try {
      await apiUpload("/storage/upload", form);
    } catch (e: any) {
      setUpload((u) => (u ? { ...u, error: e.message } : u));
    }
  }

  function downloadFile(f: any) {
    const channel = `dl_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    openWs(channel, (d) => {
      if (d.type === "progress") {
        setDownloading((m) => ({ ...m, [f.id]: d.pct }));
      }
    });
    setDownloading((m) => ({ ...m, [f.id]: 0 }));
    const a = document.createElement("a");
    a.href = `${downloadUrl(`/storage/download/${f.id}`)}?channel=${channel}`;
    a.download = f.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => {
      setDownloading((m) => {
        const n = { ...m };
        delete n[f.id];
        return n;
      });
    }, 8000);
  }

  async function deleteFile(f: any) {
    if (!confirm(`Delete ${f.filename} from your Telegram?`)) return;
    try {
      await api(`/storage/files/${f.id}`, { method: "DELETE" });
      load();
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
        <h1>💾 Storage</h1>
        <p className="muted">
          Files are stored in <b>your Telegram Saved Messages</b> — the server never keeps a copy.
        </p>
        <input type="file" onChange={(e) => e.target.files?.[0] && onUpload(e.target.files[0])} />
        {upload && (
          <ProgressBar
            pct={upload.pct}
            label={upload.error ? `❌ ${upload.error}` : upload.done ? "✅ Uploaded!" : `Uploading ${upload.filename}...`}
          />
        )}
      </div>

      <div className="card">
        <h2>Your files ({files.length})</h2>
        {files.length === 0 && <p className="muted">Nothing here yet — upload a file above.</p>}
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Size</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {files.map((f) => (
              <tr key={f.id}>
                <td>{f.filename}</td>
                <td>{fmtSize(f.size)}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  {downloading[f.id] !== undefined && (
                    <span className="muted">{downloading[f.id]}% </span>
                  )}
                  <button className="secondary" onClick={() => downloadFile(f)}>⬇️</button>{" "}
                  <button className="danger" onClick={() => deleteFile(f)}>🗑️</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
