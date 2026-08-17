import { useEffect, useState } from "react";
import type { Telegram } from "../lib/telegram";
import type { SavedFile } from "../types";
import { MonacoViewer } from "./MonacoViewer";

interface Props {
  tg: Telegram;
}

export function FileExplorer({ tg }: Props) {
  const [files, setFiles] = useState<SavedFile[]>([]);
  const [busy, setBusy] = useState(true);
  const [view, setView] = useState<{ messageId: number; fileName: string; blob: Blob } | null>(null);
  const [error, setError] = useState("");

  async function refresh() {
    setBusy(true);
    setError("");
    try {
      setFiles(await tg.listSaved(50));
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function open(messageId: number, fileName: string) {
    setError("");
    try {
      const { blob } = await tg.downloadBlob(messageId);
      setView({ messageId, fileName, blob });
    } catch (e: any) {
      setError(String(e?.message || e));
    }
  }

  if (view) {
    return (
      <div className="space-y-2">
        <button
          className="rounded-lg bg-zinc-800 px-3 py-1.5 text-sm hover:bg-zinc-700"
          onClick={() => setView(null)}
        >
          ← Back to files
        </button>
        <MonacoViewer blob={view.blob} fileName={view.fileName} />
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">📁 Saved Messages ({files.length})</h3>
        <button className="rounded-lg bg-zinc-800 px-3 py-1.5 text-sm hover:bg-zinc-700" onClick={refresh}>
          ⟳ Refresh
        </button>
      </div>
      {busy && <p className="text-sm text-zinc-500">Loading…</p>}
      {error && <p className="text-sm text-red-400">❌ {error}</p>}
      {!busy && files.length === 0 && (
        <p className="text-sm text-zinc-500">Abhi koi file nahi — pehla decompile karo!</p>
      )}
      <div className="space-y-1">
        {files.map((f) => (
          <div
            key={f.messageId}
            className="flex items-center justify-between rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2"
          >
            <div className="min-w-0">
              <p className="truncate text-sm">{f.fileName}</p>
              <p className="text-xs text-zinc-500">
                {(f.size / 1024 / 1024).toFixed(1)} MB · {new Date(f.date).toLocaleString()}
              </p>
            </div>
            <div className="flex gap-2">
              {/\.(zip|jar)$/i.test(f.fileName) && (
                <button
                  className="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-semibold hover:bg-blue-500"
                  onClick={() => open(f.messageId, f.fileName)}
                >
                  View
                </button>
              )}
              <a
                className="rounded-lg bg-zinc-700 px-3 py-1.5 text-xs font-semibold hover:bg-zinc-600"
                onClick={() => open(f.messageId, f.fileName)}
                href="#"
              >
                Open
              </a>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
