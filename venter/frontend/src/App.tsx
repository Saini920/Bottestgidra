import { useEffect, useState } from "react";
import { storage } from "./lib/storage";
import { Telegram } from "./lib/telegram";
import { dispatchJob } from "./lib/github";
import { LoginFlow } from "./components/LoginFlow";
import { SettingsPanel } from "./components/SettingsPanel";
import { UploadDropzone } from "./components/UploadDropzone";
import { EnginePicker } from "./components/EnginePicker";
import { JobProgress } from "./components/JobProgress";
import { FileExplorer } from "./components/FileExplorer";
import { ENGINE_EVENTS, ENGINE_LABELS } from "./types";
import type { Settings, StoredSession } from "./types";

type Tab = "decompile" | "files" | "settings";

export default function App() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [session, setSession] = useState<StoredSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [tg] = useState(() => new Telegram());
  const [tab, setTab] = useState<Tab>("decompile");

  // decompile flow state
  const [pickedFile, setPickedFile] = useState<{ file: File; messageId: number } | null>(null);
  const [job, setJob] = useState<{ jobId: string; engine: string; filename: string } | null>(null);

  useEffect(() => {
    (async () => {
      const [s, sess] = await Promise.all([storage.getSettings(), storage.getSession()]);
      setSettings(s);
      setSession(sess);
      setLoading(false);
    })();
  }, []);

  if (loading) return <div className="p-10 text-center text-zinc-500">Loading…</div>;

  // ---- Settings gate ----
  if (!settings) {
    return (
      <div className="p-6">
        <Header onLogout={undefined} />
        <SettingsPanel settings={defaultSettings()} onSave={saveSettings} />
      </div>
    );
  }

  // ---- Login gate ----
  if (!session) {
    return (
      <div className="p-6">
        <Header onLogout={undefined} />
        <LoginFlow settings={settings} onSession={saveSession} />
      </div>
    );
  }

  async function saveSettings(s: Settings) {
    setSettings(s);
    await storage.saveSettings(s);
  }

  async function saveSession(s: StoredSession) {
    setSession(s);
    await storage.saveSession(s);
  }

  async function handleLogout() {
    try {
      await tg.logout();
    } catch {
      /* ignore */
    }
    await storage.clearSession();
    setSession(null);
  }

  async function handleEnginePick(engine: string) {
    if (!pickedFile || !settings) return;
    const jobId = `job_${crypto.randomUUID().slice(0, 8)}`;
    setJob({ jobId, engine, filename: pickedFile.file.name });

    const eventType = ENGINE_EVENTS[engine];
    const res = await dispatchJob(settings.githubToken, settings.githubRepo, eventType, {
      file_message_id: pickedFile.messageId,
      filename: pickedFile.file.name,
      session: session!.blob,
      job_id: jobId,
      user_id: String(session!.me?.id ?? ""),
    });
    if (!res.ok) {
      alert(`❌ ${res.message}`);
      setJob(null);
    }
  }

  function handleJobDone() {
    setJob(null);
    setPickedFile(null);
    setTab("files");
  }

  return (
    <div className="min-h-screen">
      <Header onLogout={handleLogout} userName={session.me?.name} />
      <nav className="flex gap-1 border-b border-zinc-800 px-6">
        {(
          [
            ["decompile", "🧠 Decompile"],
            ["files", "📁 Files"],
            ["settings", "⚙️ Settings"],
          ] as [Tab, string][]
        ).map(([t, label]) => (
          <button
            key={t}
            className={`px-4 py-2.5 text-sm font-medium ${
              tab === t ? "border-b-2 border-blue-500 text-white" : "text-zinc-400 hover:text-zinc-200"
            }`}
            onClick={() => setTab(t)}
          >
            {label}
          </button>
        ))}
      </nav>

      <main className="mx-auto max-w-3xl p-6">
        {tab === "decompile" && (
          <div className="space-y-4">
            {!job && !pickedFile && (
              <UploadDropzone tg={tg} onUploaded={(file, messageId) => setPickedFile({ file, messageId })} />
            )}
            {!job && pickedFile && (
              <div className="space-y-4">
                <p className="text-sm text-green-400">
                  ✅ <b>{pickedFile.file.name}</b> Saved Messages me upload ho gayi!
                </p>
                <EnginePicker filename={pickedFile.file.name} onPick={handleEnginePick} />
                <button
                  className="text-sm text-zinc-500 hover:text-zinc-300"
                  onClick={() => setPickedFile(null)}
                >
                  ← Different file
                </button>
              </div>
            )}
            {job && (
              <div className="space-y-3">
                <p className="text-sm text-zinc-400">
                  {ENGINE_LABELS[job.engine] || job.engine} — <b>{job.filename}</b>
                </p>
                <JobProgress jobId={job.jobId} onDone={handleJobDone} />
              </div>
            )}
          </div>
        )}

        {tab === "files" && <FileExplorer tg={tg} />}
        {tab === "settings" && <SettingsPanel settings={settings} onSave={saveSettings} />}
      </main>
    </div>
  );
}

function defaultSettings(): Settings {
  return { githubToken: "", githubRepo: "", apiId: "", apiHash: "", sessionKey: "" };
}

function Header({ onLogout, userName }: { onLogout?: () => void; userName?: string }) {
  return (
    <header className="flex items-center justify-between px-6 py-4">
      <h1 className="text-lg font-bold">
        ⚡ Venter <span className="text-zinc-500 font-normal text-sm">— Reverse Engineering Studio</span>
      </h1>
      <div className="flex items-center gap-3">
        {userName && <span className="text-sm text-zinc-400">👤 {userName}</span>}
        {onLogout && (
          <button className="rounded-lg bg-zinc-800 px-3 py-1.5 text-xs hover:bg-zinc-700" onClick={onLogout}>
            Log out
          </button>
        )}
      </div>
    </header>
  );
}
