import { useState } from "react";
import type { ChangeEvent } from "react";
import { testConnection } from "../lib/github";
import type { Settings } from "../types";
import { inputCls, btnCls } from "./LoginFlow";

interface Props {
  settings: Settings;
  onSave: (s: Settings) => void;
}

export function SettingsPanel({ settings, onSave }: Props) {
  const [form, setForm] = useState<Settings>({ ...settings });
  const [status, setStatus] = useState<{ ok: boolean; message: string } | null>(null);
  const [saved, setSaved] = useState(false);

  const set = (k: keyof Settings) => (e: ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [k]: e.target.value });

  async function doTest() {
    setStatus(null);
    setStatus(await testConnection(form.githubToken, form.githubRepo));
  }

  function doSave() {
    onSave(form);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-5">
        <h3 className="mb-3 font-semibold">⚙️ Environment</h3>

        <label className="block text-xs text-zinc-400 mb-1">GITHUB_TOKEN (fine-grained PAT)</label>
        <input className={inputCls} type="password" value={form.githubToken} onChange={set("githubToken")} placeholder="github_pat_…" />

        <label className="block text-xs text-zinc-400 mb-1 mt-3">GITHUB_REPO</label>
        <input className={inputCls} value={form.githubRepo} onChange={set("githubRepo")} placeholder="username/venter-engine" />

        <div className="mt-3 grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-zinc-400 mb-1">API_ID</label>
            <input className={inputCls} value={form.apiId} onChange={set("apiId")} placeholder="1234567" />
          </div>
          <div>
            <label className="block text-xs text-zinc-400 mb-1">API_HASH</label>
            <input className={inputCls} type="password" value={form.apiHash} onChange={set("apiHash")} placeholder="0123abcd…" />
          </div>
        </div>

        <label className="block text-xs text-zinc-400 mb-1 mt-3">
          SESSION_KEY (passphrase — GitHub Actions secret me bhi same set karo)
        </label>
        <input className={inputCls} type="password" value={form.sessionKey} onChange={set("sessionKey")} placeholder="strong passphrase" />

        <div className="mt-4 flex gap-3">
          <button className={btnCls} onClick={doSave}>{saved ? "Saved ✅" : "Save Settings"}</button>
          <button
            className="w-full rounded-lg bg-zinc-700 px-3 py-2 text-sm font-semibold hover:bg-zinc-600"
            onClick={doTest}
          >
            Test Connection
          </button>
        </div>
        {status && (
          <p className={`mt-3 text-sm ${status.ok ? "text-green-400" : "text-red-400"}`}>
            {status.ok ? "🟢" : "🔴"} {status.message}
          </p>
        )}
      </div>

      <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-5 text-sm text-zinc-400">
        <h3 className="mb-2 font-semibold text-zinc-200">📋 Setup checklist</h3>
        <ol className="list-decimal ml-5 space-y-1">
          <li>my.telegram.org se <b>API_ID + API_HASH</b> lo</li>
          <li>GitHub repo banao (venter template) — <code>workers/</code> + <code>.github/workflows/</code> ke saath</li>
          <li>Fine-grained PAT banao (scopes: <code>Actions: write</code>, <code>Contents: read</code>)</li>
          <li>Repo me GitHub Actions secrets set karo: <code>API_ID</code>, <code>API_HASH</code>, <code>SESSION_KEY</code></li>
          <li>Yahan <b>Test Connection</b> dabao → 🟢 aaye</li>
        </ol>
      </div>
    </div>
  );
}
