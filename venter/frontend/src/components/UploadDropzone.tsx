import { useRef, useState } from "react";
import type { Telegram } from "../lib/telegram";

interface Props {
  tg: Telegram;
  onUploaded: (file: File, messageId: number) => void;
}

export function UploadDropzone({ tg, onUploaded }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pct, setPct] = useState(0);
  const [error, setError] = useState("");

  async function handleFiles(files: FileList | null) {
    const file = files?.[0];
    if (!file || busy) return;
    setBusy(true);
    setError("");
    setPct(0);
    try {
      const messageId = await tg.uploadToSaved(file, setPct);
      onUploaded(file, messageId);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className={`rounded-xl border-2 border-dashed p-10 text-center transition-colors ${
        drag ? "border-blue-500 bg-blue-500/10" : "border-zinc-700 bg-zinc-900"
      }`}
      onDragOver={(e) => {
        e.preventDefault();
        setDrag(true);
      }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDrag(false);
        handleFiles(e.dataTransfer.files);
      }}
      onClick={() => inputRef.current?.click()}
    >
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        onChange={(e) => handleFiles(e.target.files)}
      />
      {busy ? (
        <div className="space-y-2">
          <p className="text-sm text-zinc-300">📤 Saved Messages me upload ho raha hai…</p>
          <div className="mx-auto h-2 w-full max-w-xs rounded bg-zinc-700">
            <div className="h-2 rounded bg-blue-500 transition-all" style={{ width: `${pct}%` }} />
          </div>
          <p className="text-xs text-zinc-500">{pct}%</p>
        </div>
      ) : (
        <div>
          <p className="text-3xl">📦</p>
          <p className="mt-2 text-sm text-zinc-300">
            File yahan drop karo — <b>EXE, DLL, SO, ELF, APK, DEX, ZIP, PDF</b>
          </p>
          <p className="mt-1 text-xs text-zinc-500">
            File seedha aapke Telegram Saved Messages me stream hoti hai (2 GB tak, free)
          </p>
        </div>
      )}
      {error && <p className="mt-3 text-sm text-red-400">❌ {error}</p>}
    </div>
  );
}
