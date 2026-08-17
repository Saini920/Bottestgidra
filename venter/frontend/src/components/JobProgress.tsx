import { useEffect, useState } from "react";
import { subscribeJob } from "../lib/ntfy";
import type { ProgressEvent } from "../types";

interface Props {
  jobId: string;
  onDone: (messageId: number | null) => void;
}

export function JobProgress({ jobId, onDone }: Props) {
  const [pct, setPct] = useState(0);
  const [label, setLabel] = useState("🟢 Job started…");
  const [bar, setBar] = useState("");

  useEffect(() => {
    const unsub = subscribeJob(jobId, (e: ProgressEvent) => {
      if (e.type === "progress") {
        setPct(e.pct);
        setLabel(e.label);
        setBar(e.bar);
      } else if (e.type === "final") {
        if (e.status === "done") {
          setLabel("✅ Complete!");
          setPct(100);
          onDone(e.message_id ?? null);
        } else {
          setLabel(`❌ ${e.error || "Failed"}`);
          onDone(null);
        }
      }
    });
    return unsub;
  }, [jobId, onDone]);

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-5 space-y-2">
      <p className="text-sm text-zinc-300">{label}</p>
      <div className="h-3 w-full rounded bg-zinc-700 overflow-hidden">
        <div className="h-3 rounded bg-blue-500 transition-all" style={{ width: `${pct}%` }} />
      </div>
      {bar && <pre className="text-xs text-zinc-500 font-mono">{bar}</pre>}
      <p className="text-xs text-zinc-500">Job: {jobId}</p>
    </div>
  );
}
