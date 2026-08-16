"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, clearToken, getToken, fmtSize } from "@/lib/api";

export default function Dashboard() {
  const router = useRouter();
  const [me, setMe] = useState<any>(null);
  const [files, setFiles] = useState<any[]>([]);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/");
      return;
    }
    api("/auth/me").then(setMe).catch(() => router.replace("/"));
    api("/storage/files")
      .then((r) => setFiles(r.files || []))
      .catch(() => {});
  }, [router]);

  function logout() {
    api("/auth/logout", { method: "POST" }).catch(() => {});
    clearToken();
    router.replace("/");
  }

  if (!me) return <div className="container muted">Loading...</div>;

  const total = files.reduce((s, f) => s + (f.size || 0), 0);

  return (
    <div className="container">
      <div className="nav">
        <Link href="/dashboard">Dashboard</Link>
        <Link href="/storage">Storage</Link>
        <Link href="/tools">RE Tools</Link>
        {me?.is_admin && <Link href="/settings">Settings</Link>}
        <button className="secondary" onClick={logout} style={{ marginLeft: "auto" }}>
          Logout
        </button>
      </div>

      <div className="card">
        <h1>👋 Hi, {me.name || me.username || me.id}</h1>
        <p className="muted">
          User ID: {me.id} · Usage today: {me.usage_today} file(s)
        </p>
        <div className="row">
          <span className={me.is_admin ? "pill premium" : "pill"}>
            {me.is_admin ? "Admin" : "Free"}
          </span>
        </div>
      </div>

      <div className="card">
        <h2>💾 Storage</h2>
        <p className="muted">
          {files.length} file(s) · {fmtSize(total)} used
        </p>
        <Link href="/storage">Open Storage →</Link>
      </div>

      <div className="card">
        <h2>🛠️ RE Tools</h2>
        <p className="muted">Ghidra, JADX, Apktool, Smali, DEX, C/C++, APK build/sign, PDF→TXT</p>
        <Link href="/tools">Decompile something →</Link>
      </div>
    </div>
  );
}
